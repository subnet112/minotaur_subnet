"""The no-intents branch is a CONFIG failure, never a verdict on submissions.

2026-07-16 split-worker incident: a worker whose APP_INTENTS_STORE_PATH was
never wired saw an empty app store and terminally REJECTED every submission it
enumerated ({"error": "no_active_intents"}) seconds after rotation selected
each slate — every round aborted no_champion_candidate for hours, silently:
the WARNING was muted by the bittensor logging hijack, and the worker's
heartbeat kept bumping because the branch returned normally.

run_once must now RAISE instead: the submissions stay untouched (an empty
intent set says nothing about them), and run_loop's catch-all skips the
heartbeat bump so the split worker goes unhealthy instead of masking the
failure. In the monolith, evaluate_round records benchmark_failed with the
message rather than silently mass-rejecting paying miners.
"""
import time

import pytest

from minotaur_subnet.harness.benchmark_worker import BenchmarkWorker
from minotaur_subnet.harness.submission_store import (
    Submission,
    SubmissionStatus,
    SubmissionStore,
)


def _benchmarking_sub(sid: str = "sub_cfg1") -> Submission:
    return Submission(
        submission_id=sid,
        repo_url="https://github.com/test/solver",
        commit_hash="abc123",
        epoch=1,
        hotkey="5Gtest",
        round_id="round-r1",
        status=SubmissionStatus.BENCHMARKING,
        created_at=time.time(),
        updated_at=time.time(),
        image_tag="solver-abc123:screening",
        image_id="sha256:" + "0" * 64,
    )


@pytest.mark.asyncio
async def test_no_intents_raises_and_leaves_submissions_untouched(monkeypatch):
    store = SubmissionStore()
    sub = _benchmarking_sub()
    store._submissions[sub.submission_id] = sub

    # Unit-test scope is the INTENTS branch: disable the (default-ON) round-
    # anchored pin gate via its documented emergency value so the bare worker
    # (no round store / resolver) doesn't defer before reaching it.
    monkeypatch.setenv("ROUND_ANCHORED_PIN", "0")
    monkeypatch.delenv("BENCHMARK_WORKER_ONLY", raising=False)
    worker = BenchmarkWorker(store)
    # Mirror startup.py's post-construction wiring (ctx.benchmark_worker._simulator = ...)
    # so the startup-race guard ("real simulator not yet wired") doesn't defer the pass.
    worker._simulator = object()
    monkeypatch.setattr(worker, "_load_benchmark_intents", lambda: [])

    with pytest.raises(RuntimeError, match="no active benchmark intents"):
        await worker.run_once()

    fresh = store.get(sub.submission_id)
    assert fresh.status == SubmissionStatus.BENCHMARKING, (
        "a config failure must never flip a submission's status"
    )
    assert not (fresh.benchmark_details or {}).get("error")
