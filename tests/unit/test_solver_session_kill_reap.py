"""Regression: ``SolverSession.kill()`` must reap with a bounded wait.

Prod incident (2026-06-08): the live-solver quote path on the leader api
hung for ~2 days. Root cause — a QUOTE command hit its 5s per-command
timeout; ``_send`` called ``kill()`` to preserve stdio protocol sync;
``kill()`` did an UNBOUNDED ``await self._proc.wait()`` to reap the killed
process. In a container whose asyncio child-watcher had stalled (unreaped
zombie children were observed piling up under the api PID 1), that wait
never returned. Because ``kill()`` runs while ``DockerRuntimeSolver`` holds
its per-runtime ``asyncio.Lock`` (every quote/plan serializes on it), the
stalled reap deadlocked the whole live-solver path: every subsequent quote
hung forever while the event loop otherwise stayed healthy.

The fix bounds the reap with ``asyncio.wait_for(..., _KILL_REAP_TIMEOUT)``.
After SIGKILL the process is gone regardless; the wait only reaps the
zombie, and must never hold the lock indefinitely. These tests pin that
contract.
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from minotaur_subnet.harness import orchestrator
from minotaur_subnet.harness.orchestrator import SolverSession


class _HangingProc:
    """Fake subprocess whose wait() never returns — models a container
    whose child-reaping has stalled (the killed pid is never reaped)."""

    def __init__(self) -> None:
        self.killed = False
        self.stdin = None
        self.stdout = None

    def kill(self) -> None:
        self.killed = True

    async def wait(self) -> int:
        await asyncio.Event().wait()  # blocks forever
        return 0  # pragma: no cover


class _FastProc:
    """Fake subprocess that exits promptly when killed (the happy path)."""

    def __init__(self) -> None:
        self.killed = False

    def kill(self) -> None:
        self.killed = True

    async def wait(self) -> int:
        return 0


class _GoneProc:
    """Already-dead subprocess — kill() raises ProcessLookupError."""

    def kill(self) -> None:
        raise ProcessLookupError()

    async def wait(self) -> int:  # pragma: no cover - not reached
        return 0


@pytest.mark.asyncio
async def test_kill_is_bounded_when_reap_hangs(monkeypatch):
    """The core regression: a wait() that never returns must NOT make
    kill() hang. kill() returns within the reap timeout, having flipped
    _closed so the lock its caller holds is released."""
    monkeypatch.setattr(orchestrator, "_KILL_REAP_TIMEOUT", 0.05)
    proc = _HangingProc()
    sess = SolverSession(proc, label="test", live_mode=True)

    start = time.monotonic()
    # Outer guard: if the fix regressed, this wait_for trips first and the
    # assertion below makes the failure obvious instead of hanging CI.
    await asyncio.wait_for(sess.kill(), timeout=2.0)
    elapsed = time.monotonic() - start

    assert proc.killed is True
    assert sess._closed is True
    assert elapsed < 1.0, f"kill() took {elapsed:.2f}s — reap was not bounded"


@pytest.mark.asyncio
async def test_kill_reaps_normally_when_process_exits():
    """No regression on the happy path: a process that exits promptly is
    reaped and kill() returns cleanly."""
    proc = _FastProc()
    sess = SolverSession(proc, label="test")
    await sess.kill()
    assert proc.killed is True
    assert sess._closed is True


@pytest.mark.asyncio
async def test_kill_swallows_process_lookup_error():
    """A process that's already gone (ProcessLookupError on kill) must be
    treated as successfully killed, not surface an error."""
    sess = SolverSession(_GoneProc(), label="test")
    await sess.kill()  # must not raise
    assert sess._closed is True


@pytest.mark.asyncio
async def test_kill_is_idempotent():
    """A second kill() after the first is a no-op (already closed)."""
    proc = _FastProc()
    sess = SolverSession(proc, label="test")
    await sess.kill()
    proc.killed = False  # would flip True again if kill() ran a 2nd time
    await sess.kill()
    assert proc.killed is False
