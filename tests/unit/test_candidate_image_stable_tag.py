"""Candidate image push must apply a NEVER-reused per-submission tag.

Regression guard for a real outage: images were pushed only under ``pr-<N>``,
which is reused across every candidate from one long-lived private PR. A newer
push moved that tag off the prior digest, leaving the still-adopted CHAMPION's
image UNTAGGED — ghcr retention then pruned it and every round aborted with
"incumbent re-benchmark failed / image not found". A stable ``sub-<id>`` tag
keeps each pushed digest tagged (retention-safe) for the life of the package.
"""
import asyncio
import json

import pytest

from minotaur_subnet.api.routes.submissions import screening_pipeline as sp

REPO = "ghcr.io/subnet112/minotaur-solver"


def test_safe_image_tag():
    assert sp._safe_image_tag("sub_9b1e7f50e25f") == "sub_9b1e7f50e25f"  # already valid
    assert sp._safe_image_tag("a/b:c!!") == "a-b-c--"                    # sanitized
    assert sp._safe_image_tag("--lead.") == "lead."                      # strip leading .-
    assert sp._safe_image_tag("") == "unknown"


class _FakeProc:
    def __init__(self, out=b"", rc=0):
        self._out, self.returncode = out, rc

    async def communicate(self):
        return self._out, b""


def _run_push(monkeypatch, submission_id):
    monkeypatch.setenv("CANDIDATE_IMAGE_REPO", REPO)
    digest = "sha256:" + "a" * 64
    calls = []

    async def fake_exec(*args, **kwargs):
        docker_args = list(args[1:])  # drop the leading "docker"
        calls.append(docker_args)
        if docker_args[:2] == ["image", "inspect"]:  # RepoDigests lookup
            return _FakeProc(out=json.dumps([f"{REPO}@{digest}"]).encode())
        return _FakeProc()  # tag / push / manifest inspect all succeed

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    result = asyncio.run(
        sp._push_candidate_image("solver-abc123:screening", 3, submission_id)
    )
    return result, calls, digest


def test_push_applies_stable_per_submission_tag(monkeypatch):
    result, calls, digest = _run_push(monkeypatch, "sub_9b1e7f50e25f")
    assert result == f"{REPO}@{digest}"
    # BOTH the reused pr tag AND the never-reused per-submission tag are pushed.
    assert ["tag", "solver-abc123:screening", f"{REPO}:pr-3"] in calls
    assert ["push", f"{REPO}:pr-3"] in calls
    assert ["tag", "solver-abc123:screening", f"{REPO}:sub-sub_9b1e7f50e25f"] in calls
    assert ["push", f"{REPO}:sub-sub_9b1e7f50e25f"] in calls


def test_no_submission_id_still_pushes_pr_tag_only(monkeypatch):
    # Back-compat: omitting the submission id keeps the pr-<N> behavior (no crash).
    result, calls, digest = _run_push(monkeypatch, "")
    assert result == f"{REPO}@{digest}"
    assert ["push", f"{REPO}:pr-3"] in calls
    assert not any(a[:1] == ["push"] and "sub-" in a[-1] for a in calls)
