// End-to-end proof: simulate a stake on SN112 through a Chopsticks fork of Bittensor
// Finney, driven entirely through the ChopsticksAnvil shim. This is the substrate
// analog of what AnvilSimulator does for a Base/ETH DEX order: fork @ pinned block,
// deploy scoring contract, fund, run the candidate, read the delivered output.
//
// Run:
//   1) node tools/chopsticks-sim/launch.mjs   (or chopsticks CLI w/ launchArgs)
//   2) node tools/chopsticks-sim/sn112_stake_poc.mjs
//
// PASS = staking 1 TAO on SN112 returns a deterministic, positive alpha delta.

import { ChopsticksAnvil } from './chopsticks_anvil.mjs'
import { keccakAsHex } from '@polkadot/util-crypto'
import fs from 'fs'

const ROUTER = '0x0000000000000000000000000000000000009999'
const STAKING = '0x0000000000000000000000000000000000000805'
const SN112_HOTKEY = '0x56426093d1d8298bbc833d8fec69b94733841ebe0f5cebbb29062d5baf58ab5c' // SN112 uid0
const NETUID = 112
const AMOUNT_RAO = 1_000_000_000n // 1 TAO
const WS = process.env.CK_WS || 'ws://127.0.0.1:8000'

const code = fs.readFileSync(new URL('./StakeMeter.deployed.hex', import.meta.url), 'utf8').trim()
const sel = (sig) => keccakAsHex(sig).slice(0, 10)
const w = (x) => BigInt(x).toString(16).padStart(64, '0')
const b32 = (h) => h.replace(/^0x/, '').padStart(64, '0')

const ck = await ChopsticksAnvil.connect(WS)
const coldkey = ck.mappedAccount(ROUTER)
console.log(`fork @ block ${await ck.forkBlock()}  router coldkey ${coldkey}`)

// deploy measuring router + fund it (cheatcodes)
await ck.setCode(ROUTER, code)
await ck.setBalance(ROUTER, 1000n * 1_000_000_000n) // 1000 TAO

// sanity: direct precompile getStake == 0 for a fresh coldkey
const g = await ck.ethCall(STAKING, sel('getStake(bytes32,bytes32,uint256)') + b32(SN112_HOTKEY) + b32(coldkey) + w(NETUID))
console.log(`direct getStake before: ${BigInt(g.returnData)}  (exit ${JSON.stringify(g.exitReason)})`)

// THE TEST: stakeAndMeasure(hotkey, coldkey, netuid, amountRao) in one dry-run
const call = sel('stakeAndMeasure(bytes32,bytes32,uint256,uint256)') + b32(SN112_HOTKEY) + b32(coldkey) + w(NETUID) + w(AMOUNT_RAO)
const r = await ck.ethCall(ROUTER, call)
const h = (r.returnData || '0x').replace(/^0x/, '')
const before = BigInt('0x' + (h.slice(0, 64) || '0'))
const after = BigInt('0x' + (h.slice(64, 128) || '0'))
const delta = BigInt('0x' + (h.slice(128, 192) || '0'))

console.log('--- SN112 stake simulation ---')
console.log(`  staked      : 1 TAO (${AMOUNT_RAO} rao)`)
console.log(`  alpha before: ${before}`)
console.log(`  alpha after : ${after}`)
console.log(`  alpha DELTA : ${delta}  (~${Number(delta) / 1e9} alpha)`)
console.log(`  used gas    : ${r.usedGas ? BigInt(r.usedGas) : null}`)
console.log(`  exit        : ${JSON.stringify(r.exitReason)}`)
console.log(delta > 0n && r.success ? 'RESULT: PASS ✅' : 'RESULT: FAIL ❌')

await ck.disconnect()
process.exit(delta > 0n && r.success ? 0 : 1)
