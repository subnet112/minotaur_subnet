"""Relative per-order scoring — SHADOW (observe-only) decision path.

A NEW way to decide adoption: instead of comparing two aggregate JS scores,
compare the challenger and champion **per order** on the RAW delivered output
(the amount the receiver actually got), and adopt only when the challenger
beats or matches the champion on every order AND strictly wins at least one.

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
