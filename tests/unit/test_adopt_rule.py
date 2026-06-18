"""Unit tests for the pure champion-adoption rule (epoch/adopt_rule.py).

evaluate_adoption is the per-validator adoption decision body extracted from
EpochManager._should_adopt so the leader and followers make the identical
decision. These tests cover both the default "current" JS rule and the
ADOPT_RULE=p2oc on-chain-surplus rule, reading the same env knobs.
"""

from __future__ import annotations

import pytest

from minotaur_subnet.epoch.adopt_rule import (
    _app_onchain_mean,
    _onchain_pass,
    evaluate_adoption,
)


def _card(js: dict, oc: dict | None = None) -> dict:
    card: dict = {"app_scores": js}
    if oc is not None:
        card["app_onchain"] = oc
    return card


# ── default "current" JS rule ────────────────────────────────────────────────


def test_no_absolute_global_floor():
    # The absolute MIN_CHAMPION_SCORE floor was purged: a challenger BELOW any
    # absolute number still ADOPTS when it beats the champion by the dethrone
    # margin (and meets the per-app floor + non-regression). The global JS score
    # is relative to the champion reference, so only the margin governs.
    adopt, reason = evaluate_adoption(
        challenger_score=0.40,  # well below the old 0.5 floor
        champion_score=0.30,    # but beats the champion by >> the margin
        challenger_scorecard=_card({"A": 0.40}),
        champion_scorecard=_card({"A": 0.30}),
        dethrone_margin=0.05,
        has_champion=True,
    )
    assert adopt is True


def test_per_app_min_reject(monkeypatch):
    # Globally above floor, but one app is below PER_APP_MIN_SCORE (default 0.3).
    adopt, reason = evaluate_adoption(
        challenger_score=0.9,
        champion_score=0.6,
        challenger_scorecard=_card({"A": 0.9, "B": 0.2}),
        champion_scorecard=_card({"A": 0.6, "B": 0.6}),
        dethrone_margin=0.005,
        has_champion=True,
    )
    assert adopt is False
    assert "per-app minimum" in reason


def test_app_drop_reject():
    # Higher global score but drops a champion-covered app -> hard regression.
    adopt, reason = evaluate_adoption(
        challenger_score=0.9,
        champion_score=0.6,
        challenger_scorecard=_card({"A": 0.9}),  # app_B dropped
        champion_scorecard=_card({"A": 0.6, "B": 0.6}),
        dethrone_margin=0.005,
        has_champion=True,
    )
    assert adopt is False
    assert "drops app B" in reason


def test_regression_reject():
    # app_A regresses 0.6 -> 0.4 (>10% drop) even though global score rises.
    adopt, reason = evaluate_adoption(
        challenger_score=0.7,
        champion_score=0.6,
        challenger_scorecard=_card({"A": 0.4, "B": 0.9}),
        champion_scorecard=_card({"A": 0.6, "B": 0.6}),
        dethrone_margin=0.005,
        has_champion=True,
    )
    assert adopt is False
    assert "regresses on A" in reason


def test_margin_reject():
    # Per-app improvement, but global challenger only ties the incumbent -> reject.
    adopt, reason = evaluate_adoption(
        challenger_score=0.6,
        champion_score=0.6,
        challenger_scorecard=_card({"A": 0.6}),
        champion_scorecard=_card({"A": 0.6}),
        dethrone_margin=0.005,
        has_champion=True,
    )
    assert adopt is False
    assert "not better than incumbent" in reason


def test_margin_reject_below_required():
    # Strictly higher but does not clear the dethrone margin (0.6 * 1.5 = 0.9).
    adopt, reason = evaluate_adoption(
        challenger_score=0.7,
        champion_score=0.6,
        challenger_scorecard=_card({"A": 0.7}),
        champion_scorecard=_card({"A": 0.6}),
        dethrone_margin=0.5,
        has_champion=True,
    )
    assert adopt is False
    assert "dethrone margin" in reason


def test_clean_adopt():
    adopt, reason = evaluate_adoption(
        challenger_score=0.7,
        champion_score=0.6,
        challenger_scorecard=_card({"A": 0.7, "B": 0.7}),
        champion_scorecard=_card({"A": 0.6, "B": 0.6}),
        dethrone_margin=0.005,
        has_champion=True,
    )
    assert adopt is True
    assert "beats incumbent" in reason


def test_no_champion_adopt():
    # No incumbent: adopt as long as global + per-app minimums pass.
    adopt, reason = evaluate_adoption(
        challenger_score=0.5,
        champion_score=0.0,
        challenger_scorecard=_card({"A": 0.5}),
        champion_scorecard=None,
        dethrone_margin=0.005,
        has_champion=False,
    )
    assert adopt is True
    assert "no current champion" in reason


def test_degraded_champion_baseline_not_floored():
    # A degraded (sub-floor) champion at 0.3 must not be over-protected: a 0.5
    # challenger that improves the app wins (baseline is champion's actual score).
    adopt, reason = evaluate_adoption(
        challenger_score=0.5,
        champion_score=0.3,
        challenger_scorecard=_card({"A": 0.5}),
        champion_scorecard=_card({"A": 0.3}),
        dethrone_margin=0.005,
        has_champion=True,
    )
    assert adopt is True


# ── ADOPT_RULE=p2oc on-chain-surplus rule ────────────────────────────────────


def test_p2oc_adopt_when_surplus_above_margin(monkeypatch):
    monkeypatch.setenv("ADOPT_RULE", "p2oc")
    # Challenger delivers +120 BPS on-chain (1.2% / 10000 = 0.012 > 0.005 margin)
    # even though its JS score is lower (more gas). On-chain ranking adopts it.
    adopt, reason = evaluate_adoption(
        challenger_score=0.53,
        champion_score=0.54,
        challenger_scorecard=_card({"A": 0.53}, {"A": [5120]}),
        champion_scorecard=_card({"A": 0.54}, {"A": [5000]}),
        dethrone_margin=0.005,
        has_champion=True,
    )
    assert adopt is True
    assert "ADOPT" in reason


def test_p2oc_reject_when_surplus_at_or_below_margin(monkeypatch):
    monkeypatch.setenv("ADOPT_RULE", "p2oc")
    # +30 BPS surplus (0.003) <= 0.005 margin -> reject.
    adopt, reason = evaluate_adoption(
        challenger_score=0.55,
        champion_score=0.55,
        challenger_scorecard=_card({"A": 0.55}, {"A": [5030]}),
        champion_scorecard=_card({"A": 0.55}, {"A": [5000]}),
        dethrone_margin=0.005,
        has_champion=True,
    )
    assert adopt is False
    assert "surplus" in reason and "margin" in reason


def test_p2oc_reject_on_floor_fail(monkeypatch):
    monkeypatch.setenv("ADOPT_RULE", "p2oc")
    monkeypatch.setenv("ONCHAIN_FLOOR_BPS", "5000")
    # Challenger's on-chain score (4000) is below the admission floor (5000).
    adopt, reason = evaluate_adoption(
        challenger_score=0.9,
        champion_score=0.5,
        challenger_scorecard=_card({"A": 0.9}, {"A": [4000]}),
        champion_scorecard=_card({"A": 0.5}, {"A": [5000]}),
        dethrone_margin=0.005,
        has_champion=True,
    )
    assert adopt is False
    assert "on-chain floor fail" in reason


def test_p2oc_no_absolute_global_floor(monkeypatch):
    # The global-score floor was purged, so a sub-floor challenger reaches the
    # p2oc dispatch and is judged by p2oc's own rule (here it rejects on JS
    # regression) — NOT blocked by an absolute floor.
    monkeypatch.setenv("ADOPT_RULE", "p2oc")
    adopt, reason = evaluate_adoption(
        challenger_score=0.4,
        champion_score=0.5,
        challenger_scorecard=_card({"A": 0.4}, {"A": [9000]}),
        champion_scorecard=_card({"A": 0.5}, {"A": [5000]}),
        dethrone_margin=0.05,
        has_champion=True,
    )
    assert adopt is False
    assert "p2oc" in reason


def test_p2oc_genesis_adopt(monkeypatch):
    monkeypatch.setenv("ADOPT_RULE", "p2oc")
    adopt, reason = evaluate_adoption(
        challenger_score=0.6,
        champion_score=0.0,
        challenger_scorecard=_card({"A": 0.6}, {"A": [5000]}),
        champion_scorecard=None,
        dethrone_margin=0.005,
        has_champion=False,
    )
    assert adopt is True
    assert "genesis" in reason


def test_p2oc_genesis_rejects_below_per_app_floor(monkeypatch):
    # Purging the absolute global floor must NOT open the p2oc genesis path: the
    # per-app sanity floor now also guards it, so a garbage first champion can't
    # self-adopt under ADOPT_RULE=p2oc (matches the default rule).
    monkeypatch.setenv("ADOPT_RULE", "p2oc")
    adopt, reason = evaluate_adoption(
        challenger_score=0.05,
        champion_score=0.0,
        challenger_scorecard=_card({"A": 0.05}, {"A": [100]}),  # below per-app 0.3
        champion_scorecard=None,
        dethrone_margin=0.05,
        has_champion=False,
    )
    assert adopt is False
    assert "per-app minimum" in reason


# ── moved module helpers ─────────────────────────────────────────────────────


def test_onchain_pass_helper():
    assert _onchain_pass([5000, 5100], 5000) == (True, 5000, 0)
    assert _onchain_pass([4999, 5100], 5000) == (False, 4999, 0)
    assert _onchain_pass([5000, None], 5000) == (False, 5000, 1)
    # Empty list: no missing, vacuous all() -> all_pass True, min_bps None.
    assert _onchain_pass([], 5000) == (True, None, 0)


def test_app_onchain_mean_helper():
    assert _app_onchain_mean([5000, 6000]) == 5500
    assert _app_onchain_mean([5000, None]) == 5000
    assert _app_onchain_mean([None, None]) is None
    assert _app_onchain_mean([]) is None


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
