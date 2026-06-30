"""Unit tests for the relative per-order scoring rule (the sole adoption path).

Covers the pure ``evaluate_relative_adoption`` decision across the full verdict
matrix, on EXACT INTEGER wei (raw_output is a decimal STRING; the verdict
cross-multiplies the 10-bps band with no float).
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from minotaur_subnet.epoch.relative_scoring import (
    MIN_VALID_OUTPUT,
    RELATIVE_TOL,
    RELATIVE_TOL_BPS,
    evaluate_relative_adoption,
    has_raw_output_rows,
    relative_counts,
    relative_reason,
)
from minotaur_subnet.harness.orchestrator import BenchmarkResult


def _r(intent_id: str, raw_output):
    """A real BenchmarkResult carrying only intent_id + raw_output (decimal str)."""
    return BenchmarkResult(intent_id=intent_id, raw_output=raw_output)


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


def test_catastrophic_regression_vetoes_many_wins():
    # o3 is cut by 50% — a CATASTROPHIC (>1% floor) regression that hard-vetoes
    # adoption no matter how many other orders win.
    champ = [_r("o1", "100"), _r("o2", "100"), _r("o3", "100")]
    chal = [_r("o1", "200"), _r("o2", "200"), _r("o3", "50")]  # o3 -50%
    res = evaluate_relative_adoption(champ, chal)
    assert res["adopt"] is False
    assert res["n_wins"] == 2
    assert res["n_regressions"] == 1
    assert res["n_catastrophic"] == 1  # the >1% cut is the veto
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


def test_dropped_is_a_hard_veto():
    # Champion delivered on o2; challenger drops it (no value). A dropped order is
    # counted SEPARATELY in n_dropped (not folded into n_regressions) and is a
    # HARD VETO — even with a clear win on o1 the challenger cannot adopt.
    champ = [_r("o1", "100"), _r("o2", "300")]
    chal = [_r("o1", "200"), _r("o2", None)]
    res = evaluate_relative_adoption(champ, chal)
    assert res["adopt"] is False
    assert res["n_dropped"] == 1
    assert res["n_regressions"] == 0  # the drop is NOT folded into regressions
    assert res["n_wins"] == 1
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
    champ = [{"intent_id": "o1", "raw_output": "100"}]
    chal = [{"intent_id": "o1", "raw_output": "150"}]
    res = evaluate_relative_adoption(champ, chal)
    assert res["adopt"] is True
    assert res["per_order"][0]["ratio"] == 1.5
    assert res["per_order"][0]["champ"] == "100"
    assert res["per_order"][0]["chal"] == "150"


# ── bounded-regression, net-better dethrone rule (the point of THIS PR) ───────
#
# Fixtures use champ=1000 with win=1100 (+10%) and a MINOR regression=995
# (-0.5%, inside the 1% FLOOR_BPS so it is tolerated and netted, not a veto).


def _wins_and_minor_regs(n_win, n_reg, *, base=1000, win=1100, reg=995):
    """Build champ/chal lists with ``n_win`` clear wins + ``n_reg`` <=1% (minor,
    tolerated) regressions — the inputs to the net-better truth table."""
    champ, chal = [], []
    i = 0
    for _ in range(n_win):
        i += 1
        champ.append(_r(f"o{i}", str(base)))
        chal.append(_r(f"o{i}", str(win)))
    for _ in range(n_reg):
        i += 1
        champ.append(_r(f"o{i}", str(base)))
        chal.append(_r(f"o{i}", str(reg)))
    return champ, chal


def test_minor_regression_is_within_floor_not_catastrophic():
    # 995 vs 1000 = -0.5%: a regression (outside the 0.1% band) but well inside
    # the 1% floor -> counted in n_regressions, NOT n_catastrophic.
    res = evaluate_relative_adoption([_r("o1", "1000")], [_r("o1", "995")])
    assert res["per_order"][0]["verdict"] == "regression"
    assert res["n_regressions"] == 1
    assert res["n_catastrophic"] == 0


def test_net_4_over_1_adopts():
    # ▲4 ▼1 (the ▼ at -0.5%, within floor): 4 >= 1 + 1 -> ADOPT -> dethrone.
    champ, chal = _wins_and_minor_regs(4, 1)
    res = evaluate_relative_adoption(champ, chal)
    assert res["adopt"] is True
    assert (res["n_wins"], res["n_regressions"], res["n_catastrophic"]) == (4, 1, 0)
    assert relative_counts(champ, chal)["verdict"] == "dethrone"


def test_net_1_over_1_rejects():
    # ▲1 ▼1: 1 < 1 + 1 -> REJECT -> behind.
    champ, chal = _wins_and_minor_regs(1, 1)
    res = evaluate_relative_adoption(champ, chal)
    assert res["adopt"] is False
    assert relative_counts(champ, chal)["verdict"] == "behind"


def test_net_2_over_2_rejects():
    # ▲2 ▼2: 2 < 2 + 1 -> REJECT.
    champ, chal = _wins_and_minor_regs(2, 2)
    assert evaluate_relative_adoption(champ, chal)["adopt"] is False


def test_net_2_over_1_adopts_at_margin():
    # ▲2 ▼1: 2 >= 1 + 1 -> ADOPT exactly at the margin.
    champ, chal = _wins_and_minor_regs(2, 1)
    assert evaluate_relative_adoption(champ, chal)["adopt"] is True


def test_net_1_over_0_adopts():
    # ▲1 ▼0: 1 >= 0 + 1 -> ADOPT (a single clean win, no regression).
    champ, chal = _wins_and_minor_regs(1, 0)
    assert evaluate_relative_adoption(champ, chal)["adopt"] is True


def test_net_6_over_3_adopts():
    # ▲6 ▼3, all ▼ within the 1% floor: 6 >= 3 + 1 -> ADOPT.
    champ, chal = _wins_and_minor_regs(6, 3)
    res = evaluate_relative_adoption(champ, chal)
    assert res["adopt"] is True
    assert (res["n_wins"], res["n_regressions"], res["n_catastrophic"]) == (6, 3, 0)
    assert "net +3" in res["reason"]


def test_catastrophic_hard_floor_rejects_with_five_wins():
    # ▲5 with ONE order at -2% (> 1% floor): catastrophic hard veto, REJECT
    # regardless of the five wins.
    champ = [_r(f"o{i}", "1000") for i in range(1, 6)] + [_r("o6", "1000")]
    chal = [_r(f"o{i}", "1100") for i in range(1, 6)] + [_r("o6", "980")]  # o6 -2%
    res = evaluate_relative_adoption(champ, chal)
    assert res["adopt"] is False
    assert res["n_wins"] == 5
    assert res["n_catastrophic"] == 1
    assert "hard floor" in res["reason"]
    assert relative_counts(champ, chal)["verdict"] == "behind"


def test_dropped_order_rejects_with_wins():
    # Challenger drops a champion-served order: hard veto, REJECT despite 2 wins.
    champ = [_r("o1", "1000"), _r("o2", "1000"), _r("o3", "1000")]
    chal = [_r("o1", "1100"), _r("o2", "1100"), _r("o3", None)]  # o3 dropped
    res = evaluate_relative_adoption(champ, chal)
    assert res["adopt"] is False
    assert res["n_dropped"] == 1
    assert res["n_wins"] == 2
    assert "dropped" in res["reason"]
    assert relative_counts(champ, chal)["verdict"] == "behind"


def test_blind_spots_count_as_wins_for_the_net():
    # 0 wins, 2 blind-spots, 0 regressions: 2 >= 0 + 1 -> ADOPT. Blind-spot
    # covers are rewarded on the wins side of the net.
    champ = [_r("o1", None), _r("o2", None), _r("o3", "1000")]
    chal = [_r("o1", "500"), _r("o2", "500"), _r("o3", "1000")]  # o3 matched
    res = evaluate_relative_adoption(champ, chal)
    assert res["adopt"] is True
    assert (res["n_wins"], res["n_blind_spots"], res["n_regressions"]) == (0, 2, 0)
    c = relative_counts(champ, chal)
    assert (c["better"], c["new"], c["verdict"]) == (2, 2, "dethrone")


def test_all_matched_verdict_is_matched_not_adopt():
    champ = [_r("o1", "1000"), _r("o2", "1000")]
    chal = [_r("o1", "1000"), _r("o2", "1000")]
    res = evaluate_relative_adoption(champ, chal)
    assert res["adopt"] is False
    assert res["reason"] == "matched: no order better or worse"
    assert relative_counts(champ, chal)["verdict"] == "matched"


def test_floor_boundary_exactly_minus_1pct_allowed_minus_1_01pct_catastrophic():
    # Exactly -1.0% (9900 vs 10000) sits ON the floor -> a tolerated regression,
    # NOT catastrophic. One wei past it (9899 = -1.01%) IS catastrophic.
    at_floor = evaluate_relative_adoption([_r("o1", "10000")], [_r("o1", "9900")])
    assert at_floor["per_order"][0]["verdict"] == "regression"
    assert at_floor["n_regressions"] == 1
    assert at_floor["n_catastrophic"] == 0

    past_floor = evaluate_relative_adoption([_r("o1", "10000")], [_r("o1", "9899")])
    assert past_floor["n_catastrophic"] == 1
    assert past_floor["adopt"] is False


def test_bignum_catastrophic_is_bit_exact():
    # On a 1e21-wei order the floor is detected to the wei: exactly -1.0% is NOT
    # catastrophic; ONE wei below it IS. Under IEEE-754 doubles that 1-wei step
    # would be invisible -> the exact-integer cross-multiply is what makes the
    # >1% hard floor host-deterministic.
    base = 10**21
    at_floor = str(base * 99 // 100)          # exactly -1.0%
    res = evaluate_relative_adoption([_r("o1", str(base))], [_r("o1", at_floor)])
    assert res["per_order"][0]["verdict"] == "regression"
    assert res["n_catastrophic"] == 0

    one_wei_past = str(base * 99 // 100 - 1)   # -1.0% minus 1 wei -> over the floor
    res = evaluate_relative_adoption([_r("o1", str(base))], [_r("o1", one_wei_past)])
    assert res["n_catastrophic"] == 1
    assert res["adopt"] is False
    assert res["per_order"][0]["chal"] == one_wei_past  # exact, all digits


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
    # raw_output is an EXACT INTEGER DECIMAL STRING (#395), not a float.
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


def test_counts_catastrophic_regression_is_behind_regardless_of_wins():
    # Two wins but one CATASTROPHIC (>1%) regression -> behind (hard floor veto).
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


def test_has_raw_output_rows():
    assert has_raw_output_rows([{"intent_id": "o1", "raw_output": "1"}]) is True
    assert has_raw_output_rows([{"intent_id": "o1", "raw_output": None}]) is False
    assert has_raw_output_rows([{"intent_id": "o1", "score": 0.5}]) is False
    assert has_raw_output_rows([]) is False
    assert has_raw_output_rows(None) is False


def test_legacy_shadow_score_key_still_reads():
    """Backward compat: rows persisted before the rename carry the legacy
    ``shadow_score`` key; the reader must still pick them up (raw_output preferred,
    shadow_score fallback) so a champion record or in-flight round written by older
    code isn't silently treated as empty."""
    # has_raw_output_rows accepts the legacy key.
    assert has_raw_output_rows([{"intent_id": "o1", "shadow_score": "100"}]) is True
    # And the full verdict reads legacy rows identically to current ``raw_output`` rows.
    champ = [{"intent_id": "o1", "shadow_score": "100"}]
    chal = [{"intent_id": "o1", "shadow_score": "120"}]
    res = evaluate_relative_adoption(champ, chal)
    assert res["n_wins"] == 1
    assert res["scenarios_compared"] == 1
    # New key wins when both are present (a migrated row that kept the stale key).
    mixed = [{"intent_id": "o1", "raw_output": "120", "shadow_score": "100"}]
    assert evaluate_relative_adoption(
        [{"intent_id": "o1", "raw_output": "100"}], mixed,
    )["n_wins"] == 1


# ── relative_reason (display vocabulary) ──────────────────────────────────────


def test_reason_dethrone():
    counts = relative_counts([_r("o1", "100")], [_r("o1", "200")])
    msg = relative_reason(counts, candidate_id="sub-7")
    assert msg == "adopted sub-7: net better — 1 better / 0 worse (regressions within 1% floor)"


def test_reason_behind_uses_matched_and_regressed():
    counts = relative_counts(
        [_r("o1", "100"), _r("o2", "100")],
        [_r("o1", "100"), _r("o2", "50")],  # o1 matched, o2 regressed
    )
    msg = relative_reason(counts)
    assert msg == "not adopted: 0 better / 1 worse / 1 matched"


def test_reason_none_when_no_counts():
    assert relative_reason(None) is None
