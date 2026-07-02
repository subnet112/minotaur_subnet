"""Tests for boot-time resume of screening pipelines stranded by a restart.

The pipeline runs as a background task spawned once at submission time; a
process restart kills it, stranding the submission in QUEUED/SCREENING_*.
`resume_stranded_screenings` re-kicks recent strandings at api startup.
"""
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

from minotaur_subnet.api.routes.submissions import screening_pipeline as sp
from minotaur_subnet.harness.submission_store import SubmissionStatus


class FakeStore:
    def __init__(self, subs, tokens=None):
        self.subs = list(subs)
        self.tokens = dict(tokens or {})
        self.rejected: list[tuple[str, str]] = []

    def list_by_status(self, status):
        return [s for s in self.subs if s.status == status]

    def get_repo_token(self, submission_id):
        return self.tokens.get(submission_id)

    def reject(self, submission_id, reason):
        self.rejected.append((submission_id, reason))


def _sub(sid, status, *, age_s=60.0, is_private=False):
    return SimpleNamespace(
        submission_id=sid,
        status=status,
        updated_at=time.time() - age_s,
        is_private=is_private,
    )


def _run_resume(monkeypatch, store):
    """Run resume with the pipeline stubbed; return the re-spawned ids."""
    respawned: list[str] = []

    async def fake_pipeline(submission_id):
        respawned.append(submission_id)

    monkeypatch.setattr(sp, "get_store", lambda: store)
    monkeypatch.setattr(sp, "_run_screening_pipeline", fake_pipeline)

    async def main():
        n = await sp.resume_stranded_screenings()
        # let the spawned tasks run to completion
        await asyncio.sleep(0)
        return n

    return asyncio.run(main()), respawned


def test_resumes_all_screening_stages(monkeypatch):
    store = FakeStore([
        _sub("s-q", SubmissionStatus.QUEUED),
        _sub("s-1", SubmissionStatus.SCREENING_STAGE_1),
        _sub("s-2", SubmissionStatus.SCREENING_STAGE_2),
        _sub("s-3", SubmissionStatus.SCREENING_STAGE_3),
        _sub("s-done", SubmissionStatus.SCORED),        # terminal: untouched
        _sub("s-bench", SubmissionStatus.BENCHMARKING),  # worker's job, not ours
    ])
    n, respawned = _run_resume(monkeypatch, store)
    assert n == 4
    assert sorted(respawned) == ["s-1", "s-2", "s-3", "s-q"]
    assert store.rejected == []


def test_stale_strandings_left_alone(monkeypatch):
    store = FakeStore([
        _sub("s-old", SubmissionStatus.SCREENING_STAGE_2, age_s=48 * 3600),
        _sub("s-new", SubmissionStatus.SCREENING_STAGE_2, age_s=120),
    ])
    n, respawned = _run_resume(monkeypatch, store)
    assert n == 1
    assert respawned == ["s-new"]  # the 2-day-old one is not resurrected


def test_private_without_token_rejected_with_actionable_reason(monkeypatch):
    store = FakeStore([
        _sub("s-priv", SubmissionStatus.SCREENING_STAGE_1, is_private=True),
        _sub("s-priv-tok", SubmissionStatus.SCREENING_STAGE_1, is_private=True),
    ], tokens={"s-priv-tok": "ghp_x"})
    n, respawned = _run_resume(monkeypatch, store)
    # token lost -> rejected with a re-submit hint; token retained -> resumed
    assert n == 1
    assert respawned == ["s-priv-tok"]
    assert len(store.rejected) == 1
    assert store.rejected[0][0] == "s-priv"
    assert "re-submit" in store.rejected[0][1]


def test_noop_when_nothing_stranded(monkeypatch):
    n, respawned = _run_resume(monkeypatch, FakeStore([]))
    assert n == 0
    assert respawned == []
