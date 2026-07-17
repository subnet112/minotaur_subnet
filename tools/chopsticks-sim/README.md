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

### Follow-ups (DONE)
- **Per-round re-pin** ✓ — `pin_read_fork` re-anchors the live fork to the round's
  block via the sidecar's `sim_repin` (`dev_setHead`), no restart. Verified it
  re-anchors STATE (native precompile reads match the archive node at the re-pinned
  block), and it's idempotent (many candidates at one block re-pin once). Requires an
  ARCHIVE upstream (`CK_ENDPOINT`) for jumps beyond the node's pruning window — the
  leader's blockmachine node is archive.
- **Delivered-output + scorer JS** ✓ — the App's scored (terminal) call returns the
  delivered output as its last 32-byte word; `simulate()` surfaces it as a typed
  `delivered_output` state_change, and `harness/scoring_shadow/subtensor_stake_raw.js`
  (the substrate analog of `dex_aggregator_raw.js`) reads it into
  `metadata.raw_output`. Optional `scoreIntent` calldata in the order → `on_chain_score`.
- **Throughput / sharding** ✓ — `BITTENSOR_CHOPSTICKS_SIM_RPC_URL` accepts a
  COMMA-SEPARATED pool of sidecar URLs; `SubtensorSimulator` round-robins each
  `simulate()` across the pool (each call does re-pin+fund+call on one instance),
  and `pin_read_fork` pins every instance. Run N replicas of `chopsticks-btevm` to
  fan out the single-threaded JS-wasm executor across candidates.

- **scoreIntent for arbitrary Apps** ✓ — the `scoreIntent((IntentOrder),(ExecutionPlan))`
  tuple is app-agnostic (app-specific data is in the `intent_params` bytes), so the
  encoder ports directly. `SubtensorSimulator._build_score_intent_calldata` builds it
  from the orchestrator's `intent_order` (identical encoding to the anvil path), calls
  the App read-only, and decodes `(uint256 score, bool valid)` → `on_chain_score`. Any
  964 App following AppIntentBase is now scored on both `raw_output` and `on_chain_score`.

- **Relayer as msg.sender** ✓ — like the anvil path, the backend calls `relayer()`
  on the App to discover its configured relayer and uses that address as the
  `from`/msg.sender for BOTH the plan execution and the `scoreIntent` read (an
  AppIntentBase App gates both on it). Because the backend is dry-run-only, NO
  impersonation is needed (anvil impersonates only for the state-changing *send*).
  Falls back to `metadata.executor`/default when the target has no `relayer()`.

### Still open
- A concrete production 964 App + its manifest to soak against (the machinery is
  complete; it just hasn't been exercised by a real miner App yet).

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
