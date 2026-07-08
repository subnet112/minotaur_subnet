"""Reap orphaned BENCHMARKING submissions on round close (#227).

Since the lifecycle refactor these are WAITLISTED (no-fault: window elapsed,
keeps next-round priority), not rejected. The manager uses store.waitlist
when available and posts the not-selected PR comment.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

from minotaur_subnet.epoch.manager import EpochManager
from minotaur_subnet.harness.submission_store import SubmissionStatus


def _mgr(subs, *, reject_cb=None):
    m = EpochManager.__new__(EpochManager)
    store = MagicMock()
    store.list_by_round.return_value = subs
    store.get_repo_token.return_value = None
    m._sub_store = store
    m._on_champion_rejected = reject_cb
    m._is_leader = lambda: True
    return m


def _sub(sid, status, pr_number=7):
    return SimpleNamespace(submission_id=sid, status=status, pr_number=pr_number)


def test_reaps_only_benchmarking_subs():
    subs = [
        _sub("a", SubmissionStatus.BENCHMARKING),
        _sub("b", SubmissionStatus.SCORED),       # finalist — leave alone
        _sub("c", SubmissionStatus.BENCHMARKING),
        _sub("d", SubmissionStatus.REJECTED),     # already terminal
    ]
    m = _mgr(subs)
    m._reap_orphaned_benchmarking("round-1")

    # WAITLISTED (not rejected) — only the BENCHMARKING ones, window_elapsed code
    waitlisted_ids = [c.args[0] for c in m._sub_store.waitlist.call_args_list]
    assert waitlisted_ids == ["a", "c"]
    assert m._sub_store.waitlist.call_args_list[0].kwargs["outcome_code"] == "window_elapsed"
    # store.reject is NOT used for this no-fault outcome
    m._sub_store.reject.assert_not_called()


def test_no_benchmarking_subs_is_noop():
    m = _mgr([_sub("a", SubmissionStatus.SCORED)])
    m._reap_orphaned_benchmarking("round-1")
    m._sub_store.waitlist.assert_not_called()


def test_no_substore_is_safe():
    m = EpochManager.__new__(EpochManager)
    m._sub_store = None
    m._reap_orphaned_benchmarking("round-1")  # must not raise


def test_reaper_continues_past_a_failed_reject():
    subs = [_sub("a", SubmissionStatus.BENCHMARKING), _sub("b", SubmissionStatus.BENCHMARKING)]
    m = _mgr(subs, reject_cb=None)
    m._sub_store.waitlist.side_effect = [RuntimeError("boom"), None]
    m._reap_orphaned_benchmarking("round-1")  # must not raise; tries both
    assert m._sub_store.waitlist.call_count == 2
