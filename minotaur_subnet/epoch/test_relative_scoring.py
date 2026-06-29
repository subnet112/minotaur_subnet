"""Unit tests for the relative per-order scoring rule (the sole adoption path).

Covers the pure ``evaluate_relative_adoption`` decision across the full verdict
matrix, on EXACT INTEGER wei (shadow_score is a decimal STRING; the verdict
cross-multiplies the 10-bps band with no float).
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from types import SimpleNamespace

from minotaur_subnet.epoch.relative_scoring import (
    MIN_VALID_OUTPUT,
    RELATIVE_TOL,
    RELATIVE_TOL_BPS,
    evaluate_relative_adoption,
    has_shadow_rows,
    relative_counts,
    relative_counts_for_submissions,
    relative_reason,
)
from minotaur_subnet.harness.orchestrator import BenchmarkResult


def _r(intent_id: str, shadow_score):
    """A real BenchmarkResult carrying only intent_id + shadow_score (decimal str)."""
    return BenchmarkResult(intent_id=intent_id, shadow_score=shadow_score)


def test_tol_bps_matches_relative_tol():
    # The integer band and the documented float band must stay in sync.
    assert RELATIVE_TOL_BPS == 10
    assert RELATIVE_TOL == 0.001
    assert RELATIVE_TOL_BPS == round(RELATIVE_TOL * 10000)


# ── evaluate_relative_adoption (EXACT INTEGER, 10-bps band) ───────────────────


def test_clean_win_adopts():
    champ = [_r("o1", "100"), _r("o2", "200")]
    chal = [_r("o1", "120"), _r("o2", "250")]
    res = evaluate_relative_adoption(champ, chal)
    assert res["adopt"] is True
    assert res["n_wins"] == 2
    assert res["n_regressions"] == 0
    assert res["scenarios_compared"] == 2
    assert {o["verdict"] for o in res["per_order"]} == {"win"}
    # champ/chal echoed back as exact decimal strings.
    assert {o["champ"] for o in res["per_order"]} == {"100", "200"}


def test_all_matched_no_win_does_not_adopt():
    # Identical outputs everywhere -> all "matched", no win -> no adopt.
    champ = [_r("o1", "100"), _r("o2", "200")]
    chal = [_r("o1", "100"), _r("o2", "200")]
    res = evaluate_relative_adoption(champ, chal)
    assert res["adopt"] is False
    assert res["n_wins"] == 0
    assert res["n_matched"] == 2
    assert res["n_regressions"] == 0


def test_single_regression_vetoes_many_wins():
    champ = [_r("o1", "100"), _r("o2", "100"), _r("o3", "100")]
    chal = [_r("o1", "200"), _r("o2", "200"), _r("o3", "50")]  # o3 regresses
    res = evaluate_relative_adoption(champ, chal)
    assert res["adopt"] is False
    assert res["n_wins"] == 2
    assert res["n_regressions"] == 1
    verdicts = {o["intent_id"]: o["verdict"] for o in res["per_order"]}
    assert verdicts["o3"] == "regression"


def test_blind_spot_cover_counts_as_win():
    # Champion delivered nothing on o2 (blind spot); challenger covers it.
    champ = [_r("o1", "100"), _r("o2", None)]
    chal = [_r("o1", "100"), _r("o2", "500")]
    res = evaluate_relative_adoption(champ, chal)
    assert res["adopt"] is True
    assert res["n_blind_spots"] == 1
    assert res["n_wins"] == 0  # o1 only matched
    verdicts = {o["intent_id"]: o["verdict"] for o in res["per_order"]}
    assert verdicts["o2"] == "blind_spot_cover"


def test_dropped_is_a_regression():
    # Champion delivered on o2; challenger drops it (no value) -> regression veto.
    champ = [_r("o1", "100"), _r("o2", "300")]
    chal = [_r("o1", "200"), _r("o2", None)]
    res = evaluate_relative_adoption(champ, chal)
    assert res["adopt"] is False
    assert res["n_regressions"] == 1
    verdicts = {o["intent_id"]: o["verdict"] for o in res["per_order"]}
    assert verdicts["o2"] == "dropped"


def test_tolerance_band_is_matched_not_regression():
    # Exactly on the lower 10-bps boundary (0.1% below) -> "matched", not a
    # regression, so it does NOT veto (but is not a win either).
    champ = [_r("o1", "1000"), _r("o2", "1000")]
    # 999 vs 1000: 999*10000 == 1000*9990 -> on the boundary -> matched.
    chal = [_r("o1", "999"), _r("o2", "1100")]   # o2 is a clear win
    res = evaluate_relative_adoption(champ, chal)
    verdicts = {o["intent_id"]: o["verdict"] for o in res["per_order"]}
    assert verdicts["o1"] == "matched"
    assert verdicts["o2"] == "win"
    assert res["n_regressions"] == 0
    assert res["adopt"] is True


def test_just_outside_band_is_regression():
    # 998 vs 1000: 998*10000 = 9_980_000 < 1000*9990 = 9_990_000 -> regression.
    champ = [_r("o1", "1000")]
    chal = [_r("o1", "998")]
    res = evaluate_relative_adoption(champ, chal)
    assert res["per_order"][0]["verdict"] == "regression"
    assert res["adopt"] is False


def test_just_above_band_is_win():
    # 1002 vs 1000: 1002*10000 = 10_020_000 > 1000*10010 = 10_010_000 -> win.
    # 1001 vs 1000: 1001*10000 = 10_010_000 == 1000*10010 -> on boundary -> matched.
    champ = [_r("o1", "1000"), _r("o2", "1000")]
    chal = [_r("o1", "1002"), _r("o2", "1001")]
    res = evaluate_relative_adoption(champ, chal)
    verdicts = {o["intent_id"]: o["verdict"] for o in res["per_order"]}
    assert verdicts["o1"] == "win"
    assert verdicts["o2"] == "matched"


def test_both_no_value_is_skipped():
    champ = [_r("o1", None), _r("o2", "0")]
    chal = [_r("o1", None), _r("o2", str(MIN_VALID_OUTPUT))]  # "0"
    res = evaluate_relative_adoption(champ, chal)
    assert res["scenarios_compared"] == 0
    assert res["adopt"] is False
    assert {o["verdict"] for o in res["per_order"]} == {"skip"}


def test_accepts_per_intent_dicts():
    # The report/manager paths pass stored per_intent dicts, not BenchmarkResults.
    champ = [{"intent_id": "o1", "shadow_score": "100"}]
    chal = [{"intent_id": "o1", "shadow_score": "150"}]
    res = evaluate_relative_adoption(champ, chal)
    assert res["adopt"] is True
    assert res["per_order"][0]["ratio"] == 1.5
    assert res["per_order"][0]["champ"] == "100"
    assert res["per_order"][0]["chal"] == "150"


# ── EXACT big-number proof (the point of this PR) ─────────────────────────────


def test_bignum_exactness_above_2_53():
    """Token wei above 2^53 must compare BIT-EXACT — these deltas would be
    ambiguous / at-risk of flipping under IEEE-754 doubles."""
    champ_val = "1000000000000000000000"  # 1e21, far above 2^53 (~9.007e15)
    base = int(champ_val)

    # 1. identical -> matched, ratio exactly 1.0, exact strings preserved.
    res = evaluate_relative_adoption([_r("o1", champ_val)], [_r("o1", champ_val)])
    o = res["per_order"][0]
    assert o["verdict"] == "matched"
    assert o["ratio"] == 1.0
    assert o["champ"] == champ_val
    assert o["chal"] == champ_val

    # 2. +0.1% (+1e18) -> exactly on the 10-bps boundary -> matched (NOT a win).
    plus_0_1 = str(base + 10**18)
    res = evaluate_relative_adoption([_r("o1", champ_val)], [_r("o1", plus_0_1)])
    assert res["per_order"][0]["verdict"] == "matched"

    # 3. +0.6% -> outside the band -> win.
    plus_0_6 = str(base * 1006 // 1000)
    res = evaluate_relative_adoption([_r("o1", champ_val)], [_r("o1", plus_0_6)])
    assert res["per_order"][0]["verdict"] == "win"
    assert res["adopt"] is True

    # 4. exactly 1 wei less (~1e-21 relative) -> matched, NOT a regression, no
    #    exception. Under float, 1e21-1 == 1e21 (the 1 wei is lost), so the
    #    string/int path is what keeps the value representable at all.
    one_less = str(base - 1)
    assert one_less != champ_val  # the 1 wei is preserved as a string
    res = evaluate_relative_adoption([_r("o1", champ_val)], [_r("o1", one_less)])
    o = res["per_order"][0]
    assert o["verdict"] == "matched"
    assert o["chal"] == one_less  # exact, all 22 digits

    # 5. 1 wei MORE than champion on a 1e21 base -> still matched (inside band),
    #    proving sub-bps deltas don't false-trigger a win.
    one_more = str(base + 1)
    res = evaluate_relative_adoption([_r("o1", champ_val)], [_r("o1", one_more)])
    assert res["per_order"][0]["verdict"] == "matched"


def test_bignum_regression_is_exact():
    """A regression just outside the band on a huge base is detected exactly."""
    champ_val = "777066690445322700000"  # ~7.77e20
    base = int(champ_val)
    # -0.2% -> below the -10-bps band -> regression, vetoes adoption.
    minus_0_2 = str(base * 998 // 1000)
    res = evaluate_relative_adoption([_r("o1", champ_val)], [_r("o1", minus_0_2)])
    assert res["per_order"][0]["verdict"] == "regression"
    assert res["adopt"] is False
    assert res["per_order"][0]["champ"] == champ_val  # exact, not a rounded double


# ── relative_counts (count-shape mapping for the API) ─────────────────────────


def test_counts_clean_win_is_dethrone():
    # shadow_score is an EXACT INTEGER DECIMAL STRING (#395), not a float.
    champ = [_r("o1", "100"), _r("o2", "200")]
    chal = [_r("o1", "120"), _r("o2", "250")]
    c = relative_counts(champ, chal)
    assert c["better"] == 2
    assert c["worse"] == 0
    assert c["matched"] == 0
    assert c["new"] == 0
    assert c["compared"] == 2
    assert c["verdict"] == "dethrone"
    assert {o["verdict"] for o in c["per_order"]} == {"win"}
    # per_order champ/chal are echoed back as exact decimal strings.
    assert {o["champ"] for o in c["per_order"]} == {"100", "200"}


def test_counts_all_matched_is_matched():
    champ = [_r("o1", "100"), _r("o2", "200")]
    chal = [_r("o1", "100"), _r("o2", "200")]
    c = relative_counts(champ, chal)
    assert (c["better"], c["worse"], c["matched"], c["new"]) == (0, 0, 2, 0)
    assert c["verdict"] == "matched"


def test_counts_any_regression_is_behind_regardless_of_wins():
    # Two wins but one regression -> behind (a regression vetoes adoption).
    champ = [_r("o1", "100"), _r("o2", "100"), _r("o3", "100")]
    chal = [_r("o1", "200"), _r("o2", "200"), _r("o3", "50")]
    c = relative_counts(champ, chal)
    assert c["better"] == 2
    assert c["worse"] == 1
    assert c["verdict"] == "behind"


def test_counts_dropped_order_is_worse():
    # A dropped order (champion delivered, challenger produced nothing) is worse.
    champ = [_r("o1", "100"), _r("o2", "300")]
    chal = [_r("o1", "200"), _r("o2", None)]
    c = relative_counts(champ, chal)
    assert c["worse"] == 1
    assert c["verdict"] == "behind"


def test_counts_blind_spot_is_better_and_new():
    # Champion blind on o2; challenger covers it -> counts as better AND new.
    champ = [_r("o1", "100"), _r("o2", None)]
    chal = [_r("o1", "100"), _r("o2", "500")]
    c = relative_counts(champ, chal)
    assert c["better"] == 1
    assert c["new"] == 1
    assert c["worse"] == 0
    assert c["matched"] == 1  # o1 only matched
    assert c["compared"] == 2
    assert c["verdict"] == "dethrone"


def test_counts_compared_excludes_skips():
    # o2 has no value on either side -> skip, excluded from `compared`.
    champ = [_r("o1", "100"), _r("o2", None)]
    chal = [_r("o1", "150"), _r("o2", None)]
    c = relative_counts(champ, chal)
    assert c["compared"] == 1
    assert c["verdict"] == "dethrone"


# ── relative_counts_for_submissions (reads benchmark_details) ─────────────────


def _sub(per_intent):
    return SimpleNamespace(
        submission_id="sub-x",
        benchmark_details={"per_intent": per_intent},
    )


def test_for_submissions_clean_win():
    champ = _sub([{"intent_id": "o1", "shadow_score": "100"}])
    chal = _sub([{"intent_id": "o1", "shadow_score": "150"}])
    c = relative_counts_for_submissions(chal, champ)
    assert c is not None
    assert c["better"] == 1
    assert c["verdict"] == "dethrone"


def test_for_submissions_no_shadow_rows_returns_none():
    # Champion benched before shadow existed: rows present but no shadow_score.
    champ = _sub([{"intent_id": "o1", "score": 0.9}])  # no shadow_score key
    chal = _sub([{"intent_id": "o1", "shadow_score": "150"}])
    assert relative_counts_for_submissions(chal, champ) is None
    # Symmetric: challenger missing shadow rows.
    champ2 = _sub([{"intent_id": "o1", "shadow_score": "100"}])
    chal2 = _sub([{"intent_id": "o1", "score": 0.9}])
    assert relative_counts_for_submissions(chal2, champ2) is None


def test_for_submissions_missing_submission_returns_none():
    champ = _sub([{"intent_id": "o1", "shadow_score": "100"}])
    assert relative_counts_for_submissions(None, champ) is None
    assert relative_counts_for_submissions(champ, None) is None


def test_has_shadow_rows():
    assert has_shadow_rows([{"intent_id": "o1", "shadow_score": "1"}]) is True
    assert has_shadow_rows([{"intent_id": "o1", "shadow_score": None}]) is False
    assert has_shadow_rows([{"intent_id": "o1", "score": 0.5}]) is False
    assert has_shadow_rows([]) is False
    assert has_shadow_rows(None) is False


# ── relative_reason (display vocabulary) ──────────────────────────────────────


def test_reason_dethrone():
    counts = relative_counts([_r("o1", "100")], [_r("o1", "200")])
    msg = relative_reason(counts, candidate_id="sub-7")
    assert msg == "adopted sub-7: better on 1 order(s), 0 regressions"


def test_reason_behind_uses_matched_and_regressed():
    counts = relative_counts(
        [_r("o1", "100"), _r("o2", "100")],
        [_r("o1", "100"), _r("o2", "50")],  # o1 matched, o2 regressed
    )
    msg = relative_reason(counts)
    assert msg == "no challenger delivered more on any order (1 matched / 1 regressed)"


def test_reason_none_when_no_counts():
    assert relative_reason(None) is None
