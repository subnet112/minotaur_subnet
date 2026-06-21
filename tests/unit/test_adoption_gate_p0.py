"""P0 adoption-gate fixes (scoring-mechanism-design, phase P0).

Locks in:
- BenchmarkWorker._build_scorecard keys app_scores by the BARE app_id
  (not "<app_id>:<scenario>"), so the adoption gate compares true per-app quality.
- EpochManager._should_adopt rejects a challenger that DROPS a champion-covered app.
- The dethrone-margin baseline is the champion's actual score, not max(score, floor)
  — a degraded (sub-floor) champion is no longer over-protected.
"""

import types

from minotaur_subnet.epoch.manager import EpochManager
from minotaur_subnet.harness.benchmark_worker import BenchmarkWorker
from minotaur_subnet.harness.orchestrator import BenchmarkResult


def _br(intent_id: str, score: float) -> BenchmarkResult:
    # A "passing" scenario: non-None plan, no error, positive score.
    return BenchmarkResult(intent_id=intent_id, score=score, plan=object(), error=None)


def test_build_scorecard_keys_app_scores_by_bare_app_id():
    worker = BenchmarkWorker.__new__(BenchmarkWorker)  # no ctor deps needed
    results = [
        _br("app_A:WETH_to_USDC", 0.8),
        _br("app_A:USDC_to_WETH", 0.6),
        _br("app_A:hist:ord_9", 0.4),   # scenario name itself contains ':'
        _br("app_B:WBTC_to_WETH", 0.9),
    ]
    card = worker._build_scorecard(results)

    # app_scores grouped by bare app_id, NOT per scenario.
    assert set(card.app_scores) == {"app_A", "app_B"}
    assert card.app_scores["app_A"] == (0.8 + 0.6 + 0.4) / 3
    assert card.app_scores["app_B"] == 0.9
    # Per-scenario breakdown keeps full-label granularity.
    assert card.scenario_scores["app_A:hist:ord_9"] == 0.4


def _mgr(champion_score: float, dethrone_margin: float = 0.005) -> EpochManager:
    mgr = EpochManager.__new__(EpochManager)
    mgr._champion = types.SimpleNamespace(submission_id="champ", benchmark_score=champion_score)
    mgr._dethrone_margin = dethrone_margin
    return mgr


def _challenger(score: float):
    return types.SimpleNamespace(submission_id="chal", benchmark_score=score)


def test_should_adopt_rejects_dropping_a_champion_covered_app():
    mgr = _mgr(champion_score=0.6)
    mgr._get_incumbent_scorecard = lambda: {"app_scores": {"app_A": 0.6, "app_B": 0.6}}
    mgr._get_scorecard = lambda sub: {"app_scores": {"app_A": 0.9}}  # app_B dropped

    # Globally higher (0.9 > 0.6) but drops app_B → must NOT adopt.
    assert mgr._should_adopt(_challenger(0.9)) is False


def test_should_adopt_accepts_strict_per_app_improvement():
    mgr = _mgr(champion_score=0.6)
    mgr._get_incumbent_scorecard = lambda: {"app_scores": {"app_A": 0.6, "app_B": 0.6}}
    mgr._get_scorecard = lambda sub: {"app_scores": {"app_A": 0.7, "app_B": 0.7}}

    assert mgr._should_adopt(_challenger(0.7)) is True


def test_dethrone_baseline_is_not_floored_for_a_degraded_champion():
    # Champion degraded to 0.3 (below MIN_CHAMPION_SCORE=0.5). A 0.5 challenger
    # that improves the app should win. With the old max(score, 0.5) floor the
    # required bar was 0.5*1.005 and the 0.5 challenger was wrongly rejected.
    mgr = _mgr(champion_score=0.3)
    mgr._get_incumbent_scorecard = lambda: {"app_scores": {"app_X": 0.3}}
    mgr._get_scorecard = lambda sub: {"app_scores": {"app_X": 0.5}}

    assert mgr._should_adopt(_challenger(0.5)) is True


def test_abstains_when_incumbent_bar_is_stale():
    # Stale-bar guard (#242): an incumbent exists but could NOT be freshly
    # re-benchmarked this round (_refresh_incumbent_score failed) -> ABSTAIN, even
    # for a clear improvement, mirroring the follower's conservative REJECT so the
    # leader and fleet never diverge on a stale champion bar.
    mgr = _mgr(champion_score=0.6)
    mgr._get_incumbent_scorecard = lambda: {"app_scores": {"app_A": 0.6, "app_B": 0.6}}
    mgr._get_scorecard = lambda sub: {"app_scores": {"app_A": 0.7, "app_B": 0.7}}

    mgr._incumbent_refresh_failed = False
    assert mgr._should_adopt(_challenger(0.7)) is True  # fresh bar -> adopts

    mgr._incumbent_refresh_failed = True
    assert mgr._should_adopt(_challenger(0.7)) is False  # stale bar -> abstain
