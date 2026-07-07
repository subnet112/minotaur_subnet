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

# Post-ripout benchmark_details: the scalar composite (``avg_score`` /
# ``scorecard.scenario_scores`` / per-intent ``score``) is GONE. Scored-ness is
# carried by ``per_intent`` raw_output rows (EXACT DECIMAL WEI STRING; >0 ⇒
# delivered value), and per-order detail lives solely in the ``relative`` block.
_DETAILS = {
    "total_intents": 3,
    "plans_generated": 3,
    "errors": 0,
    "per_intent": [
        {"intent_id": "WETH_to_USDC", "raw_output": "5027000000",
         "has_plan": True, "error": None, "mock_simulation": False},
        {"intent_id": "USDC_to_WETH", "raw_output": "5010000000",
         "has_plan": True, "error": None, "mock_simulation": False},
        {"intent_id": "BAD", "raw_output": "0",
         "has_plan": False, "error": "no plan", "mock_simulation": False},
    ],
}


def _rel(verdict, *, better=0, worse=0, matched=0):
    return {
        "better": better, "worse": worse, "matched": matched, "new": 0,
        "compared": better + worse + matched, "verdict": verdict,
        "per_order": [], "round_id": "round-e1-n1",
    }


def _sub(status="scored", details=None, screening=None, relative=None):
    # ``benchmark_score`` was REMOVED from Submission: a scored submission is
    # signalled by ``benchmark_details`` (its per_intent raw_output rows), not a
    # scalar. The fake carries no scalar score.
    d = dict(details) if isinstance(details, dict) else details
    if relative is not None and isinstance(d, dict):
        d = {**d, "relative": relative}
    return SimpleNamespace(
        status=SimpleNamespace(value=status),
        benchmark_details=d,
        screening=screening or {},
    )


def _report(sub, reason=None):
    return build_submission_report(sub, reason=reason)


# ── in-flight ─────────────────────────────────────────────────────────────────


def test_inflight_returns_none():
    assert _report(_sub("benchmarking", details=None)) is None
    assert _report(_sub("screening_stage_2", details=None)) is None


# ── no scalars, ever ──────────────────────────────────────────────────────────


def test_scored_no_relative_has_no_aggregate():
    r = _report(_sub("scored", _DETAILS), reason="dethrone_margin_not_met")
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
    r = _report(_sub("scored", _DETAILS, relative=_rel("matched", matched=3)))
    assert r["outcome"] == "matched"


def test_outcome_regressed():
    r = _report(_sub("scored", _DETAILS, relative=_rel("worse", worse=1, matched=2)))
    assert r["outcome"] == "regressed"


def test_outcome_beat_champion():
    r = _report(_sub("scored", _DETAILS, relative=_rel("dethrone", better=2, matched=1)))
    assert r["outcome"] == "beat_champion"


# ── other outcomes ────────────────────────────────────────────────────────────


def test_adopted():
    r = _report(_sub("adopted", _DETAILS))
    assert r["outcome"] == "adopted"


def test_won_overrides_verdict():
    r = build_submission_report(
        _sub("scored", _DETAILS, relative=_rel("matched", matched=3)),
        reason="selected as finalist", won=True,
    )
    assert r["outcome"] == "won"


def test_rejected_screening_carries_detail():
    scr = {"stage": "stage_1", "detail": "banned import: os.system"}
    r = _report(_sub("rejected", details=None, screening=scr))
    assert r["outcome"] == "rejected_screening"
    assert r["screening"] == scr


def test_benchmark_failed_when_no_plans():
    details = {"errors": 3, "plans_generated": 0, "per_intent": []}
    r = _report(_sub("scored", details))
    assert r["outcome"] == "benchmark_failed"


# ── factorization transparency (armed rule must be visible to miners) ────────


class _FactorSub:
    status = type("S", (), {"value": "scored"})()
    submission_id = "sub_factor"
    max_region_nodes = 4109
    benchmark_details = {
        "relative": {
            "better": 0, "worse": 0, "matched": 5, "new": 0, "compared": 5,
            "verdict": "matched", "adopt_via": None, "per_order": [],
            "factorization": {
                "candidate_nodes": 4109, "champion_nodes": 4109,
                "factor_delta": 0, "factor_margin": 100, "armed": True,
            },
        }
    }


def test_factorization_block_reports_armed_state_and_baseline():
    from minotaur_subnet.api.routes.submissions.report import build_submission_report
    from minotaur_subnet.epoch import relative_scoring as _rs
    from minotaur_subnet.harness import screening as _sc

    rep = build_submission_report(_FactorSub(), reason=None)
    fz = rep["factorization"]
    # Armed state is COMPUTED from live constants — never hardcoded.
    expected_armed = _sc.MAX_REGION_NODES is not None or _rs.FACTOR_MARGIN is not None
    assert fz["armed"] is expected_armed
    assert fz["observe_only"] is (not expected_armed)
    assert fz["floor_cap"] == _sc.MAX_REGION_NODES
    assert fz["factor_margin"] == _rs.FACTOR_MARGIN
    # Same-pin champion baseline merged from the stored relative block.
    assert fz["champion_nodes"] == 4109
    assert fz["factor_delta"] == 0


def test_matched_hint_names_the_factor_target():
    from minotaur_subnet.api.routes.submissions.report import (
        build_submission_report,
        render_report_md,
    )

    rep = build_submission_report(_FactorSub(), reason=None)
    md = render_report_md(rep, submission_id="sub_factor")
    # The tied miner is told the SECOND way to win, with the exact target
    # (champion 4109 - margin 100 = 4009), like the ❌ rows name orders.
    assert "OR ship better-factored code" in md
    assert "≤ 4009" in md


def test_factor_win_line_renders():
    from minotaur_subnet.api.routes.submissions.report import (
        build_submission_report,
        render_report_md,
    )

    class _Winner(_FactorSub):
        status = type("S", (), {"value": "adopted"})()
        benchmark_details = {
            "relative": {
                "better": 0, "worse": 0, "matched": 5, "new": 0, "compared": 5,
                "verdict": "dethrone", "adopt_via": "factorization", "per_order": [],
                "factorization": {
                    "candidate_nodes": 1212, "champion_nodes": 4109,
                    "factor_delta": 2897, "factor_margin": 100, "armed": True,
                },
            }
        }

    md = render_report_md(build_submission_report(_Winner(), reason=None))
    assert "Won on factorization" in md
    assert "1212" in md and "4109" in md and "2897" in md


# ── GAS-PAR transparency (C2 — ships DISARMED; rule state must be honest) ────


def test_gas_block_reports_disarmed_state():
    from minotaur_subnet.epoch import relative_scoring as _rs

    rep = _report(_sub("scored", _DETAILS))
    gz = rep["gas"]
    # Armed state is COMPUTED from the live constant — never hardcoded. On
    # this branch the clause SHIPS DISARMED (GAS_MARGIN_BPS is None).
    expected_armed = _rs.GAS_MARGIN_BPS is not None
    assert gz["armed"] is expected_armed
    assert gz["observe_only"] is (not expected_armed)
    assert gz["gas_margin_bps"] == _rs.GAS_MARGIN_BPS
    assert gz["basis"] == _rs.GAS_BASIS
    # No stored same-pin totals -> none surfaced.
    assert "champ_total" not in gz and "chal_total" not in gz


def test_gas_block_armed_carries_stored_totals(monkeypatch):
    from minotaur_subnet.epoch import relative_scoring as _rs

    monkeypatch.setattr(_rs, "GAS_MARGIN_BPS", 250)
    rel = _rel("matched", matched=5)
    rel["gas"] = {
        "champ_total": 200_000, "chal_total": 198_000,
        "measured_full": True, "unmeasured": 0, "order_worse": 0,
        "gas_margin_bps": 250, "armed": True, "basis": _rs.GAS_BASIS,
    }
    rep = _report(_sub("scored", _DETAILS, relative=rel))
    gz = rep["gas"]
    assert gz["armed"] is True
    assert gz["observe_only"] is False
    assert gz["gas_margin_bps"] == 250
    # Same-pin totals merged from the stored relative block.
    assert gz["champ_total"] == 200_000
    assert gz["chal_total"] == 198_000
    assert gz["measured_full"] is True


def test_gas_win_line_renders():
    from minotaur_subnet.api.routes.submissions.report import render_report_md

    rel = _rel("dethrone", matched=5)
    rel["adopt_via"] = "gas"
    rel["gas"] = {
        "champ_total": 200_000, "chal_total": 180_000,
        "measured_full": True, "unmeasured": 0, "order_worse": 0,
        "gas_margin_bps": 250, "armed": True,
    }
    rep = _report(_sub("adopted", _DETAILS, relative=rel))
    md = render_report_md(rep)
    assert "Won on gas" in md
    assert "180000" in md and "200000" in md and "250" in md


def test_matched_hint_names_the_gas_target():
    from minotaur_subnet.api.routes.submissions.report import render_report_md

    rel = _rel("matched", matched=5)
    rel["gas"] = {
        "champ_total": 200_000, "chal_total": 198_000,
        "measured_full": True, "unmeasured": 0, "order_worse": 0,
        "gas_margin_bps": 250, "armed": True,
    }
    rep = _report(_sub("scored", _DETAILS, relative=rel))
    md = render_report_md(rep, submission_id="sub_gas")
    # The tied miner is told the gas way to win, with the exact target
    # (200000 * (10000 - 250) // 10000 = 195000), like the factor hint.
    assert "OR deliver the same outputs on less gas" in md
    assert "below 195000" in md


def test_matched_hint_omits_gas_target_when_disarmed():
    from minotaur_subnet.api.routes.submissions.report import render_report_md

    # Stored block from a DISARMED round carries no gas sub-dict at all —
    # the hint must not advertise a rule that cannot fire.
    rep = _report(_sub("scored", _DETAILS, relative=_rel("matched", matched=5)))
    md = render_report_md(rep, submission_id="sub_gas")
    assert "less gas" not in md
