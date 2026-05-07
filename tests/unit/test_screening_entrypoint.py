"""Tests for the Stage 2 runtime entrypoint-unchanged check."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from minotaur_subnet.harness.screening import (
    _FROM_LINE,
    _verify_entrypoint_unchanged,
)


def test_from_line_regex_extracts_image_ref():
    m = _FROM_LINE.search("""FROM ghcr.io/subnet112/solver-base:latest
RUN echo hi""")
    assert m is not None
    assert m.group(1) == "ghcr.io/subnet112/solver-base:latest"


@pytest.mark.asyncio
async def test_entrypoint_match_passes(tmp_path):
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text("FROM ghcr.io/subnet112/solver-base:v1\nRUN echo hi\n")

    async def fake_inspect(image_ref):
        return (["python", "-m", "harness.runner"], None)

    with patch(
        "minotaur_subnet.harness.screening._docker_inspect_entrypoint_and_cmd",
        side_effect=fake_inspect,
    ):
        assert await _verify_entrypoint_unchanged(str(tmp_path), "solver-x:screening") is None


@pytest.mark.asyncio
async def test_entrypoint_override_rejected(tmp_path):
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text("FROM ghcr.io/subnet112/solver-base:v1\n")

    async def fake_inspect(image_ref):
        if image_ref == "ghcr.io/subnet112/solver-base:v1":
            return (["python", "-m", "harness.runner"], None)
        # Built image diverged: different entrypoint
        return (["/bin/sh"], None)

    with patch(
        "minotaur_subnet.harness.screening._docker_inspect_entrypoint_and_cmd",
        side_effect=fake_inspect,
    ):
        err = await _verify_entrypoint_unchanged(str(tmp_path), "solver-x:screening")
    assert err is not None
    assert "overrode ENTRYPOINT" in err


@pytest.mark.asyncio
async def test_cmd_override_rejected(tmp_path):
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text("FROM ghcr.io/subnet112/solver-base:v1\n")

    async def fake_inspect(image_ref):
        if image_ref == "ghcr.io/subnet112/solver-base:v1":
            return (["python", "-m", "harness.runner"], None)
        return (["python", "-m", "harness.runner"], ["evil.sh"])

    with patch(
        "minotaur_subnet.harness.screening._docker_inspect_entrypoint_and_cmd",
        side_effect=fake_inspect,
    ):
        err = await _verify_entrypoint_unchanged(str(tmp_path), "solver-x:screening")
    assert err is not None
    assert "overrode CMD" in err


@pytest.mark.asyncio
async def test_unreadable_base_returns_clear_error(tmp_path):
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text("FROM ghcr.io/subnet112/solver-base:v1\n")

    async def fake_inspect(image_ref):
        if image_ref == "ghcr.io/subnet112/solver-base:v1":
            return None  # can't pull / inspect base
        return (["ok"], None)

    with patch(
        "minotaur_subnet.harness.screening._docker_inspect_entrypoint_and_cmd",
        side_effect=fake_inspect,
    ):
        err = await _verify_entrypoint_unchanged(str(tmp_path), "solver-x:screening")
    assert err is not None
    assert "base image" in err


@pytest.mark.asyncio
async def test_no_from_line_rejected(tmp_path):
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text("RUN echo hi  # but no FROM\n")
    err = await _verify_entrypoint_unchanged(str(tmp_path), "solver-x:screening")
    assert err is not None
    assert "FROM line" in err
