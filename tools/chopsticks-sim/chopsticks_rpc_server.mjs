// chopsticks_rpc_server.mjs — the "anvil binary" for Bittensor.
//
// Boots a Chopsticks fork of subtensor (child process) and serves a small
// anvil-dialect JSON-RPC over HTTP so the Python harness can drive it exactly
// like it drives anvil. Reuses the proven ChopsticksAnvil shim for all the
// substrate encode/decode (polkadot.js), so the risky part stays in the layer
// that's been verified end-to-end (see README.md).
//
// One container = one forked chain, mirroring anvil-btevm. The Python
// SubtensorSimulator backend (minotaur_subnet/simulator/subtensor_simulator.py)
// is a thin client of the methods below.
//
// Methods (JSON-RPC 2.0 over POST /):
//   sim_health()                         -> {ok, block, chain, spec}
//   sim_forkBlock()                      -> number
//   sim_mappedAccount(h160)              -> ss58 pubkey hex (HashedAddressMapping)
//   anvil_setBalance(h160, rao)          -> true        (native balance, 1 TAO = 1e9 rao)
//   anvil_setCode(h160, codeHex)         -> true
//   anvil_setStorageAt(h160, slot, val)  -> true
//   ck_ethCall({from,to,data,value,gas}) -> {success, exitReason, returnData, usedGas, logs}
//
// Env: CK_ENDPOINT (upstream subtensor RPC — blockmachine on the leader),
//      CK_BLOCK (pin block; default head-30), CK_INNER_PORT (chopsticks port, 8100),
//      CK_DB (fork cache sqlite path), PORT (this server, 8545).

import http from 'node:http'
import { spawn } from 'node:child_process'
import { ChopsticksAnvil } from './chopsticks_anvil.mjs'

const ENDPOINT = process.env.CK_ENDPOINT || 'wss://entrypoint-finney.opentensor.ai:443'
const LITE_HTTP = process.env.CK_LITE || 'https://lite.chain.opentensor.ai'
const INNER_PORT = process.env.CK_INNER_PORT || '8100'
const PORT = parseInt(process.env.PORT || '8545', 10)
const DB = process.env.CK_DB || '' // fork-cache sqlite (persistent lazy-storage cache)

async function head() {
  const r = await fetch(LITE_HTTP, {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ jsonrpc: '2.0', id: 1, method: 'eth_blockNumber', params: [] }),
  })
  return parseInt((await r.json()).result, 16)
}

async function startChopsticks() {
  const block = process.env.CK_BLOCK || String((await head()) - 30)
  const args = [
    'chopsticks', // resolves the LOCAL install (package.json dep), no runtime download
    '--endpoint', ENDPOINT,
    '--block', block,
    '--port', INNER_PORT,
    '--allow-unresolved-imports', // subtensor imports a BLS host fn chopsticks lacks;
    '--mock-signature-host',       // dry-runs never call it, so runtime executes fine
    '--build-block-mode', 'Manual',
  ]
  if (DB) args.push('--db', DB)
  console.log(`[ck] forking ${ENDPOINT} @ block ${block} on :${INNER_PORT}${DB ? ` (cache ${DB})` : ''}`)
  // PORT must be overridden for the CHILD: chopsticks' CLI lets the PORT env var
  // WIN over --port (cli.js: `if (environment.PORT) argv.port = Number(...)`), and
  // this process sets PORT for its own listener — so an inherited PORT makes the
  // fork steal our port, we never bind, and waitReady() times out on a chopsticks
  // that is up but on the wrong port. Pass it explicitly rather than deleting it:
  // the child then agrees with --port whichever precedence a future release picks.
  const child = spawn('npx', args, {
    stdio: ['ignore', 'inherit', 'inherit'],
    env: { ...process.env, PORT: String(INNER_PORT) },
  })
  child.on('exit', (c) => { console.error(`[ck] chopsticks exited ${c}`); process.exit(1) })
  return { child, block: Number(block) }
}

async function waitReady(ws, tries = 60) {
  for (let i = 0; i < tries; i++) {
    try {
      const r = await fetch(`http://127.0.0.1:${INNER_PORT}`, {
        method: 'POST', headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ jsonrpc: '2.0', id: 1, method: 'state_call', params: ['Core_version', '0x'] }),
      })
      const j = await r.json()
      if (j.result) return true
    } catch { /* not up yet */ }
    await new Promise((res) => setTimeout(res, 2000))
  }
  throw new Error('chopsticks did not become ready')
}

// CK_ATTACH=ws://host:port connects to an already-running chopsticks (two-container
// layout, or local testing) instead of spawning one.
const ATTACH = process.env.CK_ATTACH || ''
let pinBlock
let attachWs
if (ATTACH) {
  attachWs = ATTACH
  console.log(`[ck] attaching to existing chopsticks at ${ATTACH}`)
} else {
  const started = await startChopsticks()
  pinBlock = started.block
  await waitReady()
  attachWs = `ws://127.0.0.1:${INNER_PORT}`
}
// The upstream is handed through so a FORWARD re-pin can resolve a block the
// fork has never seen (chopsticks resolves a NUMBER only against its own chain,
// which ends at the fork block — see ChopsticksAnvil.repin).
const ck = await ChopsticksAnvil.connect(attachWs, { upstream: ENDPOINT })
if (pinBlock === undefined) pinBlock = await ck.forkBlock()
console.log(`[rpc] shim connected; fork @ ${await ck.forkBlock()}`)

// ── upstream liveness ────────────────────────────────────────────────────────
//
// A forked chain reads storage LAZILY: every miss goes upstream. So an upstream
// websocket that has quietly died turns every simulation into
// "WebSocket is not connected" while the fork still answers chain_getHeader from
// local state — i.e. the process looks healthy and scores nothing.
//
// Measured 2026-08-17 on the leader: `rpc.blockmachine.io` closes an IDLE
// substrate websocket within 90s (1 disconnect event, no recovery), while
// `entrypoint-finney.opentensor.ai` survived the same idle untouched. The
// benchmark's access pattern is exactly the dangerous one — pin once, then sit
// idle between rounds — and polkadot.js's WsProvider does not queue a request
// made while disconnected, it throws.
//
// Two defences, because either alone is insufficient:
//   * KEEPALIVE — touch the upstream on an interval so it never idles out.
//   * HONEST HEALTH — report ok:false once it has died, so the container
//     healthcheck (which reads exactly this field) stops claiming the sidecar
//     is serving. A fork that cannot reach its upstream is not healthy.
const KEEPALIVE_MS = Number(process.env.CK_KEEPALIVE_MS ?? 30000)
// Consecutive failed probes before we give up and exit for a restart. The
// keepalive alone is NOT enough: measured over 18h against blockmachine the
// upstream dropped 12 times and self-healed 11 — the 12th stuck permanently,
// and the error changed character with it (`disconnected ...: 100`, which the
// provider reconnects from, vs `WebSocket is not connected`, which it does
// not). A fork that cannot reach its upstream serves nothing, so once it is
// clearly not coming back the useful move is to die: the container restart
// policy re-forks in ~40s and the --db cache makes that cheap. 0 disables.
const EXIT_AFTER = Number(process.env.CK_UNHEALTHY_EXIT_AFTER ?? 5)
// Consecutive failed probes before HEALTH goes false. Not 1, deliberately.
// Measured on the leader over 28h: blockmachine drops the socket roughly hourly
// and the keepalive gets it back within a SINGLE probe — 28 UNHEALTHY events,
// 27 recoveries, every one of them "recovered after 1 failed probe(s)". With a
// 1-probe threshold the container healthcheck occasionally samples that blip
// and latches unhealthy on a sidecar that is serving perfectly, which is a
// false alarm dressed up as honesty. Report sustained failure instead, and keep
// the instantaneous read on `upstream` so a blip is still visible.
// Must stay BELOW EXIT_AFTER so health degrades before the process gives up.
const UNHEALTHY_AFTER = Number(process.env.CK_UNHEALTHY_AFTER ?? 2)
// Bounds how long ONE probe may take, so the exit threshold means a
// predictable wall-clock (see ChopsticksAnvil.probeUpstream).
const PROBE_TIMEOUT_MS = Number(process.env.CK_PROBE_TIMEOUT_MS ?? 15000)
let upstreamOk = true
let upstreamError = null
let consecutiveFailures = 0

async function touchUpstream() {
  try {
    await ck.probeUpstream(PROBE_TIMEOUT_MS)
    if (!upstreamOk) console.log(`[ck] upstream recovered after ${consecutiveFailures} failed probe(s)`)
    upstreamOk = true
    upstreamError = null
    consecutiveFailures = 0
  } catch (e) {
    consecutiveFailures++
    if (upstreamOk) console.error(`[ck] upstream UNHEALTHY: ${String(e.message || e).slice(0, 160)}`)
    upstreamOk = false
    upstreamError = String(e.message || e).slice(0, 200)
    if (EXIT_AFTER > 0 && consecutiveFailures >= EXIT_AFTER) {
      console.error(
        `[ck] upstream dead for ${consecutiveFailures} consecutive probes ` +
        `(~${Math.round(consecutiveFailures * (KEEPALIVE_MS + PROBE_TIMEOUT_MS) / 1000)}s max) — exiting for a restart`,
      )
      process.exit(1)
    }
  }
}

if (KEEPALIVE_MS > 0) {
  await touchUpstream()
  setInterval(touchUpstream, KEEPALIVE_MS).unref?.()
  console.log(`[rpc] upstream keepalive every ${KEEPALIVE_MS}ms`)
}

const HANDLERS = {
  async sim_health() {
    return {
      // SUSTAINED health — what the container healthcheck acts on.
      ok: consecutiveFailures < UNHEALTHY_AFTER,
      block: await ck.forkBlock(),
      pinBlock,
      // INSTANTANEOUS last-probe result, so a single blip stays observable
      // without condemning the container.
      upstream: upstreamOk,
      upstream_error: upstreamError,
      consecutive_failures: consecutiveFailures,
    }
  },
  async sim_forkBlock() { return await ck.forkBlock() },
  async sim_forkTimestamp() { return await ck.forkTimestamp() },
  async sim_repin([block]) { const b = await ck.repin(block); pinBlock = b; return b },
  sim_mappedAccount([h160]) { return ck.mappedAccount(h160) },
  async anvil_setBalance([h160, rao]) { await ck.setBalance(h160, BigInt(rao)); return true },
  async anvil_setCode([h160, code]) { await ck.setCode(h160, code); return true },
  async anvil_setStorageAt([h160, slot, val]) { await ck.setStorageAt(h160, slot, val); return true },
  async ck_ethCall([{ from, to, data, value, gas }]) {
    return await ck.ethCall(to, data, { from, value: value ?? 0, gas: gas ?? undefined })
  },

  // ── standard EVM dialect ────────────────────────────────────────────────
  // This file calls itself an "anvil-dialect JSON-RPC" but answered only
  // ck_ethCall, so every EVM-shaped client got `unknown method eth_call`.
  // The scoring sandbox (engine/runner.js) is exactly such a client: it sends
  // `eth_call` with params [{to, data}, blockTag] and reads `result` as the
  // return data, so chain 964 was unreachable from scoring JS no matter which
  // URL it was pointed at. ck_ethCall stays — subtensor_simulator speaks it
  // and wants the full {success, exitReason, usedGas, logs} record.
  async eth_call([tx = {}, blockTag = 'latest']) {
    const { from, to, data, value, gas } = tx || {}

    // The fork is PINNED. Answering a request for some other block with
    // pin-block state would be a silent lie of exactly the kind this repo
    // keeps getting burned by (chain 1 served Base state; 8453 served an
    // unpinned head). Serve the pin, or say why not.
    if (blockTag != null && blockTag !== 'latest' && blockTag !== 'pending') {
      const have = Number(pinBlock ?? await ck.forkBlock())
      const want = typeof blockTag === 'string' && blockTag.startsWith('0x')
        ? parseInt(blockTag, 16)
        : Number(blockTag)
      if (!Number.isFinite(want) || want !== have) {
        throw new Error(
          `eth_call: fork is pinned at block ${have}, cannot serve blockTag ${blockTag}`)
      }
    }

    // value as a DECIMAL STRING, never a JS number: the sidecar hands it to
    // polkadot.js, whose U256 codec rejects anything above
    // Number.MAX_SAFE_INTEGER (~0.009 TAO). Same trap as #1663.
    const r = await ck.ethCall(to, data, {
      from,
      value: value == null ? 0 : BigInt(value).toString(),
      gas: gas ?? undefined,
    })
    if (!r.success) {
      throw new Error(`execution reverted: ${JSON.stringify(r.exitReason)}`)
    }
    return r.returnData ?? '0x'
  },

  // Standard spelling of sim_forkBlock, hex-encoded per the JSON-RPC spec.
  async eth_blockNumber() {
    return '0x' + Number(pinBlock ?? await ck.forkBlock()).toString(16)
  },
}

const server = http.createServer((req, res) => {
  if (req.method !== 'POST') { res.writeHead(405).end(); return }
  let body = ''
  req.on('data', (c) => { body += c })
  req.on('end', async () => {
    let id = null
    try {
      const msg = JSON.parse(body)
      id = msg.id
      const fn = HANDLERS[msg.method]
      if (!fn) throw new Error(`unknown method ${msg.method}`)
      const result = await fn(msg.params || [])
      res.writeHead(200, { 'content-type': 'application/json' })
      res.end(JSON.stringify({ jsonrpc: '2.0', id, result }))
    } catch (e) {
      res.writeHead(200, { 'content-type': 'application/json' })
      res.end(JSON.stringify({ jsonrpc: '2.0', id, error: { code: -32000, message: String(e.message || e) } }))
    }
  })
})
server.listen(PORT, () => console.log(`[rpc] anvil-dialect JSON-RPC on :${PORT}`))
