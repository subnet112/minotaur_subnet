"""Pure, reusable champion-adoption decision rule.

This module holds the per-validator champion-adoption rule body extracted from
``EpochManager._should_adopt`` / ``_should_adopt_onchain`` as a PURE function so
the *exact same* decision can be made by the leader and, independently, by
followers (champion-consensus re-validation) without duplicating the logic.

The function mirrors the rule body EXACTLY — everything AFTER the
adoption-disabled / same-submission / shadow preamble (those stay in
``EpochManager`` because they touch instance state / logging side effects). It
reads the same environment knobs at call time:

    PER_APP_MIN_SCORE    (default 0.3)  per-app score floor (current rule)
    MAX_APP_REGRESSION   (default 0.10) per-app non-regression / catastrophe veto
    ONCHAIN_FLOOR_BPS    (unset = off)  on-chain admission floor (p2oc rule)
    ADOPT_RULE           (current|p2oc) dispatch between the two rules

It returns ``(adopt, reason)`` where ``reason`` is a human-readable string in
every branch (for logging by the caller).
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)


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


def _evaluate_onchain(
    *,
    challenger_scorecard: dict | None,
    champion_scorecard: dict | None,
    dethrone_margin: float,
    has_champion: bool,
) -> tuple[bool, str]:
    """On-chain co-ranked dethrone (ADOPT_RULE=p2oc) — port of the lab's
    P2OcAdoptRule. Ranks the dethrone on the unfakeable on-chain OUTPUT surplus
    (Δ scoreIntent BPS / 10000 > dethrone margin) instead of the gas-polluted JS
    score, so a more-output-but-more-gas challenger (which the JS path rejects) is
    adoptable, while a gas-gaming challenger that delivers less is not. Keeps the
    vetoes: per-app sanity floor (PER_APP_MIN_SCORE), on-chain admission floor
    (ONCHAIN_FLOOR_BPS), app-coverage drop, and a JS no-catastrophic-regression
    guard (MAX_APP_REGRESSION). The same-submission short-circuit in
    ``_should_adopt`` already ran (the absolute global-min floor was removed —
    adoption is governed by the dethrone margin + the per-app floor, not an
    absolute global number).
    """
    # Per-app sanity floor — applies to the genesis (no-champion) case too, so a
    # garbage first champion can't self-adopt under p2oc (mirrors the current rule).
    per_app_min = float(os.environ.get("PER_APP_MIN_SCORE", "0.3"))
    for app_id, app_score in (challenger_scorecard or {}).get("app_scores", {}).items():
        if app_score < per_app_min:
            return False, (
                f"Challenger app {app_id} score {app_score:.3f} below "
                f"per-app minimum {per_app_min:.3f}"
            )

    if not has_champion:
        return True, "p2oc adopt: no champion yet (genesis)"  # no champion yet (genesis)

    champ_card = champion_scorecard or {}
    chal_card = challenger_scorecard or {}
    champ_apps = champ_card.get("app_scores", {})
    chal_apps = chal_card.get("app_scores", {})
    champ_oc = champ_card.get("app_onchain", {})
    chal_oc = chal_card.get("app_onchain", {})
    max_regression = float(os.environ.get("MAX_APP_REGRESSION", "0.10"))
    floor_env = os.environ.get("ONCHAIN_FLOOR_BPS", "").strip()
    floor = int(floor_env) if floor_env else None

    oc_surpluses: list[float] = []
    for app, inc in champ_apps.items():
        ch = chal_apps.get(app)
        # (veto 1) on-chain admission floor on the challenger's every scenario
        if floor is not None:
            all_pass, min_bps, n_missing = _onchain_pass(chal_oc.get(app, []), floor)
            if not all_pass:
                return False, (
                    f"p2oc reject {app}: on-chain floor fail "
                    f"(min={min_bps} missing={n_missing})"
                )
        # (veto 2) dropping a champion-covered app is a hard regression
        if ch is None:
            return False, f"p2oc reject {app}: dropped by challenger"
        # rank input: on-chain output surplus (only when both means are present)
        co = _app_onchain_mean(champ_oc.get(app, []))
        cco = _app_onchain_mean(chal_oc.get(app, []))
        if co is not None and cco is not None:
            oc_surpluses.append(cco - co)
        elif co is not None and cco is None:
            return False, f"p2oc reject {app}: challenger produced no on-chain score"
        # (veto 3) JS no-CATASTROPHIC-regression — a gas-blowup safety net only
        if inc > 0 and ch < inc * (1 - max_regression):
            return False, f"p2oc reject {app}: JS regress {inc:.3f}->{ch:.3f}"

    # rank: mean per-app on-chain BPS surplus / 10000 must beat the dethrone margin
    net_bps = sum(oc_surpluses) / len(oc_surpluses) if oc_surpluses else 0.0
    net = net_bps / 10000.0
    if net <= dethrone_margin:
        return False, (
            f"p2oc reject: net on-chain surplus {net_bps:+.1f} BPS "
            f"<= margin {dethrone_margin:.4f}"
        )
    return True, f"p2oc ADOPT: net on-chain surplus {net_bps:+.1f} BPS"


def evaluate_adoption(
    *,
    challenger_score: float,
    champion_score: float,
    challenger_scorecard: dict | None,
    champion_scorecard: dict | None,
    dethrone_margin: float,
    has_champion: bool,
) -> tuple[bool, str]:
    """Pure per-validator adoption decision -> (adopt, reason).

    Mirrors ``EpochManager._should_adopt``'s rule body EXACTLY — everything
    AFTER the adoption-disabled / same-submission / shadow preamble (those stay
    in ``EpochManager``). Reads the same env knobs (``PER_APP_MIN_SCORE``,
    ``MAX_APP_REGRESSION``, ``ONCHAIN_FLOOR_BPS``, ``ADOPT_RULE``). Dispatches
    ``ADOPT_RULE=p2oc`` to the on-chain-surplus variant internally.

    Enforces (default "current" rule):
    1. Per-app minimum (PER_APP_MIN_SCORE, default 0.3) — the absolute sanity floor.
    2. Per-app non-regression: no champion-covered app may be dropped, and
       no app the champion solves may drop more than MAX_APP_REGRESSION (10%)
    3. Global improvement over the champion by the dethrone margin (default 5%)

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
    per_app_min = float(os.environ.get("PER_APP_MIN_SCORE", "0.3"))
    max_regression = float(os.environ.get("MAX_APP_REGRESSION", "0.10"))

    # On-chain co-ranked dethrone (opt-in). Default "current" falls through to the
    # JS logic below, byte-for-byte unchanged. ADOPT_RULE=p2oc ranks the dethrone on
    # the unfakeable on-chain OUTPUT surplus instead of the gas-polluted JS score.
    # MUST NOT be enabled live until the cross-machine determinism gate passes.
    if os.environ.get("ADOPT_RULE", "current").strip().lower() == "p2oc":
        return _evaluate_onchain(
            challenger_scorecard=challenger_scorecard,
            champion_scorecard=champion_scorecard,
            dethrone_margin=dethrone_margin,
            has_champion=has_champion,
        )

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
