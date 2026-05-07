"""Tests for image_id pinning on the reactive-benchmark path and hot-swap."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from minotaur_subnet.api.routes.submissions.champion_consensus import (
    _resolve_local_image_id,
    _reactive_benchmark_candidate,
)


@pytest.mark.asyncio
async def test_resolve_local_image_id_returns_sha_on_success():
    proc = MagicMock()
    proc.returncode = 0
    proc.communicate = AsyncMock(return_value=(b"sha256:abc123\n", b""))

    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
        assert await _resolve_local_image_id("solver-x:screening") == "sha256:abc123"


@pytest.mark.asyncio
async def test_resolve_local_image_id_returns_none_on_docker_failure():
    proc = MagicMock()
    proc.returncode = 1
    proc.communicate = AsyncMock(return_value=(b"", b"No such image"))

    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
        assert await _resolve_local_image_id("missing:latest") is None


@pytest.mark.asyncio
async def test_reactive_benchmark_refuses_on_image_id_mismatch():
    """Peer must NOT benchmark an image whose sha256 differs from the
    one the leader certified — that would score a different codebase."""
    candidate = MagicMock(
        submission_id="sub_1",
        image_tag="solver-x:screening",
        image_id="sha256:leader_certified",
    )

    with patch(
        "minotaur_subnet.api.routes.submissions.champion_consensus._resolve_local_image_id",
        new=AsyncMock(return_value="sha256:local_different"),
    ):
        verified, local_score = await _reactive_benchmark_candidate(
            candidate=candidate, leader_score=0.9, round_id="round-1",
        )
    assert verified is False
    assert local_score == 0.0


@pytest.mark.asyncio
async def test_reactive_benchmark_refuses_when_local_image_absent():
    candidate = MagicMock(
        submission_id="sub_2",
        image_tag="solver-x:screening",
        image_id="sha256:leader_certified",
    )

    with patch(
        "minotaur_subnet.api.routes.submissions.champion_consensus._resolve_local_image_id",
        new=AsyncMock(return_value=None),
    ):
        verified, local_score = await _reactive_benchmark_candidate(
            candidate=candidate, leader_score=0.9, round_id="round-1",
        )
    assert verified is False
    assert local_score == 0.0


@pytest.mark.asyncio
async def test_hot_swap_helper_resolves_image_id():
    """Epoch manager uses _resolve_image_id_via_docker to match on activation."""
    from minotaur_subnet.epoch.manager import _resolve_image_id_via_docker

    proc = MagicMock()
    proc.returncode = 0
    proc.communicate = AsyncMock(return_value=(b"sha256:xyz\n", b""))

    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
        assert await _resolve_image_id_via_docker("solver:latest") == "sha256:xyz"
