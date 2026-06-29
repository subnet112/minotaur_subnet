"""Submission feedback report — pure assembly tests.

Covers outcome classification, the aggregate-vs-champion math, and the
in-flight (None) case. Per-order detail now lives solely in the same-pin
``relative`` block (see test_relative_report.py); the cross-fork ``per_case`` /
``shadow_relative`` surfaces were removed.
"""

from __future__ import annotations

from types import SimpleNamespace

from minotaur_subnet.api.routes.submissions.report import build_submission_report

_DETAILS = {
    "total_intents": 3,
    "plans_generated": 3,
    "errors": 0,
    "avg_score": 0.594,
    "scorecard": {
        "scenario_scores": {"WETH_to_USDC": 0.65, "USDC_to_WETH": 0.55, "BAD": 0.0}
    },
    "per_intent": [
        {"intent_id": "WETH_to_USDC", "score": 0.65, "on_chain_score": 5027,
         "has_plan": True, "error": None, "mock_simulation": False},
        {"intent_id": "USDC_to_WETH", "score": 0.55, "on_chain_score": 5010,
         "has_plan": True, "error": None, "mock_simulation": False},
        {"intent_id": "BAD", "score": 0.0, "on_chain_score": None,
         "has_plan": False, "error": "no plan", "mock_simulation": False},
    ],
}


def _sub(status="scored", score=0.594, details=None, screening=None):
    return SimpleNamespace(
        status=SimpleNamespace(value=status),
        benchmark_score=score,
        benchmark_details=details,
        screening=screening or {},
    )


def _report(sub, champion_score=0.62, threshold=0.5, margin=0.005, reason=None):
    return build_submission_report(
        sub, champion_score=champion_score, threshold=threshold,
        dethrone_margin=margin, reason=reason,
    )


# ── in-flight ─────────────────────────────────────────────────────────────────


def test_inflight_returns_none():
    assert _report(_sub("benchmarking", score=None, details=None)) is None
    assert _report(_sub("screening_stage_2", score=None, details=None)) is None


# ── scored, not adopted ───────────────────────────────────────────────────────


def test_scored_not_adopted_aggregate():
    r = _report(_sub("scored", 0.594, _DETAILS), reason="dethrone_margin_not_met")
    assert r["outcome"] == "scored_not_adopted"
    assert r["reason"] == "dethrone_margin_not_met"
    agg = r["aggregate"]
    assert agg["your_score"] == 0.594
    assert agg["champion_score"] == 0.62
    assert agg["score_to_beat"] == round(0.62 * 1.005, 6)   # 0.6231
    assert agg["gap"] == round(0.594 - 0.62 * 1.005, 6)     # negative
    assert agg["gap"] < 0
    # The cross-fork per-order surfaces were removed; per-order detail is the
    # same-pin ``relative`` block alone (absent here — no stored block).
    assert "per_case" not in r
    assert "worst_cases" not in r
    assert "coverage" not in r
    assert r["scoring_mode"] == "relative"


# ── other outcomes ────────────────────────────────────────────────────────────


def test_adopted():
    r = _report(_sub("adopted", 0.65, _DETAILS))
    assert r["outcome"] == "adopted"


def test_rejected_threshold():
    r = _report(_sub("scored", 0.40, _DETAILS), threshold=0.5)
    assert r["outcome"] == "rejected_threshold"


def test_rejected_screening_carries_detail():
    scr = {"stage": "stage_1", "detail": "banned import: os.system"}
    r = _report(_sub("rejected", score=None, details=None, screening=scr))
    assert r["outcome"] == "rejected_screening"
    assert r["screening"] == scr


def test_benchmark_failed_when_no_plans():
    details = {"errors": 3, "plans_generated": 0, "per_intent": [], "scorecard": {}}
    r = _report(_sub("scored", 0.0, details))
    assert r["outcome"] == "benchmark_failed"


def test_no_champion_means_no_score_to_beat():
    r = _report(_sub("scored", 0.594, _DETAILS), champion_score=None)
    assert r["aggregate"]["score_to_beat"] is None
    assert r["aggregate"]["gap"] is None
    assert r["outcome"] == "scored"   # can't be "not adopted" without a bar
