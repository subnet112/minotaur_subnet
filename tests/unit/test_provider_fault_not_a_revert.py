"""A provider outage must not be reported to the miner as a plan revert.

ROOT CAUSE this covers. `_simulate_via_score_intent` wrapped its whole body in a
catch-all that returned ``None`` on ANY exception. The caller reads ``None`` as
"fail closed" and emits the generic ``scoreIntent simulation reverted``, so a
`{'code': -32070, 'message': 'Gateway request timeout'}` from the RPC gateway
reached the miner as ``real_sim_reverted`` — the plan's own failure — and
``epoch/relative_scoring`` graded that order ``dropped``, a HARD adoption veto.

Measured on the leader: 355 of 355 swallowed scoreIntent exceptions in a 6h
window were that one gateway timeout, and 87.4% of dropped rows carrying a
revert trace summarised "all N interactions succeeded" — the swap executed
fine; only the scoring wrapper failed.

Two things keep them apart now: -32070 is retryable at the backoff layer, and a
transient fault is labelled ``real_sim_unavailable`` rather than
``real_sim_reverted`` so the benchmark can retry it instead of scoring it 0.
"""
import asyncio

from minotaur_subnet.harness.orchestrator import (
    BenchmarkConfig,
    BenchmarkResult,
    _classify_rpc_error,
    _is_retryable_rpc_failure,
    run_benchmark,
)
from minotaur_subnet.rpc_backoff import is_retryable_rpc_code
from minotaur_subnet.simulator.anvil_simulator import (
    TRANSIENT_SIM_ERROR_PREFIX,
    _is_transient_rpc_payload,
)
from minotaur_subnet.shared.types import ExecutionPlan, ScoreResult, SimulationResult

GATEWAY_TIMEOUT = {"code": -32070, "message": "Gateway request timeout"}


def test_gateway_timeout_code_is_retryable():
    """-32070 is the JSON-RPC analogue of HTTP 504, which was already retryable."""
    assert is_retryable_rpc_code(-32070)
    assert is_retryable_rpc_code(-32005)          # unchanged
    assert not is_retryable_rpc_code(-32000)      # still excluded, deliberately


def test_raw_jsonrpc_payload_is_recognised_as_transient():
    """make_request hands back a plain dict, so the code rides on args, not as a
    typed web3 exception."""
    assert _is_transient_rpc_payload(Exception(GATEWAY_TIMEOUT))
    assert _is_transient_rpc_payload(Exception("{'code': -32070, 'message': 'Gateway request timeout'}"))
    assert not _is_transient_rpc_payload(Exception("execution reverted: SPL"))


def test_transient_error_string_reaches_the_retry_classifier():
    """The end-to-end string a transient fault now produces must be retryable —
    this is the join that was missing: the old wording carried no signature."""
    br = BenchmarkResult(intent_id="x")
    br.error = f"real_sim_unavailable: {TRANSIENT_SIM_ERROR_PREFIX} {GATEWAY_TIMEOUT}"
    assert _is_retryable_rpc_failure(br) is not None
    # The OLD wording — proof the retry could never have fired before the fix.
    stale = BenchmarkResult(intent_id="x")
    stale.error = "real_sim_reverted: scoreIntent simulation reverted"
    assert _classify_rpc_error(stale.error) is None
    assert _is_retryable_rpc_failure(stale) is None


def test_a_real_revert_is_still_a_revert():
    """The fix must not launder genuine plan failures into 'infrastructure'."""
    br = BenchmarkResult(intent_id="x")
    br.error = 'real_sim_reverted: scoreIntent reverted: Error("SPL")'
    assert _is_retryable_rpc_failure(br) is None


class _FakeSession:
    def __init__(self, plan):
        self._plan = plan

    async def initialize(self, config): return None
    async def metadata(self): return {}
    async def on_benchmark_start(self, n): return None
    async def on_benchmark_end(self, summary): return None
    async def generate_plan(self, intent, state, snapshot): return self._plan


class _FlakyProviderSim:
    """Transient gateway timeout on the first N calls, then a healthy sim."""

    def __init__(self, n_fail):
        self.n_fail = n_fail
        self.calls = 0

    async def simulate(self, plan, **kwargs):
        self.calls += 1
        if self.calls <= self.n_fail:
            return SimulationResult(
                success=False, gas_used=0,
                error=f"{TRANSIENT_SIM_ERROR_PREFIX} {GATEWAY_TIMEOUT}",
            )
        return SimulationResult(success=True, gas_used=100_000)


class _RevertingSim:
    async def simulate(self, plan, **kwargs):
        return SimulationResult(
            success=False, gas_used=0,
            error='scoreIntent reverted: Error("SPL")', revert_reason='Error("SPL")',
        )


async def _score_fn(app_id, plan, simulation, st):
    r = ScoreResult(score=0.5)
    r.raw_output = "1000"
    return r


def _run(sim, n=1):
    """require_real_sim needs a resolvable benchmark RPC (see
    test_benchmark_fail_closed) — the simulator itself is the fake here."""
    import os
    os.environ["ANVIL_RPC_URL"] = "http://localhost:8545"
    os.environ.pop("SOLVER_READ_PROXY", None)
    from minotaur_subnet.harness.test_harness import (
        make_intent, make_snapshot, make_state,
    )
    scen = [(make_intent(), make_state(), make_snapshot()) for _ in range(n)]
    plan = ExecutionPlan(
        intent_id=scen[0][0].app_id, interactions=[], deadline=0, nonce=0,
    )
    return asyncio.run(run_benchmark(
        _FakeSession(plan), scen,
        config=BenchmarkConfig(chain_ids=[scen[0][1].chain_id]),
        score_fn=_score_fn, simulator=sim, require_real_sim=True,
    ))


def test_transient_fault_is_labelled_unavailable_not_reverted(monkeypatch):
    monkeypatch.delenv("BENCHMARK_RPC_RETRY_MAX", raising=False)
    results = _run(_FlakyProviderSim(n_fail=99))
    err = results[0].error or ""
    assert err.startswith("real_sim_unavailable:"), err
    assert "real_sim_reverted" not in err
    # An outage tells us nothing about the plan, so no revert reason is claimed.
    assert results[0].revert_reason is None


def test_real_revert_still_labelled_reverted(monkeypatch):
    monkeypatch.delenv("BENCHMARK_RPC_RETRY_MAX", raising=False)
    results = _run(_RevertingSim())
    assert (results[0].error or "").startswith("real_sim_reverted:")
    assert results[0].revert_reason == 'Error("SPL")'


def test_transient_fault_is_retried_and_the_order_is_saved(monkeypatch):
    """The whole point: the miner keeps the order instead of eating a hard veto."""
    monkeypatch.setenv("BENCHMARK_RPC_RETRY_MAX", "2")
    sim = _FlakyProviderSim(n_fail=1)
    results = _run(sim)
    assert sim.calls == 2                       # retried once
    assert results[0].raw_output == "1000"      # DELIVERED — no longer a drop
    assert results[0].rpc_retries == 1
    assert results[0].error is None


def test_real_revert_is_not_retried(monkeypatch):
    monkeypatch.setenv("BENCHMARK_RPC_RETRY_MAX", "2")
    results = _run(_RevertingSim())
    assert results[0].rpc_retries == 0
