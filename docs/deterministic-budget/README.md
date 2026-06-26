# Deterministic compute budget — replacing the wall-clock GENERATE_PLAN timeout

## Problem

`generate_plan` has a **30s wall-clock timeout** (`SolverSession._send`, killed on
timeout). A cold multi-hop route makes ~100+ `eth_call`s; the benchmark Anvil
fork fetches cold storage slots from its upstream serially, so wall-clock per
case varies by host/fork/upstream speed. A borderline case therefore **times out
on a slow validator (scores 0) but completes on a fast one (~0.5)**. That
non-determinism (a) makes a candidate-vs-champion margin unreliable, and (b) is a
hard **cross-host champion-quorum blocker** — followers re-benchmarking the same
image diverge from the leader.

Client-side parallelism makes it **worse** (it floods the fork: `anvil_reset Read
timed out`, Uniswap `LOK`). The fix is a deterministic, **externally enforced**
compute budget: meter the solver's RPC *work* (an integer cost, not wall-clock)
and cut off at a fleet-uniform budget, so the same `(image, scenario, block)`
aborts at the **same point on every validator**.

## What's built (this PR — all inert)

| Piece | File | State |
|---|---|---|
| **Phase 0 — read-block pin** | `simulator/anvil_simulator.py` (`pin_read_fork`), `harness/orchestrator.py` (`run_benchmark`) | gated `PIN_SOLVER_READ_BLOCK` (default OFF) |
| **Counting/budget proxy** | `harness/rpc_budget_proxy/{cost_table,proxy}.py`, `Dockerfile` | standalone; not yet deployed/wired |
| **Pack-hash fold** | `harness/benchmark_pack.py` (`compute_pack_hash(compute_budget=)`), `cost_table.compute_budget_record` | `None` by default → hash unchanged |

**Determinism argument:** the abort point is a pure function of (solver image
bytes [digest-pinned], scenario+pack [round-derived], **read block** [Phase 0],
cost table, budget B) — none host- or speed-dependent. Identical bytes reading
identical block state issue an identical call sequence; integer costs summed over
it give an identical cumulative total; the Nth call that first exceeds the
fleet-uniform integer B is the same on every host → same `MINOTAUR_BUDGET_EXCEEDED`
→ same score 0 (or all complete). Folding `{B, cost_table}` into the pack hash
makes a divergent budget unable to reach quorum.

## What remains (rollout — phased, consensus-coordinated)

### 1. `start_docker` integration (next code step — unit-testable with a mocked proxy)
When a budget proxy is configured (`BUDGET_PROXY_DATA_URL` + `BUDGET_PROXY_CONTROL_URL`, both unset today → inert):
- mint a per-container `session_id`; `POST {control}/control/open {session_id, budget:B, mode}` over the **trusted `minotaur` net**;
- set `ANVIL_RPC_URL`/`BASE_RPC_URL`/`BITTENSOR_EVM_RPC_URL` to `{data}/rpc/<session_id>/<eth|base|btevm>` (the sandbox-net proxy IP) instead of the direct fork;
- store `session_id` on the `SolverSession`; `run_benchmark` calls `{control}/control/reset {session_id}` before each `generate_plan` and `{control}/control/close` after.
The sealed `internal: true` sandbox net means the solver's *only* route to a fork is the proxy data plane; the control plane is reachable **only** off the `minotaur` net — the untrusted solver can never reset its own budget.

### 2. Deploy the proxy sidecar (infra)
Add the proxy as a 4th multi-homed container in `platform/*/docker-compose.yml`: a static IP on `benchmark-sandbox` (data) + a leg on `minotaur` (control). `UPSTREAMS=eth=http://172.30.0.2:8545,base=...,btevm=...`, `BUDGET_PROXY_MODE=observe`.

### 3. Calibrate B (operational — needs real data)
Run observe-mode on the prod lead across real cold-DAI scenarios **at the pinned block** (Phase 0 on); record the per-scenario weighted-cost distribution; set **B = p99(cold-DAI cost) × headroom** — *not* a translation of the old 30s. Verify **cross-host count parity** (same image+scenario on two hosts → byte-identical logged cost) BEFORE enforcing.

### 4. Enforce, then fold into the pack hash (consensus-breaking — atomic fleet flip)
Flip `BUDGET_PROXY_MODE=enforce` (gated, under the adoption freeze); confirm a budget-exceed maps to `plan=None → score 0`. Then have the pack builder pass `compute_budget_record(B)` to `compute_pack_hash` — every validator must bump to the same `COST_TABLE_VERSION` **simultaneously**, or quorum drops mid-upgrade (same as `ROUND_ANCHORED_PIN`). Keep the 30s wall-clock only as a loose runaway backstop (well above B; a solver can hang *without* calls).

## Open risks
- A solver that branches on wall-clock timing reintroduces a host-dependent call sequence — mitigated only fail-safe (it can't reach quorum, so it can't be certified).
- The simulator shares the forks → the budget must count **only** the solver's session (per-session token), never simulator traffic; the simulator must NOT route through the proxy.
- The proxy is a new SPOF/throughput point — must fail **loud** (deterministic error), never silently fall back to direct anvil.
- Below-budget transparency: any byte change to request/response framing can shift a route — guard with a parity test vs direct-anvil before enforce.
- Phase 0 re-forks the solver's read to a (cold) historical block → it does **not** reduce latency (it's for determinism + the quote-vs-execution fix); the budget is what makes the cutoff deterministic.
