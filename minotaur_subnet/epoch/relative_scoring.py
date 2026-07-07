"""Relative per-order scoring — the SOLE champion-adoption decision rule.

Instead of comparing two aggregate JS scores, compare the challenger and champion
**per order** on the RAW delivered output (the amount the receiver actually got in
the simulation — ground-truth delivery, not a solver claim), and adopt on a
BOUNDED-REGRESSION, NET-BETTER (Pareto-lite) rule: a challenger may adopt while
regressing some orders, but ONLY if (1) no order is cut by more than the hard
``FLOOR_BPS`` (1%) floor, (2) it drops no order the champion serves, and (3) it is
NET better on breadth — wins (incl. blind-spot covers) exceed the tolerated (<=1%)
regressions by at least ``DETHRONE_WIN_MARGIN``. Because raw_output is the simulated
delivered amount, this rule is anti-gaming by construction (the adoption-side
on-chain scoreIntent gate it replaced was redundant with it). (The earlier rule
rejected on ANY per-order regression; this loosens that to "small regressions are
tolerated and netted, but >1% cuts and dropped orders are hard vetoes".)

This is now ALWAYS ON and AUTHORITATIVE — there is no flag and no shadow slot. The
leader (``EpochManager._meets_adoption_criteria``) and every follower
(``champion_consensus._independent_adopt_vote``) route through this one rule, so the
adoption decision is fleet-uniform by construction. The per-order RAW output is
sourced from the LIVE scorer's ``metadata.raw_output`` (the raw-output scorer an
operator PUTs into the live ``js_code`` slot at cutover), threaded onto
``BenchmarkResult.raw_output`` / ``per_intent[*].raw_output``. (Historically this
field was misnamed ``shadow_score`` after the observe-only shadow scorer it came
from; the shadow scorer is gone, so the field now matches the value it carries.
Reads still accept the legacy ``shadow_score`` key for rows persisted before the
rename — see :func:`_raw_output`.)

This module is PURE: a stateless decision function over result objects, duck-typed
on ``intent_id`` / ``raw_output`` (no imports from the heavy harness path), so it
stays trivially testable and import-light.
"""

from __future__ import annotations

from typing import Any


# ── tuning constants ─────────────────────────────────────────────────────────
#
# RELATIVE_TOL is the ONLY "margin"-like knob in the relative rule, and it is NOT
# an improvement threshold (the way the aggregate DETHRONE_MARGIN is). It is a
# symmetric NOISE BAND: a per-order output within ±RELATIVE_TOL of the champion's
# counts as "matched" (neither a win nor a regression), absorbing tiny
# sim-to-sim / rounding jitter so noise alone never reads as a regression that
# would veto adoption. Improvement is decided per order by strict inequality
# OUTSIDE this band — there is no separate "beat by X%" aggregate gate here.
#
# Kept for docs / back-compat only: the verdict path now uses the EXACT-INTEGER
# basis-points form below (no float in the decision).
RELATIVE_TOL = 0.001  # 0.1%

# Integer basis-points form of the noise band — what the verdict actually uses.
# 10 bps == 0.1% == RELATIVE_TOL. The per-order comparison cross-multiplies on
# EXACT INTEGER wei (``chal * 10000`` vs ``champ * (10000 ± RELATIVE_TOL_BPS)``)
# so there is ZERO float in the decision and zero rounding at the BPS boundary —
# the verdict is bit-exact and host-deterministic. This matters once the
# tolerance tightens toward the BPS noise floor and quorum > 1, where an IEEE-754
# rounding difference between hosts could otherwise flip a boundary verdict.
RELATIVE_TOL_BPS = 10

# ── bounded-regression dethrone floor (CODE constants — NEVER env-read) ───────
#
# These two govern the BOUNDED-REGRESSION, NET-BETTER dethrone verdict and are
# CONSENSUS-CRITICAL, so they are fleet-uniform CODE constants — deliberately NOT
# read from the environment. An env-gated dethrone bound would let a validator
# who never sets it decide adoption by a DIFFERENT rule and split consensus; the
# only safe form is a hard-coded constant every validator compiles identically.
#
# FLOOR_BPS — HARD per-order regression cap. An order whose challenger output is
# MORE than this much LESS than the champion's (exact-integer:
# ``chal*10000 < champ*(10000 - FLOOR_BPS)``) is CATASTROPHIC and a HARD VETO on
# adoption regardless of how many other orders win. 100 bps == 1.0%. Regressions
# within this floor (>10-bps noise band, <=1%) are TOLERATED and netted against
# wins. Cross-multiplied on exact integer wei — bit-exact, no float — like
# RELATIVE_TOL_BPS, so a boundary cut on a 1e21-wei order is host-deterministic.
FLOOR_BPS = 100  # 1.0% hard per-order regression cap

# DETHRONE_WIN_MARGIN — net-win requirement. Wins (incl. blind-spot covers) must
# EXCEED the tolerated (<=1%) regressions by at least this many orders, i.e. the
# challenger must be NET better on breadth, not merely break even. 1 == "strictly
# net positive by one order".
DETHRONE_WIN_MARGIN = 1

# BLIND_SPOT_BAR_TTL_S — blind-spot REPEAT guard (the anti-treadmill rule).
#
# Live-observed exploit (2026-07-07): challengers copy the champion byte-for-byte
# and win by "covering" ONE order the incumbent fails on — where the incumbent's
# own earlier reign DELIVERED on that exact order and its canned calldata merely
# went stale (ord_45a3a32b was the winning order on three consecutive dethrones,
# each within ~1% of the value the displaced champion itself delivered at ITS
# adoption). Real market adaptation looks different: the old value becomes
# unreachable, not re-photocopiable.
#
# The guard: a ``blind_spot_cover`` only counts toward dethrone when the
# challenger EXCEEDS the incumbent's ADOPTION-TIME delivered value on that order
# by the noise band — unless that recorded value is older than this TTL (market
# moved on; coverage credit works exactly as before). A cover that merely
# re-delivers what the same order already paid within the TTL is a
# ``blind_spot_repeat``: compared but NEUTRAL (neither win nor regression), so
# it can never be the +1 that dethrones.
#
# CONSENSUS-CRITICAL CODE constant (same discipline as FLOOR_BPS — never
# env-read) and the ARMING SWITCH, mirroring FACTOR_MARGIN: while ``None`` the
# guard CANNOT down-grade a cover, no matter what bar callers pass — Phase 0
# ships the mechanism observe-only (``n_blind_spot_repeats_observed`` in the
# verdict + caller logs). Arm a calibrated TTL (seconds) in a fleet-wide
# promotion ONLY — a leader-only arm would split the adoption quorum. Arming
# also requires the bar to reach the follower vote path
# (champion_consensus._independent_adopt_vote), which passes no bar today.
BLIND_SPOT_BAR_TTL_S: float | None = None  # None ⇒ guard disarmed; arm seconds fleet-wide

# Observation-only reference TTL for the Phase-0 soak: how old an adoption-time
# bar may be while still COUNTING a would-be repeat into
# ``n_blind_spot_repeats_observed``. Never affects the verdict.
BLIND_SPOT_BAR_OBSERVE_TTL_S: float = 24 * 3600.0

# Basis-points denominator for the cross-multiplied comparison.
_BPS = 10000

# A per-order raw output at/below this (EXACT integer wei) is treated as "no
# value delivered" (the order produced nothing for the receiver — a champion
# blind spot or a challenger drop). 0 because the raw-output scorer JS returns
# "0" for a below-min / no-output order.
MIN_VALID_OUTPUT = 0

# DISPLAY-ONLY cap for the per-order ``ratio`` (chal/champ) emitted into logs and
# ``/health``. The verdict NEVER depends on this float; the cap only stops a
# champion that delivered a tiny amount from producing an absurd display number.
SURPLUS_RATIO_CAP = 1000.0


# ── pure per-order decision ──────────────────────────────────────────────────


def _field(item: Any, name: str) -> Any:
    """Read ``name`` off a result whether it is a ``BenchmarkResult`` (attribute)
    or a stored ``per_intent`` dict (key). Lets callers pass either without the
    module importing the heavy harness ``BenchmarkResult`` type."""
    if isinstance(item, dict):
        return item.get(name)
    return getattr(item, name, None)


def _raw_output(item: Any) -> Any:
    """Read a per-order RAW delivered output off a result row.

    Prefers the current ``raw_output`` field; falls back to the legacy
    ``shadow_score`` key/attr for rows persisted (or benched) before the rename, so
    a champion record or in-flight round written by older code still reads. The
    fallback can be dropped once all persisted ``benchmark_details`` have cycled."""
    v = _field(item, "raw_output")
    if v is None:
        v = _field(item, "shadow_score")
    return v


def _parse_output(score: Any) -> int | None:
    """Parse a per-order raw output into EXACT integer wei.

    The canonical carrier is a decimal STRING (the raw-output scorer JS emits
    ``BigInt(...).toString()``), parsed with ``int(...)`` so amounts above 2^53
    keep full precision — no ``float`` anywhere in the decision path. Returns
    ``None`` for ``None`` / ``""`` / non-integer garbage so a bad row is treated
    as "no value", never raised. Bare ``int`` is accepted as-is and a defensive
    ``float`` collapses to its integer wei (legacy callers)."""
    if score is None or isinstance(score, bool):
        return None
    if isinstance(score, int):
        return score
    if isinstance(score, float):
        return int(score) if (score == score and score not in (float("inf"), float("-inf"))) else None
    s = str(score).strip()
    if s == "":
        return None
    try:
        return int(s)
    except (TypeError, ValueError):
        return None


def _display_ratio(chal_i: int, champ_i: int) -> float | None:
    """DISPLAY-ONLY float ratio ``chal/champ`` for logs / ``/health``.

    NEVER used in the verdict (that path is exact-integer). ``None`` when the
    champion delivered nothing (avoids div-by-zero), capped at
    :data:`SURPLUS_RATIO_CAP` so a tiny champion can't blow up the display."""
    if champ_i <= 0:
        return None
    try:
        r = float(chal_i) / float(champ_i)
    except (OverflowError, ZeroDivisionError):
        return None
    if r > SURPLUS_RATIO_CAP:
        r = SURPLUS_RATIO_CAP
    return round(r, 6)


def _has_value(score: Any) -> bool:
    """True when an order delivered a usable output (parsed int > MIN_VALID_OUTPUT)."""
    parsed = _parse_output(score)
    return parsed is not None and parsed > MIN_VALID_OUTPUT


def evaluate_relative_adoption(
    champion_results: list[Any],
    challenger_results: list[Any],
    tol_bps: int = RELATIVE_TOL_BPS,
    *,
    champion_bar: dict[str, Any] | None = None,
    bar_age_s: float | None = None,
) -> dict[str, Any]:
    """Per-order relative adoption verdict — PURE, EXACT-INTEGER.

    Joins champion and challenger results by ``intent_id`` and, for each order,
    compares the RAW delivered output (``raw_output``, an exact decimal wei
    STRING) as INTEGER wei. The verdict cross-multiplies the BPS band so there is
    no ``float`` in the decision and no rounding at the boundary:

      * both deliver value:
          - ``chal*10000 < champ*(10000-tol_bps)`` -> ``regression``
          - ``chal*10000 > champ*(10000+tol_bps)`` -> ``win``
          - else                                   -> ``matched`` (noise band)
          - a ``regression`` cut by MORE than ``FLOOR_BPS`` (1%) is ALSO counted
            ``n_catastrophic`` (the >1% subset of ``n_regressions``)
      * champion blind (no value) & challenger delivers -> ``blind_spot_cover``
        (counts as a win — the challenger covers a case the champion can't) —
        UNLESS the blind-spot REPEAT guard (below) downgrades it to a NEUTRAL
        ``blind_spot_repeat``
      * champion delivers & challenger drops it (no value) -> ``dropped``
        (counted in ``n_dropped`` — a HARD VETO, NOT netted against wins)
      * neither delivers value -> ``skip`` (nothing to compare)

    Adopt on a BOUNDED-REGRESSION, NET-BETTER rule (all exact-integer)::

        adopt = (n_catastrophic == 0)                # (1) no order cut > 1% (hard floor)
                and (n_dropped == 0)                 # (2) drop no champion-served order
                and ((n_wins + n_blind_spots)        # (3) NET better on breadth
                     >= n_regressions + DETHRONE_WIN_MARGIN)

    Blind-spot covers count on the wins side of the net (covering new orders is
    rewarded). A >1% per-order cut (catastrophic) and a dropped order are each a
    HARD VETO that no number of wins can override. When ``adopt`` holds,
    ``n_catastrophic == 0`` so every counted regression is the tolerated
    <=1% kind.

    Blind-spot REPEAT guard (keyword-only, see :data:`BLIND_SPOT_BAR_TTL_S`):
    ``champion_bar`` maps ``intent_id`` -> the incumbent's ADOPTION-TIME
    delivered output (exact wei string/int; build with
    :func:`blind_spot_bar_from_rows`), ``bar_age_s`` is seconds since that
    adoption. A cover whose output does NOT exceed the bar value by ``tol_bps``
    while the bar is fresher than the TTL is a ``blind_spot_repeat`` — compared
    but neutral, never the +1 that dethrones. While the switch is ``None``
    (Phase 0) the verdict is UNCHANGED and would-be repeats are only counted
    into ``n_blind_spot_repeats_observed`` (against
    :data:`BLIND_SPOT_BAR_OBSERVE_TTL_S`). Omitting either kwarg keeps the
    guard fully inert.

    ``champion_results`` / ``challenger_results`` may be ``BenchmarkResult``
    objects (unit path) or ``per_intent`` dicts (report / manager path); both are
    read via :func:`_field`.
    """
    champ_by: dict[str, Any] = {}
    for r in champion_results or []:
        iid = _field(r, "intent_id")
        if iid is not None:
            champ_by[iid] = _raw_output(r)
    chal_by: dict[str, Any] = {}
    for r in challenger_results or []:
        iid = _field(r, "intent_id")
        if iid is not None:
            chal_by[iid] = _raw_output(r)

    per_order: list[dict[str, Any]] = []
    n_wins = n_regressions = n_blind_spots = n_matched = 0
    n_catastrophic = n_dropped = 0
    n_blind_spot_repeats = n_blind_spot_repeats_observed = 0
    scenarios_compared = 0
    guard_armed = BLIND_SPOT_BAR_TTL_S is not None

    for iid in sorted(set(champ_by) | set(chal_by)):
        champ_i = _parse_output(champ_by.get(iid))
        chal_i = _parse_output(chal_by.get(iid))
        champ_has = champ_i is not None and champ_i > MIN_VALID_OUTPUT
        chal_has = chal_i is not None and chal_i > MIN_VALID_OUTPUT
        ratio: float | None = None
        bar_s: str | None = None
        bar_verdict: str | None = None

        if champ_has and chal_has:
            # EXACT-INTEGER verdict — cross-multiply the BPS band, no float.
            ratio = _display_ratio(chal_i, champ_i)  # type: ignore[arg-type]
            if chal_i * _BPS < champ_i * (_BPS - tol_bps):  # type: ignore[operator]
                verdict = "regression"
                n_regressions += 1
                # CATASTROPHIC: cut by MORE than the 1% hard floor. A SUBSET of
                # n_regressions (every catastrophic order is also a regression).
                # Exact-integer cross-multiply, no float — bit-exact at boundary.
                if chal_i * _BPS < champ_i * (_BPS - FLOOR_BPS):  # type: ignore[operator]
                    n_catastrophic += 1
            elif chal_i * _BPS > champ_i * (_BPS + tol_bps):  # type: ignore[operator]
                verdict = "win"
                n_wins += 1
            else:
                verdict = "matched"
                n_matched += 1
            scenarios_compared += 1
        elif chal_has and not champ_has:
            # Blind-spot REPEAT guard: the incumbent's ADOPTION-TIME output on
            # this order is the bar a cover must EXCEED (same exact-integer
            # cross-multiply as a win) while the bar is fresher than the TTL —
            # re-delivering what the same order already paid is a photocopy of
            # the incumbent's own win, not new coverage. Expired / absent bar
            # (market moved on, order never covered) ⇒ full cover credit,
            # exactly as before.
            bar_i = _parse_output((champion_bar or {}).get(iid))
            bar_has = bar_i is not None and bar_i > MIN_VALID_OUTPUT
            exceeds_bar = bar_has and (
                chal_i * _BPS > bar_i * (_BPS + tol_bps)  # type: ignore[operator]
            )
            if bar_has:
                bar_s = str(bar_i)
                bar_verdict = "exceed" if exceeds_bar else "repeat"
            is_repeat = bar_has and not exceeds_bar and bar_age_s is not None
            # Phase-0 observation: what the guard WOULD do at the reference TTL.
            if is_repeat and bar_age_s <= BLIND_SPOT_BAR_OBSERVE_TTL_S:
                n_blind_spot_repeats_observed += 1
            if (
                guard_armed
                and is_repeat
                and bar_age_s <= BLIND_SPOT_BAR_TTL_S  # type: ignore[operator]
            ):
                verdict = "blind_spot_repeat"
                n_blind_spot_repeats += 1
            else:
                verdict = "blind_spot_cover"
                n_blind_spots += 1
            scenarios_compared += 1
        elif champ_has and not chal_has:
            # Challenger produced nothing on an order the champion serves: a
            # DROPPED order. Counted separately (NOT folded into n_regressions)
            # because it is a HARD VETO, never netted against wins.
            verdict = "dropped"
            n_dropped += 1
            scenarios_compared += 1
        else:
            verdict = "skip"

        row: dict[str, Any] = {
            # champ/chal as EXACT DECIMAL STRINGS so JSON consumers (logs,
            # /health) never lose precision above 2^53. None when absent. `ratio`
            # is DISPLAY-ONLY (the verdict above is exact-integer).
            "intent_id": iid,
            "champ": None if champ_i is None else str(champ_i),
            "chal": None if chal_i is None else str(chal_i),
            "ratio": ratio,
            "verdict": verdict,
        }
        if bar_s is not None:
            # Blind-spot orders with a recorded adoption-time bar carry it (exact
            # decimal string) + how the cover graded against it, so the soak is
            # auditable per order from stored reports alone.
            row["bar"] = bar_s
            row["bar_verdict"] = bar_verdict
        per_order.append(row)

    # BOUNDED-REGRESSION, NET-BETTER (Pareto-lite) verdict — all exact-integer.
    net_better = n_wins + n_blind_spots
    adopt = (
        n_catastrophic == 0                                   # (1) no order cut > 1% (hard floor)
        and n_dropped == 0                                    # (2) drop no champion-served order
        and net_better >= n_regressions + DETHRONE_WIN_MARGIN  # (3) net better on breadth
    )
    if n_catastrophic > 0:
        reason = f"reject: {n_catastrophic} order(s) cut >1% (hard floor)"
    elif n_dropped > 0:
        reason = f"reject: dropped {n_dropped} order(s) the champion serves"
    elif adopt:
        reason = (
            f"dethrone: {net_better} better, {n_regressions} minor regression(s) "
            f"within 1% floor (net +{net_better - n_regressions})"
        )
    elif net_better == 0 and n_regressions == 0:
        reason = "matched: no order better or worse"
    else:
        reason = (
            f"reject: net better {net_better} <= regressions {n_regressions} "
            f"+ margin {DETHRONE_WIN_MARGIN}"
        )
    if n_blind_spot_repeats > 0:
        reason += (
            f" ({n_blind_spot_repeats} blind-spot repeat(s) not credited: cover "
            f"does not exceed the incumbent's adoption-time value)"
        )

    return {
        "adopt": adopt,
        "reason": reason,
        "per_order": per_order,
        "n_wins": n_wins,
        "n_regressions": n_regressions,
        "n_catastrophic": n_catastrophic,
        "n_dropped": n_dropped,
        "n_blind_spots": n_blind_spots,
        "n_matched": n_matched,
        # Blind-spot repeat guard: enforced count (0 while BLIND_SPOT_BAR_TTL_S
        # is None) + the Phase-0 observe-only count at the reference TTL.
        "n_blind_spot_repeats": n_blind_spot_repeats,
        "n_blind_spot_repeats_observed": n_blind_spot_repeats_observed,
        "scenarios_compared": scenarios_compared,
    }


# ── count-shape mapping (API report surface) ─────────────────────────────────
#
# The relative rule is authoritative, so the API reports each submission as a
# RELATIVE COUNT vs the current champion instead of a single saturated score.
# These pure helpers map the per-order verdict above onto that count shape and back
# from stored submissions; every API surface emits the relative block always.


def bar_kwargs_from_record(
    record: dict[str, Any] | None,
    incumbent_submission_id: str | None,
    now: float,
) -> dict[str, Any]:
    """``champion_bar``/``bar_age_s`` kwargs from a PERSISTED bar record — PURE.

    ``record`` is the round-store shape ``{"submission_id", "outputs",
    "activated_at"}`` (see ``RoundStore.set_champion_adoption_bar``). Returns
    ``{}`` — guard fully inert — unless the record matches the CURRENT incumbent
    (a stale record from a displaced champion must never gate a cover against
    the wrong bar) and carries non-empty outputs + a positive timestamp. The
    single kwarg-builder for round-store-sourced callers (follower vote, worker
    shadow vote / ranking); the leader's in-memory ``ChampionInfo`` path builds
    the same shape in ``EpochManager._blind_spot_bar_kwargs``.
    """
    if not record or not incumbent_submission_id:
        return {}
    if record.get("submission_id") != incumbent_submission_id:
        return {}
    outputs = record.get("outputs")
    activated_at = record.get("activated_at") or 0.0
    if not isinstance(outputs, dict) or not outputs or not activated_at:
        return {}
    return {
        "champion_bar": outputs,
        "bar_age_s": max(0.0, now - float(activated_at)),
    }


def blind_spot_bar_from_rows(rows: list[Any] | None) -> dict[str, str]:
    """Build the blind-spot repeat bar from per-order rows — PURE.

    Maps ``intent_id`` -> the DELIVERED raw output as an exact decimal wei
    STRING (JSON-safe above 2^53), keeping only rows that delivered value
    (parsed output > ``MIN_VALID_OUTPUT``). Snapshot this over the winner's
    per-order rows AT ADOPTION (its outputs are overwritten by every subsequent
    incumbent re-bench — the whole point is remembering what the order paid
    when it won the crown) and pass it back as ``champion_bar`` with
    ``bar_age_s = now - adopted_at``. Same duck-typed rows as
    :func:`evaluate_relative_adoption`; accepts the legacy ``shadow_score`` key
    via :func:`_raw_output`.
    """
    bar: dict[str, str] = {}
    for r in rows or []:
        iid = _field(r, "intent_id")
        if iid is None:
            continue
        v = _parse_output(_raw_output(r))
        if v is not None and v > MIN_VALID_OUTPUT:
            bar[str(iid)] = str(v)
    return bar


def relative_counts(
    champion_results: list[Any],
    challenger_results: list[Any],
    tol_bps: int = RELATIVE_TOL_BPS,
    *,
    champion_bar: dict[str, Any] | None = None,
    bar_age_s: float | None = None,
) -> dict[str, Any]:
    """Map :func:`evaluate_relative_adoption` onto the API count shape — PURE.

    Reports a challenger as a RELATIVE COUNT vs the champion instead of one
    saturated score:

      * ``better``   = wins + blind-spot covers (orders the challenger delivers
                       MORE on, including ones the champion can't serve at all).
      * ``worse``    = regressions + dropped orders (orders the challenger
                       delivers LESS on, plus champion-served orders it produced
                       nothing for). Both count as worse here.
      * ``matched``  = orders inside the ±``tol_bps`` noise band (neither better nor worse).
      * ``new``      = blind-spot covers — a SUBSET of ``better``: orders the
                       champion delivered nothing on that the challenger covers.
      * ``compared`` = better + worse + matched (orders actually comparable;
                       skips — neither side delivered — are excluded).
      * ``verdict``  = ``dethrone`` when the bounded-regression rule adopts,
                       ``matched`` when nothing is better or worse, else
                       ``behind`` (net-insufficient, a >1% cut, or a dropped order).

    Re-exposes ``per_order`` unchanged. Same duck-typed inputs as
    :func:`evaluate_relative_adoption` (``BenchmarkResult`` objects or stored
    ``per_intent`` dicts).
    """
    res = evaluate_relative_adoption(
        champion_results, challenger_results, tol_bps=tol_bps,
        champion_bar=champion_bar, bar_age_s=bar_age_s,
    )
    better = res["n_wins"] + res["n_blind_spots"]
    worse = res["n_regressions"] + res["n_dropped"]
    # Blind-spot REPEATS (armed guard only; 0 while disarmed) are compared-but-
    # neutral, so they surface as "matched" on the report — the miner delivered
    # on the order but no better than the incumbent's adoption-time value. The
    # separate ``repeats`` count keeps the report honest about WHY the cover
    # earned nothing (rendered by report.py / relative_reason).
    repeats = res.get("n_blind_spot_repeats", 0)
    matched = res["n_matched"] + repeats
    new = res["n_blind_spots"]
    compared = better + worse + matched
    verdict = (
        "dethrone"
        if res["adopt"]
        else ("matched" if (better == 0 and worse == 0) else "behind")
    )
    return {
        "better": better,
        "worse": worse,
        "matched": matched,
        "new": new,
        "repeats": repeats,
        "compared": compared,
        "verdict": verdict,
        "per_order": res["per_order"],
    }


def has_raw_output_rows(rows: list[Any] | None) -> bool:
    """True when at least one per-order row carries a non-None raw output.

    Used to gate the relative block: a submission benched BEFORE the raw-output
    scorer existed has no raw output on any row, so it gets no relative block
    (rather than a misleading all-skip one). Accepts the legacy ``shadow_score``
    key via :func:`_raw_output` for rows persisted before the rename.
    """
    return any(_raw_output(r) is not None for r in rows or [])


def has_delivered_value_rows(rows: list[Any] | None) -> bool:
    """True when at least one per-order row DELIVERED value (parsed raw output > 0).

    The fleet-uniform VALIDITY GATE that replaced the retired scalar
    ``benchmark_score > 0`` check: a submission is valid iff it delivered a usable
    output on >= 1 order. Stricter than :func:`has_raw_output_rows` (which passes a
    solver that emitted only ``"0"`` rows) — a zero-delivery solver is REJECTED, not
    merely scored 0. Every gate site (worker, manager, follower) imports THIS one
    definition so leader/follower parity is guaranteed by construction. Accepts the
    legacy ``shadow_score`` key via :func:`_raw_output`.
    """
    return any(_has_value(_raw_output(r)) for r in rows or [])


def relative_reason(
    counts: dict[str, Any] | None,
    *,
    candidate_id: str | None = None,
) -> str | None:
    """Phrase a round reason in relative vocabulary from a counts dict — PURE.

      * ``dethrone`` -> ``"adopted <id>: net better — N better / M worse
                          (regressions within 1% floor)"``.
      * otherwise    -> ``"not adopted: N better / M worse / K matched"``.

    Phrases the counts honestly under the bounded-regression rule: a dethrone may
    carry within-floor regressions, and a non-dethrone challenger may still be
    better on some orders (just not net-enough / or it tripped a hard veto).
    Returns ``None`` when there are no counts (nothing to phrase). This is a
    DISPLAY-only derivation: callers attach it as a separate ``reason_relative``
    field and never mutate the stored legacy ``abort_reason``.
    """
    if not counts:
        return None
    if counts.get("verdict") == "dethrone":
        who = f" {candidate_id}" if candidate_id else ""
        return (
            f"adopted{who}: net better — {counts['better']} better / "
            f"{counts['worse']} worse (regressions within 1% floor)"
        )
    reason = (
        f"not adopted: {counts['better']} better / {counts['worse']} worse / "
        f"{counts['matched']} matched"
    )
    repeats = counts.get("repeats") or 0
    if repeats:
        reason += (
            f" ({repeats} blind-spot repeat(s) not credited — cover must exceed "
            f"the incumbent's adoption-time value on that order)"
        )
    return reason
