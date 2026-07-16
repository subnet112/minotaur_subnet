"""RealSimulationUnavailable during a bench is a CONFIG failure, not a verdict.

2026-07-16 build-gate soak: the split worker was missing
SOLVER_READ_PROXY_CHAINS, so every chain-1 bench raised
RealSimulationUnavailable — and the generic handler terminally rejected the
submission with valid=False. Miners must never be judged on operator config:
the submission stays BENCHMARKING and the next pass retries it (the round's
decision deadline bounds a persistent gap).
"""
import time

import pytest

from minotaur_subnet.harness.benchmark_worker import BenchmarkWorker
from minotaur_subnet.harness.orchestrator import RealSimulationUnavailable
from minotaur_subnet.harness.submission_store import (
    Submission,
    SubmissionStatus,
    SubmissionStore,
)


@pytest.mark.asyncio
async def test_real_sim_unavailable_leaves_submission_queued(monkeypatch):
    monkeypatch.setenv("ROUND_ANCHORED_PIN", "0")
    monkeypatch.delenv("BENCHMARK_WORKER_ONLY", raising=False)
    store = SubmissionStore()
    sub = Submission(
        submission_id="sub_cfg2", repo_url="https://github.com/test/solver",
        commit_hash="abc123", epoch=1, hotkey="5Gtest", round_id="round-r1",
        status=SubmissionStatus.BENCHMARKING, created_at=time.time(),
        updated_at=time.time(), image_tag="solver-abc123:screening",
        image_id="sha256:" + "0" * 64,
    )
    store._submissions[sub.submission_id] = sub

    worker = BenchmarkWorker(store)
    worker._simulator = object()
    monkeypatch.setattr(worker, "_load_benchmark_intents", lambda: [object()])

    async def _score_fn(*_a, **_k):
        return None

    monkeypatch.setattr(worker, "_build_score_fn", lambda intents: _score_fn())
    monkeypatch.setattr(worker, "_enrich_intents_with_manifests", lambda i: i)

    async def _raise(*_a, **_k):
        raise RealSimulationUnavailable("chain 1 not routed")

    monkeypatch.setattr(worker, "_benchmark_submission", _raise)

    await worker.run_once()

    fresh = store.get(sub.submission_id)
    assert fresh.status == SubmissionStatus.BENCHMARKING, (
        "a config failure must never flip a submission's status"
    )
