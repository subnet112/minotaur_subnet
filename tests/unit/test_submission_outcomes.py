"""Waitlisted status + machine-readable outcome_code (PR-B).

Contract for the frontend: 'no slot this round' is WAITLISTED (no-fault, carries
next-round priority), distinct from REJECTED (for-cause); every terminal state
carries an outcome_code the UI switches on instead of parsing prose.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import pytest

from minotaur_subnet.api.routes.submissions.report import (
    build_submission_report,
    render_report_md,
)
from minotaur_subnet.harness.rotation import RotationLedger, apply_rotation_slate
from minotaur_subnet.harness.submission_store import (
    SubmissionStatus,
    SubmissionStore,
)


def _store(tmp_path):
    return SubmissionStore(persist_path=tmp_path / "subs.json")


def _mk(store, hotkey, sid, round_id="r1"):
    return store.create(
        repo_url="https://x/r.git", commit_hash=sid, epoch=1,
        hotkey=hotkey, round_id=round_id,
    )


# ── store: waitlist vs reject ─────────────────────────────────────────────────


def test_waitlist_sets_status_code_and_context(tmp_path):
    store = _store(tmp_path)
    sub = _mk(store, "hk", "sub_a")
    store.waitlist(
        sub.submission_id, "not selected", outcome_code="rotation_not_selected",
        position=2, contenders=13, slots=3,
    )
    got = store.get(sub.submission_id)
    assert got.status == SubmissionStatus.WAITLISTED
    assert got.outcome_code == "rotation_not_selected"
    assert got.waitlist == {
        "position": 2, "contenders": 13, "slots": 3, "next_round_priority": True,
    }


def test_reject_carries_outcome_code(tmp_path):
    store = _store(tmp_path)
    sub = _mk(store, "hk", "sub_a")
    store.reject(sub.submission_id, "bad code", outcome_code="fingerprint_repeat")
    got = store.get(sub.submission_id)
    assert got.status == SubmissionStatus.REJECTED
    assert got.outcome_code == "fingerprint_repeat"


def test_waitlist_and_outcome_survive_reload(tmp_path):
    store = _store(tmp_path)
    sub = _mk(store, "hk", "sub_a")
    store.waitlist(sub.submission_id, "x", outcome_code="window_elapsed")
    reloaded = SubmissionStore(persist_path=tmp_path / "subs.json").get(sub.submission_id)
    assert reloaded.status == SubmissionStatus.WAITLISTED
    assert reloaded.outcome_code == "window_elapsed"


# ── rotation waitlists (not rejects) the overflow, with position ──────────────


def test_rotation_waitlists_overflow_with_positions(tmp_path):
    store = _store(tmp_path)
    for i in range(5):
        _mk(store, f"hk{i}", f"sub_{i}")
    res = apply_rotation_slate(
        store, "r1", 2, RotationLedger(tmp_path / "ledger.json"),
    )
    assert res["applied"]
    waitlisted = [
        s for s in store.list_by_round("r1")
        if s.status == SubmissionStatus.WAITLISTED
    ]
    assert len(waitlisted) == 3  # 5 candidates, 2 slots
    # Every waitlisted carries the no-fault code + a positive position.
    for s in waitlisted:
        assert s.outcome_code == "rotation_not_selected"
        assert s.waitlist["contenders"] == 5
        assert s.waitlist["slots"] == 2
        assert s.waitlist["position"] >= 1
    # Positions are the seniority order, unique and contiguous 1..3.
    assert sorted(s.waitlist["position"] for s in waitlisted) == [1, 2, 3]
    # Selected are NOT waitlisted.
    selected = [s for s in store.list_by_round("r1")
                if s.status not in (SubmissionStatus.WAITLISTED, SubmissionStatus.REJECTED)]
    assert len(selected) == 2


# ── report surfaces waitlisted as a no-fault outcome ──────────────────────────


def _waitlisted_sub():
    return SimpleNamespace(
        status=SimpleNamespace(value="waitlisted"),
        benchmark_details=None, screening={},
        outcome_code="rotation_not_selected",
        waitlist={"position": 2, "contenders": 13, "slots": 3,
                  "next_round_priority": True},
    )


def test_report_outcome_is_waitlisted():
    rep = build_submission_report(_waitlisted_sub(), reason="not selected")
    assert rep is not None
    assert rep["outcome"] == "waitlisted"
    assert rep["outcome_code"] == "rotation_not_selected"
    assert rep["waitlist"]["position"] == 2


def test_report_md_waitlisted_is_not_a_rejection():
    rep = build_submission_report(_waitlisted_sub(), reason="not selected")
    md = render_report_md(rep)
    assert "⏭️ Waitlisted" in md
    assert "#2 of 13" in md
    assert "not** a verdict" in md
    assert "❌" not in md  # never renders as a rejection
