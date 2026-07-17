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
  constructor(api) {
    this.api = api
    this.provider = api._rpcCore.provider
  }

  static async connect(ws = 'ws://127.0.0.1:8000') {
    const api = await ApiPromise.create({ provider: new WsProvider(ws), noInitWarn: true })
    await api.isReady
    return new ChopsticksAnvil(api)
  }

  async forkBlock() {
    return (await this.api.rpc.chain.getHeader()).number.toNumber()
  }

  // Unix seconds of the pinned fork block (pallet_timestamp stores millis).
  async forkTimestamp() {
    const ms = await this.api.query.timestamp.now()
    return Math.floor(Number(ms.toString()) / 1000)
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
