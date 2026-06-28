"""``BenchmarkWorker.run_once`` must serialize concurrent callers.

The background ``run_loop`` (continuous screening) and the round-close
``evaluate_round`` both call ``run_once`` on the SAME worker instance. They are
independent asyncio tasks, and before this fix there was no run_once-level lock:
they raced, the per-submission idempotency guard lost a read-modify-write window
(the 2nd pass snapshotted the BENCHMARKING set before the 1st persisted SCORED),
and every challenger was benchmarked TWICE per round — doubling the
``_sim_lock``-serialized simulation work that dominates round wall-clock.

The fix is a per-worker lock around ``run_once`` so the second caller waits,
then re-enters on an already-SCORED set and no-ops via the idempotency guard.
These tests pin that serialization and confirm the single-caller path (where
``evaluate_round`` is the only caller) is unchanged.
"""
import asyncio
from unittest.mock import MagicMock

import pytest

from minotaur_subnet.harness.benchmark_worker import BenchmarkWorker


def _worker() -> BenchmarkWorker:
    # The run_once wrapper only touches the lock + _run_once_impl; the store is
    # never reached on this path, so a bare mock suffices.
    return BenchmarkWorker(submission_store=MagicMock())


@pytest.mark.asyncio
async def test_run_once_serializes_concurrent_callers():
    """Two concurrent run_once calls must never overlap inside the impl."""
    worker = _worker()
    in_flight = 0
    max_in_flight = 0
    calls = 0

    async def fake_impl() -> int:
        nonlocal in_flight, max_in_flight, calls
        calls += 1
        in_flight += 1
        max_in_flight = max(max_in_flight, in_flight)
        # Yield: an UNLOCKED second caller would overlap here (max_in_flight==2).
        await asyncio.sleep(0.05)
        in_flight -= 1
        return 1

    worker._run_once_impl = fake_impl  # type: ignore[method-assign]

    results = await asyncio.gather(worker.run_once(), worker.run_once())

    # The lock kept the two passes strictly serialized — never concurrent.
    assert max_in_flight == 1
    # Both callers still ran the impl; in the real flow the 2nd no-ops because
    # the 1st already persisted SCORED before releasing the lock.
    assert calls == 2
    assert results == [1, 1]


@pytest.mark.asyncio
async def test_run_once_single_caller_unaffected():
    """The lone-caller path (evaluate_round only) runs the impl exactly once."""
    worker = _worker()
    calls = 0

    async def fake_impl() -> int:
        nonlocal calls
        calls += 1
        return 7

    worker._run_once_impl = fake_impl  # type: ignore[method-assign]

    assert await worker.run_once() == 7
    assert calls == 1
    # Lock was created lazily and is released (reusable), not left held.
    assert worker._run_once_lock is not None
    assert not worker._run_once_lock.locked()
