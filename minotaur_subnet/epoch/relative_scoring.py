"""Relative per-order scoring — SHADOW (observe-only) decision path.

A NEW way to decide adoption: instead of comparing two aggregate JS scores,
compare the challenger and champion **per order** on the RAW delivered output
(the amount the receiver actually got), and adopt on a BOUNDED-REGRESSION,
NET-BETTER (Pareto-lite) rule: a challenger may adopt while regressing some
orders, but ONLY if (1) no order is cut by more than the hard ``FLOOR_BPS`` (1%)
floor, (2) it drops no order the champion serves, and (3) it is NET better on
breadth — wins (incl. blind-spot covers) exceed the tolerated (<=1%) regressions
by at least ``DETHRONE_WIN_MARGIN``. (The earlier rule rejected on ANY per-order
regression; this loosens that to "small regressions are tolerated and netted,
but >1% cuts and dropped orders are hard vetoes".)

**SHADOW by default.** Two independent gates, read at call time (no restart):

  * ``relative_scoring_shadow_enabled()`` — **DEFAULT ON.** When on, the
    validator ALSO computes this relative decision every round and LOGS it
    beside the real one. It changes nothing about live behaviour; it is pure
    extra computation + logging so the fleet can compare the two rules on real
    challengers before trusting the new one.
  * ``relative_scoring_active()`` — **DEFAULT OFF.** When on, the relative
    decision becomes AUTHORITATIVE (replaces the live aggregate verdict). This
    is the one-line flip after the shadow data proves the rule out.

The split mirrors the existing ``SHADOW_DETERMINISM`` observe-then-activate
pattern: ship the new rule dark, watch it agree, then flip it on fleet-wide.

This module is PURE: env reads + a stateless decision function. No I/O, no
benchmark compute, no imports from the heavy harness path (it duck-types the
result objects on ``intent_id`` / ``shadow_score``), so it stays trivially
testable and import-light.
"""

from __future__ import annotations

import os
from typing import Any

# Explicit disable values for the DEFAULT-ON shadow gate. Anything else
# (including unset, empty, or garbage) is treated as ENABLED — same convention
# as ``round_anchor.round_anchored_pin_enabled``: a typo can never silently turn
# the (harmless, observe-only) shadow computation off.
_GATE_OFF_VALUES = frozenset({"0", "false", "no", "off"})

# Explicit enable values for the DEFAULT-OFF activation gate. Only these turn the
# relative decision authoritative; anything else (including unset) stays OFF, so
# the live aggregate rule keeps deciding until an operator deliberately flips it.
_GATE_ON_VALUES = frozenset({"1", "true", "yes", "on"})


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

# A per-order shadow output at/below this (EXACT integer wei) is treated as "no
# value delivered" (the order produced nothing for the receiver — a champion
# blind spot or a challenger drop). 0 because the raw-output shadow JS returns
# "0" for a below-min / no-output order.
MIN_VALID_OUTPUT = 0

# DISPLAY-ONLY cap for the per-order ``ratio`` (chal/champ) emitted into logs and
# ``/health``. The verdict NEVER depends on this float; the cap only stops a
# champion that delivered a tiny amount from producing an absurd display number.
SURPLUS_RATIO_CAP = 1000.0


def relative_scoring_shadow_enabled() -> bool:
    """Observe-only relative-scoring shadow gate. **DEFAULT ON.**

    Purpose: while live, ALSO compute the relative per-order adoption decision +
    the raw-output shadow score, and LOG them beside the real decision, so the
    fleet can compare the new rule against the live one on real challengers
    WITHOUT affecting any adoption, weight, or consensus output.

    Consensus-relevance: NONE while it is shadow — it only adds computation and
    log lines; the live scoring and live adoption verdict are byte-for-byte
    unchanged. (It becomes consensus-relevant only once
    ``relative_scoring_active()`` is flipped on, which is a separate gate.)

    Read at call time (``os.environ.get``) so it can be toggled via compose
    without a code change or restart, exactly like the other fleet gates.

    Emergency override only: set ``RELATIVE_SCORING_SHADOW`` to one of
    ``{0, false, no, off}`` (case-insensitive) to disable the extra computation
    fleet-wide. Unset / any other value = enabled. This is the single place the
    default lives.
    """
    raw = os.environ.get("RELATIVE_SCORING_SHADOW")
    if raw is None:
        return True
    return raw.strip().lower() not in _GATE_OFF_VALUES


def relative_scoring_active() -> bool:
    """Make the relative per-order decision AUTHORITATIVE. **DEFAULT OFF.**

    Purpose: the one-line activation switch. When on, the shadow relative verdict
    REPLACES the live aggregate ``evaluate_adoption`` verdict as the adopt/reject
    decision for the round. Until then the relative rule is observe-only.

    Consensus-relevance: HIGH once enabled — it changes which challenger is
    adopted, so (like every other adoption-rule constant) it must be flipped
    fleet-uniformly. It defaults OFF so a single validator can never diverge by
    accident; turning it on is a deliberate, coordinated operator action.

    Read at call time (``os.environ.get``) so the flip needs no restart.

    Enable only by setting ``RELATIVE_SCORING_ENABLED`` to one of
    ``{1, true, yes, on}`` (case-insensitive). Unset / any other value = OFF.
    """
    raw = os.environ.get("RELATIVE_SCORING_ENABLED")
    if raw is None:
        return False
    return raw.strip().lower() in _GATE_ON_VALUES


# ── pure per-order decision ──────────────────────────────────────────────────


def _field(item: Any, name: str) -> Any:
    """Read ``name`` off a result whether it is a ``BenchmarkResult`` (attribute)
    or a stored ``per_intent`` dict (key). Lets callers pass either without the
    module importing the heavy harness ``BenchmarkResult`` type."""
    if isinstance(item, dict):
        return item.get(name)
    return getattr(item, name, None)


def _parse_output(score: Any) -> int | None:
    """Parse a per-order shadow output into EXACT integer wei.

    The canonical carrier is a decimal STRING (the raw-output shadow JS emits
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
    compares the RAW delivered output (``shadow_score``, an exact decimal wei
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
            champ_by[iid] = _field(r, "shadow_score")
    chal_by: dict[str, Any] = {}
    for r in challenger_results or []:
        iid = _field(r, "intent_id")
        if iid is not None:
            chal_by[iid] = _field(r, "shadow_score")

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
# When the relative rule is AUTHORITATIVE the API stops reporting a single
# saturated score and instead reports each submission as a RELATIVE COUNT vs the
# current champion. These pure helpers map the per-order verdict above onto that
# count shape and back from stored submissions, so every API surface can flip on
# the SAME ``relative_scoring_active()`` gate.


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


def _submission_per_intent(submission: Any) -> list[Any]:
    """Per-order rows (``benchmark_details.per_intent``) off a submission, or []."""
    details = getattr(submission, "benchmark_details", None) or {}
    rows = details.get("per_intent") if isinstance(details, dict) else None
    return rows if isinstance(rows, list) else []


def has_shadow_rows(rows: list[Any] | None) -> bool:
    """True when at least one per-order row carries a non-None ``shadow_score``.

    Used to gate the relative block: a submission benched BEFORE the shadow path
    existed has no ``shadow_score`` rows, so it gets no relative block (rather than
    a misleading all-skip one).
    """
    return any(_field(r, "shadow_score") is not None for r in rows or [])


def relative_counts_for_submissions(
    challenger_submission: Any,
    champion_submission: Any,
    tol_bps: int = RELATIVE_TOL_BPS,
) -> dict[str, Any] | None:
    """Relative counts for a CHALLENGER submission vs the CHAMPION submission.

    Reads each submission's ``benchmark_details.per_intent`` shadow_score rows and
    delegates to :func:`relative_counts`. Returns ``None`` (graceful omit, never an
    error) when either side has no shadow_score rows — so a submission scored
    before shadow existed simply carries no relative block.
    """
    if challenger_submission is None or champion_submission is None:
        return None
    champ_rows = _submission_per_intent(champion_submission)
    chal_rows = _submission_per_intent(challenger_submission)
    if not has_shadow_rows(champ_rows) or not has_shadow_rows(chal_rows):
        return None
    return relative_counts(champ_rows, chal_rows, tol_bps=tol_bps)


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
