"""Tests for merge-failure reason propagation (``MergeResult`` → ``abort_reason``).

The champion-adoption path returns a ``MergeResult`` carrying WHY a merge failed
instead of a bare bool, so the round store records ``merge_failed:<reason>``
(self-diagnosing) instead of a flat ``merge_failed`` that forces a dig into the
ephemeral relayer logs. ``MergeResult.__bool__`` preserves every existing
truthiness gate — a failed result is FALSY, so no adoption gate can be tricked
into adopting on failure.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from minotaur_subnet.relayer.solver_repo import (
    MergeResult,
    on_champion_adopted_pr,
    on_champion_adopted_via_relayer,
)


def test_mergeresult_truthiness_is_ok():
    ok = MergeResult(True)
    bad = MergeResult(False, "no_quorum_cert")
    assert bool(ok) is True and ok.reason == ""
    assert bool(bad) is False and bad.reason == "no_quorum_cert"
    # The load-bearing invariant: truthiness == success, so `if result:` /
    # `bool(result)` / `x and result` gates keep their old meaning.
    assert ok and not bad
    assert (bool(bad) and True) is False


def test_adopted_pr_non_git_submission():
    sub = SimpleNamespace(commit_hash="builtin", submission_id="s1")
    res = on_champion_adopted_pr(sub, "r1", certificate=None)
    assert not res
    assert res.reason == "non_git_submission"


def test_adopted_pr_no_certificate_is_root_reason():
    # Git-real commit but no certificate → attest is skipped → the ROOT reason is
    # surfaced (not the downstream "no_quorum_cert" symptom). No network touched.
    sub = SimpleNamespace(commit_hash="deadbeef", submission_id="s1", pr_number=None)
    res = on_champion_adopted_pr(sub, "r1", certificate=None)
    assert not res
    assert res.reason == "no_certificate"


def test_via_relayer_url_unset():
    with patch.dict("os.environ", {}, clear=True):
        res = on_champion_adopted_via_relayer(
            SimpleNamespace(commit_hash="abc", submission_id="s1"),
            "r1", certificate=object(),
        )
    assert not res
    assert res.reason == "relayer_url_unset"


def test_via_relayer_no_certificate():
    with patch.dict("os.environ", {"RELAYER_URL": "http://relayer:8091"}, clear=True):
        res = on_champion_adopted_via_relayer(
            SimpleNamespace(commit_hash="abc", submission_id="s1"),
            "r1", certificate=None,
        )
    assert not res
    assert res.reason == "no_certificate"
