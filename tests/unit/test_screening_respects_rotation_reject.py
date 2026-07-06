"""Screening must not re-queue a submission that close-time rotation rejected
mid-screening — that overwrites the terminal reject and busts the round's
SOLVER_ROUND_MAX_SUBMISSIONS slate cap (seen as 9-11 'scored' in a 3-slot round
on restart-heavy days)."""
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
    assert sp._rejected_during_screening(store, sub.submission_id) is None


def test_rotation_rejected_submission_is_blocked_with_reason():
    store, sub = _store_with_sub()
    store.reject(sub.submission_id, "not selected for round-e1-n1 (rotation)")
    reason = sp._rejected_during_screening(store, sub.submission_id)
    assert reason == "not selected for round-e1-n1 (rotation)"
    # non-None → the pipeline returns early instead of update_status(BENCHMARKING)


def test_missing_submission_is_eligible():
    store = SubmissionStore(persist_path=None)
    assert sp._rejected_during_screening(store, "sub_ghost") is None


def test_scored_is_not_treated_as_rotation_reject():
    # Only REJECTED blocks re-queue; a benign non-reject status stays eligible.
    store, sub = _store_with_sub()
    store.update_status(sub.submission_id, SubmissionStatus.BENCHMARKING)
    assert sp._rejected_during_screening(store, sub.submission_id) is None
