"""Unit tests for #227: reap orphaned BENCHMARKING submissions on round close."""

from types import SimpleNamespace
from unittest.mock import MagicMock

from minotaur_subnet.epoch.manager import EpochManager
from minotaur_subnet.harness.submission_store import SubmissionStatus


def _mgr(subs, *, reject_cb=None):
    m = EpochManager.__new__(EpochManager)
    store = MagicMock()
    store.list_by_round.return_value = subs
    m._sub_store = store
    m._on_champion_rejected = reject_cb
    return m


def _sub(sid, status, pr_number=7):
    return SimpleNamespace(submission_id=sid, status=status, pr_number=pr_number)


def test_reaps_only_benchmarking_subs():
    rejected_cb = []
    subs = [
        _sub("a", SubmissionStatus.BENCHMARKING),
        _sub("b", SubmissionStatus.SCORED),       # finalist — leave alone
        _sub("c", SubmissionStatus.BENCHMARKING),
        _sub("d", SubmissionStatus.REJECTED),     # already terminal
    ]
    m = _mgr(subs, reject_cb=lambda s, r: rejected_cb.append(s.submission_id))
    m._reap_orphaned_benchmarking("round-1")

    rejected_ids = [c.args[0] for c in m._sub_store.reject.call_args_list]
    assert rejected_ids == ["a", "c"]                       # only the BENCHMARKING ones
    # reason is the clear, actionable signal
    assert "benchmark_window_elapsed" in m._sub_store.reject.call_args_list[0].args[1]
    # the miner-facing reject callback fired for each reaped PR-based sub
    assert rejected_cb == ["a", "c"]


def test_no_benchmarking_subs_is_noop():
    m = _mgr([_sub("a", SubmissionStatus.SCORED)])
    m._reap_orphaned_benchmarking("round-1")
    m._sub_store.reject.assert_not_called()


def test_no_substore_is_safe():
    m = EpochManager.__new__(EpochManager)
    m._sub_store = None
    m._reap_orphaned_benchmarking("round-1")  # must not raise


def test_reaper_continues_past_a_failed_reject():
    subs = [_sub("a", SubmissionStatus.BENCHMARKING), _sub("b", SubmissionStatus.BENCHMARKING)]
    m = _mgr(subs, reject_cb=None)
    m._sub_store.reject.side_effect = [RuntimeError("boom"), None]
    m._reap_orphaned_benchmarking("round-1")  # must not raise; tries both
    assert m._sub_store.reject.call_count == 2
