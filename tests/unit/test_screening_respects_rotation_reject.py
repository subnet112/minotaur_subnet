"""Screening must not re-queue a submission that reached a TERMINAL state
mid-screening — that overwrites the terminal state and busts the round's
SOLVER_ROUND_MAX_SUBMISSIONS slate cap (seen as 9-11 'scored' in a 3-slot round
on restart-heavy days).

The guard must use the SHARED rotation terminal rule, not just REJECTED:
rotation has parked overflow as WAITLISTED (no-fault) since #620, and the old
REJECTED-only check let a late-finishing screening overwrite a terminal
waitlist back to BENCHMARKING — a live slate-cap leak."""
from minotaur_subnet.harness.submission_store import SubmissionStore, SubmissionStatus
from minotaur_subnet.api.routes.submissions import screening_pipeline as sp


def _store_with_sub():
    store = SubmissionStore(persist_path=None)
    sub = store.create(
        repo_url="https://example.com/r.git",
        commit_hash="c" * 40,
        epoch=1,
        hotkey="hk1",
        round_id="round-e1-n1",
        max_per_round=0,
        max_rounds_per_commit=0,
    )
    return store, sub


def test_active_submission_is_eligible():
    store, sub = _store_with_sub()
    assert sp._terminal_during_screening(store, sub.submission_id) is None


def test_rotation_rejected_submission_is_blocked_with_reason():
    store, sub = _store_with_sub()
    store.reject(sub.submission_id, "not selected for round-e1-n1 (rotation)")
    reason = sp._terminal_during_screening(store, sub.submission_id)
    assert reason == "not selected for round-e1-n1 (rotation)"
    # non-None → the pipeline returns early instead of update_status(BENCHMARKING)


def test_rotation_waitlisted_submission_is_blocked():
    # THE #620-era leak: rotation parks overflow as WAITLISTED (no-fault), and
    # a late-finishing screening must not resurrect it to BENCHMARKING.
    store, sub = _store_with_sub()
    store.waitlist(
        sub.submission_id,
        "not selected for round-e1-n1 (rotation)",
        outcome_code="rotation_not_selected",
    )
    reason = sp._terminal_during_screening(store, sub.submission_id)
    assert reason is not None


def test_missing_submission_is_eligible():
    store = SubmissionStore(persist_path=None)
    assert sp._terminal_during_screening(store, "sub_ghost") is None


def test_benchmarking_is_not_treated_as_terminal():
    # Only round-terminal states block re-queue; a benign in-flight status
    # stays eligible.
    store, sub = _store_with_sub()
    store.update_status(sub.submission_id, SubmissionStatus.BENCHMARKING)
    assert sp._terminal_during_screening(store, sub.submission_id) is None
