"""Port A — on-chain co-ranked adoption in production (ADOPT_RULE=p2oc).

Covers the three-step plumb (BenchmarkResult.on_chain_score -> scorecard.app_onchain
-> _should_adopt_onchain) and the gated dispatch. Default (ADOPT_RULE unset/current)
must leave _should_adopt byte-for-byte unchanged.
"""
import asyncio
import types

from minotaur_subnet.epoch.manager import EpochManager
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


# ── plumb step 3: _should_adopt_onchain + the gated dispatch ──

def _mgr(margin=0.005, champ_id="champ"):
    mgr = EpochManager.__new__(EpochManager)
    mgr._champion = types.SimpleNamespace(submission_id=champ_id, benchmark_score=0.6)
    mgr._dethrone_margin = margin
    return mgr


def _chal(score=0.6):
    return types.SimpleNamespace(submission_id="chal", benchmark_score=score)


def _cards(mgr, champ, chal):
    mgr._get_incumbent_scorecard = lambda: champ
    mgr._get_scorecard = lambda sub: chal


def _card(js, oc):
    return {"app_scores": js, "app_onchain": oc}


def test_dispatch_default_is_current_rule(monkeypatch):
    monkeypatch.delenv("ADOPT_RULE", raising=False)
    mgr = _mgr()
    mgr._should_adopt_onchain = lambda c: (_ for _ in ()).throw(AssertionError("p2oc must not run"))
    _cards(mgr, _card({"A": 0.6}, {"A": [5000]}), _card({"A": 0.7}, {"A": [5000]}))
    # default path = JS logic; the p2oc method must never be entered
    assert mgr._should_adopt(_chal(0.7)) is True


def test_dispatch_routes_to_p2oc_when_enabled(monkeypatch):
    # ADOPT_RULE is now a fleet-uniform CODE constant (adopt_rule.ADOPT_RULE),
    # imported into the manager namespace — not a per-validator env. Flipping the
    # rule is a code change, so the test patches the constant the dispatch reads.
    monkeypatch.setattr("minotaur_subnet.epoch.manager.ADOPT_RULE", "p2oc")
    mgr = _mgr()
    mgr._should_adopt_onchain = lambda c: "ROUTED"
    _cards(mgr, _card({"A": 0.6}, {"A": [5000]}), _card({"A": 0.7}, {"A": [5000]}))
    assert mgr._should_adopt(_chal(0.7)) == "ROUTED"


def test_p2oc_adopts_more_output_even_with_lower_js():
    # The case the JS path REJECTS: challenger delivers more output (+120 BPS) but
    # lower JS (more gas). On-chain ranking adopts it.
    mgr = _mgr()
    _cards(mgr, _card({"A": 0.54}, {"A": [5000]}), _card({"A": 0.53}, {"A": [5120]}))
    assert mgr._should_adopt_onchain(_chal(0.53)) is True


def test_p2oc_rejects_less_output():
    mgr = _mgr()
    _cards(mgr, _card({"A": 0.54}, {"A": [5100]}), _card({"A": 0.55}, {"A": [5020]}))
    assert mgr._should_adopt_onchain(_chal(0.55)) is False


def test_p2oc_rejects_below_margin():
    mgr = _mgr()  # +30 BPS < 50 BPS margin
    _cards(mgr, _card({"A": 0.5}, {"A": [5000]}), _card({"A": 0.5}, {"A": [5030]}))
    assert mgr._should_adopt_onchain(_chal()) is False


def test_p2oc_rejects_dropped_app():
    mgr = _mgr()
    _cards(mgr, _card({"A": 0.5, "B": 0.5}, {"A": [5000], "B": [5000]}), _card({"A": 0.9}, {"A": [6000]}))
    assert mgr._should_adopt_onchain(_chal(0.9)) is False


def test_p2oc_rejects_missing_onchain_on_covered_app():
    mgr = _mgr()
    _cards(mgr, _card({"A": 0.5}, {"A": [5000]}), _card({"A": 0.6}, {"A": [None]}))
    assert mgr._should_adopt_onchain(_chal()) is False


def test_p2oc_floor_veto(monkeypatch):
    monkeypatch.setenv("ONCHAIN_FLOOR_BPS", "5000")
    mgr = _mgr()
    _cards(mgr, _card({"A": 0.5}, {"A": [5000]}), _card({"A": 0.9}, {"A": [4000]}))
    assert mgr._should_adopt_onchain(_chal(0.9)) is False


def test_p2oc_js_catastrophic_regression_veto():
    mgr = _mgr()  # huge on-chain gain but JS collapses >10%
    _cards(mgr, _card({"A": 0.60}, {"A": [5000]}), _card({"A": 0.50}, {"A": [5500]}))
    assert mgr._should_adopt_onchain(_chal(0.50)) is False


def test_p2oc_adopts_genesis_when_no_champion():
    mgr = _mgr(champ_id="")  # no champion yet
    assert mgr._should_adopt_onchain(_chal()) is True


# ── shadow-determinism (observe-only) ────────────────────────────────────────

def _shadow_mgr():
    mgr = _mgr()
    _cards(mgr, _card({"A": 0.6}, {"A": [5000]}), _card({"A": 0.7}, {"A": [5100]}))
    return mgr


def test_shadow_off_by_default_not_invoked(monkeypatch):
    monkeypatch.delenv("SHADOW_DETERMINISM", raising=False)
    mgr = _shadow_mgr()
    mgr._log_shadow_determinism = lambda *a: (_ for _ in ()).throw(AssertionError("shadow ran"))
    assert mgr._should_adopt(_chal(0.7)) is True  # current rule adopts; shadow NOT invoked


def test_shadow_on_logs_without_changing_decision(monkeypatch):
    monkeypatch.setenv("SHADOW_DETERMINISM", "1")
    calls = []
    mgr = _shadow_mgr()
    mgr._log_shadow_determinism = lambda *a: calls.append(a)
    assert mgr._should_adopt(_chal(0.7)) is True  # live decision UNCHANGED
    assert len(calls) == 1                          # but the shadow WAS invoked


def test_shadow_log_never_raises_on_empty_cards():
    mgr = _mgr()
    mgr._log_shadow_determinism(_chal(), {}, {})  # observe-only must not raise
