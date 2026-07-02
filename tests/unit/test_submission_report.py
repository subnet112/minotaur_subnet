"""Submission feedback report — pure assembly tests.

Covers outcome classification (now derived from the per-order verdict, NOT a
scalar) and the in-flight (None) case. The aggregate-vs-champion scalars were
removed; per-order detail lives solely in the same-pin ``relative`` block (see
test_relative_report.py); the cross-fork ``per_case`` / ``shadow_relative``
surfaces were removed.
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


def _rel(verdict, *, better=0, worse=0, matched=0):
    return {
        "better": better, "worse": worse, "matched": matched, "new": 0,
        "compared": better + worse + matched, "verdict": verdict,
        "per_order": [], "round_id": "round-e1-n1",
    }


def _sub(status="scored", score=0.594, details=None, screening=None, relative=None):
    d = dict(details) if isinstance(details, dict) else details
    if relative is not None and isinstance(d, dict):
        d = {**d, "relative": relative}
    return SimpleNamespace(
        status=SimpleNamespace(value=status),
        benchmark_score=score,
        benchmark_details=d,
        screening=screening or {},
    )


def _report(sub, reason=None):
    return build_submission_report(sub, reason=reason)


# ── in-flight ─────────────────────────────────────────────────────────────────


def test_inflight_returns_none():
    assert _report(_sub("benchmarking", score=None, details=None)) is None
    assert _report(_sub("screening_stage_2", score=None, details=None)) is None


# ── no scalars, ever ──────────────────────────────────────────────────────────


def test_scored_no_relative_has_no_aggregate():
    r = _report(_sub("scored", 0.594, _DETAILS), reason="dethrone_margin_not_met")
    # No stored relative block -> generic "scored", and NO aggregate scalars.
    assert r["outcome"] == "scored"
    assert r["reason"] == "dethrone_margin_not_met"
    assert r["scoring_mode"] == "relative"
    assert "aggregate" not in r
    for legacy in ("your_score", "champion_score", "score_to_beat", "gap", "dethrone_margin"):
        assert legacy not in r
    # The cross-fork per-order surfaces were removed.
    assert "per_case" not in r and "worst_cases" not in r and "coverage" not in r


# ── outcome derived from the per-order verdict ────────────────────────────────


def test_outcome_matched():
    r = _report(_sub("scored", 0.594, _DETAILS, relative=_rel("matched", matched=3)))
    assert r["outcome"] == "matched"


def test_outcome_regressed():
    r = _report(_sub("scored", 0.594, _DETAILS, relative=_rel("worse", worse=1, matched=2)))
    assert r["outcome"] == "regressed"


def test_outcome_beat_champion():
    r = _report(_sub("scored", 0.594, _DETAILS, relative=_rel("dethrone", better=2, matched=1)))
    assert r["outcome"] == "beat_champion"


# ── other outcomes ────────────────────────────────────────────────────────────


def test_adopted():
    r = _report(_sub("adopted", 0.65, _DETAILS))
    assert r["outcome"] == "adopted"


def test_won_overrides_verdict():
    r = build_submission_report(
        _sub("scored", 0.65, _DETAILS, relative=_rel("matched", matched=3)),
        reason="selected as finalist", won=True,
    )
    assert r["outcome"] == "won"


def test_rejected_screening_carries_detail():
    scr = {"stage": "stage_1", "detail": "banned import: os.system"}
    r = _report(_sub("rejected", score=None, details=None, screening=scr))
    assert r["outcome"] == "rejected_screening"
    assert r["screening"] == scr


def test_benchmark_failed_when_no_plans():
    details = {"errors": 3, "plans_generated": 0, "per_intent": [], "scorecard": {}}
    r = _report(_sub("scored", 0.0, details))
    assert r["outcome"] == "benchmark_failed"
