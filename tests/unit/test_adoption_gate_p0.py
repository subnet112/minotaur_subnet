"""P0 adoption-gate fixes (scoring-mechanism-design, phase P0).

Locks in:
- BenchmarkWorker._build_scorecard keys app_scores by the BARE app_id
  (not "<app_id>:<scenario>") — the per-app grouping the scorecard still exposes.
- EpochManager._should_adopt routes through the AUTHORITATIVE relative per-order
  rule: it REJECTS a challenger that drops/regresses an order the champion served,
  and ADOPTS one that strictly wins an order with no regression.
- The stale-bar guard still abstains when the incumbent bar could not be refreshed.
"""

import types
from unittest.mock import MagicMock

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


def _per_intent(pairs):
    """Per-order rows with RAW delivered output (raw_output, decimal str)."""
    return [{"intent_id": iid, "raw_output": sc} for iid, sc in pairs]


def _mgr(champ_per_intent) -> EpochManager:
    """Bare manager whose stored champion submission carries ``champ_per_intent``
    (the freshly re-benched per-order RAW outputs the relative rule joins against)."""
    mgr = EpochManager.__new__(EpochManager)
    mgr._champion = types.SimpleNamespace(submission_id="champ")
    mgr._incumbent_refresh_failed = False
    champ_sub = types.SimpleNamespace(
        submission_id="champ",
        benchmark_details={"per_intent": champ_per_intent},
    )
    sub_store = MagicMock()
    sub_store.get.return_value = champ_sub
    mgr._sub_store = sub_store
    # Observe-only would-be vote is exercised elsewhere; no-op it here.
    mgr._record_would_be_vote = lambda challenger: None
    return mgr


def _challenger(chal_per_intent):
    return types.SimpleNamespace(
        submission_id="chal",
        benchmark_details={"per_intent": chal_per_intent},
    )


def test_should_adopt_rejects_dropping_a_champion_covered_order():
    # Champion delivered on o2; the challenger drops it (0) -> regression -> REJECT,
    # even though it strictly wins o1.
    mgr = _mgr(_per_intent([("o1", "100"), ("o2", "200")]))
    chal = _challenger(_per_intent([("o1", "150"), ("o2", "0")]))
    assert mgr._should_adopt(chal) is False


def test_should_adopt_accepts_strict_per_order_improvement():
    # Challenger delivers strictly more on every order -> ADOPT.
    mgr = _mgr(_per_intent([("o1", "100"), ("o2", "100")]))
    chal = _challenger(_per_intent([("o1", "120"), ("o2", "130")]))
    assert mgr._should_adopt(chal) is True


def test_should_adopt_rejects_when_only_matched():
    # Challenger ties the champion on every order (within the noise band) -> no win
    # -> not adopted (the relative rule requires a strict per-order improvement).
    mgr = _mgr(_per_intent([("o1", "1000")]))
    chal = _challenger(_per_intent([("o1", "1000")]))
    assert mgr._should_adopt(chal) is False


def test_abstains_when_incumbent_bar_is_stale():
    # Stale-bar guard (#242): an incumbent exists but could NOT be freshly
    # re-benchmarked this round (_refresh_incumbent_score failed) -> ABSTAIN, even
    # for a clear per-order win, mirroring the follower's conservative REJECT so the
    # leader and fleet never diverge on a stale champion bar.
    mgr = _mgr(_per_intent([("o1", "100")]))
    chal = _challenger(_per_intent([("o1", "200")]))  # a clear per-order win

    mgr._incumbent_refresh_failed = False
    assert mgr._should_adopt(chal) is True   # fresh bar -> adopts

    mgr._incumbent_refresh_failed = True
    assert mgr._should_adopt(chal) is False  # stale bar -> abstain
