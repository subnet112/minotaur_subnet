"""Per-(hotkey, commit) round-participation cap — anti-resubmit-spam.

Measured live (50 rounds, 2026-07-02): 61% of benchmark slots re-scored a commit
already benched in the window; one bot resubmitted the identical commit 36 rounds
straight. The cap rejects the same commit at intake after it has occupied
SUBMISSIONS_MAX_ROUNDS_PER_COMMIT benchmark slates (default 5).
"""

from __future__ import annotations

import pytest

from minotaur_subnet.harness.submission_store import (
    BENCHED_STATUSES,
    SubmissionStatus,
    SubmissionStore,
)

HOTKEY = "5E2cqACTHotkeyAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
OTHER_HOTKEY = "5GuhqBcEOtherBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB"
COMMIT = "4f86cb6800aa00aa00aa00aa00aa00aa00aa00aa"


def _bench_one(store: SubmissionStore, round_no: int, *, hotkey: str = HOTKEY,
               commit: str = COMMIT,
               status: SubmissionStatus = SubmissionStatus.SCORED):
    """Create a submission in its own round and drive it to `status`."""
    sub = store.create(
        repo_url="https://github.com/x/y.git",
        commit_hash=commit,
        epoch=round_no,
        hotkey=hotkey,
        round_id=f"round-{round_no}",
    )
    store.update_status(sub.submission_id, status)
    return sub


class TestCountBenchedRoundsByCommit:
    def test_counts_distinct_benched_rounds(self):
        store = SubmissionStore()
        for i in range(3):
            _bench_one(store, i)
        assert store.count_benched_rounds_by_commit(HOTKEY, COMMIT) == 3

    def test_unbenched_statuses_do_not_count(self):
        # Rotation "not selected" / screening failures are REJECTED, and a
        # submission still queued/screening hasn't occupied a slate slot yet.
        store = SubmissionStore()
        _bench_one(store, 1, status=SubmissionStatus.REJECTED)
        _bench_one(store, 2, status=SubmissionStatus.QUEUED)
        _bench_one(store, 3, status=SubmissionStatus.SCREENING_STAGE_2)
        assert store.count_benched_rounds_by_commit(HOTKEY, COMMIT) == 0
        # benchmarking / scored / adopted all count
        _bench_one(store, 4, status=SubmissionStatus.BENCHMARKING)
        _bench_one(store, 5, status=SubmissionStatus.SCORED)
        _bench_one(store, 6, status=SubmissionStatus.ADOPTED)
        assert store.count_benched_rounds_by_commit(HOTKEY, COMMIT) == 3

    def test_scoped_per_hotkey(self):
        # Another miner submitting the same commit must NOT burn this miner's
        # quota (poisoning guard).
        store = SubmissionStore()
        _bench_one(store, 1, hotkey=OTHER_HOTKEY)
        _bench_one(store, 2, hotkey=OTHER_HOTKEY)
        assert store.count_benched_rounds_by_commit(HOTKEY, COMMIT) == 0
        assert store.count_benched_rounds_by_commit(OTHER_HOTKEY, COMMIT) == 2

    def test_case_insensitive_and_empty_commit(self):
        store = SubmissionStore()
        _bench_one(store, 1, commit=COMMIT.upper())
        assert store.count_benched_rounds_by_commit(HOTKEY, COMMIT.lower()) == 1
        assert store.count_benched_rounds_by_commit(HOTKEY, "") == 0
        assert store.count_benched_rounds_by_commit(HOTKEY, None) == 0

    def test_benched_statuses_is_the_slate_set(self):
        assert BENCHED_STATUSES == {
            SubmissionStatus.BENCHMARKING,
            SubmissionStatus.SCORED,
            SubmissionStatus.ADOPTED,
        }


class TestCreateCap:
    def test_rejects_after_cap_benched_rounds(self):
        store = SubmissionStore()
        for i in range(5):
            _bench_one(store, i)
        with pytest.raises(ValueError, match="submit new code"):
            store.create(
                repo_url="https://github.com/x/y.git",
                commit_hash=COMMIT,
                epoch=99,
                hotkey=HOTKEY,
                round_id="round-99",
                max_rounds_per_commit=5,
            )

    def test_under_cap_and_new_commit_pass(self):
        store = SubmissionStore()
        for i in range(4):
            _bench_one(store, i)
        # 4 benched rounds < cap 5 → the 5th participation is accepted
        store.create(
            repo_url="https://github.com/x/y.git",
            commit_hash=COMMIT,
            epoch=98,
            hotkey=HOTKEY,
            round_id="round-98",
            max_rounds_per_commit=5,
        )
        # a NEW commit is never blocked, regardless of the old commit's history
        store.create(
            repo_url="https://github.com/x/y.git",
            commit_hash="deadbeef" * 5,
            epoch=99,
            hotkey=HOTKEY,
            round_id="round-99",
            max_rounds_per_commit=5,
        )

    def test_zero_disables_cap(self):
        store = SubmissionStore()
        for i in range(10):
            _bench_one(store, i)
        store.create(
            repo_url="https://github.com/x/y.git",
            commit_hash=COMMIT,
            epoch=99,
            hotkey=HOTKEY,
            round_id="round-99",
            max_rounds_per_commit=0,
        )

    def test_rejections_do_not_burn_quota(self):
        # 10 rotation-rejected attempts, 0 benched → still admissible.
        store = SubmissionStore()
        for i in range(10):
            _bench_one(store, i, status=SubmissionStatus.REJECTED)
        store.create(
            repo_url="https://github.com/x/y.git",
            commit_hash=COMMIT,
            epoch=99,
            hotkey=HOTKEY,
            round_id="round-99",
            max_rounds_per_commit=5,
        )


class TestEnvKnob:
    def test_default_is_5(self, monkeypatch):
        from minotaur_subnet.api.routes.submissions.routes import _max_rounds_per_commit
        monkeypatch.delenv("SUBMISSIONS_MAX_ROUNDS_PER_COMMIT", raising=False)
        assert _max_rounds_per_commit() == 5

    def test_env_override_and_disable(self, monkeypatch):
        from minotaur_subnet.api.routes.submissions.routes import _max_rounds_per_commit
        monkeypatch.setenv("SUBMISSIONS_MAX_ROUNDS_PER_COMMIT", "3")
        assert _max_rounds_per_commit() == 3
        monkeypatch.setenv("SUBMISSIONS_MAX_ROUNDS_PER_COMMIT", "0")
        assert _max_rounds_per_commit() == 0

    def test_invalid_env_falls_back_to_default(self, monkeypatch):
        from minotaur_subnet.api.routes.submissions.routes import _max_rounds_per_commit
        monkeypatch.setenv("SUBMISSIONS_MAX_ROUNDS_PER_COMMIT", "many")
        assert _max_rounds_per_commit() == 5
