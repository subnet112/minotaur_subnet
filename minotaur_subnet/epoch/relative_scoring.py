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
        (counts as a win — the challenger covers a case the champion can't)
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
    scenarios_compared = 0

    for iid in sorted(set(champ_by) | set(chal_by)):
        champ_i = _parse_output(champ_by.get(iid))
        chal_i = _parse_output(chal_by.get(iid))
        champ_has = champ_i is not None and champ_i > MIN_VALID_OUTPUT
        chal_has = chal_i is not None and chal_i > MIN_VALID_OUTPUT
        ratio: float | None = None

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

        per_order.append({
            # champ/chal as EXACT DECIMAL STRINGS so JSON consumers (logs,
            # /health) never lose precision above 2^53. None when absent. `ratio`
            # is DISPLAY-ONLY (the verdict above is exact-integer).
            "intent_id": iid,
            "champ": None if champ_i is None else str(champ_i),
            "chal": None if chal_i is None else str(chal_i),
            "ratio": ratio,
            "verdict": verdict,
        })

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
        "scenarios_compared": scenarios_compared,
    }


# ── count-shape mapping (API report surface) ─────────────────────────────────
#
# The relative rule is authoritative, so the API reports each submission as a
# RELATIVE COUNT vs the current champion instead of a single saturated score.
# These pure helpers map the per-order verdict above onto that count shape and back
# from stored submissions; every API surface emits the relative block always.


def relative_counts(
    champion_results: list[Any],
    challenger_results: list[Any],
    tol_bps: int = RELATIVE_TOL_BPS,
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
    res = evaluate_relative_adoption(champion_results, challenger_results, tol_bps=tol_bps)
    better = res["n_wins"] + res["n_blind_spots"]
    worse = res["n_regressions"] + res["n_dropped"]
    matched = res["n_matched"]
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
    return (
        f"not adopted: {counts['better']} better / {counts['worse']} worse / "
        f"{counts['matched']} matched"
    )
