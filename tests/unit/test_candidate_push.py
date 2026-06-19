"""Unit tests for the P3 candidate image push (content-addressed transport)."""

import asyncio
from unittest.mock import AsyncMock, patch

from minotaur_subnet.api.routes.submissions.screening_pipeline import (
    _push_candidate_image,
)

REPO = "ghcr.io/subnet112/minotaur-solver-candidates"
DIGEST = f"{REPO}@sha256:{'a' * 64}"


class _FakeProc:
    def __init__(self, returncode, out=b""):
        self.returncode = returncode
        self._out = out

    async def communicate(self):
        return self._out, b""


def _run(coro):
    return asyncio.run(coro)


def _patch_docker(procs):
    return patch(
        "minotaur_subnet.api.routes.submissions.screening_pipeline.asyncio.create_subprocess_exec",
        new=AsyncMock(side_effect=procs),
    )


def test_push_candidate_success(monkeypatch):
    monkeypatch.setenv("CANDIDATE_IMAGE_REPO", REPO)
    procs = [
        _FakeProc(0),                              # docker tag
        _FakeProc(0, b"pushed"),                   # docker push
        _FakeProc(0, (DIGEST + "\n").encode()),    # docker image inspect RepoDigests
    ]
    with _patch_docker(procs) as m:
        out = _run(_push_candidate_image("solver-abc123:screening", 7))
    assert out == DIGEST
    # First docker call retags the local image to <repo>:pr-<N>.
    tag_args = m.call_args_list[0].args
    assert tag_args[:3] == ("docker", "tag", "solver-abc123:screening")
    assert tag_args[3] == f"{REPO}:pr-7"


def test_push_candidate_tag_failure_returns_none(monkeypatch):
    monkeypatch.setenv("CANDIDATE_IMAGE_REPO", REPO)
    with _patch_docker([_FakeProc(1, b"no such image")]):
        out = _run(_push_candidate_image("missing:screening", 7))
    assert out is None


def test_push_candidate_push_failure_returns_none(monkeypatch):
    monkeypatch.setenv("CANDIDATE_IMAGE_REPO", REPO)
    procs = [_FakeProc(0), _FakeProc(1, b"denied: permission")]  # tag ok, push fails
    with _patch_docker(procs):
        out = _run(_push_candidate_image("solver-abc:screening", 7))
    assert out is None


def test_push_candidate_bad_digest_returns_none(monkeypatch):
    monkeypatch.setenv("CANDIDATE_IMAGE_REPO", REPO)
    # tag ok, push ok, but inspect returns a non-digest string -> None (don't trust it)
    procs = [_FakeProc(0), _FakeProc(0), _FakeProc(0, b"not-a-digest\n")]
    with _patch_docker(procs):
        out = _run(_push_candidate_image("solver-abc:screening", 7))
    assert out is None
