"""Reconcile canonical solver-repo ``main`` against the ADOPTED champion.

Part 2 of the finalize-reconcile design (``docs/champion-finalize-reconcile.md``).
Part 1 makes the merge-gate DEFER (not abort) on an unknown finalize outcome so the
coordinator re-drives and completes it. This module is the belt-and-suspenders: a
leader-only sweep that heals a ``main`` that drifted from the adopted champion
because a finalization half-completed (the PR merged but the throne never moved —
the 2026-07-17 orphaned-merge split).

Decision (pure, exhaustively tested — see ``classify_main_reconcile``):

  - ``main`` tree == adopted champion tree                 -> NOOP (consistent).
  - ``main`` drifted AND the ON-CHAIN throne is STILL the  -> REVERT ``main`` to the
    adopted champion (the throne never moved, so the merge     adopted champion's tree
    on main never took the throne = an ORPHAN).               (auto-heal the split).
  - ``main`` drifted AND the on-chain throne MOVED off the -> ALERT (a real win the
    adopted champion                                          leader hasn't adopted
                                                              locally; Part 1's retry
                                                              completes it — NEVER
                                                              auto-revert a real win).

FAIL-SAFE: on any read error / ambiguity the action is NOOP or ALERT, never a
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
    main_tree_sha: str | None,
    adopted_tree_sha: str | None,
    onchain_throne_is_adopted: bool,
) -> tuple[str, str]:
    """Pure reconcile decision → ``(action, reason)``.

    NEVER returns REVERT unless we are CERTAIN ``main`` drifted away from the adopted
    champion AND the on-chain throne is unchanged (so the drift never took the throne
    = an orphaned merge). Any missing/ambiguous input degrades to NOOP or ALERT.
    """
    if not adopted_tree_sha:
        # We don't know what main SHOULD be — nothing to reconcile against.
        return (RECONCILE_NOOP, "no adopted champion tree to reconcile against")
    if not main_tree_sha:
        return (RECONCILE_ALERT, "could not read canonical main HEAD tree")
    if main_tree_sha == adopted_tree_sha:
        return (RECONCILE_NOOP, "main matches the adopted champion tree")
    # main drifted from the adopted champion.
    if onchain_throne_is_adopted:
        # The on-chain throne is STILL the adopted champion, yet main differs — the
        # merge on main never took the throne. That is an ORPHAN (the 2026-07-17
        # split). Safe to revert main back to the adopted champion.
        return (
            RECONCILE_REVERT,
            "main drifted but the on-chain throne is unchanged — orphaned merge",
        )
    # The on-chain throne has moved off the adopted champion — main reflects a REAL
    # win the leader hasn't adopted locally. Do NOT revert; surface it (Part 1's
    # defer/retry completes CERTIFIED rounds).
    return (
        RECONCILE_ALERT,
        "on-chain throne moved but leader has not adopted — needs completion, not revert",
    )


def _commit_tree_sha(owner: str, repo: str, sha: str, *, token: str | None) -> str | None:
    """Tree SHA of a commit on ``owner/repo`` (fork commits are reachable on canonical
    by SHA in its object network). ``None`` on any read error (=> fail-safe NOOP)."""
    if not sha:
        return None
    ok, body = _gh_json(
        "GET", f"https://api.github.com/repos/{owner}/{repo}/git/commits/{sha}", token=token,
    )
    if not ok or not isinstance(body, dict):
        return None
    return ((body.get("tree") or {}).get("sha") or "").strip() or None


def _main_head(owner: str, repo: str, *, token: str | None) -> tuple[str | None, str | None]:
    """``(head_commit_sha, head_tree_sha)`` for canonical ``main``; ``(None, None)`` on error."""
    ok, ref = _gh_json(
        "GET", f"https://api.github.com/repos/{owner}/{repo}/git/ref/heads/main", token=token,
    )
    if not ok or not isinstance(ref, dict):
        return (None, None)
    head = ((ref.get("object") or {}).get("sha") or "").strip()
    if not head:
        return (None, None)
    return (head, _commit_tree_sha(owner, repo, head, token=token))


def revert_main_to_tree(
    *,
    owner: str,
    repo: str,
    target_tree_sha: str,
    current_head_sha: str,
    message: str,
    token: str | None,
) -> bool:
    """Advance ``main`` to a new commit whose tree == ``target_tree_sha`` (the adopted
    champion), parented on ``current_head_sha`` — restoring main content to the adopted
    champion REGARDLESS of drift depth, without rewriting history. Ruleset-honoring
    where the token lacks bypass would require a PR; here we do a fast-forward commit +
    ref update (the relayer/admin token can advance main, mirroring the manual heal).
    FAIL-CLOSED: any error returns False (=> the sweep ALERTs instead)."""
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
        "[reconcile] REVERTED orphaned merge: main %s -> %s (tree %s = adopted champion)",
        current_head_sha[:12], new_sha[:12], target_tree_sha[:12],
    )
    return True


def reconcile_champion_main(
    *,
    adopted_commit_hash: str | None,
    onchain_throne_is_adopted: bool,
    is_leader: bool = True,
    dry_run: bool = False,
    revert_fn: Callable[..., bool] = revert_main_to_tree,
) -> dict[str, Any]:
    """One reconcile pass. Reads canonical main + the adopted champion's tree, decides
    via ``classify_main_reconcile``, and on REVERT restores main to the adopted
    champion. Returns a compact result (also suitable for /health). Best-effort +
    leader-only; never raises."""
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

        adopted_tree = (
            _commit_tree_sha(owner, repo, adopted_commit_hash, token=token)
            if adopted_commit_hash else None
        )
        head_sha, main_tree = _main_head(owner, repo, token=token)

        action, reason = classify_main_reconcile(
            main_tree_sha=main_tree,
            adopted_tree_sha=adopted_tree,
            onchain_throne_is_adopted=onchain_throne_is_adopted,
        )
        result.update(action=action, reason=reason, main_head=head_sha, main_tree=main_tree,
                      adopted_tree=adopted_tree)

        if action == RECONCILE_REVERT and not dry_run:
            reverted = bool(revert_fn(
                owner=owner, repo=repo, target_tree_sha=adopted_tree,
                current_head_sha=head_sha,
                message=(
                    "reconcile: revert orphaned champion merge — restore main to the "
                    f"adopted champion (head {(head_sha or '')[:12]} had no matching "
                    "on-chain throne)\n\nAuto-heal of a half-completed finalization "
                    "(PR merged, attest/adoption never landed). See "
                    "docs/champion-finalize-reconcile.md."
                ),
                token=token,
            ))
            result["reverted"] = reverted
            if not reverted:
                result["action"] = RECONCILE_ALERT
                result["reason"] = "orphaned merge detected but auto-revert failed"
        if action == RECONCILE_ALERT:
            logger.warning("[reconcile] ALERT: %s (main_head=%s)", reason, (head_sha or "")[:12])
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
    destructive revert."""
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
    adopted_commit_hash: str | None,
    adopted_round_id: str | None,
    is_leader: bool,
    enforce: bool,
) -> dict[str, Any]:
    """Compute the on-chain throne signal and run one reconcile pass. Does GitHub +
    web3 I/O — call via ``asyncio.to_thread`` from the loop. ``enforce=False`` => OBSERVE
    (detect + alert, no auto-revert)."""
    throne_ok = onchain_throne_is_adopted(adopted_commit_hash, adopted_round_id)
    res = reconcile_champion_main(
        adopted_commit_hash=adopted_commit_hash,
        onchain_throne_is_adopted=throne_ok,
        is_leader=is_leader,
        dry_run=not enforce,
    )
    res["onchain_throne_is_adopted"] = throne_ok
    res["enforce"] = enforce
    return res
