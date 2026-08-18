// ChopsticksAnvil — an anvil-compatible shim over an Acala Chopsticks fork of a
// Frontier-based Substrate chain (Bittensor subtensor, chainId 964).
//
// WHY: Minotaur benchmarks solver Apps by forking a chain at a pinned block and
// simulating candidate execution deterministically. anvil does this for Base/ETH,
// but anvil (revm) cannot execute subtensor's NATIVE precompiles (staking 0x805,
// alpha 0x808, ...) — they are Substrate runtime code, invisible on an anvil fork.
// A Chopsticks fork runs the REAL runtime wasm, so the precompiles execute.
//
// This shim maps the anvil cheatcode + eth_call surface AnvilSimulator relies on
// onto Chopsticks' substrate primitives (dev_setStorage + EthereumRuntimeRPCApi).
//
// PROVEN (see ../chopsticks-sim/README.md): setBalance, setCode, setStorageAt,
//   and ethCall(arbitrary-from) that returns {exitReason, returnData, usedGas, logs}
//   — enough to score BOTH DEX apps (Transfer logs) and staking apps (state-delta
//   via a measuring router), deterministically, WITHOUT building a block.
// GAP: buildBlock()/state-persistence is blocked (pallet_drand's per-block hook
//   calls a BLS12-381 host fn Chopsticks' executor lacks). Not needed for scoring.
//
// Launch Chopsticks with these flags (see launchArgs()):
//   --allow-unresolved-imports  (instantiate despite the missing BLS import;
//                                only traps if BLS is actually CALLED — dry-runs don't)
//   --mock-signature-host       (impersonation for the future block-building path)

import { randomBytes } from 'node:crypto'

import { ApiPromise, WsProvider } from '@polkadot/api'
import { blake2AsHex, keccakAsU8a } from '@polkadot/util-crypto'
import { u8aConcat, hexToU8a, stringToU8a, u8aToHex } from '@polkadot/util'

const DEAD_FROM = '0x0000000000000000000000000000000000000001'
const DEFAULT_GAS = '0x77359400' // 2e9

export function launchArgs({ endpoint, block, port = 8000 }) {
  return [
    '--endpoint', endpoint,
    '--block', String(block),
    '--port', String(port),
    '--allow-unresolved-imports',
    '--mock-signature-host',
    '--build-block-mode', 'Manual',
  ]
}

export class ChopsticksAnvil {
  constructor(api, { ws, upstream } = {}) {
    this.api = api
    this.provider = api._rpcCore.provider
    this.ws = ws                 // local chopsticks, for reconnecting after an upgrade
    this.upstream = upstream     // real node, for resolving blocks the fork hasn't seen
    this._upstreamProvider = null
  }

  static async connect(ws = 'ws://127.0.0.1:8000', { upstream = '' } = {}) {
    const api = await ApiPromise.create({ provider: new WsProvider(ws), noInitWarn: true })
    await api.isReady
    return new ChopsticksAnvil(api, { ws, upstream })
  }

  async _specVersion() {
    const v = await this.provider.send('state_getRuntimeVersion', [])
    return Number(v?.specVersion ?? -1)
  }

  // The upstream node, lazily. Only a FORWARD re-pin needs it (see repin).
  async _upstreamSend(method, params) {
    if (!this.upstream) throw new Error('no upstream endpoint configured (CK_ENDPOINT)')
    if (!this._upstreamProvider) {
      this._upstreamProvider = new WsProvider(this.upstream)
      await this._upstreamProvider.isReady
    }
    return await this._upstreamProvider.send(method, params)
  }

  async forkBlock() {
    return (await this.api.rpc.chain.getHeader()).number.toNumber()
  }

  // Unix seconds of the pinned fork block (pallet_timestamp stores millis).
  async forkTimestamp() {
    const ms = await this.api.query.timestamp.now()
    return Math.floor(Number(ms.toString()) / 1000)
  }

  // Re-anchor the fork to a different historical block WITHOUT restarting
  // (per-round re-pin). dev_setHead moves the head; subsequent state reads
  // lazy-load from the upstream at that block — so the upstream MUST be an
  // archive node for a jump beyond its pruning window. Returns the new head.
  // Any pending cheatcode overrides (setBalance/setCode) are dropped by the
  // re-pin (fresh state), so re-pin FIRST, then seed, then dry-run.
  //
  // TWO things make this more than one dev_setHead call, both found by driving a
  // real fork of Finney:
  //
  //  1. BY NUMBER ONLY GOES BACKWARD. Chopsticks resolves a NUMBER against its
  //     own chain, which ends at the block it forked; anything newer is
  //     "Block not found". Rounds move FORWARD, so re-pinning by number would
  //     fail on every round after the container started. A HASH is resolved
  //     against the UPSTREAM, so we fetch the hash there and set that instead.
  //
  //  2. A RE-PIN CAN CROSS A RUNTIME UPGRADE. The polkadot.js api decorates
  //     `api.call.*` from the metadata it saw at connect time and learns about
  //     upgrades from a new-heads subscription that chopsticks (Manual block
  //     mode) never emits. Cross the boundary and `api.call.ethereumRuntimeRPCApi`
  //     is undefined — every ethCall dies with "Cannot read properties of
  //     undefined". Reconnecting re-decorates against the runtime now in force.
  //     Live: Finney 8800000 is spec 443 and head is spec 447.
  async repin(blockNumber) {
    const target = Number(blockNumber)
    const before = await this._specVersion()
    try {
      await this.provider.send('dev_setHead', [target])
    } catch (err) {
      if (!/not found/i.test(String(err?.message || err))) throw err
      const hash = await this._upstreamSend('chain_getBlockHash', [target])
      if (!hash) throw new Error(`upstream has no block ${target}`)
      await this.provider.send('dev_setHead', [hash])
    }
    if (await this._specVersion() !== before) await this.reconnect()
    return await this.forkBlock()
  }

  // Liveness probe AND keepalive for the FORK'S OWN upstream socket.
  //
  // It must travel through chopsticks, not through our own upstream provider:
  // the socket that dies is the one chopsticks holds, and keeping a second
  // connection warm proves nothing about it. A RANDOM storage key is the lever —
  // it can never be in the fork's cache, so answering it forces the real
  // upstream fetch, which is exactly the call that fails when the socket is
  // dead. Returns null (no such key) on success; throws on a dead upstream.
  // ``timeoutMs`` bounds DETECTION. polkadot.js has its own 60s RPC timeout, so
  // without this a probe against a HALF-OPEN socket (severed link, no RST) sits
  // for a full minute before throwing — which makes "N consecutive failures"
  // mean an unpredictable number of MINUTES, and stalls the health flag
  // meanwhile. Fail fast instead: this call only has to answer "is the upstream
  // answering right now?".
  async probeUpstream(timeoutMs = 15000) {
    const key = '0x' + randomBytes(32).toString('hex')
    let timer
    try {
      return await Promise.race([
        this.provider.send('state_getStorage', [key]),
        new Promise((_, rej) => {
          timer = setTimeout(() => rej(new Error(`upstream probe timed out after ${timeoutMs}ms`)), timeoutMs)
        }),
      ])
    } finally {
      clearTimeout(timer)
    }
  }

  // Rebuild the api against the runtime currently in force (see repin note 2).
  async reconnect() {
    if (!this.ws) throw new Error('cannot reconnect: no local ws endpoint recorded')
    try { await this.api.disconnect() } catch { /* already gone */ }
    this.api = await ApiPromise.create({ provider: new WsProvider(this.ws), noInitWarn: true })
    await this.api.isReady
    this.provider = this.api._rpcCore.provider
  }

  // H160 -> the ss58 account that owns its balance/gas (HashedAddressMapping):
  // blake2_256("evm:" ++ h160). This is the coldkey a contract stakes under.
  mappedAccount(h160) {
    return blake2AsHex(u8aConcat(stringToU8a('evm:'), hexToU8a(h160)), 256)
  }

  // anvil_setBalance analog. `rao` = native balance (1 TAO = 1e9 rao). Sets the
  // free balance of the H160's mapped account (what addStake debits, and what the
  // EVM sees as the address's balance).
  async setBalance(h160, rao) {
    const acct = this.api.createType('AccountInfo', {
      nonce: 0, consumers: 0, providers: 1, sufficients: 0,
      data: { free: BigInt(rao), reserved: 0, frozen: 0, flags: 0 },
    })
    await this.provider.send('dev_setStorage', [
      { System: { Account: [[[this.mappedAccount(h160)], acct.toJSON()]] } },
    ])
  }

  // anvil_setCode analog. Writes EVM.AccountCodes at the RAW key with a properly
  // SCALE-encoded Bytes value (compact(len)++code) — the nice-form omits the
  // length prefix and yields malformed code (stack underflow). Also sets
  // AccountCodesMetadata so EXTCODESIZE/EXTCODEHASH are consistent.
  async setCode(h160, codeHex) {
    const code = codeHex.startsWith('0x') ? codeHex : '0x' + codeHex
    const key = this.api.query.evm.accountCodes.key(h160)
    const val = u8aToHex(this.api.createType('Bytes', code).toU8a())
    await this.provider.send('dev_setStorage', [[[key, val]]])
    try {
      const mKey = this.api.query.evm.accountCodesMetadata.key(h160)
      const mVal = this.api.createType('PalletEvmCodeMetadata', {
        size: (code.length - 2) / 2,
        hash: u8aToHex(keccakAsU8a(hexToU8a(code))),
      }).toHex()
      await this.provider.send('dev_setStorage', [[[mKey, mVal]]])
    } catch { /* type name varies by version; execution reads AccountCodes anyway */ }
  }

  // anvil_setStorageAt analog (EVM.AccountStorages double-map: H160, H256 -> H256).
  async setStorageAt(h160, slotHex, valueHex) {
    await this.provider.send('dev_setStorage', [
      { EVM: { AccountStorages: [[[h160, slotHex], valueHex]] } },
    ])
  }

  async getStorageAt(h160, slotHex) {
    return (await this.api.query.evm.accountStorages(h160, slotHex)).toHex()
  }

  // eth_call with an arbitrary `from` (no signature needed — this is the read/dry-run
  // path). Returns the full scoring surface. State changes made by precompiles inside
  // the call ARE visible to later reads in the SAME call (enables the measuring-router
  // pattern) but are DISCARDED at the end — so it is side-effect-free and repeatable.
  async ethCall(to, data, { from = DEAD_FROM, value = 0, gas = DEFAULT_GAS } = {}) {
    const res = await this.api.call.ethereumRuntimeRPCApi.call(
      from, to, data, value, gas, null, null, null, false, null, null)
    const j = res.toJSON()
    const ok = j.ok || j.Ok
    return {
      success: !!ok && ('succeed' in (ok.exitReason || {})),
      exitReason: ok?.exitReason ?? j,
      returnData: ok?.value ?? null,
      usedGas: ok?.usedGas?.effective ?? ok?.usedGas ?? null, // real EVM gas
      logs: ok?.logs ?? [],
    }
  }

  async disconnect() { await this.api.disconnect() }
}
