# Deterministic compute budget — replacing the wall-clock GENERATE_PLAN timeout

> **STATUS — SHIPPED + DEPLOYED + VERIFIED LIVE (2026-06-26).** Everything below is
> implemented and on `:stable` (PRs #278/#280/#282/#284/#285/#287/#291). The block-pin
> proxy is **deployed and wired**, the budget is **enforced** (`B=5000`, a uniform code
> constant `solver_read_proxy.DEFAULT_GENERATE_PLAN_BUDGET`), and both fold into
> `benchmark_pack_hash`. Verified live on the prod lead: a round close seals the pin and
> the benchmark routes **all** solver reads through the proxy (18,673 reads pinned to a
> single Base block, `mode=enforce`, max 240/5000, **zero** anvil fallback); the
> submission scores deterministically.
>
> **One change from the plan below:** the proxy is **NOT a compose sidecar**. The **api
> launches it as a managed container** at startup (`minotaur_subnet/api/read_proxy_manager.py`)
> from its own image, so the whole stack rides the normal `:stable` image update with no
> operator action — a Watchtower-only validator would never get a new compose *service*.
> Sections below are kept as the design rationale + rollout record; the "what remains"
> steps are all DONE.

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

## What was built (now SHIPPED + active on `:stable`)

| Piece | File | State |
|---|---|---|
| **Read-block pin (round-anchored)** | `consensus/round_anchor.py`, `harness/benchmark_worker.py`, `api/startup.py` | DEFAULT-ON; **fails loud** on an unavailable pin — defers, never benchmarks at live head (#285) |
| **Block-pin + counting proxy** | `harness/rpc_budget_proxy/{cost_table,proxy,rewrite_table}.py` | **api-launched** managed container (#291); reads pinned + metered, `mode=enforce` |
| **Solver-read wiring** | `harness/solver_read_proxy.py`, `api/read_proxy_manager.py`, `harness/orchestrator.py` | active; `B=5000` uniform code constant, default-on when the proxy is present |
| **Pack-hash fold** | `harness/benchmark_pack.py` (`compute_pack_hash`), `cost_table.compute_budget_record`, `rewrite_table.rewrite_table_record` | folds budget + rewrite-table; consensus-versioned (#284) |

**Determinism argument:** the abort point is a pure function of (solver image
bytes [digest-pinned], scenario+pack [round-derived], **read block** [Phase 0],
cost table, budget B) — none host- or speed-dependent. Identical bytes reading
identical block state issue an identical call sequence; integer costs summed over
it give an identical cumulative total; the Nth call that first exceeds the
fleet-uniform integer B is the same on every host → same `MINOTAUR_BUDGET_EXCEEDED`
→ same score 0 (or all complete). Folding `{B, cost_table}` into the pack hash
makes a divergent budget unable to reach quorum.

## What remains (rollout — phased, consensus-coordinated)

### 1. Orchestrator integration — ✅ DONE (#282, #291)
When a budget proxy is configured (now auto-exported by `read_proxy_manager` after it launches the proxy):
- mint a per-container `session_id`; `POST {control}/control/open {session_id, budget:B, mode}` over the **trusted `minotaur` net**;
- set `ANVIL_RPC_URL`/`BASE_RPC_URL`/`BITTENSOR_EVM_RPC_URL` to `{data}/rpc/<session_id>/<eth|base|btevm>` (the sandbox-net proxy IP) instead of the direct fork;
- store `session_id` on the `SolverSession`; `run_benchmark` calls `{control}/control/reset {session_id}` before each `generate_plan` and `{control}/control/close` after.
The sealed `internal: true` sandbox net means the solver's *only* route to a fork is the proxy data plane; the control plane is reachable **only** off the `minotaur` net — the untrusted solver can never reset its own budget.

### 2. Deploy the proxy — ✅ DONE (api-launched, NOT a compose sidecar)
SUPERSEDED: rather than a 4th compose container, the **api launches the proxy as a
managed container** (`api/read_proxy_manager.py`) on `benchmark-sandbox` (`.5`, data) +
`minotaur` (control) from its own image, reusing the api's upstream RPCs +
`SOLVER_ROUND_INTERNAL_API_KEY` as the control token — so it activates on the normal
`:stable` image update with **no compose change** (a Watchtower-only validator would never
get a new compose service). Idempotent across restarts; disable with `DISABLE_READ_PROXY=1`.

### 3. Calibrate B — ✅ DONE (B=5000, a uniform code constant)
Calibrated from observed per-scenario reads at the pinned block (~300 max early; live prod max **240/scenario**), B is set to **5000** — a generous ~20× runaway backstop. It is `DEFAULT_GENERATE_PLAN_BUDGET` (code, not a per-validator env) so the fleet folds an identical value into the pack hash. Cross-host count parity is now provided by the fleet itself (every validator runs the same `:stable` code + the same pinned block) rather than a synthetic two-host test.

### 4. Enforce + fold into the pack hash — ✅ DONE (#284, #287; enforcing live)
The proxy session opens with `mode=enforce` + `budget=B` (default-on when the proxy is present), so a budget-exceed maps to `plan=None → score 0`. The pack builder folds `compute_budget_record(B)` + the block-rewrite-table version into `compute_pack_hash`, so a divergent budget/cost-table can't reach quorum. The wall-clock `GENERATE_PLAN` timeout is loosened to a loose runaway backstop (well above B; a solver can hang *without* calls). The staggered `:stable` rollout briefly mixes pack hashes — harmless under the adoption freeze; uniform once the fleet converges (same discipline as `ROUND_ANCHORED_PIN`).

## Open risks
- A solver that branches on wall-clock timing reintroduces a host-dependent call sequence — mitigated only fail-safe (it can't reach quorum, so it can't be certified).
- The simulator shares the forks → the budget must count **only** the solver's session (per-session token), never simulator traffic; the simulator must NOT route through the proxy.
- The proxy is a new SPOF/throughput point — must fail **loud** (deterministic error), never silently fall back to direct anvil.
- Below-budget transparency: any byte change to request/response framing can shift a route — guard with a parity test vs direct-anvil before enforce.
- Phase 0 re-forks the solver's read to a (cold) historical block → it does **not** reduce latency (it's for determinism + the quote-vs-execution fix); the budget is what makes the cutoff deterministic.
