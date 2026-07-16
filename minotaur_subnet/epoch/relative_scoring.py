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
# guard CANNOT down-grade a cover, no matter what bar callers pass.
#
# ARMED 2026-07-08 at 24h after the Phase-0 soak. The soak fired the first live
# observations (15:16Z: challengers sub_70027ead0699 + sub_2e9c590c1476 each did
# ONE blind_spot_cover that did NOT exceed the incumbent's adoption-time value,
# bar age ~1223s — textbook photocopy-covers the guard now blocks). 24h ≫ the
# treadmill's ~1–3h decay-and-recover cycle, so a stale-then-refreshed cover is
# always caught, while a genuinely improved route (which EXCEEDS the bar) is
# never blocked.
#
# QUORUM SAFETY — why leader-only is correct HERE. The general rule is "arm
# fleet-wide only; a leader-only arm splits the adoption quorum" — because at
# quorum>1 followers run champion_consensus._independent_adopt_vote with THEIR
# constant (disarmed on :stable) while the leader is armed → divergent verdicts →
# no quorum. That hazard is ABSENT at quorum==1 (verified live 2026-07-08: every
# round quorum_required=1), where the leader is the SOLE certifying voter and
# followers trust-adopt its signed champion (FOLLOWER_TRUST_LEADER_QUORUM1) —
# they cast no counted vote to diverge. So arming the leader arms the live rule
# cleanly.
#   HARD INVARIANT: quorum MUST NOT be raised above 1 until the ENTIRE fleet
#   carries BOTH this armed constant AND #578's follower-vote bar wiring
#   (bar_kwargs_from_record in _independent_adopt_vote), i.e. after a
#   develop→main promotion reaches :stable. Raising quorum before that splits
#   consensus on the first contested round. See memory: DON'T raise quorum until
#   fleet-wide.
BLIND_SPOT_BAR_TTL_S: float | None = 24 * 3600.0  # ARMED (24h); None disarms

# Observation-only reference TTL for the Phase-0 soak: how old an adoption-time
# bar may be while still COUNTING a would-be repeat into
# ``n_blind_spot_repeats_observed``. Never affects the verdict.
BLIND_SPOT_BAR_OBSERVE_TTL_S: float = 24 * 3600.0
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
# CONSENSUS-CRITICAL CODE constant (same discipline as FLOOR_BPS/FACTOR_MARGIN
# — never env-read): MERGING AN INT HERE ARMS THE CLAUSE — land it only in a
# develop->main promotion window (leader + followers together).
#
# ARMED = 200, calibrated from the 2026-07-07/08 soak (172 [gas-shadow]
# comparisons over 10.5h vs the 2462-region champion) + a full simulation of
# THIS clause over that data:
#   * same-pin comparison noise is ZERO (69/172 identical-fork comparisons tie
#     to the exact wei), so 200 bps is far above measurement noise;
#   * cross-pin route-flip jitter (median 2,836 bps/order) NEVER fires the
#     clause: the per-order Pareto gate demands no-worse on EVERY matched
#     order, and pin luck always leaves some orders gassier — the simulation
#     produced 0 would-be dethrones at margins 100/200/300 across all 172 real
#     comparisons. Arming is therefore incentive-first (the factorization
#     precedent: signal appears only under enforcement), not churn-tolerant:
#     nothing fires until a genuinely Pareto-dominant, materially-cheaper
#     solver exists.
#   * 200 = bottom of the straw band: the most reachable bounty that is still
#     100x the observed noise floor; observed best-case genuine improvements
#     (~600-833 bps) clear it comfortably.
# Post-arm churn backstops: per-pair dethrone-rate tripwire (#582), sub-tempo
# reigns earn nothing, and None disarms in one constant.
GAS_MARGIN_BPS: int | None = 200  # ARMED, soak-calibrated; None ⇒ disarmed

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

# ── DEADWOOD tie-break (less-dead-code dethrone — the 4th and FINAL key) ──────
#
# UNPRODUCTIVE_MARGIN — THE arming switch AND the materiality margin for the
# deadwood tie-break clause, the last key of the adoption ladder
# (output > gas > factorization > deadwood): on a true all-matched tie whose
# FACTOR race is also genuinely tied (abs(factor_delta) < FACTOR_MARGIN — the
# deadwood clause may never override a factor decision in EITHER direction), a
# challenger whose PERSISTED ``unproductive_nodes`` (dead-code AST mass; see
# the deadwood metric on the screening side) is at least this many nodes
# SMALLER than the champion's dethrones. ``None`` disarms the clause entirely.
#
# SHIPS ARMED at 2000 — unlike GAS_MARGIN_BPS this calibration is
# MEASUREMENT-GROUNDED, not soak-dependent: the canonical champion repo
# carries ~15k dead nodes, so a 2000-node margin allows at most ~7 substantive
# deadwood dethrones before the backlog is gone — no salami-slicing a big
# cleanup into dozens of micro-reigns (and a sub-tempo reign earns nothing
# on-chain anyway). CONSENSUS-CRITICAL CODE constant (same discipline as
# FLOOR_BPS/FACTOR_MARGIN/GAS_MARGIN_BPS — never env-read): MERGING THIS ARMS
# THE CLAUSE — land it only in a develop->main promotion window (leader +
# followers together). Activation is STILL gated by DATA (the exact
# activation-by-data pattern FACTOR_MARGIN/#554 used): the metric fields
# (``Submission.unproductive_nodes`` / ``unproductive_metric_version``) ship
# on the #575 lineage, so until the lineages merge AND records on BOTH sides
# of a comparison carry SAME-VERSION values, :func:`deadwood_delta_between`
# returns 0 and the armed clause is provably inert.
UNPRODUCTIVE_MARGIN: int | None = 2000  # ARMED, measurement-grounded; None ⇒ disarmed

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


def deadwood_delta_between(
    champion_nodes: int | None,
    challenger_nodes: int | None,
    champion_version: int | None,
    challenger_version: int | None,
) -> int:
    """``champion.unproductive_nodes − challenger.unproductive_nodes``, None- AND
    version-safe. Positive = the challenger carries LESS dead code.

    THE one shared metric-version guard for the deadwood tie-break — all three
    consensus threading sites (leader vote, leader decision, follower vote)
    call THIS helper so the guard can never drift between them. Returns 0 —
    rendering the deadwood clause inert — unless BOTH sides carry
    ``unproductive_metric_version`` and the versions are EQUAL: cross-version
    node counts are NOT comparable (any semantic change to the metric bumps
    the version), so a version-mismatched pair must never produce a nonzero
    delta. The None-safe subtraction itself is :func:`factor_delta_between` —
    one arithmetic, deliberately not duplicated.

    Callers pass the PERSISTED ``Submission.unproductive_nodes`` /
    ``unproductive_metric_version`` values (computed once at screening on the
    #575 lineage; read via ``getattr(..., None)`` here so records that predate
    the metric are None ⇒ 0 ⇒ inert — the activation-by-data rollout lever,
    exactly like :func:`factor_delta_between`). Never recompute at decision
    time.
    """
    if champion_version is None or challenger_version is None:
        return 0
    if champion_version != challenger_version:
        return 0
    return factor_delta_between(champion_nodes, challenger_nodes)


def evaluate_relative_adoption(
    champion_results: list[Any],
    challenger_results: list[Any],
    tol_bps: int = RELATIVE_TOL_BPS,
    *,
    champion_bar: dict[str, Any] | None = None,
    bar_age_s: float | None = None,
    factor_delta: int = 0,
    deadwood_delta: int = 0,
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
                         and not gas_tie_worse)      #      (never buys a gas regression)
                     or deadwood_tie_adopt)          # (3d) saturated-tie DEADWOOD dethrone
                                                     #      (only when UNPRODUCTIVE_MARGIN
                                                     #      armed AND the factor race is
                                                     #      itself tied: abs(factor_delta)
                                                     #      < FACTOR_MARGIN)

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
    ``factor_delta`` (keyword-only) is the Phase-2 factorization tie-break input:
    ``champion.max_region_nodes − challenger.max_region_nodes`` from the PERSISTED
    screening metric (see :func:`factor_delta_between` — 0 when either side is
    unmeasured, keeping the clause inert). Branch (3b) fires ONLY on a true
    all-matched tie over a non-empty comparison — a materially better-factored
    challenger (delta >= ``FACTOR_MARGIN``) dethrones an otherwise-identical
    champion; performance always outranks cleanliness everywhere else.

    ``deadwood_delta`` (keyword-only) is the 4th-key deadwood tie-break input:
    ``champion.unproductive_nodes − challenger.unproductive_nodes`` from the
    PERSISTED screening metric, version-guarded (see
    :func:`deadwood_delta_between` — 0 when either side is unmeasured OR the
    metric versions differ, keeping the clause inert). Branch (3d) fires ONLY
    strictly AFTER factorization: on a true all-matched tie whose factor race
    is itself genuinely tied (``abs(factor_delta) < FACTOR_MARGIN``; a
    disarmed ``FACTOR_MARGIN is None`` counts as region-tied), a challenger
    with materially LESS dead code (delta >= ``UNPRODUCTIVE_MARGIN``)
    dethrones. It can never override a factor decision in either direction and
    never buys a material gas regression (same ``gas_tie_worse`` guard the
    factor clause carries).

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
    n_blind_spot_repeats = n_blind_spot_repeats_observed = 0
    scenarios_compared = 0
    guard_armed = BLIND_SPOT_BAR_TTL_S is not None

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
        bar_s: str | None = None
        bar_verdict: str | None = None
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
    # Saturated-tie DEADWOOD dethrone (4th and final ladder key — ships ARMED,
    # inert until records carry same-version metrics on both sides; see
    # deadwood_delta_between): fires strictly AFTER factorization, ONLY inside
    # a genuine factor REGION-TIE — abs(factor_delta) < FACTOR_MARGIN, so
    # deadwood can never override a factor decision in EITHER direction (a
    # better-factored fork wins via the factor clause; a worse-factored one is
    # blocked here). A disarmed FACTOR_MARGIN (None) counts as region-tied —
    # there is no factor decision to defer to. Same true-tie base as gas/
    # factor, same gas_tie_worse guard (less dead code can never buy a
    # material gas regression). All exact-integer, like the rest.
    factor_region_tied = FACTOR_MARGIN is None or abs(factor_delta) < FACTOR_MARGIN
    deadwood_tie_adopt = (
        UNPRODUCTIVE_MARGIN is not None      # armed (fleet-wide promotion only)
        and scenarios_compared > 0
        and net_better == 0
        and n_regressions == 0
        and n_matched > 0
        and factor_region_tied               # never overrides a factor decision
        and not gas_tie_worse                # never buys a gas regression
        and deadwood_delta >= UNPRODUCTIVE_MARGIN
    )
    adopt = (
        n_catastrophic == 0                       # (1) no order cut > 1% (hard floor)
        and n_dropped == 0                        # (2) drop no champion-served order
        and (performance_adopt or gas_tie_adopt or factor_tie_adopt or deadwood_tie_adopt)
        # (3) net better OR gas tie-break OR factor tie-break OR deadwood tie-break
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
    elif adopt and not performance_adopt and factor_tie_adopt:
        reason = (
            f"dethrone: matched on all {n_matched} order(s), better factored "
            f"(max region -{factor_delta} nodes >= margin {FACTOR_MARGIN})"
        )
    elif adopt and not performance_adopt:
        # deadwood_tie_adopt — the only remaining non-performance way in.
        reason = (
            f"dethrone: matched on all {n_matched} order(s), less dead code "
            f"(unproductive -{deadwood_delta} nodes >= margin {UNPRODUCTIVE_MARGIN})"
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
        if (
            UNPRODUCTIVE_MARGIN is not None
            and scenarios_compared > 0
            and 0 < deadwood_delta < UNPRODUCTIVE_MARGIN
        ):
            # Armed + cleaner-but-under-margin tie: tell the miner how far off
            # the deadwood tie-break they landed (display only; a disarmed or
            # unmeasured fleet — delta 0 — never hints at a rule that cannot
            # fire, and an over-margin delta blocked by a factor decision gets
            # no false "< margin" claim).
            reason += f" (deadwood delta {deadwood_delta} < margin {UNPRODUCTIVE_MARGIN})"
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
        # How the adopt (if any) was won — "performance" (net-better), "gas"
        # (matched-output-less-gas tie dethrone), "factorization"
        # (saturated-tie factor dethrone) or "deadwood" (saturated-tie
        # less-dead-code dethrone), in that precedence — the full adoption
        # ladder. factor_tie_adopt WINS over deadwood by chain position (and
        # by construction: an armed factor decision — abs(delta) >= margin —
        # makes deadwood_tie_adopt False via factor_region_tied). None when
        # not adopting. Additive display/observability key; the boolean
        # ``adopt`` stays the single authoritative verdict.
        "adopt_via": (
            "performance" if (adopt and performance_adopt)
            else "gas" if (adopt and gas_tie_adopt)
            else "factorization" if (adopt and factor_tie_adopt)
            else "deadwood" if adopt
            else None
        ),
        "factor_delta": factor_delta,
        "deadwood_delta": deadwood_delta,
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
    factor_delta: int = 0,
    deadwood_delta: int = 0,
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
        factor_delta=factor_delta, deadwood_delta=deadwood_delta,
    )
    return counts_from_verdict(res)


def counts_from_verdict(res: dict[str, Any]) -> dict[str, Any]:
    """Map an :func:`evaluate_relative_adoption` RESULT onto the API count shape
    — PURE, no recomputation.

    :func:`relative_counts` is just ``evaluate_relative_adoption`` followed by
    this mapping. Split out so a caller that ALREADY holds the authoritative
    verdict — the adoption DECISION (``EpochManager._evaluate_per_order_adoption``)
    — can persist the miner-facing badge from the SAME verdict object it decided
    on, instead of a second, independently-recomputed comparison. That is the fix
    for a badge that read "dethrone / OUTPERFORMS" while the round it belongs to
    ended "no change": the display persist and the decision each re-read the
    champion's freshly re-benched rows, which can drift by a few bps between the
    two reads (offloaded re-bench settling, sim jitter), so a boundary order at
    the ``RELATIVE_TOL_BPS`` band flipped win↔matched between them. Mapping the
    decision's own verdict removes the second read entirely.
    """
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
    counts: dict[str, Any] = {
        "better": better,
        "worse": worse,
        "matched": matched,
        "new": new,
        "repeats": repeats,
        "compared": compared,
        "verdict": verdict,
        # How an adopt was won ("performance" | "gas" | "factorization" |
        # "deadwood" | None) — lets the report explain a tie-break dethrone
        # instead of the absurd "net better — 0 better / 0 worse". Additive key.
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
        if counts.get("adopt_via") == "deadwood":
            # A deadwood-tie dethrone has 0 better / 0 worse by definition —
            # the performance phrasing would read as nonsense. Name the real
            # reason (sub-dict attached by _persist_round_relative_counts).
            dz = counts.get("deadwood") or {}
            delta = dz.get("deadwood_delta")
            margin = dz.get("margin")
            return (
                f"adopted{who}: less dead code — matched all "
                f"{counts['matched']} order(s), unproductive "
                f"{delta if delta is not None else '?'} nodes fewer "
                f"(margin {margin if margin is not None else '?'})"
            )
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
