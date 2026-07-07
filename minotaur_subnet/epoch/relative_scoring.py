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

import logging
from typing import Any

logger = logging.getLogger(__name__)


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

# FACTOR_MARGIN — saturated-tie FACTORIZATION dethrone margin (Phase 2 of the
# factorization rule; see harness/screening.max_region_nodes). When a challenger
# ties the champion on EVERY compared order (all matched — zero wins, zero
# regressions, zero blind spots, zero drops), it may still dethrone iff its
# ``max_region_nodes`` is at least this many AST nodes SMALLER than the
# champion's. Output saturates on this subnet, so exact ties are the common
# case; this turns them into continuous downward pressure on the champion's
# worst-entangled region instead of pure incumbency. Guards:
#   * fires ONLY on a true all-matched tie — a single regression, drop, or
#     catastrophic cut disables it (cleanliness never buys past performance);
#   * well above 1 so a cosmetic few-node diff can't churn the throne;
#   * each factor-win strictly LOWERS the champion's max region, so the process
#     is monotone and self-terminating.
# CONSENSUS-CRITICAL CODE constant (same discipline as FLOOR_BPS above — never
# env-read) and the Phase-2 ARMING SWITCH, mirroring MAX_REGION_NODES in
# screening: while None the tie-break clause CANNOT fire, no matter what
# factor_delta callers pass. The explicit switch matters — None-safety of
# factor_delta_between alone is NOT one: natural champion turnover puts
# measured records on BOTH sides (observed live 2026-07-06: the standing
# champion carried a measured value with no backfill ever run).
#
# CALIBRATED = 100 from the 2026-07-03..07 Phase-0 soak (leader, ~2900
# measured submissions): champion-fork tweak noise moved the worst region by
# +21/+54 nodes (margin must clear ~2x that), while the one GENUINE
# incremental refactor step observed in the wild was 122 nodes (1334 -> 1212;
# a 200 margin would have blocked a real improvement from winning a tie).
# 100 sits between. MERGING THIS ARMS THE TIE-BREAK — land it only in a
# develop->main promotion window (leader + followers together; a leader-only
# deploy splits a factor-tie vote at quorum > 1), followed by the champion
# reattest so follower stores carry the champion's metric. None disarms.
FACTOR_MARGIN: int | None = 100  # ARMED, soak-calibrated; None ⇒ disarmed

# ── GAS-PAR clause (matched-output-less-gas dethrone — ships DISARMED) ───────
#
# GAS_MARGIN_BPS — THE arming switch AND the single materiality band for the
# gas tie-break clause: it is the per-order no-worse band, the aggregate-win
# margin, and the gas_tie_worse margin. While ``None`` (the shipped value) all
# gas classification is SKIPPED — every gas counter is 0/False and verdicts
# are bit-identical to the pre-gas rule (proved by the golden tests). To be
# SOAK-CALIBRATED before arming (working straw 200-300; the soak number wins,
# exactly as the 07-03..07 soak picked FACTOR_MARGIN=100). CONSENSUS-CRITICAL
# CODE constant (same discipline as FLOOR_BPS/FACTOR_MARGIN — never env-read):
# MERGING AN INT HERE ARMS THE CLAUSE — land it only in a develop->main
# promotion window (leader + followers together), after the C1 gas plumbing is
# fleet-deployed and the anvil version is fleet-pinned.
GAS_MARGIN_BPS: int | None = None  # DISARMED; soak-calibrated int arms it

# GAS_OUT_GUARD_BPS — per-order OUTPUT-parity band inside a gas dethrone: a
# gas win may not shave delivered output by more than this many bps on any
# matched order (absorbs the measured ~2-bps cross-host output residual; the
# soak may tighten it to 0). Closes the directed band-edge sell-off: a gas
# dethrone can never ratchet delivered output down 10 bps per reign.
GAS_OUT_GUARD_BPS = 2

# GAS_COLLAPSE_FLOOR — implausibility tripwire: the challenger's total gas
# must satisfy ``chal_total * GAS_COLLAPSE_FLOOR >= champ_total`` (i.e. a
# >50% total-gas collapse — the stash-plan profile — renders the clause INERT
# and logs a WARN instead of dethroning).
GAS_COLLAPSE_FLOOR = 2

# GAS_BASIS — measurement-version tag. A row's gas is comparable ONLY when it
# carries this exact basis on BOTH sides, so any future re-mechanism of the
# meter becomes NON-COMPARABLE (clause inert) instead of silently mixed.
GAS_BASIS = "scoreintent_prerefund_v1"

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


def _gas_of(row: Any) -> int | None:
    """Read a per-order METERED GAS off a result row — ``None`` unless eligible.

    A row's gas is comparable ONLY when ALL of:
      * not a mock simulation (fabricated gas is meaningless),
      * no per-order error,
      * ``gas_basis`` equals :data:`GAS_BASIS` exactly (measurement-version
        pinning — a re-mechanised meter becomes non-comparable, never mixed),
      * ``gas_metered`` parses (``int(...)`` — int or decimal str) to an int > 0.

    ``None`` ⇒ the order is UNMEASURED and the gas clause goes inert for the
    whole comparison (fail-safe toward incumbency, no cherry subsets).
    Reverted/dropped rows never reach this (their raw_output is "0"/None so
    they are not output-matched), so revert-receipt gas is structurally
    excluded; contract-less manual-fallback sims carry no ``gas_basis`` ⇒
    ineligible ⇒ inert. ``row`` may be a ``BenchmarkResult`` or a stored
    ``per_intent`` dict (read via :func:`_field`)."""
    if row is None:
        return None
    if _field(row, "mock_simulation"):
        return None
    if _field(row, "error") is not None:
        return None
    if _field(row, "gas_basis") != GAS_BASIS:
        return None
    raw = _field(row, "gas_metered")
    if raw is None or isinstance(raw, bool):
        return None
    try:
        g = int(raw)
    except (TypeError, ValueError):
        return None
    return g if g > 0 else None


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


def factor_delta_between(
    champion_nodes: int | None,
    challenger_nodes: int | None,
) -> int:
    """``champion.max_region_nodes − challenger.max_region_nodes``, None-safe.

    Positive = the challenger's worst region is SMALLER (better factored). Returns
    0 — rendering the factor tie-break clause inert — when EITHER side's metric is
    missing: a champion adopted before the metric existed, or a record from a
    fleet member not yet carrying the field. That None-safety is the ROLLOUT
    LEVER: the clause activates fleet-wide only once the standing champion's
    value is backfilled, never piecemeal via a leader-only deploy.

    Callers pass the PERSISTED ``Submission.max_region_nodes`` values (computed
    once at screening) — never recompute the metric at decision time, so a
    cross-CPython AST difference can never split consensus.
    """
    if champion_nodes is None or challenger_nodes is None:
        return 0
    return int(champion_nodes) - int(challenger_nodes)


def evaluate_relative_adoption(
    champion_results: list[Any],
    challenger_results: list[Any],
    tol_bps: int = RELATIVE_TOL_BPS,
    *,
    factor_delta: int = 0,
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
                and (((n_wins + n_blind_spots)       # (3a) NET better on breadth, OR
                      >= n_regressions + DETHRONE_WIN_MARGIN)
                     or gas_tie_adopt                # (3b) saturated-tie GAS-PAR dethrone
                                                     #      (only when GAS_MARGIN_BPS armed;
                                                     #      full coverage, per-order Pareto,
                                                     #      output parity, collapse floor)
                     or (FACTOR_MARGIN is not None   # (3c) saturated-tie factorization
                         and scenarios_compared > 0  #      (only when Phase-2 is armed)
                         and n_wins + n_blind_spots == 0
                         and n_regressions == 0
                         and factor_delta >= FACTOR_MARGIN
                         and not gas_tie_worse))     #      (never buys a gas regression)

    Blind-spot covers count on the wins side of the net (covering new orders is
    rewarded). A >1% per-order cut (catastrophic) and a dropped order are each a
    HARD VETO that no number of wins can override. When ``adopt`` holds,
    ``n_catastrophic == 0`` so every counted regression is the tolerated
    <=1% kind.

    ``factor_delta`` (keyword-only) is the Phase-2 factorization tie-break input:
    ``champion.max_region_nodes − challenger.max_region_nodes`` from the PERSISTED
    screening metric (see :func:`factor_delta_between` — 0 when either side is
    unmeasured, keeping the clause inert). Branch (3b) fires ONLY on a true
    all-matched tie over a non-empty comparison — a materially better-factored
    challenger (delta >= ``FACTOR_MARGIN``) dethrones an otherwise-identical
    champion; performance always outranks cleanliness everywhere else.

    ``champion_results`` / ``challenger_results`` may be ``BenchmarkResult``
    objects (unit path) or ``per_intent`` dicts (report / manager path); both are
    read via :func:`_field`.
    """
    # champ_by/chal_by keep the WHOLE row (not just its raw_output) so the gas
    # clause can read row-carried gas fields on the matched branch. Output reads
    # go via ``_parse_output(_raw_output(row))`` — ``_raw_output(None)`` is
    # already None, so this refactor is behavior-identical for the output rule.
    champ_by: dict[str, Any] = {}
    for r in champion_results or []:
        iid = _field(r, "intent_id")
        if iid is not None:
            champ_by[iid] = r
    chal_by: dict[str, Any] = {}
    for r in challenger_results or []:
        iid = _field(r, "intent_id")
        if iid is not None:
            chal_by[iid] = r

    per_order: list[dict[str, Any]] = []
    n_wins = n_regressions = n_blind_spots = n_matched = 0
    n_catastrophic = n_dropped = 0
    scenarios_compared = 0

    # GAS-PAR accumulators — touched ONLY when the clause is armed
    # (GAS_MARGIN_BPS is not None); disarmed they stay at these zero values so
    # the verdict (and its additive gas keys) is bit-identical to the pre-gas
    # rule. All exact-integer, like the rest of the verdict.
    gas_armed = GAS_MARGIN_BPS is not None
    champ_gas_total = 0
    chal_gas_total = 0
    gas_unmeasured = 0
    gas_order_worse = 0
    gas_out_ok = True

    for iid in sorted(set(champ_by) | set(chal_by)):
        champ_row = champ_by.get(iid)
        chal_row = chal_by.get(iid)
        champ_i = _parse_output(_raw_output(champ_row))
        chal_i = _parse_output(_raw_output(chal_row))
        champ_has = champ_i is not None and champ_i > MIN_VALID_OUTPUT
        chal_has = chal_i is not None and chal_i > MIN_VALID_OUTPUT
        ratio: float | None = None
        champ_gas: int | None = None
        chal_gas: int | None = None

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
                if gas_armed:
                    # Gas is classified on the MATCHED branch only — the gas
                    # clause is a tie-break, never a performance substitute.
                    cg = _gas_of(champ_row)
                    xg = _gas_of(chal_row)
                    champ_gas, chal_gas = cg, xg
                    if cg is None or xg is None:
                        gas_unmeasured += 1
                    else:
                        champ_gas_total += cg
                        chal_gas_total += xg
                        # Per-order no-worse band: materially gassier on ANY
                        # matched order blocks a gas win (kills one-big-order
                        # masking). Exact-integer cross-multiply.
                        if xg * _BPS > cg * (_BPS + GAS_MARGIN_BPS):
                            gas_order_worse += 1
                        # Output-parity guard: a gas win may not shave the
                        # delivered output by more than GAS_OUT_GUARD_BPS on
                        # any matched order (band-edge sell-off closed).
                        gas_out_ok = gas_out_ok and (
                            chal_i * _BPS >= champ_i * (_BPS - GAS_OUT_GUARD_BPS)  # type: ignore[operator]
                        )
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
        if gas_armed:
            # DISPLAY-ONLY per-order gas (None when ineligible/unmatched);
            # totals are ≤ tens of millions — far below 2^53, JSON-safe as
            # ints. Emitted only when armed so disarmed per_order rows stay
            # byte-identical to the pre-gas shape.
            row["champ_gas"] = champ_gas
            row["chal_gas"] = chal_gas
        per_order.append(row)

    # BOUNDED-REGRESSION, NET-BETTER (Pareto-lite) verdict — all exact-integer.
    net_better = n_wins + n_blind_spots
    performance_adopt = net_better >= n_regressions + DETHRONE_WIN_MARGIN
    # GAS-PAR clause (ships DISARMED — GAS_MARGIN_BPS is None): on a true
    # all-matched saturated tie a challenger delivering the SAME outputs on
    # materially less TOTAL gas dethrones — but only with FULL measurement
    # coverage (every matched order measured on BOTH sides; any unmeasured /
    # mock / errored / basis-mismatched matched row ⇒ clause inert, fail-safe
    # toward incumbency), no matched order materially gassier (per-order
    # Pareto — kills one-big-order masking), per-order output parity within
    # GAS_OUT_GUARD_BPS (no band-edge sell-off), and above the collapse floor
    # (a >50% total-gas collapse is implausible ⇒ inert + WARN). All
    # exact-integer cross-multiplies — zero float, bit-exact at boundaries.
    gas_measured_full = (
        GAS_MARGIN_BPS is not None and n_matched > 0 and gas_unmeasured == 0
    )
    gas_tie_adopt = (
        GAS_MARGIN_BPS is not None           # armed (fleet-wide promotion only)
        and scenarios_compared > 0
        and net_better == 0
        and n_regressions == 0
        and n_matched > 0
        and gas_measured_full                # full coverage, no cherry subsets
        and gas_order_worse == 0             # per-order Pareto: no order gassier
        and chal_gas_total * _BPS < champ_gas_total * (_BPS - GAS_MARGIN_BPS)
        and gas_out_ok                       # output parity on every matched order
        and chal_gas_total * GAS_COLLAPSE_FLOOR >= champ_gas_total
    )
    # gas_tie_worse — a MATERIAL gas regression on a measured tie (total
    # gassier beyond margin, or any single matched order materially gassier).
    # Factorization may break only GENUINE gas-ties: cleanliness can never buy
    # a material gas regression. False by construction when unmeasured or
    # disarmed, so the armed factor clause stays bit-identical to the pre-gas
    # rule through the whole rollout.
    gas_tie_worse = (
        GAS_MARGIN_BPS is not None
        and gas_measured_full
        and (
            chal_gas_total * _BPS > champ_gas_total * (_BPS + GAS_MARGIN_BPS)
            or gas_order_worse > 0
        )
    )
    # Saturated-tie FACTORIZATION dethrone (Phase 2): a true all-matched tie over
    # a non-empty comparison, broken toward the materially better-factored tree.
    # scenarios_compared > 0 blocks the degenerate empty-vs-empty case (two
    # no-data solvers must never adopt on cleanliness alone). With net_better and
    # n_regressions both 0 (and n_dropped == 0 outer), compared > 0 implies every
    # compared order MATCHED. All exact-integer, like the rest of the verdict.
    factor_tie_adopt = (
        FACTOR_MARGIN is not None            # Phase-2 armed (fleet-wide promotion)
        and scenarios_compared > 0
        and net_better == 0
        and n_regressions == 0
        and factor_delta >= FACTOR_MARGIN
        and not gas_tie_worse                # cleanliness can't buy a gas regression
    )
    adopt = (
        n_catastrophic == 0                       # (1) no order cut > 1% (hard floor)
        and n_dropped == 0                        # (2) drop no champion-served order
        and (performance_adopt or gas_tie_adopt or factor_tie_adopt)
        # (3) net better OR gas tie-break OR factor tie-break
    )
    if n_catastrophic > 0:
        reason = f"reject: {n_catastrophic} order(s) cut >1% (hard floor)"
    elif n_dropped > 0:
        reason = f"reject: dropped {n_dropped} order(s) the champion serves"
    elif adopt and not performance_adopt and gas_tie_adopt:
        # Display-only integer division for the bps delta (the verdict above
        # compared exact cross-multiplies; this is just the human number).
        reason = (
            f"dethrone: matched on all {n_matched} order(s), materially cheaper "
            f"(gas {chal_gas_total} vs {champ_gas_total}, "
            f"-{(champ_gas_total - chal_gas_total) * _BPS // champ_gas_total} bps "
            f">= margin {GAS_MARGIN_BPS})"
        )
    elif adopt and not performance_adopt:
        reason = (
            f"dethrone: matched on all {n_matched} order(s), better factored "
            f"(max region -{factor_delta} nodes >= margin {FACTOR_MARGIN})"
        )
    elif adopt:
        reason = (
            f"dethrone: {net_better} better, {n_regressions} minor regression(s) "
            f"within 1% floor (net +{net_better - n_regressions})"
        )
    elif net_better == 0 and n_regressions == 0:
        reason = "matched: no order better or worse"
        if FACTOR_MARGIN is not None and scenarios_compared > 0 and factor_delta > 0:
            # A cleaner-but-not-clean-enough tie: tell the miner how far off the
            # factorization tie-break they landed (display only; armed-only so a
            # disarmed fleet never hints at a rule that cannot fire).
            reason += f" (factor delta {factor_delta} < margin {FACTOR_MARGIN})"
        if GAS_MARGIN_BPS is not None and gas_measured_full:
            # Armed + fully measured tie: mirror the factor hint (display only;
            # a disarmed or unmeasured fleet never hints at a rule that cannot
            # fire).
            if chal_gas_total * GAS_COLLAPSE_FLOOR < champ_gas_total:
                # Implausible >50% total-gas collapse — the stash-plan profile.
                # The clause deliberately goes INERT (fail-safe toward
                # incumbency) and the alert makes the inertness auditable.
                reason += " (gas collapse >50%: implausible, gas clause inert)"
                logger.warning(
                    "[gas-clause] total-gas collapse >50%% on an all-matched tie "
                    "(chal %d vs champ %d over %d matched order(s)) — implausible, "
                    "gas clause INERT",
                    chal_gas_total, champ_gas_total, n_matched,
                )
            elif chal_gas_total < champ_gas_total and not (
                chal_gas_total * _BPS < champ_gas_total * (_BPS - GAS_MARGIN_BPS)
            ):
                # Cheaper-but-not-cheap-enough: tell the miner how far off the
                # gas tie-break they landed (display-only integer division).
                d = (champ_gas_total - chal_gas_total) * _BPS // champ_gas_total
                reason += f" (gas -{d} bps < margin {GAS_MARGIN_BPS})"
    else:
        reason = (
            f"reject: net better {net_better} <= regressions {n_regressions} "
            f"+ margin {DETHRONE_WIN_MARGIN}"
        )

    return {
        "adopt": adopt,
        "reason": reason,
        # How the adopt (if any) was won — "performance" (net-better), "gas"
        # (matched-output-less-gas tie dethrone) or "factorization"
        # (saturated-tie factor dethrone), in that precedence. None when not
        # adopting. Additive display/observability key; the boolean ``adopt``
        # stays the single authoritative verdict.
        "adopt_via": (
            "performance" if (adopt and performance_adopt)
            else "gas" if (adopt and gas_tie_adopt)
            else "factorization" if adopt
            else None
        ),
        "factor_delta": factor_delta,
        # GAS-PAR additive keys — all-zero/False while disarmed (golden-tested).
        "gas_champ_total": champ_gas_total,
        "gas_chal_total": chal_gas_total,
        "gas_measured_full": gas_measured_full,
        "gas_unmeasured": gas_unmeasured,
        "gas_order_worse": gas_order_worse,
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
    *,
    factor_delta: int = 0,
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
        champion_results, challenger_results, tol_bps=tol_bps, factor_delta=factor_delta,
    )
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
    counts: dict[str, Any] = {
        "better": better,
        "worse": worse,
        "matched": matched,
        "new": new,
        "compared": compared,
        "verdict": verdict,
        # How an adopt was won ("performance" | "gas" | "factorization" |
        # None) — lets the report explain a tie-break dethrone instead of the
        # absurd "net better — 0 better / 0 worse". Additive key.
        "adopt_via": res["adopt_via"],
        "per_order": res["per_order"],
    }
    if GAS_MARGIN_BPS is not None:
        # GAS-PAR pass-through (ARMED only — disarmed counts stay byte-identical
        # to the pre-gas shape): the verdict's gas totals/coverage, so the
        # persisted relative block and the report can explain a gas verdict.
        counts["gas"] = {
            "champ_total": res["gas_champ_total"],
            "chal_total": res["gas_chal_total"],
            "measured_full": res["gas_measured_full"],
            "unmeasured": res["gas_unmeasured"],
            "order_worse": res["gas_order_worse"],
        }
    return counts


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
        if counts.get("adopt_via") == "gas":
            # A gas-tie dethrone has 0 better / 0 worse by definition — the
            # performance phrasing would read as nonsense. Name the real reason.
            g = counts.get("gas") or {}
            chal_t = g.get("chal_total")
            champ_t = g.get("champ_total")
            margin = g.get("gas_margin_bps")
            return (
                f"adopted{who}: materially cheaper — matched all "
                f"{counts['matched']} order(s), total gas "
                f"{chal_t if chal_t is not None else '?'} vs "
                f"{champ_t if champ_t is not None else '?'} "
                f"(margin {margin if margin is not None else '?'} bps)"
            )
        if counts.get("adopt_via") == "factorization":
            # A factor-tie dethrone has 0 better / 0 worse by definition — the
            # performance phrasing would read as nonsense. Name the real reason.
            fz = counts.get("factorization") or {}
            delta = fz.get("factor_delta")
            margin = fz.get("factor_margin")
            return (
                f"adopted{who}: better factored — matched all "
                f"{counts['matched']} order(s), max region "
                f"{delta if delta is not None else '?'} nodes smaller "
                f"(margin {margin if margin is not None else '?'})"
            )
        return (
            f"adopted{who}: net better — {counts['better']} better / "
            f"{counts['worse']} worse (regressions within 1% floor)"
        )
    return (
        f"not adopted: {counts['better']} better / {counts['worse']} worse / "
        f"{counts['matched']} matched"
    )
