# Chopsticks simulation backend for Bittensor (chain 964) — feasibility PoC

**Question:** can Minotaur benchmark Apps deployed to Bittensor's EVM (chain 964),
including Apps that call the **native precompiles** (staking `0x805`, alpha `0x808`,
swap) that a plain **anvil fork cannot execute**?

**Answer: YES for scoring, proven end-to-end.** A Chopsticks fork of subtensor runs
the real runtime wasm, so the native precompiles execute. Everything Minotaur scores
on — delivered output, gas, validity — is obtainable from a single deterministic
**dry-run** (`eth_call`), with **no block-building required**.

## Acceptance test (the one asked for): simulate a stake on SN112

`sn112_stake_poc.mjs` forks Finney at a pinned block and, entirely through the
`ChopsticksAnvil` shim, stakes 1 TAO into an SN112 hotkey and measures the alpha
delivered — the substrate analog of scoring a Base/ETH DEX order.

```
fork @ block 8637391  router coldkey 0xec74999c…
direct getStake before: 0
--- SN112 stake simulation ---
  staked      : 1 TAO (1000000000 rao)
  alpha DELTA : 216804943218  (~216.8 alpha)
  used gas    : 105251
  exit        : {"succeed":"Returned"}
RESULT: PASS ✅
```

**Deterministic:** the delta is byte-identical (`216804943218`) across repeated runs
at the same pinned block — the property 5 validators need to agree on a score.

### How it works
The staking precompile emits **no EVM logs** and `addStake` returns void, so you
cannot read delivered alpha from logs. Instead a tiny **measuring-router** contract
(`StakeMeter.sol`) does, in one call: `getStake` (before) → `addStake` → `getStake`
(after) → returns the delta. The precompile's state change is visible to the later
read **within the same EVM execution**, and `eth_call` (`EthereumRuntimeRPCApi_call`)
returns that delta as return data. No transaction, no block, no impersonation.

## What was proven (anvil-parity matrix)

| anvil capability | Chopsticks/subtensor mapping | status |
|---|---|---|
| fork @ pinned block | `chopsticks --block N` | ✅ |
| **native precompiles execute** | real runtime wasm (`getAlphaPrice`, `getStake`, `addStake` verified) | ✅ |
| `eth_call` (arbitrary `from`) | `EthereumRuntimeRPCApi_call` → `{exitReason, value, usedGas, logs}` | ✅ |
| delivered-output scoring | staking: state-delta via measuring router · DEX: `logs` from the dry-run | ✅ |
| gas metering | `usedGas` = real pre-refund EVM gas (105k above) → GAS-PAR reusable | ✅ |
| `anvil_setBalance` | `dev_setStorage` `System.Account` on the mapped account | ✅ |
| `anvil_setCode` | `dev_setStorage` `EVM.AccountCodes` (raw key, **SCALE `Bytes` = compact(len)++code**) + `AccountCodesMetadata` | ✅ |
| `anvil_setStorageAt` | `dev_setStorage` `EVM.AccountStorages` | ✅ |
| determinism across validators | same pinned block ⇒ byte-identical result | ✅ |
| `eth_sendTransaction` / persisted state / `evm_snapshot`/`revert` / impersonated sends | needs `dev_newBlock` | ⚠️ blocked, see below |

### The one gap: building blocks
`dev_newBlock` **hangs**. Subtensor's runtime imports a BLS12-381 host function
(`ext_host_calls_bls12_381_mul_projective_g2_version_1`, for `pallet_drand`) that
Chopsticks' bundled executor (`chopsticks-executor@1.5.0`) does not provide.
`--allow-unresolved-imports` lets the runtime **instantiate and run dry-runs** (which
never call BLS), but *building a block* runs `pallet_drand`'s per-block hook, which
calls BLS → trap/hang.

**This does not block Minotaur scoring** (scoring is dry-run only). It only blocks a
literal `eth_sendTransaction`/state-persistence/snapshot-revert parity. If that is
ever needed, options (untried, ranked): (1) neuter `pallet_drand` via `dev_setStorage`
so its hook short-circuits before BLS; (2) `--wasm-override` a runtime built with
pure-wasm EC (no host BLS); (3) a `chopsticks-executor` build that provides the BLS
host functions. Impersonation itself is solved (`--mock-signature-host` +
`EnsureAddressTruncated` truncated-account) — it's only the block-build hook that traps.

## Integration into Minotaur (IMPLEMENTED)

Wired the same way anvil is:

- `chains/registry.py` — `ChainSpec.sim_backend`; chain 964 = `"substrate_chopsticks"`.
- `simulator/anvil_simulator.py` — `MultiChainSimulator` dispatches 964 →
  `SubtensorSimulator`, all else → `AnvilSimulator` (same duck-typed surface).
  **Gated** on `BITTENSOR_CHOPSTICKS_SIM_RPC_URL` being set → ships INERT (964 stays
  on anvil-btevm, byte-identical) until turned on fleet-wide.
- `simulator/subtensor_simulator.py` — the Python backend; drives this sidecar over
  JSON-RPC, populates `SimulationResult` `token_transfers` (logs) + `gas_used`/
  `gas_metered` (real pre-refund EVM gas) + `return_data`. `raw_output` is an opaque
  BigInt downstream, so the per-App scorer JS + `relative_scoring` are unchanged.
- `platform/validator/docker-compose.yml` — `chopsticks-btevm` service behind the
  `chopsticks` profile (inert like the -bench forks), `--db` lazy-storage fork cache,
  `CK_ENDPOINT` = the leader's blockmachine subtensor node.

Test: `minotaur_subnet/simulator/test_subtensor_simulator.py` (dispatch gate on/off +
an SN112 stake integration test).

### To activate on the leader
1. `COMPOSE_PROFILES=chopsticks` (starts `chopsticks-btevm`).
2. `BITTENSOR_CHOPSTICKS_SIM_RPC_URL=http://chopsticks-btevm:8545` on api + validator.
3. `BITTENSOR_SUBSTRATE_WS_URL=<blockmachine subtensor wss>` (fork upstream).
4. `CK_BLOCK=<round fork block>` per round (see the per-round re-pin follow-up below).

### Known follow-ups (not blocking the integration)
- **Per-round re-pin**: the fork is pinned at container launch (`CK_BLOCK`); live
  re-pin to the round's fork block (`dev_setHead`) so `pin_read_fork` re-anchors
  without a restart. Until then the sidecar must be (re)launched at the round block
  for cross-validator determinism.
- **scoreIntent decode**: `simulate()` surfaces the terminal call's `return_data`;
  wiring the App's `scoreIntent` tuple + a substrate raw-output scorer JS that reads
  it is the next step for full parity with the DexAggregator scorer.
- **Throughput**: the JS-wasm executor is slow — shard for hundreds of candidates.

## Running it

```bash
cd tools/chopsticks-sim && npm install
node launch.mjs            # terminal 1: forks Finney with the required flags
CK_WS=ws://127.0.0.1:8000 node sn112_stake_poc.mjs   # terminal 2
```

`StakeMeter.deployed.hex` is the compiled runtime bytecode of `StakeMeter.sol`
(rebuild with `forge build`). Files: `chopsticks_anvil.mjs` (the shim),
`sn112_stake_poc.mjs` (acceptance test), `launch.mjs` (fork launcher).

## Verdict

Reasonable anvil parity **good enough for Minotaur scoring is achieved and proven**:
native-precompile Apps on chain 964 (including a staking/vault App on SN112) can be
benchmarked deterministically via the dry-run + measuring-router pattern, reusing the
existing scorer/gas/relative-scoring machinery. Full transactional parity
(state-persistence) is blocked only by the drand-BLS executor gap, which is not on the
scoring path.
