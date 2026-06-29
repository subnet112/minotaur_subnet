"""Pure, reusable champion-adoption decision rule.

This module holds the per-validator champion-adoption rule body extracted from
``EpochManager._should_adopt`` as a PURE function so the *exact same* decision
can be made by the leader and, independently, by followers (champion-consensus
re-validation) without duplicating the logic.

The function mirrors the rule body EXACTLY — everything AFTER the
adoption-disabled / same-submission / shadow preamble (those stay in
``EpochManager`` because they touch instance state / logging side effects).

These knobs are FLEET-UNIFORM CODE CONSTANTS, not per-validator env reads
(see ``_AdoptRuleConfig`` below). They are consensus-relevant: the leader
(``EpochManager._should_adopt``) and every follower
(``champion_consensus._independent_adopt_vote``) route through THIS pure rule,
so a divergent value on any single node flips its verdict and breaks the
adoption quorum — exactly the split the round-anchored pin (#246/#247) and
``DETHRONE_MARGIN`` already foreclose by being code. So they are constants here
(the single source of truth), NOT envs a 3rd-party validator would never set:

    PER_APP_MIN_SCORE      = 0.3   per-app score floor (current rule)
    MAX_APP_REGRESSION     = 0.10  per-app non-regression / catastrophe veto
    ONCHAIN_MAX_REGRESSION = 0.10  on-chain HARD-VETO non-regression band
    ONCHAIN_FLOOR_BPS      = None  on-chain admission floor (off)

It returns ``(adopt, reason)`` where ``reason`` is a human-readable string in
every branch (for logging by the caller).

The current (default) rule keeps the JS score as the RANKING signal but adds an
unfakeable on-chain HARD VETO (``_evaluate_onchain_gate``): a challenger whose
benchmark plans revert / run on a fabricated mock (on-chain score None) or
regress vs the champion's on-chain ``scoreIntent`` cannot be adopted on JS score
alone. The veto runs only when there IS a champion (after the genesis early
return). It is symmetric across leader + followers because they all route
through this one pure function.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


# ── Fleet-uniform adoption-rule constants — THE SINGLE SOURCE OF TRUTH ──────────
#
# Consensus-relevant config: leader + every follower route through this pure rule,
# so these MUST be identical fleet-wide or the adoption quorum splits (a divergent
# floor/band/rule on one node flips its verdict). They are therefore hardcoded CODE
# (propagated via :stable / redeploy), NOT per-validator envs a 3rd-party validator
# would never set — the same reason ``DETHRONE_MARGIN`` (manager.py),
# ``CHAMPION_MINER_WEIGHT_FRACTION``, ``EPOCH_SECONDS`` and the round-anchored fork
# pin (consensus/round_anchor.py) are constants. Change here = move the bar fleet-
# wide in one place.
PER_APP_MIN_SCORE: float = 0.3          # per-app sanity floor (current rule)
MAX_APP_REGRESSION: float = 0.10        # per-app JS non-regression / catastrophe veto
ONCHAIN_MAX_REGRESSION: float = 0.10    # on-chain HARD-VETO non-regression band
ONCHAIN_FLOOR_BPS: "int | None" = None  # on-chain admission floor (off; one value, not per-node)


@dataclass(frozen=True)
class _AdoptRuleConfig:
    """Bundle of the fleet-uniform adoption thresholds.

    Production ALWAYS uses :data:`DEFAULT_ADOPT_RULE_CONFIG` (the constants above),
    so leader + followers are byte-identical. The ONLY override path is the offline
    scoring-lab parameter sweep, which constructs its own config and passes it
    explicitly — it never mutates process env, so it cannot leak a non-default value
    into a live validator's rule.
    """

    per_app_min_score: float = PER_APP_MIN_SCORE
    max_app_regression: float = MAX_APP_REGRESSION
    onchain_max_regression: float = ONCHAIN_MAX_REGRESSION
    onchain_floor_bps: "int | None" = ONCHAIN_FLOOR_BPS


DEFAULT_ADOPT_RULE_CONFIG = _AdoptRuleConfig()


def _onchain_pass(scores: list, floor: int) -> tuple[bool, "int | None", int]:
    """all_pass, min_bps, n_missing — a champion-covered app must clear the floor on
    every scenario (ported from scoring_lab/stages.py)."""
    present = [s for s in scores if s is not None]
    n_missing = sum(1 for s in scores if s is None)
    all_pass = n_missing == 0 and all(s >= floor for s in present)
    return all_pass, (min(present) if present else None), n_missing


def _app_onchain_mean(scores: list) -> "float | None":
    """Mean on-chain scoreIntent BPS over present scenarios for an app — the unfakeable
    output-quality signal, independent of the gas-weighted JS score."""
    present = [s for s in scores if s is not None]
    return (sum(present) / len(present)) if present else None


def _evaluate_onchain_gate(
    *,
    challenger_scorecard: dict | None,
    champion_scorecard: dict | None,
    onchain_regression: float,
    floor: "int | None",
) -> tuple[bool, "str | None"]:
    """On-chain HARD GATE for the current rule: for every app where the CHAMPION
    produced a present (non-None) on-chain mean, the challenger must also produce a
    valid on-chain score that doesn't regress beyond ``onchain_regression``. Apps
    with no on-chain signal for the champion are skipped (not every app swaps). This
    is a VETO, not a re-ranking — JS still ranks. None AND 0 both fail (None = no
    valid execution; 0 = below the champion's by the no-regression band)."""
    champ_card = champion_scorecard or {}
    chal_card = challenger_scorecard or {}
    champ_oc = champ_card.get("app_onchain", {})
    chal_oc = chal_card.get("app_onchain", {})
    for app_id in champ_card.get("app_scores", {}).keys():
        champ_mean = _app_onchain_mean(champ_oc.get(app_id, []))
        if champ_mean is None:
            continue  # champion has no on-chain signal for this app -> not gated
        cco = _app_onchain_mean(chal_oc.get(app_id, []))
        if cco is None:
            return False, f"Challenger produced no valid on-chain score for {app_id} (champion did)"
        # partial-revert guard: champion all-present but challenger has any missing scenario
        _, _, champ_missing = _onchain_pass(champ_oc.get(app_id, []), 0)
        _, _, chal_missing = _onchain_pass(chal_oc.get(app_id, []), 0)
        if champ_missing == 0 and chal_missing > 0:
            return False, f"Partial on-chain revert on {app_id} (champion fully executed)"
        if champ_mean > 0 and cco < champ_mean * (1 - onchain_regression):
            return False, (
                f"Challenger on-chain regresses on {app_id}: "
                f"{champ_mean:.0f} -> {cco:.0f} BPS (max drop {onchain_regression*100:.0f}%)"
            )
        if floor is not None:
            all_pass, min_bps, n_missing = _onchain_pass(chal_oc.get(app_id, []), floor)
            if not all_pass:
                return False, f"Challenger on-chain floor fail on {app_id} (min={min_bps} missing={n_missing})"
    return True, None


def evaluate_adoption(
    *,
    challenger_score: float,
    champion_score: float,
    challenger_scorecard: dict | None,
    champion_scorecard: dict | None,
    dethrone_margin: float,
    has_champion: bool,
    config: _AdoptRuleConfig = DEFAULT_ADOPT_RULE_CONFIG,
) -> tuple[bool, str]:
    """Pure per-validator adoption decision -> (adopt, reason).

    Mirrors ``EpochManager._should_adopt``'s rule body EXACTLY — everything
    AFTER the adoption-disabled / same-submission / shadow preamble (those stay
    in ``EpochManager``). The thresholds come from ``config`` — production passes
    nothing, so they are the fleet-uniform CODE CONSTANTS
    (:data:`DEFAULT_ADOPT_RULE_CONFIG`: ``PER_APP_MIN_SCORE``,
    ``MAX_APP_REGRESSION``, ``ONCHAIN_MAX_REGRESSION``, ``ONCHAIN_FLOOR_BPS``);
    only the offline scoring-lab passes an explicit override.

    Enforces:
    1. Per-app minimum (PER_APP_MIN_SCORE, default 0.3) — the absolute sanity floor.
    2. Per-app non-regression: no champion-covered app may be dropped, and
       no app the champion solves may drop more than MAX_APP_REGRESSION (10%)
    3. On-chain HARD VETO: for every app the champion scores on-chain, the
       challenger's plans must validly EXECUTE on-chain (not revert / mock ->
       None) and not regress beyond ONCHAIN_MAX_REGRESSION. A mock-simulation
       scorecard is rejected outright. JS still ranks; this only vetoes. Runs
       only with a champion (after the genesis early return), so it is symmetric
       across leader + followers.
    4. Global improvement over the champion by the dethrone margin (default 1%)

    There is intentionally NO absolute global-score floor: the global JS score is
    a RELATIVE measure (anchored on the champion reference, ~0.5 == "matches the
    reference"), so an absolute floor on it is meaningless and was mis-calibrated
    above the achievable ceiling — it blocked every adoption. The meaningful
    absolute floor lives per-order in the on-chain ``scoreIntent`` gate
    (``on_chain_threshold``: "the user got at least their minimum outcome"), which
    is a separate, per-execution check — not an adoption criterion.

    ``has_champion`` is True iff there is a current champion submission_id
    (i.e. ``bool(self._champion.submission_id)``).
    """
    per_app_min = config.per_app_min_score
    max_regression = config.max_app_regression

    # Belt-and-suspenders: a challenger benchmarked on the fabricated mock
    # simulator (require_real_sim off + no Anvil) has unfakeable on-chain scores
    # that mean nothing — refuse it outright in the current rule. run_benchmark
    # already fails closed when require_real_sim is set; this catches the case
    # where a mock slipped through and the scorecard recorded it.
    if (challenger_scorecard or {}).get("mock_simulation_count", 0) > 0:
        return False, "Challenger benchmarked on a fabricated mock simulation"

    # 2. Per-app minimum — every app must be above floor
    if challenger_scorecard:
        for app_id, app_score in challenger_scorecard.get("app_scores", {}).items():
            if app_score < per_app_min:
                return False, (
                    f"Challenger app {app_id} score {app_score:.3f} below "
                    f"per-app minimum {per_app_min:.3f}"
                )

    # No current champion — adopt if above minimums
    if not has_champion:
        return True, "Adopt: no current champion, above minimums"

    # 3. Per-app non-regression. app_scores is keyed by bare app_id (see
    #    BenchmarkWorker._build_scorecard), so this compares true per-app
    #    quality, not per-scenario. A challenger may neither drop an app the
    #    champion covers nor regress > MAX_APP_REGRESSION on any app it solves.
    if challenger_scorecard and champion_scorecard:
        inc_apps = champion_scorecard.get("app_scores", {})
        ch_apps = challenger_scorecard.get("app_scores", {})
        for app_id, inc_score in inc_apps.items():
            ch_score = ch_apps.get(app_id)
            # (a) Dropping a champion-covered app is a hard regression.
            if ch_score is None:
                return False, f"Challenger drops app {app_id} that the champion covers"
            # (b) A non-positive incumbent baseline gives no meaningful drop
            #     threshold; the real per-app floor arrives with the on-chain
            #     gate (design doc P2). Skip only the magnitude check here.
            if inc_score <= 0:
                continue
            if ch_score < inc_score * (1 - max_regression):
                return False, (
                    f"Challenger regresses on {app_id}: {inc_score:.3f} -> {ch_score:.3f} "
                    f"(max drop {max_regression * 100:.0f}%)"
                )

    # On-chain HARD GATE: the challenger's plans must validly EXECUTE on-chain (not
    # revert / mock) and not regress vs the champion, for apps the champion scores
    # on-chain. Keeps JS as the ranking signal; this only vetoes.
    onchain_regression = config.onchain_max_regression
    onchain_floor = config.onchain_floor_bps
    oc_ok, oc_reason = _evaluate_onchain_gate(
        challenger_scorecard=challenger_scorecard,
        champion_scorecard=champion_scorecard,
        onchain_regression=onchain_regression,
        floor=onchain_floor,
    )
    if not oc_ok:
        return False, oc_reason

    # 3. Global improvement over the champion's actual (freshly re-benchmarked)
    #    score by the dethrone margin. This is the operative gate: the challenger
    #    must beat the current champion by the margin — the champion score is the
    #    moving baseline (NOT floored), so a genuinely-better challenger adopts
    #    even when both sit below any absolute number.
    required = champion_score * (1 + dethrone_margin)
    if challenger_score <= champion_score:
        return False, (
            f"Challenger score {challenger_score:.3f} not better than "
            f"incumbent {champion_score:.3f}"
        )
    if challenger_score < required:
        return False, (
            f"Challenger score {challenger_score:.3f} doesn't meet dethrone margin "
            f"(need {required:.3f})"
        )
    return True, f"Adopt: challenger {challenger_score:.3f} beats incumbent {champion_score:.3f}"
