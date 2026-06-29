"""Sparse per-app on-chain aggregation in the adoption rule (epoch/adopt_rule.py).

A per-app on-chain list has ONE entry per benchmark scenario, with ``None`` for
any scenario that produced no ``scoreIntent`` output (revert / mock / no plan) —
so the list is SPARSE: ``[bps, None, bps, ...]`` (see BenchmarkWorker._build_scorecard,
benchmark_worker.py:1558). ``_app_onchain_mean`` (adopt_rule.py) collapses that
list to the per-app mean used by ``evaluate_adoption``'s on-chain floor / regression
gates (the current-rule veto ``_evaluate_onchain_gate``, adopt_rule.py).

The helper itself (None-safe mean over present-only entries) is unit-tested in
test_adopt_rule.py::test_app_onchain_mean_helper. These tests instead pin the
END-TO-END aggregation BEHAVIOR through ``evaluate_adoption`` for the genuinely
-missing sparse shapes — specifically that ``None`` is never coerced to ``0`` when
a sparse list is reduced to its mean, which would silently corrupt the regression
and surplus gates. They are pure-function tests (no Docker / chain / Anvil).
"""

from __future__ import annotations

from minotaur_subnet.epoch.adopt_rule import (
    _app_onchain_mean,
    evaluate_adoption,
)


def _card(js: dict, oc: dict | None = None) -> dict:
    card: dict = {"app_scores": js}
    if oc is not None:
        card["app_onchain"] = oc
    return card


# ── the load-bearing invariant: None is dropped, never coerced to 0 ──────────


def test_sparse_mean_ignores_none_not_zero():
    """A ``None`` scenario is OMITTED from the per-app mean (averaged over present
    entries), NOT averaged in as 0. If it were 0, ``[10000, None]`` would mean 5000."""
    assert _app_onchain_mean([10000, None]) == 10000.0  # present-only, not 5000
    assert _app_onchain_mean([6000, None, 6000]) == 6000.0  # not (6000+0+6000)/3
    assert _app_onchain_mean([None, 5000]) == 5000.0  # leading None dropped
    # all-missing -> there is NO signal; the mean is None (sentinel), never 0.
    assert _app_onchain_mean([None, None]) is None


def test_champion_sparse_mean_anchors_regression_gate_to_present_value():
    """Current-rule on-chain VETO. The CHAMPION's sparse list ``[10000, None]`` must
    aggregate to its present-only mean (10000) as the regression baseline. A
    challenger at 8000 is then a 20% drop (> the 10% band) -> VETO.

    If ``None`` were coerced to 0, the champion mean would collapse to 5000 and the
    8000 challenger would look like a +60% IMPROVEMENT and wrongly clear the gate —
    so this asserts the sparse aggregation is None-dropping, not None-as-zero."""
    adopt, reason = evaluate_adoption(
        challenger_score=0.9,
        champion_score=0.6,
        challenger_scorecard=_card({"A": 0.9}, {"A": [8000, 8000]}),
        champion_scorecard=_card({"A": 0.6}, {"A": [10000, None]}),
        dethrone_margin=0.005,
        has_champion=True,
    )
    assert adopt is False
    assert "regress" in reason
    # The baseline shown in the reason is the present-only mean (10000), not 5000.
    assert "10000" in reason


def test_partial_revert_guard_fires_even_when_sparse_mean_is_favorable():
    """Current-rule gate, sparse-MASKING case. The challenger reverted scenario 2
    but its present-only mean (``[6000, None]`` -> 6000) is HIGHER than the fully
    -present champion (``[5000, 5000]`` -> 5000), so the regression check on the mean
    alone would pass. The partial-revert guard must still VETO it — proving the
    mean cannot be gamed by reverting hard scenarios and only reporting easy ones.

    (Distinct from test_adopt_rule.py::test_onchain_veto_partial_revert, whose
    ``[5000, None]`` has a mean EQUAL to the champion, so it never isolates the
    case where the sparse mean would otherwise have masked the revert.)"""
    adopt, reason = evaluate_adoption(
        challenger_score=0.9,
        champion_score=0.6,
        challenger_scorecard=_card({"A": 0.9}, {"A": [6000, None]}),
        champion_scorecard=_card({"A": 0.6}, {"A": [5000, 5000]}),
        dethrone_margin=0.005,
        has_champion=True,
    )
    assert adopt is False
    assert "Partial on-chain revert" in reason


def test_both_sides_sparse_mean_compared_present_only():
    """Both lists sparse. Champion ``[5000, None]`` -> 5000, challenger ``[5025, None]``
    -> 5025: equal missing-counts (no partial-revert guard fires) and the present-only
    means compare within the band, so the JS ranking adopts. Confirms the mean
    aggregation operates on present entries on BOTH sides symmetrically."""
    adopt, reason = evaluate_adoption(
        challenger_score=0.7,
        champion_score=0.6,
        challenger_scorecard=_card({"A": 0.7}, {"A": [5025, None]}),
        champion_scorecard=_card({"A": 0.6}, {"A": [5000, None]}),
        dethrone_margin=0.005,
        has_champion=True,
    )
    assert adopt is True
    assert "beats incumbent" in reason


def test_all_missing_app_is_not_gated_current_rule():
    """When the CHAMPION's whole list for an app is ``[None, None]`` the present-only
    mean is ``None`` (no on-chain signal at all), so that app is SKIPPED by the
    current-rule veto (not every app swaps) and the decision falls through to JS
    ranking. Confirms an all-missing sparse list aggregates to a None sentinel that
    means "no gate", not a 0 that would (wrongly) make the app look fully-regressed."""
    adopt, reason = evaluate_adoption(
        challenger_score=0.7,
        champion_score=0.6,
        challenger_scorecard=_card({"A": 0.7}, {"A": [None, None]}),
        champion_scorecard=_card({"A": 0.6}, {"A": [None, None]}),
        dethrone_margin=0.005,
        has_champion=True,
    )
    assert adopt is True
    assert "beats incumbent" in reason
