"""Tests for the burn-fallback defer logic + EpochManager instrumentation.

Yesterday's "leader is flagged external by validator-health" incident
exposed that the leader has two emit code paths racing for the chain
rate-limit slot:

  - validator daemon's ``_epoch_loop`` → simple burn / champion weights
  - api's ``EpochManager._emit_weights`` → per-miner solver-round scores

When the daemon's tick fires within the chain's rate-limit window of a
recent EpochManager emission, the daemon's burn fallback would race for
the slot — and if it won, the api's real per-miner ranking got dropped.

The fix: the daemon defers when chain shows a fresh last_update. These
tests pin that defer behaviour + the EpochManager's new
``_last_emit_state`` field so the validator-health workflow can attribute
either-path emissions to "self".
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from minotaur_subnet.validator.main import AppIntentsValidator


async def _run_one_iteration(self_stub) -> None:
    """Run ``_epoch_loop`` long enough to execute one iteration body."""
    call_count = {"sleep": 0}

    async def fake_sleep(delay):
        call_count["sleep"] += 1
        if call_count["sleep"] >= 2:
            raise asyncio.CancelledError

    with patch("minotaur_subnet.validator.main.asyncio.sleep", new=fake_sleep):
        with pytest.raises(asyncio.CancelledError):
            await AppIntentsValidator._epoch_loop(self_stub)


def _make_self_stub(*,
                    my_uid: int | None = 0,
                    my_last_update_block: int | None = None,
                    state_block: int = 8275000,
                    epoch_seconds: int = 1200,
                    weights_returned: dict | None = None):
    """Build a minimal validator stub with metagraph_sync state populated."""
    self_stub = MagicMock()
    self_stub._champion_miner_id = None
    self_stub._weights_emitter = MagicMock()
    self_stub._weights_emitter.emit_async = AsyncMock(return_value=True)
    self_stub._is_leader = True
    self_stub._last_emit_state = None

    self_stub.weights = MagicMock()
    self_stub.weights.epoch_seconds = epoch_seconds
    self_stub.weights.maybe_emit = MagicMock(
        return_value=weights_returned if weights_returned is not None
                     else {"5OwnerHotkey": 1.0}
    )

    state = MagicMock()
    state.my_uid = my_uid
    state.my_last_update_block = my_last_update_block
    state.block = state_block
    self_stub._metagraph_sync = MagicMock()
    self_stub._metagraph_sync.state = state

    return self_stub


# ── defer-on-fresh-chain behaviour ──────────────────────────────────────


@pytest.mark.asyncio
async def test_emits_when_chain_is_stale():
    """Standard happy path: chain shows old last_update, daemon emits."""
    # Chain shows last_update 30 min ago (state_block - my_last_update_block
    # = 150 blocks * 12s = 1800s, > 0.9 * 1200 = 1080s threshold)
    self_stub = _make_self_stub(
        my_uid=0,
        my_last_update_block=8275000 - 150,
        state_block=8275000,
    )
    await _run_one_iteration(self_stub)

    self_stub._weights_emitter.emit_async.assert_awaited_once()
    assert self_stub._last_emit_state["result"] == "ok"


@pytest.mark.asyncio
async def test_defers_when_chain_shows_fresh_emit():
    """Chain shows our last_update was 5 min ago — burn fallback defers."""
    # state_block - my_last_update_block = 25 blocks * 12s = 300s, < 1080s
    self_stub = _make_self_stub(
        my_uid=0,
        my_last_update_block=8275000 - 25,
        state_block=8275000,
    )
    await _run_one_iteration(self_stub)

    self_stub._weights_emitter.emit_async.assert_not_awaited()
    assert self_stub._last_emit_state["result"] == "deferred"
    assert "fresh last_update" in self_stub._last_emit_state["error"]


@pytest.mark.asyncio
async def test_emits_when_chain_just_at_threshold():
    """Chain shows last_update at 91% of epoch — proceeds with emit."""
    # 90% of 1200s = 1080s threshold; 91% = 1092s = 91 blocks
    self_stub = _make_self_stub(
        my_uid=0,
        my_last_update_block=8275000 - 91,
        state_block=8275000,
    )
    await _run_one_iteration(self_stub)

    self_stub._weights_emitter.emit_async.assert_awaited_once()


@pytest.mark.asyncio
async def test_emits_when_my_last_update_block_is_zero():
    """Brand-new validator (never emitted) — no chain state to defer to."""
    self_stub = _make_self_stub(
        my_uid=0,
        my_last_update_block=0,
        state_block=8275000,
    )
    await _run_one_iteration(self_stub)

    self_stub._weights_emitter.emit_async.assert_awaited_once()


@pytest.mark.asyncio
async def test_emits_when_my_uid_none():
    """No metagraph entry → no defer state available → emit normally."""
    self_stub = _make_self_stub(
        my_uid=None,
        my_last_update_block=None,
        state_block=8275000,
    )
    await _run_one_iteration(self_stub)

    self_stub._weights_emitter.emit_async.assert_awaited_once()


@pytest.mark.asyncio
async def test_emits_when_metagraph_sync_state_unavailable():
    """metagraph_sync.state is None (initial sync didn't complete) — fall
    through to emit rather than block. We'd rather race than be silent."""
    self_stub = _make_self_stub()
    self_stub._metagraph_sync.state = None
    await _run_one_iteration(self_stub)

    self_stub._weights_emitter.emit_async.assert_awaited_once()


# ── EpochManager last_emit_state ────────────────────────────────────────


def test_epoch_manager_has_last_emit_state_field():
    """EpochManager initializes ``_last_emit_state`` to None so the api's
    /health can surface it without AttributeError."""
    from minotaur_subnet.epoch.manager import EpochManager
    em = EpochManager()
    assert hasattr(em, "_last_emit_state")
    assert em._last_emit_state is None


@pytest.mark.asyncio
async def test_epoch_manager_records_successful_emit():
    """A successful _emit_weights populates _last_emit_state with result=ok
    and source='epoch_manager' (so the workflow's classifier can tell
    which code path attempted)."""
    from minotaur_subnet.epoch.manager import EpochManager
    em = EpochManager()
    em._weights_emitter = MagicMock()
    em._weights_emitter.emit_async = AsyncMock(return_value=True)
    # Skip the mapping builder — patch it to return a non-empty dict
    em._build_weights_mapping = MagicMock(return_value={"5Miner": 1.0})

    success = await em._emit_weights(epoch=10, round_id="r-10")

    assert success is True
    assert em._last_emit_state["result"] == "ok"
    assert em._last_emit_state["source"] == "epoch_manager"
    assert em._last_emit_state["uids_attempted"] == 1


@pytest.mark.asyncio
async def test_epoch_manager_records_empty_mapping():
    """When _build_weights_mapping returns empty, record result='empty'
    (distinct from 'error' — this is benign, no miners to score)."""
    from minotaur_subnet.epoch.manager import EpochManager
    em = EpochManager()
    em._weights_emitter = MagicMock()
    em._weights_emitter.emit_async = AsyncMock()
    em._build_weights_mapping = MagicMock(return_value={})

    success = await em._emit_weights(epoch=10)

    assert success is False
    assert em._last_emit_state["result"] == "empty"
    em._weights_emitter.emit_async.assert_not_called()


@pytest.mark.asyncio
async def test_epoch_manager_records_emit_failure():
    """emit_async returning False (chain rate-limit rejection) → result=error."""
    from minotaur_subnet.epoch.manager import EpochManager
    em = EpochManager()
    em._weights_emitter = MagicMock()
    em._weights_emitter.emit_async = AsyncMock(return_value=False)
    em._build_weights_mapping = MagicMock(return_value={"5Miner": 1.0})

    success = await em._emit_weights(epoch=10)

    assert success is False
    assert em._last_emit_state["result"] == "error"
    assert "emit_async returned False" in em._last_emit_state["error"]


@pytest.mark.asyncio
async def test_epoch_manager_records_exception():
    """Unhandled exception during emit_async → result=error, truncated."""
    from minotaur_subnet.epoch.manager import EpochManager
    em = EpochManager()
    em._weights_emitter = MagicMock()
    em._weights_emitter.emit_async = AsyncMock(
        side_effect=RuntimeError("substrate timeout " + "x" * 500)
    )
    em._build_weights_mapping = MagicMock(return_value={"5Miner": 1.0})

    success = await em._emit_weights(epoch=10)

    assert success is False
    assert em._last_emit_state["result"] == "error"
    assert len(em._last_emit_state["error"]) <= 300
    assert "substrate timeout" in em._last_emit_state["error"]
