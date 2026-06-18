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
        cfg = ProtocolConfig.from_validator_registry(_RPC, _REGISTRY, quorum_address=_REGISTRY)
        assert cfg.quorum_bps == 6666
        assert cfg.rpc_url == _RPC
        assert cfg.registry_address == _REGISTRY
        mock_read.assert_called_once_with(_RPC, _REGISTRY)


def test_override_skips_chain_read():
    os.environ["QUORUM_BPS_OVERRIDE"] = "7500"
    with patch(
        "minotaur_subnet.consensus.protocol_config._read_quorum_bps",
    ) as mock_read:
        cfg = ProtocolConfig.from_validator_registry(_RPC, _REGISTRY, quorum_address=_REGISTRY)
        assert cfg.quorum_bps == 7500
        mock_read.assert_not_called()


def test_override_garbage_falls_back_to_chain():
    os.environ["QUORUM_BPS_OVERRIDE"] = "not-an-int"
    with patch(
        "minotaur_subnet.consensus.protocol_config._read_quorum_bps",
        return_value=6666,
    ):
        cfg = ProtocolConfig.from_validator_registry(_RPC, _REGISTRY, quorum_address=_REGISTRY)
        assert cfg.quorum_bps == 6666  # fell through


def test_chain_failure_propagates_at_startup():
    """Failing fast at startup is intentional — a misconfigured registry
    should be loud, not silently degrade to a hardcoded default."""
    with patch(
        "minotaur_subnet.consensus.protocol_config._read_quorum_bps",
        side_effect=ConnectionError("RPC down"),
    ):
        with pytest.raises(ConnectionError):
            ProtocolConfig.from_validator_registry(_RPC, _REGISTRY, quorum_address=_REGISTRY)


def test_quorum_address_required_no_silent_fallback():
    """quorum_address is REQUIRED — there must be no silent fallback to
    registry_address. The champion path uses a distinct ChampionRegistry as
    its quorum source, so silently defaulting to registry_address would read
    the quorum from the wrong contract / chain. Every caller must be explicit.
    """
    # Empty string (the previous default) now raises before any RPC happens.
    with patch(
        "minotaur_subnet.consensus.protocol_config._read_quorum_bps",
    ) as mock_read:
        with pytest.raises(ValueError, match="explicit quorum_address"):
            ProtocolConfig.from_validator_registry(_RPC, _REGISTRY)
        mock_read.assert_not_called()
    # None is equally rejected.
    with pytest.raises(ValueError, match="explicit quorum_address"):
        ProtocolConfig.from_validator_registry(
            _RPC, _REGISTRY, quorum_address=None,  # type: ignore[arg-type]
        )


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
async def test_refresh_loop_skips_quorum_under_override():
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
        # The loop continues to spin (it may run peer discovery if wired)
        # but must NOT call _read_quorum_bps while override is active.
        task = asyncio.create_task(cfg.refresh_loop())
        for _ in range(5):
            await asyncio.sleep(0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        mock_read.assert_not_called()


# ── Observability snapshot (registry_view surfaced in /health) ────────────

_PC = "minotaur_subnet.consensus.protocol_config"
_VSET = ["0x" + "aa" * 20, "0x" + "bb" * 20]


def test_observability_snapshot_stamped_at_construction():
    """from_validator_registry stamps chain_id / block / freshness so
    /health can show the exact registry view without any RPC on the
    health path."""
    with patch(f"{_PC}._read_quorum_bps", return_value=6666), \
         patch(f"{_PC}._read_validator_count", return_value=2), \
         patch(f"{_PC}._read_validators", return_value=list(_VSET)), \
         patch(f"{_PC}._read_chain_id", return_value=964), \
         patch(f"{_PC}._read_block_number", return_value=8_000_000):
        cfg = ProtocolConfig.from_validator_registry(_RPC, _REGISTRY, quorum_address=_REGISTRY)

    assert cfg.chain_id == 964
    assert cfg.last_refresh_block == 8_000_000
    assert cfg.last_successful_refresh_at is not None
    assert cfg.last_refresh_error is None

    snap = cfg.observability_snapshot()
    assert snap["on_chain_validator_count"] == 2
    assert snap["on_chain_validators"] == sorted(a.lower() for a in _VSET)
    assert snap["quorum_bps"] == 6666
    assert snap["chain_id"] == 964
    assert snap["rpc_block_number"] == 8_000_000
    assert snap["registry_address"] == _REGISTRY
    assert snap["last_refresh_error"] is None


def test_observability_records_error_on_failed_read():
    """A failed registry read at construction records the error and leaves
    last_successful_refresh_at unset — the 'frozen cache' signal."""
    with patch(f"{_PC}._read_quorum_bps", return_value=6666), \
         patch(f"{_PC}._read_validator_count", side_effect=ConnectionError("boom")), \
         patch(f"{_PC}._read_validators", return_value=[]), \
         patch(f"{_PC}._read_chain_id", return_value=964), \
         patch(f"{_PC}._read_block_number", return_value=8_000_000):
        cfg = ProtocolConfig.from_validator_registry(_RPC, _REGISTRY, quorum_address=_REGISTRY)

    assert cfg.last_successful_refresh_at is None
    assert cfg.last_refresh_error is not None
    assert "getValidatorCount" in cfg.last_refresh_error


@pytest.mark.asyncio
async def test_refresh_stamps_freshness_on_success():
    cfg = ProtocolConfig(
        quorum_bps=6666, rpc_url=_RPC, registry_address=_REGISTRY,
        refresh_interval_seconds=0,
    )
    cfg.last_refresh_error = "stale-from-before"  # must clear on success

    with patch(f"{_PC}._read_quorum_bps", return_value=6666), \
         patch(f"{_PC}._read_validator_count", return_value=3), \
         patch(f"{_PC}._read_validators", return_value=list(_VSET)), \
         patch(f"{_PC}._read_block_number", return_value=8_111_111):
        task = asyncio.create_task(cfg.refresh_loop())
        for _ in range(5):
            await asyncio.sleep(0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    assert cfg.on_chain_validator_count == 3
    assert cfg.last_refresh_block == 8_111_111
    assert cfg.last_successful_refresh_at is not None
    assert cfg.last_refresh_error is None


@pytest.mark.asyncio
async def test_refresh_records_error_and_freezes_cache_on_failure():
    """The prod TAO scenario: count read fails, so the cached count stays
    frozen (e.g. at a stale 7) and the error is recorded — never silently
    overwritten with a healthy-looking stamp."""
    cfg = ProtocolConfig(
        quorum_bps=6666, rpc_url=_RPC, registry_address=_REGISTRY,
        refresh_interval_seconds=0, on_chain_validator_count=7,
    )

    with patch(f"{_PC}._read_quorum_bps", return_value=6666), \
         patch(f"{_PC}._read_validator_count", side_effect=RuntimeError("rpc down")):
        task = asyncio.create_task(cfg.refresh_loop())
        for _ in range(5):
            await asyncio.sleep(0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    assert cfg.on_chain_validator_count == 7  # cached value preserved
    assert cfg.last_refresh_error is not None
    assert "getValidatorCount" in cfg.last_refresh_error
    assert cfg.last_successful_refresh_at is None
