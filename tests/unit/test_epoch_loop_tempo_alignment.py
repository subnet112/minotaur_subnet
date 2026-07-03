"""Tests for tempo-aligned commit scheduling in ``_epoch_loop``.

Commit-reveal keeps ONE pending commit per validator per tempo epoch —
committing twice in an epoch silently discards the first commit. The
TempoEmitGate therefore gates ALL emission (queued mapping AND the
champion/burn fallback) on a pre-boundary window:

  - gate False → nothing emits this tick; a queued mapping WAITS in its
    slot instead of being consumed mid-epoch (this is the fix for the
    "48-min champion earned zero" incident: the round-activation emit no
    longer erases the pending commit);
  - gate True  → normal priority order (queue first, champion/burn
    fallback second), bypassing the wall-clock ``maybe_emit`` check, and a
    SUCCESSFUL emit marks the boundary committed;
  - gate None  → chain state unknown: the legacy wall-clock cadence runs
    unchanged (pinned exhaustively by test_epoch_loop_queue_consumption /
    test_epoch_loop_emits_for_followers with ``_tempo_gate = None``).
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from minotaur_subnet.validator.main import AppIntentsValidator


def _make_stub(*, gate_decision, queued=None, queued_source=None,
               champion=None, emit_result=True):
    self_stub = MagicMock()
    self_stub._tempo_gate = MagicMock()
    self_stub._tempo_gate.should_emit_now = AsyncMock(return_value=gate_decision)
    self_stub._tempo_gate.mark_committed = MagicMock()

    self_stub.weights = MagicMock()
    burn_weights = (
        {champion: 0.1, "5Owner": 0.9} if champion else {"5Owner": 1.0}
    )
    self_stub.weights.close_epoch_now = MagicMock(return_value=burn_weights)
    self_stub.weights.maybe_emit = MagicMock(return_value=burn_weights)

    self_stub._champion_miner_id = champion
    self_stub._local_champion_hotkey = AsyncMock(return_value=champion)
    self_stub._champion_source = "api"
    self_stub._weights_emitter = MagicMock()
    self_stub._weights_emitter.emit_async = AsyncMock(return_value=emit_result)
    self_stub._queued_weights_mapping = queued
    self_stub._queued_weights_source = queued_source
    self_stub._last_emit_state = None
    self_stub._do_emit = AppIntentsValidator._do_emit.__get__(
        self_stub, AppIntentsValidator,
    )
    return self_stub


async def _run_n_iterations(self_stub, n: int) -> None:
    call_count = {"sleep": 0}

    async def fake_sleep(delay):
        call_count["sleep"] += 1
        if call_count["sleep"] > n:
            raise asyncio.CancelledError

    with patch("minotaur_subnet.validator.main.asyncio.sleep", new=fake_sleep):
        with pytest.raises(asyncio.CancelledError):
            await AppIntentsValidator._epoch_loop(self_stub)


# ── gate False: hold everything ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_outside_window_holds_queued_mapping():
    """Mid-epoch, a queued mapping is NOT consumed and nothing emits. The
    mapping waits (newest-wins slot) for the pre-boundary window."""
    queued = {"5MinerA": 0.7, "5Owner": 0.3}
    self_stub = _make_stub(gate_decision=False, queued=queued,
                           queued_source="epoch_manager")

    await _run_n_iterations(self_stub, 3)

    self_stub._weights_emitter.emit_async.assert_not_awaited()
    self_stub.weights.maybe_emit.assert_not_called()
    self_stub.weights.close_epoch_now.assert_not_called()
    # Held, not consumed.
    assert self_stub._queued_weights_mapping == queued
    assert self_stub._queued_weights_source == "epoch_manager"


@pytest.mark.asyncio
async def test_outside_window_skips_champion_fallback():
    self_stub = _make_stub(gate_decision=False, champion="5Champ")

    await _run_n_iterations(self_stub, 1)

    self_stub._weights_emitter.emit_async.assert_not_awaited()
    self_stub._local_champion_hotkey.assert_not_awaited()


# ── gate True: emit now, mark on success ─────────────────────────────────


@pytest.mark.asyncio
async def test_in_window_emits_queued_mapping_and_marks():
    queued = {"5MinerA": 0.7, "5Owner": 0.3}
    self_stub = _make_stub(gate_decision=True, queued=queued,
                           queued_source="epoch_manager")

    await _run_n_iterations(self_stub, 1)

    self_stub._weights_emitter.emit_async.assert_awaited_once_with(queued)
    self_stub._tempo_gate.mark_committed.assert_called_once()
    assert self_stub._queued_weights_mapping is None
    assert self_stub._last_emit_state["source"] == "epoch_manager"


@pytest.mark.asyncio
async def test_in_window_champion_path_bypasses_wall_clock():
    """No queue → the champion/burn fallback runs via close_epoch_now (the
    gate owns timing), NOT maybe_emit (whose epoch_seconds clock could veto
    the one commit that matters)."""
    self_stub = _make_stub(gate_decision=True, champion="5Champ")

    await _run_n_iterations(self_stub, 1)

    self_stub.weights.close_epoch_now.assert_called_once_with("5Champ")
    self_stub.weights.maybe_emit.assert_not_called()
    self_stub._weights_emitter.emit_async.assert_awaited_once_with(
        {"5Champ": 0.1, "5Owner": 0.9},
    )
    self_stub._tempo_gate.mark_committed.assert_called_once()
    assert self_stub._last_emit_state["source"] == "champion"


@pytest.mark.asyncio
async def test_failed_emit_does_not_mark_boundary():
    """emit_async returning False (chain rejection) must leave the boundary
    unmarked so the next tick in the window retries."""
    self_stub = _make_stub(gate_decision=True, champion="5Champ",
                           emit_result=False)

    await _run_n_iterations(self_stub, 1)

    self_stub._weights_emitter.emit_async.assert_awaited_once()
    self_stub._tempo_gate.mark_committed.assert_not_called()


@pytest.mark.asyncio
async def test_raising_emit_does_not_mark_boundary():
    self_stub = _make_stub(gate_decision=True, champion="5Champ")
    self_stub._weights_emitter.emit_async = AsyncMock(
        side_effect=RuntimeError("substrate timeout"),
    )

    await _run_n_iterations(self_stub, 1)

    self_stub._tempo_gate.mark_committed.assert_not_called()


@pytest.mark.asyncio
async def test_in_window_unresolved_champion_still_skips():
    """The skip-don't-burn protection survives tempo mode: champion
    UNRESOLVED (source != 'api') inside the window → no emit, retry next
    tick. Burning out of ignorance at the boundary would be strictly worse
    than legacy — that commit is the epoch's ONLY reveal."""
    self_stub = _make_stub(gate_decision=True, champion=None)
    self_stub._champion_source = "none"
    self_stub._local_champion_hotkey = AsyncMock(return_value=None)

    await _run_n_iterations(self_stub, 1)

    self_stub._weights_emitter.emit_async.assert_not_awaited()
    self_stub._tempo_gate.mark_committed.assert_not_called()


# ── gate None: legacy fallback ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_unknown_chain_state_falls_back_to_wall_clock():
    """gate None → the legacy path: queue consumed immediately, maybe_emit
    (wall clock) governs the fallback, and the gate is never marked."""
    queued = {"5MinerA": 1.0}
    self_stub = _make_stub(gate_decision=None, queued=queued,
                           queued_source="epoch_manager")

    await _run_n_iterations(self_stub, 2)

    # Tick 1: queue consumed (legacy immediate consumption).
    # Tick 2: burn fallback via maybe_emit.
    assert self_stub._weights_emitter.emit_async.await_count == 2
    calls = self_stub._weights_emitter.emit_async.await_args_list
    assert calls[0].args[0] == queued
    self_stub.weights.maybe_emit.assert_called_once()
    self_stub.weights.close_epoch_now.assert_not_called()
    self_stub._tempo_gate.mark_committed.assert_not_called()
