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


# ── blind-spot REPEAT guard (BLIND_SPOT_BAR_TTL_S; Phase 0 disarmed) ──────────
#
# Anti-treadmill rule: a blind_spot_cover that does NOT exceed the incumbent's
# ADOPTION-TIME value on that order (within the TTL) is a NEUTRAL
# blind_spot_repeat, never the +1 that dethrones. Disarmed (TTL None) it only
# observes. Armed via monkeypatch here — the live switch is a fleet-uniform
# CODE constant.


def _bar_case():
    """Champion fails o2 fresh (treadmill decay); challenger re-covers it at
    exactly the value the incumbent delivered when IT was adopted."""
    champ = [_r("o1", "100"), _r("o2", "0")]
    chal = [_r("o1", "100"), _r("o2", "5000")]
    bar = {"o2": "5000"}
    return champ, chal, bar


def test_armed_by_default_blocks_repeat():
    # ARMED 2026-07-08 at 24h: the DEFAULT constant now blocks a within-TTL
    # photocopy cover (no monkeypatch — this is the shipped value).
    from minotaur_subnet.epoch import relative_scoring as rs

    assert rs.BLIND_SPOT_BAR_TTL_S == 24 * 3600.0  # shipped armed
    champ, chal, bar = _bar_case()
    res = evaluate_relative_adoption(champ, chal, champion_bar=bar, bar_age_s=3600.0)
    assert res["adopt"] is False           # photocopy no longer dethrones
    assert res["n_blind_spots"] == 0
    assert res["n_blind_spot_repeats"] == 1
    o2 = [o for o in res["per_order"] if o["intent_id"] == "o2"][0]
    assert o2["verdict"] == "blind_spot_repeat"
    assert o2["bar_verdict"] == "repeat"


def test_disarmed_keeps_cover_and_observes(monkeypatch):
    # The disarmed path stays testable (a revert sets the constant back to None):
    # the cover still counts + adopts, and only the soak counter sees the repeat.
    from minotaur_subnet.epoch import relative_scoring as rs

    monkeypatch.setattr(rs, "BLIND_SPOT_BAR_TTL_S", None)
    champ, chal, bar = _bar_case()
    res = evaluate_relative_adoption(champ, chal, champion_bar=bar, bar_age_s=3600.0)
    assert res["adopt"] is True
    assert res["n_blind_spots"] == 1
    assert res["n_blind_spot_repeats"] == 0
    assert res["n_blind_spot_repeats_observed"] == 1
    o2 = [o for o in res["per_order"] if o["intent_id"] == "o2"][0]
    assert o2["verdict"] == "blind_spot_cover"
    assert o2["bar"] == "5000"
    assert o2["bar_verdict"] == "repeat"


def test_no_bar_kwargs_is_fully_inert():
    champ, chal, _ = _bar_case()
    res = evaluate_relative_adoption(champ, chal)
    assert res["adopt"] is True
    assert res["n_blind_spots"] == 1
    assert res["n_blind_spot_repeats_observed"] == 0
    o2 = [o for o in res["per_order"] if o["intent_id"] == "o2"][0]
    assert "bar" not in o2


def test_armed_repeat_is_neutral_and_blocks_dethrone(monkeypatch):
    from minotaur_subnet.epoch import relative_scoring as rs

    monkeypatch.setattr(rs, "BLIND_SPOT_BAR_TTL_S", 24 * 3600.0)
    champ, chal, bar = _bar_case()
    res = evaluate_relative_adoption(champ, chal, champion_bar=bar, bar_age_s=3600.0)
    assert res["adopt"] is False  # the +1 was a photocopy — no dethrone
    assert res["n_blind_spots"] == 0
    assert res["n_blind_spot_repeats"] == 1
    assert res["n_blind_spot_repeats_observed"] == 1
    assert "repeat(s) not credited" in res["reason"]
    o2 = [o for o in res["per_order"] if o["intent_id"] == "o2"][0]
    assert o2["verdict"] == "blind_spot_repeat"
    # Repeats are compared (not skips): o1 matched + o2 repeat.
    assert res["scenarios_compared"] == 2


def test_armed_cover_exceeding_bar_still_dethrones(monkeypatch):
    from minotaur_subnet.epoch import relative_scoring as rs

    monkeypatch.setattr(rs, "BLIND_SPOT_BAR_TTL_S", 24 * 3600.0)
    champ, chal, bar = _bar_case()
    chal = [_r("o1", "100"), _r("o2", "5010")]  # +20 bps > 10-bps band
    res = evaluate_relative_adoption(champ, chal, champion_bar=bar, bar_age_s=3600.0)
    assert res["adopt"] is True
    assert res["n_blind_spots"] == 1
    assert res["n_blind_spot_repeats"] == 0
    o2 = [o for o in res["per_order"] if o["intent_id"] == "o2"][0]
    assert o2["verdict"] == "blind_spot_cover"
    assert o2["bar_verdict"] == "exceed"


def test_armed_bar_within_band_is_repeat_boundary(monkeypatch):
    # Exactly on the +tol boundary is NOT "exceed" (strict inequality, same
    # exact-integer cross-multiply as a win).
    from minotaur_subnet.epoch import relative_scoring as rs

    monkeypatch.setattr(rs, "BLIND_SPOT_BAR_TTL_S", 24 * 3600.0)
    champ = [_r("o2", "0")]
    chal = [_r("o2", str(5000 * (10000 + RELATIVE_TOL_BPS) // 10000))]  # 5005
    res = evaluate_relative_adoption(
        champ, chal, champion_bar={"o2": "5000"}, bar_age_s=60.0,
    )
    assert res["n_blind_spot_repeats"] == 1
    assert res["adopt"] is False


def test_armed_expired_bar_gives_full_cover_credit(monkeypatch):
    # Market moved on: a bar older than the TTL never downgrades a cover.
    from minotaur_subnet.epoch import relative_scoring as rs

    monkeypatch.setattr(rs, "BLIND_SPOT_BAR_TTL_S", 24 * 3600.0)
    champ, chal, bar = _bar_case()
    res = evaluate_relative_adoption(
        champ, chal, champion_bar=bar, bar_age_s=25 * 3600.0,
    )
    assert res["adopt"] is True
    assert res["n_blind_spots"] == 1
    assert res["n_blind_spot_repeats"] == 0
    # Observation TTL (24h) also lapsed — nothing observed either.
    assert res["n_blind_spot_repeats_observed"] == 0


def test_armed_unknown_age_is_inert(monkeypatch):
    # No bar_age_s (e.g. restored champion without a snapshot) ⇒ guard inert.
    from minotaur_subnet.epoch import relative_scoring as rs

    monkeypatch.setattr(rs, "BLIND_SPOT_BAR_TTL_S", 24 * 3600.0)
    champ, chal, bar = _bar_case()
    res = evaluate_relative_adoption(champ, chal, champion_bar=bar)
    assert res["adopt"] is True
    assert res["n_blind_spot_repeats"] == 0
    assert res["n_blind_spot_repeats_observed"] == 0


def test_armed_order_without_bar_entry_unaffected(monkeypatch):
    # A genuinely NEW blind spot (no adoption-time value) keeps full credit.
    from minotaur_subnet.epoch import relative_scoring as rs

    monkeypatch.setattr(rs, "BLIND_SPOT_BAR_TTL_S", 24 * 3600.0)
    champ, chal, _ = _bar_case()
    res = evaluate_relative_adoption(
        champ, chal, champion_bar={"other": "123"}, bar_age_s=60.0,
    )
    assert res["adopt"] is True
    assert res["n_blind_spots"] == 1


def test_blind_spot_bar_from_rows_keeps_only_delivered():
    from minotaur_subnet.epoch.relative_scoring import blind_spot_bar_from_rows

    rows = [_r("o1", "100"), _r("o2", "0"), _r("o3", None), _r("o4", 7)]
    assert blind_spot_bar_from_rows(rows) == {"o1": "100", "o4": "7"}
    assert blind_spot_bar_from_rows(None) == {}
# ── Phase-2 factorization tie-break (saturated-tie dethrone) ──────────────────
#
# factor_delta = champion.max_region_nodes - challenger.max_region_nodes (the
# PERSISTED screening metric). The clause fires ONLY when FACTOR_MARGIN is armed
# (a fleet-wide code promotion — None ships disarmed) AND on a true all-matched
# tie over a non-empty comparison; performance always outranks cleanliness.

import pytest  # noqa: E402

from minotaur_subnet.epoch import relative_scoring as _rs  # noqa: E402
from minotaur_subnet.epoch.relative_scoring import factor_delta_between  # noqa: E402

_ARMED_MARGIN = 25


@pytest.fixture
def armed_margin(monkeypatch):
    """Arm the Phase-2 tie-break the way the fleet-wide promotion would."""
    monkeypatch.setattr(_rs, "FACTOR_MARGIN", _ARMED_MARGIN)
    return _ARMED_MARGIN


def test_factor_delta_between_none_safety():
    # None on EITHER side ⇒ 0 ⇒ clause inert (the data-side rollout guard).
    assert factor_delta_between(None, 40) == 0
    assert factor_delta_between(100, None) == 0
    assert factor_delta_between(None, None) == 0
    assert factor_delta_between(100, 40) == 60
    assert factor_delta_between(40, 100) == -60


def test_factor_margin_pinned_to_calibrated_value():
    # Consensus-critical constant, calibrated from the 2026-07-03..07 soak:
    # tweak noise +21/+54 (must clear ~2x), real incremental refactor step 122
    # (must pass). Changing this changes WHO WINS ties fleet-wide — bump only
    # in a develop->main promotion window.
    assert _rs.FACTOR_MARGIN == 100


def test_disarmed_never_fires_even_with_huge_delta(monkeypatch):
    # FACTOR_MARGIN=None (the disarm value): natural champion turnover putting
    # measured metrics on BOTH sides must NOT activate the tie-break.
    monkeypatch.setattr(_rs, "FACTOR_MARGIN", None)
    champ = [_r("o1", "100")]
    chal = [_r("o1", "100")]
    res = evaluate_relative_adoption(champ, chal, factor_delta=10**6)
    assert res["adopt"] is False
    # No hint about a rule that cannot fire.
    assert res["reason"] == "matched: no order better or worse"


def test_all_matched_tie_with_factor_margin_dethrones(armed_margin):
    champ = [_r("o1", "100"), _r("o2", "200")]
    chal = [_r("o1", "100"), _r("o2", "200")]
    res = evaluate_relative_adoption(champ, chal, factor_delta=armed_margin)
    assert res["adopt"] is True
    assert res["adopt_via"] == "factorization"
    assert "better factored" in res["reason"]
    assert res["n_matched"] == 2 and res["n_wins"] == 0


def test_all_matched_tie_below_margin_does_not_adopt(armed_margin):
    champ = [_r("o1", "100")]
    chal = [_r("o1", "100")]
    res = evaluate_relative_adoption(champ, chal, factor_delta=armed_margin - 1)
    assert res["adopt"] is False
    assert res["adopt_via"] is None
    # Armed + cleaner-but-not-enough: the miner sees how far off they landed.
    assert "factor delta" in res["reason"]


def test_all_matched_tie_default_delta_unchanged(armed_margin):
    # No factor_delta passed (a call site that predates Phase 2): behavior
    # identical to before even when armed — a tie never adopts.
    champ = [_r("o1", "100")]
    chal = [_r("o1", "100")]
    res = evaluate_relative_adoption(champ, chal)
    assert res["adopt"] is False
    assert res["factor_delta"] == 0
    assert res["reason"] == "matched: no order better or worse"


def test_factor_never_buys_past_a_regression(armed_margin):
    # 1 tolerated regression, everything else matched, huge factor delta:
    # the factor path requires ZERO regressions — no adopt.
    champ = [_r("o1", "10000"), _r("o2", "200")]
    chal = [_r("o1", "9950"), _r("o2", "200")]  # -0.5%: tolerated regression
    res = evaluate_relative_adoption(champ, chal, factor_delta=10**6)
    assert res["n_regressions"] == 1 and res["n_catastrophic"] == 0
    assert res["adopt"] is False


def test_factor_never_buys_past_a_drop(armed_margin):
    champ = [_r("o1", "100"), _r("o2", "200")]
    chal = [_r("o1", "100")]  # drops o2
    res = evaluate_relative_adoption(champ, chal, factor_delta=10**6)
    assert res["n_dropped"] == 1
    assert res["adopt"] is False


def test_factor_never_buys_past_a_catastrophic_cut(armed_margin):
    champ = [_r("o1", "10000")]
    chal = [_r("o1", "9800")]  # -2%: catastrophic
    res = evaluate_relative_adoption(champ, chal, factor_delta=10**6)
    assert res["n_catastrophic"] == 1
    assert res["adopt"] is False


def test_factor_path_requires_true_tie_not_net_zero(armed_margin):
    # 1 win + 1 regression nets to zero but is NOT an all-matched tie: the
    # factor clause must not fire (net-zero-with-noise is the performance
    # rule's domain, and it rejects at DETHRONE_WIN_MARGIN=1).
    champ = [_r("o1", "10000"), _r("o2", "10000")]
    chal = [_r("o1", "10500"), _r("o2", "9950")]  # +5% win, -0.5% regression
    res = evaluate_relative_adoption(champ, chal, factor_delta=10**6)
    assert res["n_wins"] == 1 and res["n_regressions"] == 1
    assert res["adopt"] is False


def test_factor_ignored_when_performance_decides(armed_margin):
    # A clean performance win adopts via performance regardless of a NEGATIVE
    # factor delta (dirtier challenger still wins on orders).
    champ = [_r("o1", "100")]
    chal = [_r("o1", "150")]
    res = evaluate_relative_adoption(champ, chal, factor_delta=-(10**6))
    assert res["adopt"] is True
    assert res["adopt_via"] == "performance"


def test_factor_tie_requires_nonempty_comparison(armed_margin):
    # Two no-data solvers must never adopt on cleanliness alone.
    res = evaluate_relative_adoption([], [], factor_delta=10**6)
    assert res["scenarios_compared"] == 0
    assert res["adopt"] is False


def test_negative_delta_on_tie_does_not_adopt(armed_margin):
    # Challenger DIRTIER than champion on a tie: no adopt.
    champ = [_r("o1", "100")]
    chal = [_r("o1", "100")]
    res = evaluate_relative_adoption(champ, chal, factor_delta=-5)
    assert res["adopt"] is False
    # No misleading "factor delta" hint when the challenger isn't cleaner.
    assert res["reason"] == "matched: no order better or worse"


def test_counts_factorization_dethrone_maps_to_dethrone_verdict(armed_margin):
    champ = [_r("o1", "100"), _r("o2", "200")]
    chal = [_r("o1", "100"), _r("o2", "200")]
    res = evaluate_relative_adoption(champ, chal, factor_delta=armed_margin + 10)
    assert res["adopt"] is True and res["factor_delta"] == armed_margin + 10


def test_relative_counts_factor_delta_maps_to_dethrone(armed_margin):
    # The stored/report counts must agree with the live verdict on a factor win:
    # verdict "dethrone" + adopt_via "factorization", not a misleading "matched".
    champ = [_r("o1", "100"), _r("o2", "200")]
    chal = [_r("o1", "100"), _r("o2", "200")]
    counts = relative_counts(champ, chal, factor_delta=armed_margin + 1)
    assert counts["verdict"] == "dethrone"
    assert counts["adopt_via"] == "factorization"
    assert counts["better"] == 0 and counts["worse"] == 0


def test_relative_reason_phrases_factor_win():
    counts = {
        "verdict": "dethrone", "adopt_via": "factorization",
        "better": 0, "worse": 0, "matched": 5,
        "factorization": {"factor_delta": 2897, "factor_margin": 100},
    }
    reason = relative_reason(counts, candidate_id="sub_x")
    assert "better factored" in reason
    assert "2897" in reason and "100" in reason
    # The absurd performance phrasing must NOT appear on a factor win.
    assert "net better — 0 better" not in reason


# ── GAS-PAR clause (C2 — matched-output-less-gas dethrone, ships DISARMED) ────
#
# GAS_MARGIN_BPS is the arming switch AND the single materiality band. While
# None (the shipped value) the clause is PROVABLY INERT: gas-carrying rows
# produce a verdict bit-identical to gas-less rows (the golden tests below).
# Armed (fleet-wide code promotion) it fires ONLY on a true all-matched
# saturated tie with FULL gas coverage, no matched order materially gassier,
# per-order output parity, and above the collapse floor. Precedence:
# performance > gas > factorization; factorization can never buy a MATERIAL
# gas regression (gas_tie_worse).

_GAS_ARMED_MARGIN = 250

# The additive verdict keys the clause introduced — excluded from the golden
# bit-identity comparison (they are all-zero/False while disarmed).
_GAS_VERDICT_KEYS = {
    "gas_champ_total", "gas_chal_total", "gas_measured_full",
    "gas_unmeasured", "gas_order_worse",
}


@pytest.fixture
def armed_gas(monkeypatch):
    """Arm the GAS-PAR clause the way the fleet-wide promotion would."""
    monkeypatch.setattr(_rs, "GAS_MARGIN_BPS", _GAS_ARMED_MARGIN)
    return _GAS_ARMED_MARGIN


def _gr(intent_id, raw_output, gas=None, *, basis=None, mock=False, error=None):
    """A per_intent-shaped dict row carrying optional metered gas."""
    row = {
        "intent_id": intent_id,
        "raw_output": raw_output,
        "mock_simulation": mock,
        "error": error,
    }
    if gas is not None:
        row["gas_metered"] = gas
        row["gas_basis"] = basis if basis is not None else _rs.GAS_BASIS
    return row


def _strip_gas(verdict):
    return {k: v for k, v in verdict.items() if k not in _GAS_VERDICT_KEYS}


def test_gas_margin_pinned_to_calibrated_value():
    # Consensus-critical constant, calibrated 2026-07-08: same-pin noise is 0
    # (69/172 identical-fork ties to the wei), the Pareto gate filters all
    # cross-pin route-flip noise (0 would-be dethrones at 100/200/300 over 172
    # real comparisons), best genuine improvements ~600-833 bps clear it.
    # Changing this changes WHO WINS ties fleet-wide — bump only in a
    # develop->main promotion window.
    assert _rs.GAS_MARGIN_BPS == 200

# ── DISARMED: the clause must be PROVABLY inert (golden bit-identity) ─────────


def test_disarmed_gas_rows_bit_identical_to_gasless_rows(monkeypatch):
    monkeypatch.setattr(_rs, "GAS_MARGIN_BPS", None)
    # Rows WITH gas keys vs the SAME rows WITHOUT them: the entire verdict
    # (minus the additive gas keys) must be byte-identical — a challenger
    # 50% cheaper on gas changes NOTHING while disarmed.
    champ_gas = [_gr("o1", "100", 100_000), _gr("o2", "200", 100_000)]
    chal_gas = [_gr("o1", "100", 50_000), _gr("o2", "200", 50_000)]
    champ_plain = [{"intent_id": "o1", "raw_output": "100"},
                   {"intent_id": "o2", "raw_output": "200"}]
    chal_plain = [{"intent_id": "o1", "raw_output": "100"},
                  {"intent_id": "o2", "raw_output": "200"}]
    with_gas = evaluate_relative_adoption(champ_gas, chal_gas)
    without_gas = evaluate_relative_adoption(champ_plain, chal_plain)
    assert _strip_gas(with_gas) == _strip_gas(without_gas)
    # The additive keys themselves are inert zeros while disarmed.
    assert with_gas["gas_champ_total"] == 0
    assert with_gas["gas_chal_total"] == 0
    assert with_gas["gas_measured_full"] is False
    assert with_gas["gas_unmeasured"] == 0
    assert with_gas["gas_order_worse"] == 0
    # per_order rows carry NO gas keys while disarmed (pre-gas shape).
    assert all("champ_gas" not in o and "chal_gas" not in o
               for o in with_gas["per_order"])


def test_disarmed_gas_rows_bit_identical_across_verdict_matrix(monkeypatch):
    monkeypatch.setattr(_rs, "GAS_MARGIN_BPS", None)
    # Same golden identity across every verdict shape (win / regression /
    # catastrophic / blind spot / drop / skip), not just the tie.
    pairs = [
        ("win", "100", "120"),
        ("regression", "10000", "9950"),
        ("catastrophic", "10000", "9800"),
        ("blind_spot", None, "500"),
        ("dropped", "300", None),
        ("skip", "0", "0"),
        ("matched", "1000", "1000"),
    ]
    champ_gas = [_gr(f"o_{n}", c, 77_000) for n, c, _x in pairs]
    chal_gas = [_gr(f"o_{n}", x, 33_000) for n, _c, x in pairs]
    champ_plain = [{"intent_id": f"o_{n}", "raw_output": c} for n, c, _x in pairs]
    chal_plain = [{"intent_id": f"o_{n}", "raw_output": x} for n, _c, x in pairs]
    with_gas = evaluate_relative_adoption(champ_gas, chal_gas)
    without_gas = evaluate_relative_adoption(champ_plain, chal_plain)
    assert _strip_gas(with_gas) == _strip_gas(without_gas)


def test_disarmed_never_fires_and_never_hints(armed_margin, monkeypatch):
    monkeypatch.setattr(_rs, "GAS_MARGIN_BPS", None)
    # Disarmed (default) on a tie with a huge gas advantage: no adopt, and the
    # reason must NOT hint at a rule that cannot fire.
    champ = [_gr("o1", "100", 100_000)]
    chal = [_gr("o1", "100", 10_000)]
    res = evaluate_relative_adoption(champ, chal)
    assert res["adopt"] is False
    assert res["adopt_via"] is None
    assert "gas" not in res["reason"]


def test_rows_as_whole_objects_refactor_is_behavior_identical():
    # champ_by/chal_by now keep whole ROWS instead of extracted raw_outputs.
    # Prove zero behavior change: BenchmarkResult objects and equivalent dict
    # rows produce IDENTICAL verdict dicts across existing scenario shapes.
    scenarios = [
        # (champ pairs, chal pairs) — from the existing verdict-matrix tests.
        ([("o1", "100"), ("o2", "200")], [("o1", "120"), ("o2", "250")]),
        ([("o1", "100"), ("o2", "100"), ("o3", "100")],
         [("o1", "200"), ("o2", "200"), ("o3", "50")]),
        ([("o1", "100"), ("o2", None)], [("o1", "100"), ("o2", "500")]),
        ([("o1", "100"), ("o2", "300")], [("o1", "200"), ("o2", None)]),
        ([("o1", "1000"), ("o2", "1000")], [("o1", "999"), ("o2", "1100")]),
        ([("o1", "0")], [("o1", "0")]),
        ([], [("o1", "5")]),
    ]
    for champ_pairs, chal_pairs in scenarios:
        via_objects = evaluate_relative_adoption(
            [_r(i, v) for i, v in champ_pairs],
            [_r(i, v) for i, v in chal_pairs],
        )
        via_dicts = evaluate_relative_adoption(
            [{"intent_id": i, "raw_output": v} for i, v in champ_pairs],
            [{"intent_id": i, "raw_output": v} for i, v in chal_pairs],
        )
        assert via_objects == via_dicts


def test_disarmed_relative_counts_have_no_gas_block(monkeypatch):
    monkeypatch.setattr(_rs, "GAS_MARGIN_BPS", None)
    counts = relative_counts(
        [_gr("o1", "100", 100_000)], [_gr("o1", "100", 50_000)],
    )
    assert "gas" not in counts


# ── ARMED: the tie-break fires only on the exact conjunction ──────────────────


def _tie(champ_gas=(100_000, 100_000), chal_gas=(90_000, 90_000)):
    """An all-matched two-order tie with per-order metered gas."""
    champ = [_gr("o1", "100", champ_gas[0]), _gr("o2", "200", champ_gas[1])]
    chal = [_gr("o1", "100", chal_gas[0]), _gr("o2", "200", chal_gas[1])]
    return champ, chal


def test_armed_tie_cheaper_beyond_margin_adopts_via_gas(armed_gas):
    champ, chal = _tie()  # totals 180k vs 200k: -1000 bps, margin 250
    res = evaluate_relative_adoption(champ, chal)
    assert res["adopt"] is True
    assert res["adopt_via"] == "gas"
    assert res["gas_champ_total"] == 200_000
    assert res["gas_chal_total"] == 180_000
    assert res["gas_measured_full"] is True
    assert res["gas_unmeasured"] == 0
    assert res["gas_order_worse"] == 0
    assert "materially cheaper" in res["reason"]
    assert "gas 180000 vs 200000" in res["reason"]
    assert "-1000 bps" in res["reason"]
    assert f"margin {armed_gas}" in res["reason"]


def test_armed_per_order_rows_carry_display_gas(armed_gas):
    champ, chal = _tie()
    res = evaluate_relative_adoption(champ, chal)
    by_iid = {o["intent_id"]: o for o in res["per_order"]}
    assert by_iid["o1"]["champ_gas"] == 100_000
    assert by_iid["o1"]["chal_gas"] == 90_000


def test_armed_one_unmeasured_matched_row_is_inert(armed_gas):
    # One matched row without gas keys on the challenger side ⇒ NO cherry
    # subsets: the whole clause goes inert (fail-safe toward incumbency).
    champ = [_gr("o1", "100", 100_000), _gr("o2", "200", 100_000)]
    chal = [_gr("o1", "100", 10_000), _gr("o2", "200")]  # o2 unmeasured
    res = evaluate_relative_adoption(champ, chal)
    assert res["adopt"] is False
    assert res["gas_unmeasured"] == 1
    assert res["gas_measured_full"] is False
    assert res["adopt_via"] is None


def test_armed_mock_row_is_inert(armed_gas):
    champ, chal = _tie(chal_gas=(10_000, 10_000))
    chal[1]["mock_simulation"] = True  # fabricated sim gas is meaningless
    res = evaluate_relative_adoption(champ, chal)
    assert res["adopt"] is False
    assert res["gas_unmeasured"] == 1


def test_armed_error_row_is_inert(armed_gas):
    champ, chal = _tie(chal_gas=(10_000, 10_000))
    champ[0]["error"] = "sim hiccup"
    res = evaluate_relative_adoption(champ, chal)
    assert res["adopt"] is False
    assert res["gas_unmeasured"] == 1


def test_armed_basis_mismatch_is_inert(armed_gas):
    # A re-mechanised meter becomes NON-COMPARABLE, never silently mixed.
    champ, chal = _tie(chal_gas=(10_000, 10_000))
    chal[0]["gas_basis"] = "receipt_gas_v0"
    res = evaluate_relative_adoption(champ, chal)
    assert res["adopt"] is False
    assert res["gas_unmeasured"] == 1


def test_armed_per_order_gassier_blocks_despite_cheaper_total(armed_gas):
    # o1 materially gassier (+30%) even though the TOTAL is 15% cheaper —
    # the per-order Pareto arm kills one-big-order masking.
    champ, chal = _tie(chal_gas=(130_000, 40_000))  # total 170k vs 200k
    res = evaluate_relative_adoption(champ, chal)
    assert res["gas_order_worse"] == 1
    assert res["adopt"] is False
    assert res["adopt_via"] is None


def test_armed_output_parity_guard_blocks_band_edge_selloff(armed_gas):
    # Outputs "matched" within the 10-bps band but the challenger shaves
    # -4 bps (> GAS_OUT_GUARD_BPS=2) on o1: a gas win may not buy output down.
    champ = [_gr("o1", "1000000", 100_000), _gr("o2", "200", 100_000)]
    chal = [_gr("o1", "999600", 50_000), _gr("o2", "200", 50_000)]
    res = evaluate_relative_adoption(champ, chal)
    assert res["n_matched"] == 2  # still inside the output noise band
    assert res["adopt"] is False  # but the gas clause refuses to fire
    assert res["adopt_via"] is None


def test_armed_collapse_floor_blocks_and_notes(armed_gas, caplog):
    # >50% total-gas collapse (the stash-plan profile): clause INERT + WARN.
    import logging as _logging
    champ, chal = _tie(chal_gas=(40_000, 39_000))  # 79k vs 200k: >50% collapse
    with caplog.at_level(_logging.WARNING, logger="minotaur_subnet.epoch.relative_scoring"):
        res = evaluate_relative_adoption(champ, chal)
    assert res["adopt"] is False
    assert "(gas collapse >50%: implausible, gas clause inert)" in res["reason"]
    assert any("gas clause INERT" in r.message for r in caplog.records)


def test_armed_no_matched_orders_never_fires(armed_gas):
    # Dropped-only comparison (compared > 0, n_matched == 0): hard veto anyway,
    # and the gas clause cannot fire without a measured tie.
    champ = [_gr("o1", "100", 100_000)]
    chal = []
    res = evaluate_relative_adoption(champ, chal)
    assert res["n_matched"] == 0 and res["n_dropped"] == 1
    assert res["adopt"] is False
    # Degenerate empty-vs-empty: nothing compared, nothing fires.
    res2 = evaluate_relative_adoption([], [])
    assert res2["adopt"] is False and res2["scenarios_compared"] == 0


def test_armed_regression_blocks_gas(armed_gas):
    # One tolerated (-0.5%) regression + cheap gas: the gas clause requires a
    # TRUE all-matched tie — cheap gas never buys past a regression.
    champ = [_gr("o1", "10000", 100_000), _gr("o2", "200", 100_000)]
    chal = [_gr("o1", "9950", 10_000), _gr("o2", "200", 10_000)]
    res = evaluate_relative_adoption(champ, chal)
    assert res["n_regressions"] == 1
    assert res["adopt"] is False


def test_armed_drop_blocks_gas(armed_gas):
    champ = [_gr("o1", "100", 100_000), _gr("o2", "200", 100_000)]
    chal = [_gr("o1", "100", 10_000)]  # drops o2
    res = evaluate_relative_adoption(champ, chal)
    assert res["n_dropped"] == 1
    assert res["adopt"] is False


def test_armed_catastrophic_blocks_gas(armed_gas):
    champ = [_gr("o1", "10000", 100_000)]
    chal = [_gr("o1", "9800", 10_000)]  # -2% cut
    res = evaluate_relative_adoption(champ, chal)
    assert res["n_catastrophic"] == 1
    assert res["adopt"] is False


def test_armed_gas_tie_worse_blocks_factorization(armed_gas, armed_margin):
    # Cleaner code cannot buy a MATERIAL gas regression: total +10% gassier
    # (beyond the 250-bps margin) on a tie blocks the factor clause outright.
    champ, chal = _tie(chal_gas=(110_000, 110_000))  # 220k vs 200k: +1000 bps
    res = evaluate_relative_adoption(champ, chal, factor_delta=10**6)
    assert res["adopt"] is False
    assert res["adopt_via"] is None


def test_armed_per_order_gassier_also_blocks_factorization(armed_gas, armed_margin):
    # gas_tie_worse's second arm: ANY matched order materially gassier blocks
    # the factor clause even when the totals are level.
    champ, chal = _tie(chal_gas=(130_000, 70_000))  # total equal-ish, o1 +30%
    res = evaluate_relative_adoption(champ, chal, factor_delta=10**6)
    assert res["gas_order_worse"] == 1
    assert res["adopt"] is False


def test_armed_unmeasured_gas_leaves_factorization_exactly_as_today(armed_gas, armed_margin):
    # The accepted fail-inert case: gas unmeasured ⇒ gas_tie_worse is False by
    # construction ⇒ the factor clause fires EXACTLY as on the base branch.
    champ = [_r("o1", "100"), _r("o2", "200")]  # no gas fields at all
    chal = [_r("o1", "100"), _r("o2", "200")]
    res = evaluate_relative_adoption(champ, chal, factor_delta=armed_margin)
    assert res["adopt"] is True
    assert res["adopt_via"] == "factorization"


def test_armed_gas_beats_factor_when_both_could_fire(armed_gas, armed_margin):
    # Precedence: performance > gas > factorization.
    champ, chal = _tie()  # gas -1000 bps
    res = evaluate_relative_adoption(champ, chal, factor_delta=10**6)
    assert res["adopt"] is True
    assert res["adopt_via"] == "gas"


def test_armed_performance_beats_gas(armed_gas):
    # A clean output win adopts via performance even with a huge gas edge.
    champ = [_gr("o1", "100", 100_000), _gr("o2", "200", 100_000)]
    chal = [_gr("o1", "150", 10_000), _gr("o2", "200", 10_000)]
    res = evaluate_relative_adoption(champ, chal)
    assert res["adopt"] is True
    assert res["adopt_via"] == "performance"


def test_armed_aggregate_margin_boundary_is_exact(armed_gas):
    # Exactly AT the margin (chal*10000 == champ*(10000-250)) must NOT fire —
    # strict inequality, exact-integer cross-multiply. One notch below fires.
    champ = [_gr("o1", "100", 10_000)]
    at_margin = [_gr("o1", "100", 9_750)]
    res = evaluate_relative_adoption(champ, at_margin)
    assert res["adopt"] is False
    below_margin = [_gr("o1", "100", 9_749)]
    res2 = evaluate_relative_adoption(champ, below_margin)
    assert res2["adopt"] is True and res2["adopt_via"] == "gas"


def test_armed_per_order_worse_boundary_is_exact(armed_gas):
    # o1 exactly AT +250 bps is NOT worse (strict >); the total is cheap
    # enough to adopt. One notch above IS worse and blocks.
    champ, chal = _tie(chal_gas=(102_500, 90_000))  # o1 at the band edge
    res = evaluate_relative_adoption(champ, chal)
    assert res["gas_order_worse"] == 0
    assert res["adopt"] is True and res["adopt_via"] == "gas"
    champ2, chal2 = _tie(chal_gas=(102_510, 89_990))
    res2 = evaluate_relative_adoption(champ2, chal2)
    assert res2["gas_order_worse"] == 1
    assert res2["adopt"] is False


def test_armed_under_margin_hint_names_the_gap(armed_gas):
    # Cheaper-but-not-cheap-enough tie: -100 bps < margin 250 ⇒ display hint.
    champ, chal = _tie(chal_gas=(99_000, 99_000))  # 198k vs 200k
    res = evaluate_relative_adoption(champ, chal)
    assert res["adopt"] is False
    assert res["reason"].startswith("matched: no order better or worse")
    assert f"(gas -100 bps < margin {armed_gas})" in res["reason"]


def test_armed_gassier_tie_has_no_gas_hint(armed_gas):
    # A GASSIER challenger on a tie gets no misleading "gas -X bps" hint.
    champ, chal = _tie(chal_gas=(110_000, 110_000))
    res = evaluate_relative_adoption(champ, chal)
    assert res["adopt"] is False
    assert "gas -" not in res["reason"]


def test_armed_bootstrap_empty_champion_unchanged(armed_gas):
    # Bootstrap ([], chal): all blind-spot covers ⇒ performance adopt, exactly
    # as before the gas clause existed.
    res = evaluate_relative_adoption([], [_gr("o1", "100", 10_000)])
    assert res["adopt"] is True
    assert res["adopt_via"] == "performance"


def test_armed_relative_counts_carry_gas_subdict(armed_gas):
    champ, chal = _tie()
    counts = relative_counts(champ, chal)
    assert counts["verdict"] == "dethrone"
    assert counts["adopt_via"] == "gas"
    assert counts["better"] == 0 and counts["worse"] == 0
    assert counts["gas"] == {
        "champ_total": 200_000,
        "chal_total": 180_000,
        "measured_full": True,
        "unmeasured": 0,
        "order_worse": 0,
    }


def test_relative_reason_phrases_gas_win():
    counts = {
        "verdict": "dethrone", "adopt_via": "gas",
        "better": 0, "worse": 0, "matched": 5,
        "gas": {"champ_total": 200_000, "chal_total": 180_000,
                "gas_margin_bps": 250},
    }
    reason = relative_reason(counts, candidate_id="sub_g")
    assert "materially cheaper" in reason
    assert "180000" in reason and "200000" in reason and "250" in reason
    # The absurd performance phrasing must NOT appear on a gas win.
    assert "net better — 0 better" not in reason


# ── DEADWOOD tie-break (4th and FINAL ladder key — ships ARMED at 2000) ───────
#
# deadwood_delta = champion.unproductive_nodes - challenger.unproductive_nodes
# (PERSISTED, version-guarded — see deadwood_delta_between). UNPRODUCTIVE_MARGIN
# ships ARMED (measurement-grounded calibration, unlike the soak-gated gas
# margin), but the clause is DATA-INERT until records carry same-version
# metrics on both sides (the fields live on the #575 lineage). It fires
# strictly AFTER factorization — only inside a genuine abs(factor_delta) <
# FACTOR_MARGIN region-tie — and never past a performance/gas decision.
# Ladder precedence: performance > gas > factorization > deadwood.

from minotaur_subnet.epoch.relative_scoring import deadwood_delta_between  # noqa: E402

_DW_MARGIN = 2000  # the shipped UNPRODUCTIVE_MARGIN (pinned below)


def test_unproductive_margin_ships_armed_at_2000():
    # THE arming switch, shipped ARMED: calibration is measurement-grounded
    # (~15k dead-node backlog in the canonical repo → ≤ ~7 substantive
    # dethrones at 2000; no salami-slicing). Changing this changes WHO WINS
    # ties fleet-wide — bump only in a develop->main promotion window.
    assert _rs.UNPRODUCTIVE_MARGIN == _DW_MARGIN


def test_deadwood_delta_between_version_guard():
    # Equal, non-None versions ⇒ delegates to the None-safe subtraction.
    assert deadwood_delta_between(15_000, 12_000, 1, 1) == 3_000
    assert deadwood_delta_between(12_000, 15_000, 1, 1) == -3_000
    # None nodes on EITHER side ⇒ 0 (activation-by-data, like factor).
    assert deadwood_delta_between(None, 12_000, 1, 1) == 0
    assert deadwood_delta_between(15_000, None, 1, 1) == 0
    # Version mismatch ⇒ 0 REGARDLESS of node values (cross-version node
    # counts are not comparable — the consensus-critical guard).
    assert deadwood_delta_between(15_000, 0, 1, 2) == 0
    assert deadwood_delta_between(10**6, 0, 2, 1) == 0
    # None version on EITHER side ⇒ 0 regardless of node values.
    assert deadwood_delta_between(15_000, 0, None, 1) == 0
    assert deadwood_delta_between(15_000, 0, 1, None) == 0
    assert deadwood_delta_between(15_000, 0, None, None) == 0


def test_deadwood_default_delta_is_data_inert():
    # ARMED margin but deadwood_delta=0 (the default — i.e. what every caller
    # passes until records carry the metric): every verdict is identical to
    # the pre-deadwood rule across the verdict matrix, and no reason ever
    # mentions the rule.
    scenarios = [
        ([("o1", "100")], [("o1", "150")], True, "performance"),       # win
        ([("o1", "100")], [("o1", "100")], False, None),               # tie
        ([("o1", "10000")], [("o1", "9950")], False, None),            # regression
        ([("o1", "10000")], [("o1", "9800")], False, None),            # catastrophic
        ([("o1", "100"), ("o2", "200")], [("o1", "100")], False, None),  # drop
        ([("o1", None)], [("o1", "500")], True, "performance"),        # blind spot
        ([], [], False, None),                                          # empty
    ]
    for champ_pairs, chal_pairs, want_adopt, want_via in scenarios:
        champ = [_r(i, v) for i, v in champ_pairs]
        chal = [_r(i, v) for i, v in chal_pairs]
        res_default = evaluate_relative_adoption(champ, chal)
        res_zero = evaluate_relative_adoption(champ, chal, deadwood_delta=0)
        assert res_default == res_zero
        assert res_default["adopt"] is want_adopt
        assert res_default["adopt_via"] == want_via
        assert res_default["deadwood_delta"] == 0
        assert "deadwood" not in res_default["reason"]
        assert "dead code" not in res_default["reason"]


def test_deadwood_disarmed_never_fires_even_with_huge_delta(monkeypatch):
    monkeypatch.setattr(_rs, "UNPRODUCTIVE_MARGIN", None)
    champ = [_r("o1", "100")]
    chal = [_r("o1", "100")]
    res = evaluate_relative_adoption(champ, chal, deadwood_delta=10**6)
    assert res["adopt"] is False
    # No hint about a rule that cannot fire.
    assert res["reason"] == "matched: no order better or worse"


def test_all_matched_tie_with_deadwood_margin_dethrones():
    # Region-tied (factor_delta=0 < FACTOR_MARGIN) + delta at the margin.
    champ = [_r("o1", "100"), _r("o2", "200")]
    chal = [_r("o1", "100"), _r("o2", "200")]
    res = evaluate_relative_adoption(champ, chal, deadwood_delta=_DW_MARGIN)
    assert res["adopt"] is True
    assert res["adopt_via"] == "deadwood"
    assert res["deadwood_delta"] == _DW_MARGIN
    assert res["n_matched"] == 2 and res["n_wins"] == 0
    assert res["reason"] == (
        f"dethrone: matched on all 2 order(s), less dead code "
        f"(unproductive -{_DW_MARGIN} nodes >= margin {_DW_MARGIN})"
    )


def test_deadwood_under_margin_does_not_adopt_and_hints():
    # delta 1999 (one under the shipped margin): no adopt, and the armed +
    # cleaner-but-under-margin hint names the gap (mirrors factor/gas hints).
    champ = [_r("o1", "100")]
    chal = [_r("o1", "100")]
    res = evaluate_relative_adoption(champ, chal, deadwood_delta=_DW_MARGIN - 1)
    assert res["adopt"] is False
    assert res["adopt_via"] is None
    assert res["reason"].startswith("matched: no order better or worse")
    assert f"(deadwood delta {_DW_MARGIN - 1} < margin {_DW_MARGIN})" in res["reason"]


def test_deadwood_no_hint_when_delta_not_positive():
    # A dirtier (negative-delta) or unmeasured (0) challenger gets no
    # misleading deadwood hint on a tie.
    champ = [_r("o1", "100")]
    chal = [_r("o1", "100")]
    for delta in (0, -5_000):
        res = evaluate_relative_adoption(champ, chal, deadwood_delta=delta)
        assert res["adopt"] is False
        assert "deadwood" not in res["reason"]


def test_deadwood_blocked_when_factor_decides_for_challenger():
    # abs(factor_delta) >= FACTOR_MARGIN with the challenger BETTER factored:
    # the region race is NOT tied — the FACTOR clause decides (and wins),
    # deadwood never fires. adopt_via must say "factorization".
    champ = [_r("o1", "100")]
    chal = [_r("o1", "100")]
    res = evaluate_relative_adoption(
        champ, chal, factor_delta=_rs.FACTOR_MARGIN, deadwood_delta=10**6,
    )
    assert res["adopt"] is True
    assert res["adopt_via"] == "factorization"
    assert "better factored" in res["reason"]


def test_deadwood_blocked_when_factor_decides_against_challenger():
    # abs(factor_delta) >= FACTOR_MARGIN with the challenger WORSE factored:
    # deadwood must NOT buy back a factor decision — no adopt, however clean.
    champ = [_r("o1", "100")]
    chal = [_r("o1", "100")]
    res = evaluate_relative_adoption(
        champ, chal, factor_delta=-_rs.FACTOR_MARGIN, deadwood_delta=10**6,
    )
    assert res["adopt"] is False
    assert res["adopt_via"] is None


def test_deadwood_fires_across_the_whole_region_tie_band():
    # abs(factor_delta) < FACTOR_MARGIN in BOTH directions is a genuine
    # region-tie: deadwood may fire (a <margin factor edge decides nothing).
    champ = [_r("o1", "100")]
    chal = [_r("o1", "100")]
    for fd in (_rs.FACTOR_MARGIN - 1, 0, -(_rs.FACTOR_MARGIN - 1)):
        res = evaluate_relative_adoption(
            champ, chal, factor_delta=fd, deadwood_delta=_DW_MARGIN,
        )
        assert res["adopt"] is True, fd
        assert res["adopt_via"] == "deadwood", fd


def test_deadwood_region_tied_when_factor_disarmed(monkeypatch):
    # FACTOR_MARGIN=None ⇒ no factor decision exists to defer to ⇒ treat as
    # region-tied: an armed deadwood clause may still fire.
    monkeypatch.setattr(_rs, "FACTOR_MARGIN", None)
    champ = [_r("o1", "100")]
    chal = [_r("o1", "100")]
    res = evaluate_relative_adoption(
        champ, chal, factor_delta=10**6, deadwood_delta=_DW_MARGIN,
    )
    assert res["adopt"] is True
    assert res["adopt_via"] == "deadwood"


def test_deadwood_blocked_by_gas_tie_worse(armed_gas):
    # Less dead code can never buy a MATERIAL gas regression — the same
    # gas_tie_worse guard the factor clause carries. Total +1000 bps gassier
    # on a measured tie blocks deadwood outright.
    champ, chal = _tie(chal_gas=(110_000, 110_000))  # 220k vs 200k
    res = evaluate_relative_adoption(champ, chal, deadwood_delta=10**6)
    assert res["adopt"] is False
    assert res["adopt_via"] is None


def test_deadwood_never_buys_past_a_regression():
    champ = [_r("o1", "10000"), _r("o2", "200")]
    chal = [_r("o1", "9950"), _r("o2", "200")]  # -0.5%: tolerated regression
    res = evaluate_relative_adoption(champ, chal, deadwood_delta=10**6)
    assert res["n_regressions"] == 1
    assert res["adopt"] is False


def test_deadwood_never_buys_past_a_drop():
    champ = [_r("o1", "100"), _r("o2", "200")]
    chal = [_r("o1", "100")]  # drops o2
    res = evaluate_relative_adoption(champ, chal, deadwood_delta=10**6)
    assert res["n_dropped"] == 1
    assert res["adopt"] is False


def test_deadwood_never_buys_past_a_catastrophic_cut():
    champ = [_r("o1", "10000")]
    chal = [_r("o1", "9800")]  # -2%: catastrophic
    res = evaluate_relative_adoption(champ, chal, deadwood_delta=10**6)
    assert res["n_catastrophic"] == 1
    assert res["adopt"] is False


def test_deadwood_requires_nonempty_comparison():
    # Two no-data solvers must never adopt on cleanliness alone.
    res = evaluate_relative_adoption([], [], deadwood_delta=10**6)
    assert res["scenarios_compared"] == 0
    assert res["adopt"] is False


def test_deadwood_requires_matched_orders():
    # Dropped-only comparison (compared > 0 but n_matched == 0): the clause's
    # own n_matched > 0 arm refuses, on top of the outer drop veto.
    champ = [_r("o1", "100")]
    chal = []
    res = evaluate_relative_adoption(champ, chal, deadwood_delta=10**6)
    assert res["n_matched"] == 0 and res["n_dropped"] == 1
    assert res["adopt"] is False


def test_precedence_performance_beats_deadwood():
    champ = [_r("o1", "100")]
    chal = [_r("o1", "150")]
    res = evaluate_relative_adoption(champ, chal, deadwood_delta=10**6)
    assert res["adopt"] is True
    assert res["adopt_via"] == "performance"


def test_precedence_gas_beats_deadwood(armed_gas):
    # A measured cheaper-gas tie with a huge deadwood delta (region-tied):
    # gas outranks deadwood on the ladder.
    champ, chal = _tie()  # -1000 bps gas
    res = evaluate_relative_adoption(champ, chal, deadwood_delta=10**6)
    assert res["adopt"] is True
    assert res["adopt_via"] == "gas"


def test_precedence_full_ladder_performance_gas_factor_deadwood(armed_gas):
    # Every clause's own margin satisfied at once — the ladder resolves
    # top-down. (1) performance present ⇒ performance; (2) no performance,
    # cheap gas + factor edge + deadwood edge ⇒ gas; (3) no gas edge, factor
    # edge + deadwood edge ⇒ factorization (an armed factor decision also
    # un-ties the region, so deadwood COULDN'T fire — verified by adopt_via);
    # (4) only the deadwood edge ⇒ deadwood.
    champ_perf = [_gr("o1", "100", 100_000)]
    chal_perf = [_gr("o1", "150", 10_000)]
    res1 = evaluate_relative_adoption(
        champ_perf, chal_perf, factor_delta=10**6, deadwood_delta=10**6,
    )
    assert res1["adopt_via"] == "performance"

    champ_gas, chal_gas = _tie()  # matched tie, -1000 bps gas
    res2 = evaluate_relative_adoption(
        champ_gas, chal_gas, factor_delta=10**6, deadwood_delta=10**6,
    )
    assert res2["adopt_via"] == "gas"

    champ_tie = [_r("o1", "100")]
    chal_tie = [_r("o1", "100")]
    res3 = evaluate_relative_adoption(
        champ_tie, chal_tie, factor_delta=10**6, deadwood_delta=10**6,
    )
    assert res3["adopt_via"] == "factorization"

    res4 = evaluate_relative_adoption(
        champ_tie, chal_tie, factor_delta=0, deadwood_delta=10**6,
    )
    assert res4["adopt_via"] == "deadwood"


def test_relative_counts_deadwood_delta_maps_to_dethrone():
    # The stored/report counts must agree with the live verdict on a deadwood
    # win: verdict "dethrone" + adopt_via "deadwood", not a misleading
    # "matched".
    champ = [_r("o1", "100"), _r("o2", "200")]
    chal = [_r("o1", "100"), _r("o2", "200")]
    counts = relative_counts(champ, chal, deadwood_delta=_DW_MARGIN + 1)
    assert counts["verdict"] == "dethrone"
    assert counts["adopt_via"] == "deadwood"
    assert counts["better"] == 0 and counts["worse"] == 0


def test_relative_counts_deadwood_default_unchanged():
    # No deadwood_delta passed (a call site that predates the 4th key):
    # a tie stays "matched" even though the margin ships armed.
    champ = [_r("o1", "100")]
    chal = [_r("o1", "100")]
    counts = relative_counts(champ, chal)
    assert counts["verdict"] == "matched"
    assert counts["adopt_via"] is None


def test_relative_reason_phrases_deadwood_win():
    counts = {
        "verdict": "dethrone", "adopt_via": "deadwood",
        "better": 0, "worse": 0, "matched": 5,
        "deadwood": {"deadwood_delta": 2897, "margin": 2000},
    }
    reason = relative_reason(counts, candidate_id="sub_d")
    assert "less dead code" in reason
    assert "2897" in reason and "2000" in reason
    # The absurd performance phrasing must NOT appear on a deadwood win.
    assert "net better — 0 better" not in reason
