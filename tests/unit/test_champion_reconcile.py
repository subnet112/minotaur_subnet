"""Tests for the champion-main reconcile sweep + auto-revert (Part 2).

See ``docs/champion-finalize-reconcile.md`` and
``minotaur_subnet/relayer/champion_reconcile.py``.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import minotaur_subnet.relayer.champion_reconcile as R


# ── classify_main_reconcile (pure decision) ──────────────────────────────────
def test_classify_noop_when_main_matches_adopted():
    action, _ = R.classify_main_reconcile(
        main_tree_sha="t1", adopted_tree_sha="t1", onchain_throne_is_adopted=True,
    )
    assert action == R.RECONCILE_NOOP


def test_classify_noop_when_no_adopted_tree():
    action, _ = R.classify_main_reconcile(
        main_tree_sha="t1", adopted_tree_sha=None, onchain_throne_is_adopted=True,
    )
    assert action == R.RECONCILE_NOOP


def test_classify_alert_when_main_tree_unreadable():
    action, _ = R.classify_main_reconcile(
        main_tree_sha=None, adopted_tree_sha="t1", onchain_throne_is_adopted=True,
    )
    assert action == R.RECONCILE_ALERT


def test_classify_revert_on_orphan_drift_when_throne_unchanged():
    # main drifted from the adopted champion, but the on-chain throne is STILL the
    # adopted champion => the merge on main never took the throne => orphan => revert.
    action, _ = R.classify_main_reconcile(
        main_tree_sha="orphan", adopted_tree_sha="adopted", onchain_throne_is_adopted=True,
    )
    assert action == R.RECONCILE_REVERT


def test_classify_alert_when_throne_actually_moved():
    # main drifted AND the on-chain throne moved off the adopted champion => a real win
    # the leader hasn't adopted locally => never auto-revert.
    action, _ = R.classify_main_reconcile(
        main_tree_sha="winner", adopted_tree_sha="adopted", onchain_throne_is_adopted=False,
    )
    assert action == R.RECONCILE_ALERT


# ── revert_main_to_tree (mocked GitHub Git-Data) ─────────────────────────────
def test_revert_creates_commit_and_advances_main():
    calls = []

    def fake_gh(method, url, payload=None, *, token=None):
        calls.append((method, url))
        if method == "POST" and url.endswith("/git/commits"):
            return (True, {"sha": "newsha"})
        if method == "PATCH" and url.endswith("/git/refs/heads/main"):
            return (True, {})
        return (False, {})

    with patch.object(R, "_gh_json", fake_gh):
        ok = R.revert_main_to_tree(
            owner="o", repo="r", target_tree_sha="adopted",
            current_head_sha="orphanhead", message="m", token="t",
        )
    assert ok is True
    assert any(m == "POST" for m, _ in calls) and any(m == "PATCH" for m, _ in calls)


def test_revert_fails_closed_when_commit_create_fails():
    with patch.object(R, "_gh_json", lambda *a, **k: (False, {})):
        assert R.revert_main_to_tree(
            owner="o", repo="r", target_tree_sha="a",
            current_head_sha="h", message="m", token="t",
        ) is False


def test_revert_fails_closed_when_ref_update_blocked():
    def fake_gh(method, url, payload=None, *, token=None):
        if method == "POST":
            return (True, {"sha": "newsha"})
        return (False, {"message": "protected branch — PR required"})  # ruleset blocks PATCH

    with patch.object(R, "_gh_json", fake_gh):
        assert R.revert_main_to_tree(
            owner="o", repo="r", target_tree_sha="a",
            current_head_sha="h", message="m", token="t",
        ) is False


# ── reconcile_champion_main (orchestrator; revert_fn injected) ───────────────
def _env():
    return patch.object(R, "_parse_github_owner_repo", lambda: ("o", "r"))


def test_reconcile_noop_when_consistent():
    with _env(), \
            patch.object(R, "_commit_tree_sha", lambda *a, **k: "SAME"), \
            patch.object(R, "_main_head", lambda *a, **k: ("head", "SAME")):
        revert = MagicMock()
        res = R.reconcile_champion_main(
            adopted_commit_hash="c", onchain_throne_is_adopted=True, revert_fn=revert,
        )
    assert res["action"] == R.RECONCILE_NOOP
    assert res["reverted"] is False
    revert.assert_not_called()


def test_reconcile_reverts_orphan_and_restores_adopted_tree():
    with _env(), \
            patch.object(R, "_commit_tree_sha", lambda *a, **k: "ADOPTED"), \
            patch.object(R, "_main_head", lambda *a, **k: ("orphanhead", "ORPHAN")):
        revert = MagicMock(return_value=True)
        res = R.reconcile_champion_main(
            adopted_commit_hash="c", onchain_throne_is_adopted=True, revert_fn=revert,
        )
    assert res["action"] == R.RECONCILE_REVERT
    assert res["reverted"] is True
    revert.assert_called_once()
    # restores to the ADOPTED tree, parented on the orphan head — never to the orphan.
    assert revert.call_args.kwargs["target_tree_sha"] == "ADOPTED"
    assert revert.call_args.kwargs["current_head_sha"] == "orphanhead"


def test_reconcile_alerts_when_throne_moved_never_reverts():
    with _env(), \
            patch.object(R, "_commit_tree_sha", lambda *a, **k: "ADOPTED"), \
            patch.object(R, "_main_head", lambda *a, **k: ("winnerhead", "WINNER")):
        revert = MagicMock()
        res = R.reconcile_champion_main(
            adopted_commit_hash="c", onchain_throne_is_adopted=False, revert_fn=revert,
        )
    assert res["action"] == R.RECONCILE_ALERT
    revert.assert_not_called()


def test_reconcile_dry_run_detects_but_does_not_revert():
    with _env(), \
            patch.object(R, "_commit_tree_sha", lambda *a, **k: "ADOPTED"), \
            patch.object(R, "_main_head", lambda *a, **k: ("orphanhead", "ORPHAN")):
        revert = MagicMock()
        res = R.reconcile_champion_main(
            adopted_commit_hash="c", onchain_throne_is_adopted=True, dry_run=True, revert_fn=revert,
        )
    assert res["action"] == R.RECONCILE_REVERT
    assert res["reverted"] is False
    revert.assert_not_called()


def test_reconcile_noop_when_not_leader():
    revert = MagicMock()
    res = R.reconcile_champion_main(
        adopted_commit_hash="c", onchain_throne_is_adopted=True, is_leader=False, revert_fn=revert,
    )
    assert res["action"] == R.RECONCILE_NOOP
    revert.assert_not_called()


def test_reconcile_downgrades_to_alert_when_revert_write_fails():
    with _env(), \
            patch.object(R, "_commit_tree_sha", lambda *a, **k: "ADOPTED"), \
            patch.object(R, "_main_head", lambda *a, **k: ("orphanhead", "ORPHAN")):
        revert = MagicMock(return_value=False)  # write blocked
        res = R.reconcile_champion_main(
            adopted_commit_hash="c", onchain_throne_is_adopted=True, revert_fn=revert,
        )
    assert res["reverted"] is False
    assert res["action"] == R.RECONCILE_ALERT


# ── onchain_throne_is_adopted + run_reconcile_pass ───────────────────────────
def test_onchain_throne_true_when_cert_binds():
    with patch("minotaur_subnet.relayer.solver_repo._onchain_cert_binds", lambda *a, **k: True):
        assert R.onchain_throne_is_adopted("commit", "round") is True


def test_onchain_throne_false_on_read_error_is_fail_safe():
    def boom(*a, **k):
        raise RuntimeError("rpc down")

    with patch("minotaur_subnet.relayer.solver_repo._onchain_cert_binds", boom):
        # A read error must NOT be read as "throne unchanged" (which would revert) —
        # it degrades to False => classify ALERTs, never REVERTs.
        assert R.onchain_throne_is_adopted("commit", "round") is False


def test_onchain_throne_false_on_missing_input():
    assert R.onchain_throne_is_adopted(None, "round") is False
    assert R.onchain_throne_is_adopted("commit", None) is False


def test_run_reconcile_pass_observe_detects_without_revert():
    with _env(), \
            patch.object(R, "_commit_tree_sha", lambda *a, **k: "ADOPTED"), \
            patch.object(R, "_main_head", lambda *a, **k: ("orphan", "ORPHAN")), \
            patch.object(R, "onchain_throne_is_adopted", lambda *a, **k: True):
        res = R.run_reconcile_pass(
            adopted_commit_hash="c", adopted_round_id="r", is_leader=True, enforce=False,
        )
    assert res["action"] == R.RECONCILE_REVERT   # drift detected
    assert res["reverted"] is False              # OBSERVE => no write
    assert res["enforce"] is False
    assert res["onchain_throne_is_adopted"] is True
