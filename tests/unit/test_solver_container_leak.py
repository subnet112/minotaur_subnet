"""Solver containers are named + force-reaped so a hung `docker run` CLI can't
leak its threads (root cause of a 45h api thread-leak → HTTP starvation)."""
import asyncio

import pytest

from minotaur_subnet.harness import orchestrator as o


def test_docker_rm_f_runs_and_reaps(monkeypatch):
    calls = []

    class _FakeRm:
        returncode = 0
        async def wait(self):
            return 0

    async def fake_exec(*args, **kw):
        calls.append(list(args))
        return _FakeRm()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    asyncio.run(o._docker_rm_f("minotaur-bench-abc"))
    assert ["docker", "rm", "-f", "minotaur-bench-abc"] in calls
    # empty name → no-op (no docker invocation)
    calls.clear()
    asyncio.run(o._docker_rm_f(""))
    assert calls == []


def test_docker_rm_f_swallows_errors(monkeypatch):
    async def boom(*a, **k):
        raise OSError("docker missing")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", boom)
    asyncio.run(o._docker_rm_f("x"))  # must not raise


class _HangThenExitProc:
    """proc.wait() hangs on the 1st call (→ reap times out), returns on the 2nd."""
    stdin = stdout = stderr = None

    def __init__(self):
        self.kill_called = 0
        self.wait_calls = 0

    def kill(self):
        self.kill_called += 1

    async def wait(self):
        self.wait_calls += 1
        if self.wait_calls == 1:
            await asyncio.sleep(10)  # hang → wait_for(_KILL_REAP_TIMEOUT) times out
        return 0


def test_kill_force_removes_container_then_reaps(monkeypatch):
    monkeypatch.setattr(o, "_KILL_REAP_TIMEOUT", 0.05)
    removed = []

    async def fake_rm(name):
        removed.append(name)

    monkeypatch.setattr(o, "_docker_rm_f", fake_rm)
    proc = _HangThenExitProc()
    sess = o.SolverSession(proc, label="t", container_name="minotaur-bench-xyz")
    asyncio.run(sess.kill())

    assert proc.kill_called == 1
    assert removed == ["minotaur-bench-xyz"], "hung reap must docker-rm-f the container"
    assert proc.wait_calls == 2, "reap retried after the container was removed"


def test_kill_no_container_name_keeps_legacy_abandon(monkeypatch):
    monkeypatch.setattr(o, "_KILL_REAP_TIMEOUT", 0.05)
    removed = []
    async def fake_rm(name):
        removed.append(name)
    monkeypatch.setattr(o, "_docker_rm_f", fake_rm)
    proc = _HangThenExitProc()
    sess = o.SolverSession(proc, label="t")  # no container_name (subprocess mode)
    asyncio.run(sess.kill())
    assert removed == [], "no name → no docker rm -f (legacy abandon path)"
    assert proc.wait_calls == 1
