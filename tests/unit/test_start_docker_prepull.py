"""Unit tests for the P4c run-chokepoint pre-pull in orchestrator.start_docker."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from minotaur_subnet.harness.orchestrator import SolverOrchestrator

REPO = "ghcr.io/subnet112/minotaur-solver-candidates"
DIGEST_REF = f"{REPO}@sha256:{'a' * 64}"


class _FakeProc:
    returncode = 0

    async def communicate(self):
        return b"", b""


def _run(coro):
    return asyncio.run(coro)


def _exec_recorder():
    calls = []

    async def _exec(*args, **kwargs):
        calls.append(args)
        return _FakeProc()

    return _exec, calls


def test_digest_ref_triggers_prepull():
    _exec, calls = _exec_recorder()
    with patch("minotaur_subnet.harness.orchestrator.asyncio.create_subprocess_exec", new=_exec), \
         patch("minotaur_subnet.harness.orchestrator.SolverSession", new=MagicMock()):
        _run(SolverOrchestrator().start_docker(DIGEST_REF))
    # First subprocess call must be `docker pull <digest ref>`.
    assert calls[0][:3] == ("docker", "pull", DIGEST_REF)
    # A later call is the actual `docker run`.
    assert any(c[:2] == ("docker", "run") for c in calls)


def test_local_tag_is_not_prepulled():
    _exec, calls = _exec_recorder()
    with patch("minotaur_subnet.harness.orchestrator.asyncio.create_subprocess_exec", new=_exec), \
         patch("minotaur_subnet.harness.orchestrator.SolverSession", new=MagicMock()):
        _run(SolverOrchestrator().start_docker("solver-abc123:screening"))
    # No `docker pull` for a local tag — only the run.
    assert all(c[:2] != ("docker", "pull") for c in calls)
    assert any(c[:2] == ("docker", "run") for c in calls)
