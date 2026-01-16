# Subnet Best-Practice Gap Report (Minotaur vs example_subnet/Chi)

This report compares the subnet code at the repository root (Minotaur) with the
best-practice example in `example_subnet/Chi`. It focuses on dependencies,
validator/miner behavior, weight-setting, and resilience. Items below are
required changes where the current implementation falls short of the example
standard or introduces operational risk.

## Required changes

### 1) Add missing runtime dependencies and lock them
**Where:** `requirements.txt`, `neurons/aggregator_client.py`,
`neurons/onchain_emitter.py`, `neurons/window_planner.py`

**Required change:**
- Add missing runtime deps used by code:
  - `aiohttp` (used by `AggregatorClient`)
  - `async-substrate-interface` (used by `OnchainWeightsEmitter` and
    `WindowPlanner`)
  - `bittensor-wallet` (used by `OnchainWeightsEmitter`)
- Consider moving to a `pyproject.toml` + lock file (as the Chi example does),
  or add a lock file to ensure deterministic installs.

**Reason:** The Chi example ships a locked dependency set; Minotaur currently
references packages that are not declared, which will break installs or lead to
non-reproducible environments.

**Consequence if unchanged:** Validators will fail at runtime with import
errors, and deployments will be inconsistent across hosts.

**Confidence:** High

---

### 2) Fix broken on-chain weight emission call
**Where:** `neurons/bittensor_validator.py`, `neurons/onchain_emitter.py`

**Required change:**
- `BittensorWeightCallback` calls `onchain_emitter.set_weights(...)`, but
  `OnchainWeightsEmitter` exposes `emit(...)` / `emit_async(...)`.
- Update the callback to call `emit_async(...)` (or add a `set_weights` wrapper
  that forwards to `emit_async`).

**Reason:** The current code will raise an attribute error and never emit
weights on-chain.

**Consequence if unchanged:** Validator runs but never updates weights, leading
to no emissions and likely validator deregistration or zero influence.

**Confidence:** High

---

### 3) Replace placeholder miner_id -> UID mapping
**Where:** `neurons/bittensor_validator.py`,
`neurons/metagraph_manager.py`,
`neurons/validation_engine.py`

**Required change:**
- Replace `hash(miner_id) % metagraph.n` with a deterministic mapping based on
  actual hotkeys from the metagraph.
- If `miner_id` is a hotkey (expected), map it via
  `MetagraphManager`’s `uid_for_hotkey`.
- If `miner_id` is not a hotkey, create a canonical mapping path (e.g., lookup
  miner_id -> hotkey from the aggregator) and apply it consistently.

**Reason:** Python’s hash is salted per process and non-deterministic across
validators. The example validator uses direct UID targeting without hash-based
guessing.

**Consequence if unchanged:** Different validators will emit different UID
weights for the same epoch, producing inconsistent chain updates and
misdirected rewards.

**Confidence:** High

---

### 4) Align epochs and weight emission to chain tempo + finalization buffer
**Where:** `neurons/validation_engine.py`, `neurons/validator.py`,
`neurons/window_planner.py`

**Required change:**
- Use the chain tempo to control weight emission frequency, as Chi does.
- Integrate `WindowPlanner` (currently unused) to derive epoch windows based on
  the previous finalized tempo window.
- Enforce the finalization buffer before emitting weights.

**Reason:** The Chi example only sets weights once per tempo. Minotaur uses
time-based epochs that are not tied to chain tempo and do not use the
finalization buffer, which is a best-practice for consistent, reorg-safe
windows.

**Consequence if unchanged:** Validators may emit weights too frequently,
miss the intended on-chain window alignment, or process overlapping epochs,
leading to non-deterministic rewards and increased on-chain failures.

**Confidence:** Medium-High

---

### 5) Add a validator liveness watchdog (heartbeat / restart)
**Where:** `neurons/validator.py`

**Required change:**
- Add a heartbeat monitor similar to Chi’s validator that restarts the process
  if no heartbeat is detected for a configurable interval.
- At minimum, ensure the background validation thread updates a shared heartbeat
  timestamp and have a watchdog thread restart on stalling.

**Reason:** The Chi example includes a watchdog to recover from deadlocks or
silent stalls. Minotaur’s validator runs async loops and background threads
without any liveness enforcement.

**Consequence if unchanged:** The validator can stall indefinitely without
automatic recovery, resulting in stale weights and downtime.

**Confidence:** Medium

---

### 6) Enforce validator permit before emission
**Where:** `neurons/metagraph_manager.py`, `neurons/bittensor_validator.py`

**Required change:**
- If `MetagraphSnapshot.validator_permit` is false, skip weight emission and log
  a clear reason.
- Ensure this check gates `OnchainWeightsEmitter.emit(...)`.

**Reason:** The metagraph manager already detects permit status but the emission
path does not use it.

**Consequence if unchanged:** Validators without permits will repeatedly submit
weight updates that fail, causing noisy logs and unnecessary chain calls.

**Confidence:** Medium

---

### 7) Use real hotkey signing in miner bittensor mode
**Where:** `neurons/miner.py`

**Required change:**
- In bittensor mode, sign miner registration and updates using the wallet
  hotkey keypair, not a derived key from the hotkey address.

**Reason:** The Chi example validator always uses the wallet/hotkey. Deriving a
signing key from the hotkey address is explicitly marked as a placeholder and
does not authenticate the actual hotkey owner.

**Consequence if unchanged:** Miner signatures are not cryptographically tied
to the real hotkey, weakening security and potentially failing validation if
the aggregator verifies signatures against the on-chain hotkey.

**Confidence:** Medium-High

---

## Summary
The largest correctness gaps vs the Chi example are: missing dependencies,
broken weight emission, non-deterministic UID mapping, and missing tempo-based
weight scheduling. Fixing these will bring the validator/miner flows in line
with best-practice expectations and materially improve resilience and
determinism.

