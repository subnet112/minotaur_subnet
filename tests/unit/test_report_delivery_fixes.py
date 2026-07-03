"""Tests for the private-PR report-delivery fixes (2026-07-03).

Three live gaps, one theme — a miner's report must reach THEIR PR or fail
loudly, never silently vanish or land somewhere public:

1. ``_pr_comment_target`` returned the same ``(None, None)`` for "public
   submission" and "private submission with a lost token", so a lost token
   MISDIRECTED private reports onto the canonical repo — silently succeeding
   whenever a same-numbered PR existed there.
2. Rotation-unselected submissions were terminal-rejected with no PR comment.
3. (epoch manager) candidates outranked by an adopted finalist got silence —
   covered in test_epoch_manager.py.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from minotaur_subnet.harness.rotation import RotationLedger, apply_rotation_slate
from minotaur_subnet.relayer import solver_repo as sr


def _private_sub(token_ok: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        submission_id="sub_p",
        pr_number=4,
        is_private=True,
        private_repo_full="minerdev/solver-private",
    )


# ──────────────────────────────────────────────────────────────────────
# 1. _pr_comment_target — no canonical fallback for private submissions
# ──────────────────────────────────────────────────────────────────────


def test_target_public_falls_back_to_canonical():
    pub = SimpleNamespace(submission_id="sub_pub", pr_number=204, is_private=False)
    assert sr._pr_comment_target(pub, None) == (None, None)


def test_target_private_with_token_is_miner_repo():
    assert sr._pr_comment_target(_private_sub(), "ghp_x") == (
        ("minerdev", "solver-private"), "ghp_x",
    )


def test_target_private_without_token_is_skip_not_canonical():
    assert sr._pr_comment_target(_private_sub(), None) is None


def test_rejected_pr_private_without_token_skips_comment_but_still_gcs(monkeypatch):
    monkeypatch.setenv("SOLVER_REPO_URL", "https://github.com/subnet112/minotaur-solver")
    calls = []
    with patch.object(
        sr, "comment_on_pr",
        lambda *a, **k: calls.append("comment") or True,
    ), patch.object(
        sr, "delete_candidate_image",
        lambda n: calls.append("gc") or True,
    ):
        assert sr.on_champion_rejected_pr(_private_sub(), "reject: x") is False
    assert calls == ["gc"]  # no comment POST anywhere — especially not canonical


def test_finalist_pr_private_without_token_skips_comment(monkeypatch):
    monkeypatch.setenv("SOLVER_REPO_URL", "https://github.com/subnet112/minotaur-solver")
    with patch.object(sr, "comment_on_pr") as post:
        assert sr.on_champion_finalist_pr(_private_sub(), "selected") is False
    post.assert_not_called()


# ──────────────────────────────────────────────────────────────────────
# 2. on_round_not_selected_pr + rotation notify hook
# ──────────────────────────────────────────────────────────────────────


def test_not_selected_comment_posts_to_miner_repo():
    calls = []
    with patch.object(
        sr, "comment_on_pr",
        lambda n, body, owner_repo=None, token=None: calls.append(
            (n, body, owner_repo, token)
        ) or True,
    ):
        assert sr.on_round_not_selected_pr(
            _private_sub(), "not selected for round-r1 (rotation: 7 candidates, 4 slots)",
            repo_token="ghp_x",
        ) is True
    n, body, owner_repo, token = calls[0]
    assert (n, owner_repo, token) == (4, ("minerdev", "solver-private"), "ghp_x")
    assert "Not selected this round" in body
    assert "rotation" in body


def test_not_selected_private_without_token_skips():
    with patch.object(sr, "comment_on_pr") as post:
        assert sr.on_round_not_selected_pr(_private_sub(), "not selected") is False
    post.assert_not_called()


class _RotSub:
    def __init__(self, sid: str, hotkey: str) -> None:
        self.submission_id = sid
        self.hotkey = hotkey
        self.status = "queued"
        self.round_id = "r1"


class _RotStore:
    """Minimal store: records the call ORDER so tests can assert the notify
    fires before the (token-purging) reject."""

    def __init__(self, subs: list[_RotSub]) -> None:
        self._subs = subs
        self.events: list[tuple[str, str]] = []

    def list_by_round(self, round_id: str):
        return list(self._subs)

    def reject(self, submission_id: str, reason: str) -> None:
        self.events.append(("reject", submission_id))


def test_rotation_notifies_before_reject(tmp_path):
    subs = [_RotSub(f"sub_{i}", f"hk{i}") for i in range(3)]
    store = _RotStore(subs)

    def notify(sub, reason):
        store.events.append(("notify", sub.submission_id))
        assert "not selected for r1" in reason

    res = apply_rotation_slate(
        store, "r1", 2, RotationLedger(str(tmp_path / "ledger.json")),
        now=100.0, notify=notify,
    )
    assert res["applied"] is True
    assert len(res["skipped"]) == 1
    (skipped_id,) = res["skipped"]
    assert store.events == [("notify", skipped_id), ("reject", skipped_id)]


def test_rotation_notify_failure_never_blocks_reject(tmp_path):
    subs = [_RotSub(f"sub_{i}", f"hk{i}") for i in range(3)]
    store = _RotStore(subs)

    def notify(sub, reason):
        raise RuntimeError("github down")

    res = apply_rotation_slate(
        store, "r1", 1, RotationLedger(str(tmp_path / "ledger.json")),
        now=100.0, notify=notify,
    )
    rejected = [sid for ev, sid in store.events if ev == "reject"]
    assert sorted(rejected) == sorted(res["skipped"])
    assert len(rejected) == 2


def test_rotation_without_notify_unchanged(tmp_path):
    subs = [_RotSub(f"sub_{i}", f"hk{i}") for i in range(2)]
    store = _RotStore(subs)
    res = apply_rotation_slate(
        store, "r1", 1, RotationLedger(str(tmp_path / "ledger.json")), now=100.0,
    )
    assert len(res["skipped"]) == 1
    assert [ev for ev, _ in store.events] == ["reject"]
