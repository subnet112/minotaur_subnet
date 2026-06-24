"""run_benchmark must NOT truncate the run when the solver times out / crashes.

A per-scenario timeout kills the solver process; previously the next scenario
hit the dead process (``SolverCrashedError``) and the run ``break``-ed — so a
single slow case zeroed every case after it, and *which* case is slow is RPC-
latency-dependent → non-deterministic scores across validators. The fix: score
only the offending scenario 0, RESPAWN the solver, and continue, so the result
set is always the full corpus and reproducible.
"""
import asyncio

from minotaur_subnet.harness.orchestrator import (
    BenchmarkConfig,
    SolverCrashedError,
    SolverTimeoutError,
    run_benchmark,
)
from minotaur_subnet.shared.types import ExecutionPlan, ScoreResult, SimulationResult


class _CrashingSession:
    """Fake SolverSession that crashes on chosen generate_plan calls."""

    def __init__(self, plan, crash_indices, crash_exc, revive=True):
        self._plan = plan
        self._crash_indices = set(crash_indices)
        self._crash_exc = crash_exc
        self._revive = revive
        self._calls = 0
        self._dead = False
        self.restart_count = 0
        self._relaunch = object()  # non-None → run_benchmark takes the respawn path

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
        if not self._revive:
            raise RuntimeError("relaunch failed")
        self._dead = False  # a successful respawn revives the process

    async def generate_plan(self, intent, state, snapshot):
        if self._dead:
            # A dead process: every command reports "not running" (the cascade
            # the old `break` truncated on).
            raise SolverCrashedError("Solver process is not running")
        idx = self._calls
        self._calls += 1
        if idx in self._crash_indices:
            self._dead = True  # the timeout/crash kills the process (like kill())
            raise self._crash_exc
        return self._plan


class _OkSim:
    async def simulate(self, plan, **kwargs):
        return SimulationResult(success=True, gas_used=100_000)


async def _score_fn(app_id, plan, simulation, st):
    return ScoreResult(score=0.5)


def _scenarios(n):
    from minotaur_subnet.harness.test_harness import (
        make_intent, make_snapshot, make_state,
    )
    return [(make_intent(), make_state(), make_snapshot()) for _ in range(n)]


def _run(crash_indices, crash_exc, n=3, revive=True):
    scen = _scenarios(n)
    plan = ExecutionPlan(
        intent_id=scen[0][0].app_id, interactions=[], deadline=0, nonce=0,
    )
    sess = _CrashingSession(plan, crash_indices, crash_exc, revive=revive)
    results = asyncio.run(run_benchmark(
        sess, scen,
        config=BenchmarkConfig(chain_ids=[scen[0][1].chain_id]),
        score_fn=_score_fn, simulator=_OkSim(),
    ))
    return sess, results


def test_timeout_scores_only_that_scenario_zero_and_continues():
    sess, results = _run([1], SolverTimeoutError("plan timed out"), n=3)
    assert len(results) == 3                       # full corpus, NOT truncated
    assert results[1].score == 0
    assert "timeout" in (results[1].error or "")   # surfaced in the report
    assert results[0].score == 0.5                 # crash did NOT cascade
    assert results[2].score == 0.5
    assert sess.restart_count == 1                 # respawned once, after the timeout


def test_crash_does_not_truncate_and_respawns():
    sess, results = _run([0], SolverCrashedError("Solver process is not running"), n=3)
    assert len(results) == 3
    assert results[0].score == 0 and "crashed" in (results[0].error or "")
    assert results[1].score == 0.5 and results[2].score == 0.5
    assert sess.restart_count == 1


def test_total_run_budget_scores_remaining_zero(monkeypatch):
    # restart() resets the per-session cap, so a per-RUN wall-clock budget caps
    # the whole run. Force it to 0 → tripped on the first scenario → every
    # scenario scored 0 deterministically (full corpus, distinct reason).
    import minotaur_subnet.harness.orchestrator as orch_mod
    monkeypatch.setattr(orch_mod, "TOTAL_BENCHMARK_TIMEOUT", 0.0)
    sess, results = _run([], SolverTimeoutError("unused"), n=3)
    assert len(results) == 3
    assert all(r.score == 0 for r in results)
    assert all("total run budget exceeded" in (r.error or "") for r in results)


def test_unrecoverable_solver_scores_remaining_zero_not_truncate():
    # restart() always fails → solver_dead → the rest scored 0 deterministically
    # (full result set, never truncated).
    sess, results = _run([1], SolverCrashedError("dead"), n=4, revive=False)
    assert len(results) == 4                        # full corpus
    assert results[0].score == 0.5                  # ran before the crash
    assert results[1].score == 0                    # the crash
    assert results[2].score == 0 and "unrecoverable" in (results[2].error or "")
    assert results[3].score == 0 and "unrecoverable" in (results[3].error or "")
