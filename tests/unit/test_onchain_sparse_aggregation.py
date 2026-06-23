"""Sparse per-app on-chain aggregation in the adoption rule (epoch/adopt_rule.py).

A per-app on-chain list has ONE entry per benchmark scenario, with ``None`` for
any scenario that produced no ``scoreIntent`` output (revert / mock / no plan) —
so the list is SPARSE: ``[bps, None, bps, ...]`` (see BenchmarkWorker._build_scorecard,
benchmark_worker.py:1558). ``_app_onchain_mean`` (adopt_rule.py:99) collapses that
list to the per-app mean used by ``evaluate_adoption``'s on-chain floor / regression
gates (the current-rule veto ``_evaluate_onchain_gate``, adopt_rule.py:106, and the
p2oc surplus rule ``_evaluate_onchain``, adopt_rule.py:147).

The helper itself (None-safe mean over present-only entries) is unit-tested in
test_adopt_rule.py::test_app_onchain_mean_helper. These tests instead pin the
END-TO-END aggregation BEHAVIOR through ``evaluate_adoption`` for the genuinely
-missing sparse shapes — specifically that ``None`` is never coerced to ``0`` when
a sparse list is reduced to its mean, which would silently corrupt the regression
and surplus gates. They are pure-function tests (no Docker / chain / Anvil).
"""

from __future__ import annotations

from minotaur_subnet.epoch.adopt_rule import (
    _AdoptRuleConfig,
    _app_onchain_mean,
    evaluate_adoption,
)


def _card(js: dict, oc: dict | None = None) -> dict:
    card: dict = {"app_scores": js}
    if oc is not None:
        card["app_onchain"] = oc
    return card


def _p2oc_cfg(*, onchain_floor_bps: "int | None" = None) -> _AdoptRuleConfig:
    return _AdoptRuleConfig(adopt_rule="p2oc", onchain_floor_bps=onchain_floor_bps)


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


# ── p2oc surplus rule: sparse mean is None-safe (doesn't crash / treat None as 0) ──


def test_p2oc_sparse_surplus_uses_present_only_mean():
    """p2oc ranks on the per-app on-chain surplus = ``mean(challenger) - mean(champion)``.
    With a sparse challenger ``[6000, None]`` (present mean 6000) vs champion 5000, the
    aggregation must not crash and must use the present-only mean -> +1000 BPS surplus
    (0.10 > the 0.005 margin) -> ADOPT. Documents that p2oc's present-only mean is
    None-safe; if None were treated as 0 the challenger mean would be 3000 and the
    surplus would flip negative (-2000 BPS) -> a wrong REJECT."""
    adopt, reason = evaluate_adoption(
        challenger_score=0.53,
        champion_score=0.54,
        challenger_scorecard=_card({"A": 0.53}, {"A": [6000, None]}),
        champion_scorecard=_card({"A": 0.54}, {"A": [5000, 5000]}),
        dethrone_margin=0.005,
        has_champion=True,
        config=_p2oc_cfg(),
    )
    assert adopt is True
    assert "ADOPT" in reason
    assert "+1000.0 BPS" in reason  # present-only surplus, not a None-as-0 -2000


def test_p2oc_floor_veto_counts_sparse_none_as_missing():
    """p2oc admission-floor veto runs ``_onchain_pass`` over the challenger's sparse
    list. A ``None`` scenario counts as MISSING (n_missing>0) -> floor fail, so a
    challenger that reverted a scenario can't clear the floor even though its present
    score (6000) is above it. The reason surfaces the missing count, proving None is
    accounted as missing rather than silently dropped from the floor check."""
    adopt, reason = evaluate_adoption(
        challenger_score=0.9,
        champion_score=0.5,
        challenger_scorecard=_card({"A": 0.9}, {"A": [6000, None]}),
        champion_scorecard=_card({"A": 0.5}, {"A": [5000, 5000]}),
        dethrone_margin=0.005,
        has_champion=True,
        config=_p2oc_cfg(onchain_floor_bps=5000),
    )
    assert adopt is False
    assert "on-chain floor fail" in reason
    assert "missing=1" in reason


def test_p2oc_champion_sparse_anchors_surplus_baseline():
    """p2oc with a sparse CHAMPION list. Champion ``[8000, None]`` -> present mean 8000;
    challenger ``[8100, 8100]`` -> 8100. Surplus = +100 BPS (0.01 > 0.005 margin) ->
    ADOPT. If the champion's None were 0, its mean would be 4000 and the surplus would
    balloon to +4100 BPS — so this pins the champion-side present-only aggregation."""
    adopt, reason = evaluate_adoption(
        challenger_score=0.7,
        champion_score=0.6,
        challenger_scorecard=_card({"A": 0.7}, {"A": [8100, 8100]}),
        champion_scorecard=_card({"A": 0.6}, {"A": [8000, None]}),
        dethrone_margin=0.005,
        has_champion=True,
        config=_p2oc_cfg(),
    )
    assert adopt is True
    assert "+100.0 BPS" in reason  # 8100 - 8000, not 8100 - 4000


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
