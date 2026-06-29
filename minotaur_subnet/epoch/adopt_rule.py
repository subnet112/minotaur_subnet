"""Surviving adoption-rule constant after the relative cutover.

The legacy quote-anchored adoption rule (``evaluate_adoption`` + the on-chain
HARD-VETO gate ``_evaluate_onchain_gate`` and its ``_onchain_pass`` /
``_app_onchain_mean`` helpers, plus the ``_AdoptRuleConfig`` knobs
``MAX_APP_REGRESSION`` / ``ONCHAIN_MAX_REGRESSION`` / ``ONCHAIN_FLOOR_BPS``) was
REMOVED: the SOLE authoritative champion-adoption decision is now the PER-ORDER
relative rule (:func:`minotaur_subnet.epoch.relative_scoring.evaluate_relative_adoption`),
routed identically by the leader (``EpochManager._meets_adoption_criteria``) and
every follower (``champion_consensus._independent_adopt_vote``).

Only ``PER_APP_MIN_SCORE`` survives — NOT as an adoption gate (the relative rule
needs no aggregate floor), but as the per-app "too low" sanity floor the
submission-report surface (``api/routes/submissions/report.py`` via routes.py and
``relayer/solver_repo.py``) shows miners. It is a fleet-uniform CODE CONSTANT
(propagated via :stable / redeploy), not a per-validator env.
"""

from __future__ import annotations

# Per-app sanity floor shown on the submission report ("your app scored below the
# minimum"). One value, fleet-wide — not a per-node env.
PER_APP_MIN_SCORE: float = 0.3
