"""A transient RPC/provider failure must not be scored as a miner's drop.

When the provider rate-limits / times out / 5xx's, the solver emits no plan for
the affected order. ``epoch/relative_scoring`` records that as a ``dropped``
order — a HARD VETO that no number of wins can offset — so the round is decided
by the provider's hiccup rather than the miner's capability. The harness already
CLASSIFIED these failures (``_classify_rpc_error``) but only logged them; these
tests cover actually re-running the scenario instead.

The retry is deliberately narrow: it may only ever re-run a row that delivered
NOTHING and failed with a transient signature, so it can never change a result
the solver actually produced.
"""
import asyncio

import pytest

from minotaur_subnet.harness.orchestrator import (
    BenchmarkConfig,
    SolverTimeoutError,
    run_benchmark,
)
from minotaur_subnet.shared.types import ExecutionPlan, ScoreResult, SimulationResult


class _FlakySession:
    """Fake SolverSession whose first ``n_fail`` generate_plan calls fail."""

    def __init__(self, plan, *, n_fail, exc):
        self._plan = plan
        self._n_fail = n_fail
        self._exc = exc
        self.calls = 0
        self.restart_count = 0
        self._dead = False
        self._relaunch = object()  # non-None → the respawn path is available

    async def initialize(self, config):
        return None

    async def metadata(self):
        return {}

    async def on_benchmark_start(self, n):
        return None

    async def on_benchmark_end(self, summary):
        return None

    async def restart(self):
        self.restart_count += 1
        self._dead = False

    async def generate_plan(self, intent, state, snapshot):
        self.calls += 1
        if self.calls <= self._n_fail:
            # A timeout/crash kills the process, exactly like the real session.
            self._dead = True
            raise self._exc
        return self._plan


class _OkSim:
    async def simulate(self, plan, **kwargs):
        return SimulationResult(success=True, gas_used=100_000)


async def _score_fn(app_id, plan, simulation, st):
    r = ScoreResult(score=0.5)
    r.raw_output = "1000"  # a DELIVERING row
    return r


def _scenarios(n):
    from minotaur_subnet.harness.test_harness import (
        make_intent, make_snapshot, make_state,
    )
    return [(make_intent(), make_state(), make_snapshot()) for _ in range(n)]


def _run(*, n_fail, exc=None, n=1):
    scen = _scenarios(n)
    plan = ExecutionPlan(
        intent_id=scen[0][0].app_id, interactions=[], deadline=0, nonce=0,
    )
    exc = exc or SolverTimeoutError("upstream 429 rate limit")
    sess = _FlakySession(plan, n_fail=n_fail, exc=exc)
    results = asyncio.run(run_benchmark(
        sess, scen,
        config=BenchmarkConfig(chain_ids=[scen[0][1].chain_id]),
        score_fn=_score_fn, simulator=_OkSim(),
    ))
    return sess, results


def test_disarmed_by_default_transient_failure_still_drops(monkeypatch):
    """Ships OFF: without the env flag the run is byte-identical to today."""
    monkeypatch.delenv("BENCHMARK_RPC_RETRY_MAX", raising=False)
    sess, results = _run(n_fail=1)
    assert sess.calls == 1                      # no retry issued
    assert results[0].raw_output is None        # the order is still a DROP
    assert results[0].rpc_retries == 0


def test_transient_failure_is_retried_and_rescued(monkeypatch):
    """The whole point: a provider hiccup no longer costs the miner the order."""
    monkeypatch.setenv("BENCHMARK_RPC_RETRY_MAX", "2")
    sess, results = _run(n_fail=1)
    assert sess.calls == 2                      # first attempt + one retry
    assert sess.restart_count == 1              # respawned before retrying
    assert results[0].raw_output == "1000"      # DELIVERED — no longer a drop
    assert results[0].score == 0.5
    assert results[0].rpc_retries == 1


def test_retry_is_capped_per_scenario(monkeypatch):
    """A provider outage degrades to today's behaviour, bounded — never a hang."""
    monkeypatch.setenv("BENCHMARK_RPC_RETRY_MAX", "2")
    sess, results = _run(n_fail=99)
    assert sess.calls == 3                      # 1 attempt + 2 retries, then stop
    assert results[0].raw_output is None        # still a drop, as it must be
    assert results[0].rpc_retries == 2


def test_non_transient_failure_is_never_retried(monkeypatch):
    """A genuine routing failure is the miner's own result — retrying it would
    launder a real capability gap."""
    monkeypatch.setenv("BENCHMARK_RPC_RETRY_MAX", "2")
    sess, results = _run(n_fail=1, exc=RuntimeError("no route for pair"))
    assert sess.calls == 1
    assert results[0].rpc_retries == 0
    assert "no route for pair" in (results[0].error or "")


def test_delivering_row_is_never_retried(monkeypatch):
    """Retry may only rescue a row that delivered NOTHING, so it can never
    change a result the solver actually produced."""
    from minotaur_subnet.harness.orchestrator import (
        BenchmarkResult, _is_retryable_rpc_failure,
    )
    br = BenchmarkResult(intent_id="x")
    br.raw_output = "500"                       # it DELIVERED
    br.error = "timeout: upstream 429 rate limit"
    assert _is_retryable_rpc_failure(br) is None


def test_real_revert_is_never_retried():
    """A reverted scoreIntent is a deterministic plan outcome; its revert text
    can incidentally contain a transient-looking substring."""
    from minotaur_subnet.harness.orchestrator import (
        BenchmarkResult, _is_retryable_rpc_failure,
    )
    br = BenchmarkResult(intent_id="x")
    br.error = "real_sim_reverted: execution timeout in callee"
    assert _is_retryable_rpc_failure(br) is None


def test_run_budget_bounds_total_retries(monkeypatch):
    """Per-run ceiling: one flaky order cannot spend the budget the rest of the
    corpus needs for its FIRST attempt."""
    monkeypatch.setenv("BENCHMARK_RPC_RETRY_MAX", "5")
    monkeypatch.setenv("BENCHMARK_RPC_RETRY_RUN_MAX", "1")
    sess, results = _run(n_fail=99, n=3)
    # 3 first attempts + exactly 1 retry allowed across the whole run.
    assert sess.calls == 4
    assert sum(r.rpc_retries for r in results) == 1


def test_retries_stop_past_the_wall_clock_deadline(monkeypatch):
    """Retries spend the SAME budget as first attempts — past the deadline the
    run must spend what is left finishing first attempts, not re-running."""
    import minotaur_subnet.harness.orchestrator as orch_mod
    monkeypatch.setenv("BENCHMARK_RPC_RETRY_MAX", "2")
    # Budget large enough that the run is not abandoned, but the retry deadline
    # (75% of it) is already behind us at the first scenario.
    monkeypatch.setattr(orch_mod, "_RPC_RETRY_DEADLINE_FRACTION", 0.0)
    sess, results = _run(n_fail=1)
    assert sess.calls == 1                      # no retry issued
    assert results[0].rpc_retries == 0


@pytest.mark.parametrize("value,expected", [
    ("", 0), ("0", 0), ("2", 2), ("nonsense", 0), ("99", 5),
])
def test_retry_max_is_bounded_and_defaults_off(monkeypatch, value, expected):
    from minotaur_subnet.harness.orchestrator import _rpc_retry_max
    monkeypatch.setenv("BENCHMARK_RPC_RETRY_MAX", value)
    assert _rpc_retry_max() == expected
