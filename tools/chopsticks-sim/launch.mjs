// Launch a Chopsticks fork of Bittensor Finney with the flags the shim needs.
// Usage: node launch.mjs [block] [port]
//   block defaults to (head - 30) pulled from the lite RPC; port defaults to 8000.
// Leave running in one terminal, then run sn112_stake_poc.mjs in another.

import { spawn } from 'node:child_process'

const ENDPOINT = process.env.CK_ENDPOINT || 'wss://entrypoint-finney.opentensor.ai:443'
const LITE_HTTP = process.env.CK_LITE || 'https://lite.chain.opentensor.ai'
const port = process.argv[3] || process.env.CK_PORT || '8000'

async function head() {
  const r = await fetch(LITE_HTTP, {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ jsonrpc: '2.0', id: 1, method: 'eth_blockNumber', params: [] }),
  })
  return parseInt((await r.json()).result, 16)
}

const block = process.argv[2] || (await head()) - 30
console.log(`forking ${ENDPOINT} @ block ${block} on :${port}`)

// KEY FLAGS:
//   --allow-unresolved-imports : subtensor's runtime imports a BLS12-381 host fn
//        (pallet_drand) that Chopsticks' executor lacks; this lets the runtime
//        instantiate and run — dry-runs never call BLS so they succeed. (Building a
//        block DOES hit it via drand's per-block hook and hangs — a known gap.)
//   --mock-signature-host : forge any signer; needed only for the block-building
//        impersonation path, harmless for dry-run scoring.
//   --build-block-mode Manual : never auto-seal.
const args = [
  '-y', '@acala-network/chopsticks@latest',
  '--endpoint', ENDPOINT,
  '--block', String(block),
  '--port', String(port),
  '--allow-unresolved-imports',
  '--mock-signature-host',
  '--build-block-mode', 'Manual',
]
spawn('npx', args, { stdio: 'inherit' })
