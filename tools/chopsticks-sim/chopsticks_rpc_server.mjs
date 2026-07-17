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
  const child = spawn('npx', args, { stdio: ['ignore', 'inherit', 'inherit'] })
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
const ck = await ChopsticksAnvil.connect(attachWs)
if (pinBlock === undefined) pinBlock = await ck.forkBlock()
console.log(`[rpc] shim connected; fork @ ${await ck.forkBlock()}`)

const HANDLERS = {
  async sim_health() {
    return { ok: true, block: await ck.forkBlock(), pinBlock }
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
