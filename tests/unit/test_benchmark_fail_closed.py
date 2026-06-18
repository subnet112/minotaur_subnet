"""Phase 4 (safe slice) — fail-closed simulation.

By default, when a real Anvil sim is unavailable, run_benchmark falls back to a
FABRICATED mock (`_build_benchmark_simulation`) that reports a ~min*1.05 success
and is then scored normally — exploitable, and it masks infra failures as
passing scores. The opt-in `require_real_sim` switch closes that:

  * no simulator injected      -> raise RealSimulationUnavailable (misconfig;
                                  don't silently zero/score every solver)
  * a real simulate() throws   -> score that scenario 0 (same as an on-chain
                                  revert), never the mock

Default `require_real_sim=False` must preserve the mock fallback exactly.
"""
import asyncio

import pytest

from minotaur_subnet.harness.orchestrator import (
    BenchmarkConfig,
    RealSimulationUnavailable,
    run_benchmark,
)
from minotaur_subnet.shared.types import ExecutionPlan, ScoreResult, SimulationResult


class _FakeSession:
    def __init__(self, plan):
        self._plan = plan

    async def initialize(self, config):
        return None

    async def metadata(self):
        return {}

    async def on_benchmark_start(self, n):
        return None

    async def generate_plan(self, intent, state, snapshot):
        return self._plan

    async def on_benchmark_end(self, summary):
        return None


class _ThrowingSimulator:
    async def simulate(self, plan, **kwargs):
        raise RuntimeError("anvil down")


class _RevertingSimulator:
    # A REAL simulation that returns success=False (an on-chain revert) — does
    # not throw. The JS scorer doesn't hard-gate on success, so without the
    # switch a lenient app scorer could still pass it.
    async def simulate(self, plan, **kwargs):
        return SimulationResult(success=False, error="execution reverted")


def _intent_state_snapshot():
    from minotaur_subnet.harness.test_harness import (
        make_intent,
        make_snapshot,
        make_state,
    )

    return make_intent(), make_state(), make_snapshot()


def _run(*, simulator, require_real_sim):
    intent, state, snapshot = _intent_state_snapshot()
    plan = ExecutionPlan(intent_id=intent.app_id, interactions=[], deadline=0, nonce=0)
    calls = {"score_fn": 0}

    async def score_fn(app_id, plan, simulation, st):
        calls["score_fn"] += 1
        # A deliberately PASSING score so we can detect if the mock path scored.
        return ScoreResult(score=0.9)

    async def _go():
        return await run_benchmark(
            _FakeSession(plan),
            [(intent, state, snapshot)],
            config=BenchmarkConfig(chain_ids=[state.chain_id]),
            score_fn=score_fn,
            simulator=simulator,
            require_real_sim=require_real_sim,
        )

    return asyncio.run(_go()), calls


# ── fail-closed ON ───────────────────────────────────────────────────────────

def test_no_simulator_raises_when_required():
    with pytest.raises(RealSimulationUnavailable):
        _run(simulator=None, require_real_sim=True)


def test_sim_throw_scores_zero_when_required_no_mock(monkeypatch):
    # A real simulator IS injected; an RPC must be present so run_benchmark's RPC
    # precondition passes and we exercise the THROW path (not the no-RPC raise).
    monkeypatch.setenv("ANVIL_RPC_URL", "http://localhost:8545")
    results, calls = _run(simulator=_ThrowingSimulator(), require_real_sim=True)
    assert len(results) == 1
    r = results[0]
    assert r.score == 0.0, "fail-closed scenario must score 0, not a mock pass"
    assert r.mock_simulation is False, "must NOT have used the fabricated mock"
    assert r.error and r.error.startswith("real_sim_unavailable")
    assert calls["score_fn"] == 0, "the scorer must not run on absent sim data"


def test_sim_revert_scores_zero_when_required(monkeypatch):
    # success=False (a real revert) must be fail-closed to 0, not handed to a
    # possibly-lenient scorer — closes the gap the adversarial review found.
    # RPC present so the precondition passes and we reach the revert path.
    monkeypatch.setenv("ANVIL_RPC_URL", "http://localhost:8545")
    results, calls = _run(simulator=_RevertingSimulator(), require_real_sim=True)
    r = results[0]
    assert r.score == 0.0
    assert r.mock_simulation is False
    assert r.error and r.error.startswith("real_sim_reverted")
    assert calls["score_fn"] == 0, "the scorer must not run on a reverted sim"


# ── fail-closed OFF (default) preserves today's mock fallback ─────────────────

def test_sim_throw_falls_back_to_mock_by_default():
    results, calls = _run(simulator=_ThrowingSimulator(), require_real_sim=False)
    r = results[0]
    assert r.mock_simulation is True, "default must fall back to the mock"
    assert calls["score_fn"] == 1, "the mock is scored normally"
    assert r.score == pytest.approx(0.9)


def test_no_simulator_falls_back_to_mock_by_default():
    results, calls = _run(simulator=None, require_real_sim=False)
    r = results[0]
    assert r.mock_simulation is True
    assert calls["score_fn"] == 1
    assert r.score == pytest.approx(0.9)


def test_sim_revert_is_scored_normally_by_default():
    # Default path unchanged: a success=False sim is passed to the scorer as
    # before (no fail-closed), so this is NOT a behavior change off the switch.
    results, calls = _run(simulator=_RevertingSimulator(), require_real_sim=False)
    r = results[0]
    assert r.mock_simulation is False  # a real sim ran, just reverted
    assert calls["score_fn"] == 1
    assert r.score == pytest.approx(0.9)
