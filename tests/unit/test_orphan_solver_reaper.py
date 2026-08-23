"""Solver containers must not outlive the process that spawned them.

A container OUTLIVES its `docker run` CLI: SIGKILL of the CLI detaches, it does
not stop the container. `kill()` removed the container only in its TimeoutError
branch, so the HAPPY path — CLI dies promptly, wait() returns — stranded it
forever. On 2026-08-23 four orphans were found on the leader (up to 9 days old),
three writing ~19 GB/hour and each holding a 4g memory + 2 CPU reservation.

Two layers here: kill() now always removes its own container (closes the
ordinary leak), and a run-start sweep catches what no session can — the owning
PROCESS dying before kill() ever runs.
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from minotaur_subnet.harness import orchestrator as orch
from minotaur_subnet.harness.orchestrator import (
    SOLVER_ROLE_LABEL,
    TOTAL_BENCHMARK_TIMEOUT,
    _orphan_reap_age_seconds,
    reap_orphaned_solver_containers,
)


# ── the age horizon ───────────────────────────────────────────────────────────

def test_horizon_is_anchored_to_the_run_budget(monkeypatch):
    """Not a bare constant: a run cannot outlive its own budget, so a multiple
    of it cannot mistake an in-flight run — of ANY process — for an orphan."""
    monkeypatch.delenv("BENCHMARK_ORPHAN_REAP_AGE_S", raising=False)
    assert _orphan_reap_age_seconds() >= 2 * TOTAL_BENCHMARK_TIMEOUT


def test_horizon_is_tunable_and_zero_disables(monkeypatch):
    monkeypatch.setenv("BENCHMARK_ORPHAN_REAP_AGE_S", "60")
    assert _orphan_reap_age_seconds() == 60
    monkeypatch.setenv("BENCHMARK_ORPHAN_REAP_AGE_S", "0")
    assert _orphan_reap_age_seconds() == 0
    monkeypatch.setenv("BENCHMARK_ORPHAN_REAP_AGE_S", "nonsense")
    assert _orphan_reap_age_seconds() >= 2 * TOTAL_BENCHMARK_TIMEOUT


def test_disabled_horizon_reaps_nothing(monkeypatch):
    monkeypatch.setenv("BENCHMARK_ORPHAN_REAP_AGE_S", "0")
    assert asyncio.run(reap_orphaned_solver_containers()) == []


# ── the sweep ─────────────────────────────────────────────────────────────────

def _fake_docker(ps_out, inspect_out):
    """Stand in for `docker ps` then `docker inspect`, in call order."""
    calls = {"argv": []}

    async def _exec(*argv, **kw):
        calls["argv"].append(argv)
        proc = AsyncMock()
        payload = ps_out if argv[1] == "ps" else inspect_out
        proc.communicate.return_value = (payload.encode(), b"")
        proc.wait.return_value = 0
        return proc

    return _exec, calls


def _run_sweep(ps_out, inspect_out, removed):
    exec_stub, calls = _fake_docker(ps_out, inspect_out)
    with patch.object(orch.asyncio, "create_subprocess_exec", exec_stub), \
         patch.object(orch, "_docker_rm_f", AsyncMock(side_effect=lambda n: removed.append(n))):
        out = asyncio.run(reap_orphaned_solver_containers(age_s=3600))
    return out, calls


def test_only_bench_labelled_containers_are_even_listed():
    """A LIVE solver serves /quote for days. Safety here is the ROLE LABEL, not
    ownership — so the filter must pin role=bench at the docker level."""
    removed = []
    _, calls = _run_sweep("", "", removed)
    ps_argv = calls["argv"][0]
    assert "--filter" in ps_argv
    assert f"label={SOLVER_ROLE_LABEL}=bench" in ps_argv


def test_old_orphan_is_removed():
    removed = []
    out, _ = _run_sweep(
        "minotaur-bench-aaaa\n",
        "/minotaur-bench-aaaa 2020-01-01T00:00:00.000000000Z\n",
        removed,
    )
    assert out == ["minotaur-bench-aaaa"]
    assert removed == ["minotaur-bench-aaaa"]


def test_young_container_is_left_alone():
    """An in-flight run must survive the sweep."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    removed = []
    out, _ = _run_sweep("minotaur-bench-bbbb\n", f"/minotaur-bench-bbbb {now}\n", removed)
    assert out == []
    assert removed == []


def test_unparseable_timestamp_is_not_treated_as_an_orphan():
    """Fail safe: if we cannot prove it is old, we do not touch it."""
    removed = []
    out, _ = _run_sweep("minotaur-bench-cccc\n", "/minotaur-bench-cccc garbage\n", removed)
    assert out == [] and removed == []


def test_nanosecond_timestamps_parse():
    """Docker emits RFC-3339 with NANOSECOND precision, which fromisoformat
    rejects on older Pythons — the real format, not a tidied one."""
    removed = []
    out, _ = _run_sweep(
        "minotaur-bench-dddd\n",
        "/minotaur-bench-dddd 2020-06-01T12:34:56.123456789Z\n",
        removed,
    )
    assert out == ["minotaur-bench-dddd"]


def test_no_containers_means_no_inspect_call():
    removed = []
    out, calls = _run_sweep("", "", removed)
    assert out == []
    assert len(calls["argv"]) == 1, "must not inspect when ps returned nothing"


def test_docker_failure_is_swallowed():
    """A sweep must never break a benchmark run."""
    async def boom(*a, **k):
        raise OSError("docker gone")
    with patch.object(orch.asyncio, "create_subprocess_exec", boom):
        assert asyncio.run(reap_orphaned_solver_containers(age_s=1)) == []


# ── the source fix: kill() must always remove its own container ───────────────

def _session(container="minotaur-bench-zzzz", *, wait_hangs=False, lookup_error=False):
    from minotaur_subnet.harness.orchestrator import SolverSession
    proc = AsyncMock()
    proc.pid = 4242
    # Process.kill() is SYNCHRONOUS on a real asyncio subprocess — an AsyncMock
    # would hand back an un-awaited coroutine and mask what we are testing.
    proc.kill = MagicMock(side_effect=ProcessLookupError() if lookup_error else None)
    if wait_hangs:
        async def _hang():
            await asyncio.sleep(3600)
        proc.wait.side_effect = _hang
    else:
        proc.wait.return_value = 0
    s = SolverSession.__new__(SolverSession)
    s._proc = proc
    s._closed = False
    s._stderr_task = None
    s._label = "test"
    s._container_name = container
    return s


def _kill_and_capture(sess):
    removed = []
    with patch.object(orch, "_docker_rm_f", AsyncMock(side_effect=lambda n: removed.append(n))):
        asyncio.run(sess.kill())
    return removed


def test_kill_removes_the_container_on_the_happy_path():
    """THE ORIGINAL LEAK. The CLI dies promptly, wait() returns, and the branch
    that removed the container was only reached on TimeoutError — so the
    container ran forever."""
    assert _kill_and_capture(_session()) == ["minotaur-bench-zzzz"]


def test_kill_removes_the_container_when_the_cli_was_already_gone():
    """ProcessLookupError means the CLI is gone — which says nothing about its
    container."""
    assert _kill_and_capture(_session(lookup_error=True)) == ["minotaur-bench-zzzz"]


def test_kill_still_removes_the_container_when_wait_hangs(monkeypatch):
    """The pre-existing TimeoutError path must keep working."""
    monkeypatch.setattr(orch, "_KILL_REAP_TIMEOUT", 0.01)
    assert "minotaur-bench-zzzz" in _kill_and_capture(_session(wait_hangs=True))
