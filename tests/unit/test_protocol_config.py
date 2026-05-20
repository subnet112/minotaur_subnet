"""Unit tests for ProtocolConfig — startup read, env override, refresh loop."""

from __future__ import annotations

import asyncio
import os
from unittest.mock import patch

import pytest

from minotaur_subnet.consensus.protocol_config import ProtocolConfig


_REGISTRY = "0x" + "11" * 20
_RPC = "http://anvil:8545"


@pytest.fixture(autouse=True)
def _clear_override():
    """Make sure no test leaks the override env var to the next one."""
    old = os.environ.pop("QUORUM_BPS_OVERRIDE", None)
    yield
    if old is not None:
        os.environ["QUORUM_BPS_OVERRIDE"] = old


def test_from_validator_registry_reads_chain():
    with patch(
        "minotaur_subnet.consensus.protocol_config._read_quorum_bps",
        return_value=6666,
    ) as mock_read:
        cfg = ProtocolConfig.from_validator_registry(_RPC, _REGISTRY)
        assert cfg.quorum_bps == 6666
        assert cfg.rpc_url == _RPC
        assert cfg.registry_address == _REGISTRY
        mock_read.assert_called_once_with(_RPC, _REGISTRY)


def test_override_skips_chain_read():
    os.environ["QUORUM_BPS_OVERRIDE"] = "7500"
    with patch(
        "minotaur_subnet.consensus.protocol_config._read_quorum_bps",
    ) as mock_read:
        cfg = ProtocolConfig.from_validator_registry(_RPC, _REGISTRY)
        assert cfg.quorum_bps == 7500
        mock_read.assert_not_called()


def test_override_garbage_falls_back_to_chain():
    os.environ["QUORUM_BPS_OVERRIDE"] = "not-an-int"
    with patch(
        "minotaur_subnet.consensus.protocol_config._read_quorum_bps",
        return_value=6666,
    ):
        cfg = ProtocolConfig.from_validator_registry(_RPC, _REGISTRY)
        assert cfg.quorum_bps == 6666  # fell through


def test_chain_failure_propagates_at_startup():
    """Failing fast at startup is intentional — a misconfigured registry
    should be loud, not silently degrade to a hardcoded default."""
    with patch(
        "minotaur_subnet.consensus.protocol_config._read_quorum_bps",
        side_effect=ConnectionError("RPC down"),
    ):
        with pytest.raises(ConnectionError):
            ProtocolConfig.from_validator_registry(_RPC, _REGISTRY)


@pytest.mark.asyncio
async def test_refresh_loop_updates_in_place():
    cfg = ProtocolConfig(
        quorum_bps=6666,
        rpc_url=_RPC,
        registry_address=_REGISTRY,
        refresh_interval_seconds=0,  # tight loop for the test
    )

    # Sequence: first refresh sees 6666 (no change), second sees 8000 (change).
    values = iter([6666, 8000, 8000])
    with patch(
        "minotaur_subnet.consensus.protocol_config._read_quorum_bps",
        side_effect=lambda *_a, **_kw: next(values),
    ):
        task = asyncio.create_task(cfg.refresh_loop())
        # Yield a few times so the task can run two iterations.
        for _ in range(5):
            await asyncio.sleep(0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    assert cfg.quorum_bps == 8000


@pytest.mark.asyncio
async def test_refresh_loop_survives_rpc_error():
    cfg = ProtocolConfig(
        quorum_bps=6666,
        rpc_url=_RPC,
        registry_address=_REGISTRY,
        refresh_interval_seconds=0,
    )

    def fake(_rpc, _reg):
        raise RuntimeError("transient blip")

    with patch(
        "minotaur_subnet.consensus.protocol_config._read_quorum_bps",
        side_effect=fake,
    ):
        task = asyncio.create_task(cfg.refresh_loop())
        for _ in range(5):
            await asyncio.sleep(0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    # Cached value preserved through transient errors.
    assert cfg.quorum_bps == 6666


@pytest.mark.asyncio
async def test_refresh_loop_noops_under_override():
    os.environ["QUORUM_BPS_OVERRIDE"] = "9000"
    cfg = ProtocolConfig(
        quorum_bps=9000,
        rpc_url=_RPC,
        registry_address=_REGISTRY,
        refresh_interval_seconds=0,
    )

    with patch(
        "minotaur_subnet.consensus.protocol_config._read_quorum_bps",
    ) as mock_read:
        # The loop should return immediately without calling _read.
        await asyncio.wait_for(cfg.refresh_loop(), timeout=0.5)
        mock_read.assert_not_called()
