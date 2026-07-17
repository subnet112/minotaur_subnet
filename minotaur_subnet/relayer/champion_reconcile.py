"""Reconcile canonical solver-repo ``main`` against the ADOPTED champion.

Part 2 of the finalize-reconcile design (``docs/champion-finalize-reconcile.md``).
Part 1 makes the merge-gate DEFER (not abort) on an unknown finalize outcome so the
coordinator re-drives and completes it. This module is the belt-and-suspenders: a
leader-only sweep that heals a ``main`` that drifted from the adopted champion because a
finalization half-completed (the PR merged but the throne never moved — the 2026-07-17
orphaned-merge split).

The leader records the canonical ``main`` HEAD SHA at adoption
(``ChampionSnapshot.canonical_main_sha``). This sweep compares the LIVE main HEAD to
that recorded SHA — plain SHA equality, no reading of the miner's commit — so it works
for PRIVATE champions too (the published-to-main commit is always on canonical, unlike
the miner's own commit which lives in a private fork):

  - main HEAD == recorded champion SHA                  -> NOOP (consistent).
  - main HEAD drifted AND the on-chain throne is STILL  -> REVERT main back to the
    the adopted champion (the merge never took the         recorded champion SHA's tree
    throne = orphan)                                       (auto-heal the split).
  - main HEAD drifted AND the on-chain throne MOVED off -> ALERT (a real win the leader
    the adopted champion                                   hasn't adopted; never revert)
  - no recorded SHA / can't read main HEAD              -> NOOP / ALERT (fail-safe).

FAIL-SAFE: on any read error or ambiguity the action is NOOP or ALERT, never a
destructive REVERT. Leader-only; the relayer holds the write token.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Callable

from minotaur_subnet.relayer.solver_repo import (
    _gh_json,
    _parse_github_owner_repo,
)

logger = logging.getLogger(__name__)

RECONCILE_NOOP = "noop"
RECONCILE_REVERT = "revert"
RECONCILE_ALERT = "alert"


def classify_main_reconcile(
    *,
    main_head_sha: str | None,
    expected_main_sha: str | None,
    onchain_throne_is_adopted: bool,
) -> tuple[str, str]:
    """Pure reconcile decision → ``(action, reason)``.

    Plain SHA equality between the live ``main`` HEAD and the champion's recorded
    canonical SHA. NEVER returns REVERT unless ``main`` drifted from the recorded SHA
    AND the on-chain throne is unchanged (so the drift never took the throne = an
    orphan). Any missing/ambiguous input degrades to NOOP or ALERT.
    """
    if not expected_main_sha:
        # No recorded baseline — champion adopted before reconcile-tracking existed, or
        # the capture failed. Nothing to safely reconcile against.
        return (
            RECONCILE_NOOP,
            "no recorded canonical champion sha (adopted before reconcile-tracking, or capture failed)",
        )
    if not main_head_sha:
        return (RECONCILE_ALERT, "could not read canonical main HEAD")
    if main_head_sha == expected_main_sha:
        return (RECONCILE_NOOP, "main HEAD == recorded canonical champion sha")
    # main HEAD drifted from where the champion published.
    if onchain_throne_is_adopted:
        return (
            RECONCILE_REVERT,
            "main HEAD drifted from the recorded champion sha but the on-chain throne is "
            "unchanged — orphaned merge",
        )
    return (
        RECONCILE_ALERT,
        "main HEAD drifted and the on-chain throne moved — needs completion, not revert",
    )


def _commit_tree_sha(owner: str, repo: str, sha: str, *, token: str | None) -> str | None:
    """Tree SHA of a canonical commit. ``None`` on any read error (=> fail-safe)."""
    if not sha:
        return None
    ok, body = _gh_json(
        "GET", f"https://api.github.com/repos/{owner}/{repo}/git/commits/{sha}", token=token,
    )
    if not ok or not isinstance(body, dict):
        return None
    return ((body.get("tree") or {}).get("sha") or "").strip() or None


def _main_head_sha(owner: str, repo: str, *, token: str | None) -> str | None:
    """Canonical ``main`` HEAD commit SHA; ``None`` on error."""
    ok, ref = _gh_json(
        "GET", f"https://api.github.com/repos/{owner}/{repo}/git/ref/heads/main", token=token,
    )
    if not ok or not isinstance(ref, dict):
        return None
    return ((ref.get("object") or {}).get("sha") or "").strip() or None


def revert_main_to_tree(
    *,
    owner: str,
    repo: str,
    target_tree_sha: str,
    current_head_sha: str,
    message: str,
    token: str | None,
) -> bool:
    """Advance ``main`` to a new commit whose tree == ``target_tree_sha`` (the recorded
    champion), parented on ``current_head_sha`` — restoring main content to the adopted
    champion regardless of drift depth, without rewriting history. FAIL-CLOSED: any
    error returns False (=> the sweep ALERTs instead)."""
    if not target_tree_sha or not current_head_sha:
        return False
    ok, commit = _gh_json(
        "POST",
        f"https://api.github.com/repos/{owner}/{repo}/git/commits",
        {"message": message, "tree": target_tree_sha, "parents": [current_head_sha]},
        token=token,
    )
    if not ok or not isinstance(commit, dict) or not commit.get("sha"):
        logger.error("[reconcile] revert: could not create restore commit on %s/%s", owner, repo)
        return False
    new_sha = commit["sha"]
    ok, _body = _gh_json(
        "PATCH",
        f"https://api.github.com/repos/{owner}/{repo}/git/refs/heads/main",
        {"sha": new_sha, "force": False},
        token=token,
    )
    if not ok:
        logger.error(
            "[reconcile] revert: created restore commit %s but could not advance main "
            "(ruleset/permissions?) — main unchanged, will ALERT", new_sha[:12],
        )
        return False
    logger.info(
        "[reconcile] REVERTED orphaned merge: main %s -> %s (tree %s = recorded champion)",
        current_head_sha[:12], new_sha[:12], target_tree_sha[:12],
    )
    return True


def reconcile_champion_main(
    *,
    expected_main_sha: str | None,
    onchain_throne_is_adopted: bool,
    is_leader: bool = True,
    dry_run: bool = False,
    revert_fn: Callable[..., bool] = revert_main_to_tree,
) -> dict[str, Any]:
    """One reconcile pass. Reads canonical main HEAD, compares it to the champion's
    recorded ``expected_main_sha`` via ``classify_main_reconcile``, and on REVERT
    restores main to the recorded champion's tree. Returns a compact result. Best-effort
    + leader-only; never raises."""
    result: dict[str, Any] = {"action": RECONCILE_NOOP, "reason": "", "reverted": False}
    try:
        if not is_leader:
            result["reason"] = "not leader"
            return result
        owner_repo = _parse_github_owner_repo()
        if owner_repo is None:
            result.update(action=RECONCILE_ALERT, reason="no canonical owner/repo configured")
            return result
        owner, repo = owner_repo
        token = (
            os.environ.get("SOLVER_REPO_PR_TOKEN")
            or os.environ.get("SOLVER_REPO_TOKEN")
            or ""
        ).strip() or None

        head_sha = _main_head_sha(owner, repo, token=token)
        action, reason = classify_main_reconcile(
            main_head_sha=head_sha,
            expected_main_sha=expected_main_sha,
            onchain_throne_is_adopted=onchain_throne_is_adopted,
        )
        result.update(action=action, reason=reason, main_head=head_sha,
                      expected_main_sha=expected_main_sha)

        if action == RECONCILE_REVERT and not dry_run:
            # Restore main to the RECORDED champion sha's tree. That SHA is a canonical
            # commit (the published-to-main commit), so its tree is always readable —
            # this is what makes the reconciler work for private champions.
            target_tree = _commit_tree_sha(owner, repo, expected_main_sha, token=token)
            if not target_tree:
                result.update(
                    action=RECONCILE_ALERT,
                    reason="orphan detected but could not read the recorded champion tree — not reverting",
                )
                logger.warning("[reconcile] ALERT: %s", result["reason"])
                return result
            reverted = bool(revert_fn(
                owner=owner, repo=repo, target_tree_sha=target_tree,
                current_head_sha=head_sha,
                message=(
                    "reconcile: revert orphaned champion merge — restore main to the "
                    f"adopted champion (recorded {expected_main_sha[:12]}; live head "
                    f"{(head_sha or '')[:12]} had no matching on-chain throne)\n\n"
                    "Auto-heal of a half-completed finalization (PR merged, "
                    "attest/adoption never landed). See docs/champion-finalize-reconcile.md."
                ),
                token=token,
            ))
            result["reverted"] = reverted
            if not reverted:
                result.update(action=RECONCILE_ALERT,
                              reason="orphaned merge detected but auto-revert failed")
        if result["action"] == RECONCILE_ALERT:
            logger.warning("[reconcile] ALERT: %s (main_head=%s)", result["reason"], (head_sha or "")[:12])
    except Exception as exc:  # noqa: BLE001 — a reconcile sweep must never crash the loop
        logger.warning("[reconcile] pass failed (non-fatal): %s", exc)
        result.update(action=RECONCILE_ALERT, reason=f"reconcile crashed: {exc}")
    return result


def onchain_throne_is_adopted(
    adopted_commit_hash: str | None, adopted_round_id: str | None,
) -> bool:
    """Best-effort: is the on-chain ChampionRegistry throne consistent with the LEADER's
    adopted champion? True only when the adopted champion's commit is bound on-chain for
    its round. Returns False (=> ``classify_main_reconcile`` ALERTs, never REVERTs) on
    ANY read failure or missing input — so a bad/absent on-chain read can never cause a
    destructive revert. (Uses the on-chain registry, not GitHub, so it works for private
    champions.)"""
    if not adopted_commit_hash or not adopted_round_id:
        return False
    try:
        from minotaur_subnet.relayer.solver_repo import _onchain_cert_binds
        return bool(_onchain_cert_binds(adopted_commit_hash, adopted_round_id))
    except Exception as exc:  # noqa: BLE001 — never let a read error force a revert
        logger.debug("[reconcile] on-chain throne read failed (=> ALERT, no revert): %s", exc)
        return False


def run_reconcile_pass(
    *,
    expected_main_sha: str | None,
    onchain_commit_hash: str | None,
    onchain_round_id: str | None,
    is_leader: bool,
    enforce: bool,
) -> dict[str, Any]:
    """Compute the on-chain throne signal and run one reconcile pass. Does GitHub +
    web3 I/O — call via ``asyncio.to_thread`` from the loop. ``enforce=False`` => OBSERVE
    (detect + alert, no auto-revert).

    ``expected_main_sha`` is the champion's recorded ``canonical_main_sha`` (from the
    active-champion snapshot); ``onchain_commit_hash``/``onchain_round_id`` are the
    champion submission's commit + round for the on-chain throne check."""
    throne_ok = onchain_throne_is_adopted(onchain_commit_hash, onchain_round_id)
    res = reconcile_champion_main(
        expected_main_sha=expected_main_sha,
        onchain_throne_is_adopted=throne_ok,
        is_leader=is_leader,
        dry_run=not enforce,
    )
    res["onchain_throne_is_adopted"] = throne_ok
    res["enforce"] = enforce
    return res
