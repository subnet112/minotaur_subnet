"""On-chain score plumbing into the benchmark scorecard.

Covers the live plumb of the unfakeable on-chain ``scoreIntent``:
``SimulationResult.on_chain_score`` -> ``BenchmarkResult.on_chain_score`` ->
per-app ``scorecard.app_onchain``. NOTE: the legacy on-chain HARD-VETO adoption
gate that consumed this (``adopt_rule._evaluate_onchain_gate``) was removed with
the relative cutover; the scorecard field is still built + round-tripped (and used
for determinism logging), which is what these tests pin.
"""
import asyncio

from minotaur_subnet.harness.benchmark_worker import BenchmarkScorecard, BenchmarkWorker
from minotaur_subnet.harness.orchestrator import (
    BenchmarkConfig,
    BenchmarkResult,
    run_benchmark,
)
from minotaur_subnet.shared.types import ExecutionPlan, ScoreResult, SimulationResult


# ── plumb step 1: orchestrator captures sim.on_chain_score into BenchmarkResult ──

class _FakeSession:
    def __init__(self, plan):
        self._plan = plan

    async def initialize(self, c): return None
    async def metadata(self): return {}
    async def on_benchmark_start(self, n): return None
    async def generate_plan(self, i, s, sn): return self._plan
    async def on_benchmark_end(self, summary): return None


class _ScoringSimulator:
    async def simulate(self, plan, **kwargs):
        return SimulationResult(success=True, gas_used=100_000, on_chain_score=7200)


def test_run_benchmark_captures_on_chain_score():
    from minotaur_subnet.harness.test_harness import make_intent, make_snapshot, make_state
    intent, state, snapshot = make_intent(), make_state(), make_snapshot()
    plan = ExecutionPlan(intent_id=intent.app_id, interactions=[], deadline=0, nonce=0)

    async def score_fn(app_id, plan, sim, st):
        return ScoreResult(score=0.5)

    async def _go():
        return await run_benchmark(
            _FakeSession(plan), [(intent, state, snapshot)],
            config=BenchmarkConfig(chain_ids=[state.chain_id]),
            score_fn=score_fn, simulator=_ScoringSimulator())

    results = asyncio.run(_go())
    assert results[0].on_chain_score == 7200


# ── plumb step 2: _build_scorecard aggregates per-app on-chain + round-trips ──

def _br_oc(intent_id, score, oc):
    return BenchmarkResult(intent_id=intent_id, score=score, plan=object(), error=None, on_chain_score=oc)


def test_build_scorecard_aggregates_app_onchain():
    worker = BenchmarkWorker.__new__(BenchmarkWorker)
    card = worker._build_scorecard([
        _br_oc("app_A:s1", 0.8, 6800), _br_oc("app_A:s2", 0.6, None), _br_oc("app_B:s3", 0.9, 7100)])
    assert card.app_onchain["app_A"] == [6800, None]
    assert card.app_onchain["app_B"] == [7100]


def test_scorecard_app_onchain_round_trips():
    c = BenchmarkScorecard(app_onchain={"app_A": [6800, None, 7100]})
    assert BenchmarkScorecard.from_dict(c.to_dict()).app_onchain == {"app_A": [6800, None, 7100]}
