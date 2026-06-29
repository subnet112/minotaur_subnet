"""Relative per-order scoring — the SOLE champion-adoption decision rule.

Instead of comparing two aggregate JS scores, compare the challenger and champion
**per order** on the RAW delivered output (the amount the receiver actually got in
the simulation — ground-truth delivery, not a solver claim), and adopt only when
the challenger beats or matches the champion on every order AND strictly wins at
least one. Because raw_output is the simulated delivered amount, this rule is
anti-gaming by construction (the adoption-side on-chain scoreIntent gate it
replaced was redundant with it).

This is now ALWAYS ON and AUTHORITATIVE — there is no flag and no shadow slot. The
leader (``EpochManager._meets_adoption_criteria``) and every follower
(``champion_consensus._independent_adopt_vote``) route through this one rule, so the
adoption decision is fleet-uniform by construction. The per-order RAW output is
sourced from the LIVE scorer's ``metadata.raw_output`` (the raw-output scorer an
operator PUTs into the live ``js_code`` slot at cutover), threaded onto
``BenchmarkResult.shadow_score`` / ``per_intent[*].shadow_score`` (field name kept
to avoid rippling the API counts shape).

This module is PURE: a stateless decision function over result objects, duck-typed
on ``intent_id`` / ``shadow_score`` (no imports from the heavy harness path), so it
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
      * champion blind (no value) & challenger delivers -> ``blind_spot_cover``
        (counts as a win — the challenger covers a case the champion can't)
      * champion delivers & challenger drops it (no value) -> ``dropped``
        (counts as a regression — the challenger lost a case the champion had)
      * neither delivers value -> ``skip`` (nothing to compare)

    Adopt iff NO order regressed/dropped AND at least one order is a win or a
    blind-spot cover. (A challenger that merely ties everywhere does not adopt —
    same "must strictly improve something" spirit as the dethrone margin, but
    enforced per order on real delivered output rather than on the blended JS
    score.)

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
            verdict = "dropped"
            n_regressions += 1
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

    adopt = (n_regressions == 0) and (n_wins + n_blind_spots >= 1)
    if n_regressions > 0:
        reason = f"reject: {n_regressions} regression(s)/drop(s)"
    elif n_wins + n_blind_spots == 0:
        reason = "reject: no win (challenger only matched the champion)"
    else:
        reason = (
            f"adopt: {n_wins} win(s), {n_blind_spots} blind-spot cover(s), "
            f"0 regressions over {scenarios_compared} order(s)"
        )

    return {
        "adopt": adopt,
        "reason": reason,
        "per_order": per_order,
        "n_wins": n_wins,
        "n_regressions": n_regressions,
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
      * ``worse``    = regressions. The decision impl FOLDS a dropped order
                       (champion delivered, challenger produced nothing) into
                       ``n_regressions``, so a dropped order correctly counts as
                       worse here.
      * ``matched``  = orders inside the ±``tol_bps`` noise band (neither better nor worse).
      * ``new``      = blind-spot covers — a SUBSET of ``better``: orders the
                       champion delivered nothing on that the challenger covers.
      * ``compared`` = better + worse + matched (orders actually comparable;
                       skips — neither side delivered — are excluded).
      * ``verdict``  = ``dethrone`` when the relative rule adopts, ``matched`` when
                       nothing is better or worse, else ``behind`` (any regression
                       is ``behind`` regardless of how many wins it has).

    Re-exposes ``per_order`` unchanged. Same duck-typed inputs as
    :func:`evaluate_relative_adoption` (``BenchmarkResult`` objects or stored
    ``per_intent`` dicts).
    """
    res = evaluate_relative_adoption(champion_results, challenger_results, tol_bps=tol_bps)
    better = res["n_wins"] + res["n_blind_spots"]
    worse = res["n_regressions"]
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


def has_shadow_rows(rows: list[Any] | None) -> bool:
    """True when at least one per-order row carries a non-None ``shadow_score``.

    Used to gate the relative block: a submission benched BEFORE the shadow path
    existed has no ``shadow_score`` rows, so it gets no relative block (rather than
    a misleading all-skip one).
    """
    return any(_field(r, "shadow_score") is not None for r in rows or [])


def relative_reason(
    counts: dict[str, Any] | None,
    *,
    candidate_id: str | None = None,
) -> str | None:
    """Phrase a round reason in relative vocabulary from a counts dict — PURE.

      * ``dethrone`` -> ``"adopted <id>: better on N order(s), 0 regressions"``.
      * otherwise    -> ``"no challenger delivered more on any order
                          (N matched / M regressed)"``.

    Returns ``None`` when there are no counts (nothing to phrase). This is a
    DISPLAY-only derivation: callers attach it as a separate ``reason_relative``
    field and never mutate the stored legacy ``abort_reason``.
    """
    if not counts:
        return None
    if counts.get("verdict") == "dethrone":
        who = f" {candidate_id}" if candidate_id else ""
        return f"adopted{who}: better on {counts['better']} order(s), 0 regressions"
    return (
        "no challenger delivered more on any order "
        f"({counts['matched']} matched / {counts['worse']} regressed)"
    )
