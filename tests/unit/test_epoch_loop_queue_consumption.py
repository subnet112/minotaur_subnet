"""Tests for the queue-first / burn-second priority in ``_epoch_loop``.

The single-emit-path refactor introduces a slot
(``self._queued_weights_mapping``) that the api EpochManager populates
via ``/internal/weights/queue``. The daemon's ``_epoch_loop`` consumes
the slot on its next tick and emits via the SAME WeightsEmitter that
handles the burn fallback. Ordering matters:

  1. Queue populated → emit queued mapping (source="queued_from_api").
     The burn path does NOT run this tick.
  2. Queue empty → burn fallback via ``weights.maybe_emit``.

This file pins those two cases plus several edge cases:

  - the queue is CONSUMED (cleared) after emit, so the next tick goes
    to burn unless the api POSTs another mapping;
  - a queue emit FAILURE does not re-queue the failed mapping (the
    api will POST a fresh one on the next round close);
  - the recorded ``source`` discriminator is correct so the
    validator-health workflow can tell which path drove the emit;
  - ``_persist_last_emit_state`` is called on every state mutation
    so a Watchtower restart doesn't wipe the attestation.

Burn-safety regression check: every failure mode of the queue path
must leave the burn fallback functional on the NEXT tick. Those
properties are exercised end-to-end here.
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


def _make_stub(*, queued=None, queued_source=None, burn_returns=None):
    """Build a validator stub for _epoch_loop testing."""
    self_stub = MagicMock()
    self_stub.weights = MagicMock()
    self_stub.weights.maybe_emit = MagicMock(return_value=burn_returns)
    self_stub._champion_miner_id = None
    self_stub._weights_emitter = MagicMock()
    self_stub._weights_emitter.emit_async = AsyncMock(return_value=True)
    self_stub._queued_weights_mapping = queued
    self_stub._queued_weights_source = queued_source
    self_stub._last_emit_state = None
    # Bind real _do_emit so it actually exercises emit_async + state.
    self_stub._do_emit = AppIntentsValidator._do_emit.__get__(
        self_stub, AppIntentsValidator,
    )
    return self_stub


async def _run_n_iterations(self_stub, n: int) -> None:
    """Run ``_epoch_loop`` for ``n`` full iterations, then cancel."""
    call_count = {"sleep": 0}

    async def fake_sleep(delay):
        call_count["sleep"] += 1
        if call_count["sleep"] > n:
            raise asyncio.CancelledError

    with patch("minotaur_subnet.validator.main.asyncio.sleep", new=fake_sleep):
        with pytest.raises(asyncio.CancelledError):
            await AppIntentsValidator._epoch_loop(self_stub)


# ── Queue takes priority over burn ──────────────────────────────────────


@pytest.mark.asyncio
async def test_queued_mapping_emits_when_burn_would_also_fire():
    """When BOTH a queue mapping and a burn-eligible weights.maybe_emit
    return are available, the queue wins. This is the architecturally
    enforced single-source-of-truth — per-miner ranking from the api
    always supersedes the burn fallback when both are present."""
    queued = {"5MinerA": 0.7, "5MinerB": 0.3}
    burn = {"5OwnerHotkey": 1.0}
    self_stub = _make_stub(
        queued=queued,
        queued_source="epoch_manager",
        burn_returns=burn,
    )

    await _run_n_iterations(self_stub, 1)

    # emit_async called exactly once, with the QUEUED mapping, not burn.
    self_stub._weights_emitter.emit_async.assert_awaited_once_with(queued)
    # burn path skipped → maybe_emit NOT consulted at all this tick.
    self_stub.weights.maybe_emit.assert_not_called()
    # source attribution preserved.
    assert self_stub._last_emit_state["source"] == "epoch_manager"


@pytest.mark.asyncio
async def test_queue_consumed_after_emit():
    """After consumption, the slot is cleared. The next tick falls
    through to burn unless the api POSTs another mapping."""
    queued = {"5MinerA": 1.0}
    burn = {"5OwnerHotkey": 1.0}
    self_stub = _make_stub(
        queued=queued,
        queued_source="epoch_manager",
        burn_returns=burn,
    )

    await _run_n_iterations(self_stub, 2)

    # Two emit calls total: first with queued, second with burn.
    assert self_stub._weights_emitter.emit_async.await_count == 2
    calls = self_stub._weights_emitter.emit_async.await_args_list
    assert calls[0].args[0] == queued
    assert calls[1].args[0] == burn
    # Slot is cleared at the end.
    assert self_stub._queued_weights_mapping is None
    assert self_stub._queued_weights_source is None


@pytest.mark.asyncio
async def test_queued_source_defaults_when_missing():
    """If the api forgets to set _queued_weights_source for some reason
    (None instead of a string), _do_emit still records SOMETHING in
    last_emit.source — never None — so the workflow's classifier
    can always read it."""
    self_stub = _make_stub(
        queued={"5MinerA": 1.0},
        queued_source=None,  # ← unusual but possible
        burn_returns=None,
    )

    await _run_n_iterations(self_stub, 1)

    # source falls through to "queued" (the default we pass to _do_emit)
    assert self_stub._last_emit_state["source"] == "queued"


# ── Burn fallback fires when queue is empty ─────────────────────────────


@pytest.mark.asyncio
async def test_burn_fires_when_queue_empty():
    """The whole point of the burn fallback: if the queue is empty
    (api never POSTed, api is down, etc.), the validator still emits
    weights so it keeps earning dividends."""
    burn = {"5OwnerHotkey": 1.0}
    self_stub = _make_stub(queued=None, burn_returns=burn)

    await _run_n_iterations(self_stub, 1)

    self_stub._weights_emitter.emit_async.assert_awaited_once_with(burn)
    assert self_stub._last_emit_state["source"] == "burn_fallback"


@pytest.mark.asyncio
async def test_no_emit_when_queue_empty_and_burn_rate_limited():
    """``weights.maybe_emit`` returns None when the epoch_seconds window
    hasn't elapsed. We must NOT call emit_async then — that's the
    chain-rate-limit-respecting layer."""
    self_stub = _make_stub(queued=None, burn_returns=None)

    await _run_n_iterations(self_stub, 1)

    self_stub._weights_emitter.emit_async.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_emit_when_weights_emitter_missing():
    """If the daemon couldn't load a wallet, the queue endpoint already
    returns 503 (so the api won't POST). Even if a mapping somehow
    sat in the slot, the loop must short-circuit before calling
    emit_async on None."""
    self_stub = _make_stub(queued={"5A": 1.0}, burn_returns={"5B": 1.0})
    self_stub._weights_emitter = None

    await _run_n_iterations(self_stub, 1)
    # No crash, no emit. The slot is left as-is (we never reached
    # the consume step).


# ── Failure semantics: queue emit fails → next tick still works ─────────


@pytest.mark.asyncio
async def test_queue_emit_failure_does_not_re_queue():
    """When emit_async raises on a queue mapping, we do NOT put it back
    in the slot. The api will POST a fresh mapping next round close.
    Otherwise a poison mapping could starve the burn path forever."""
    queued = {"5MinerA": 1.0}
    burn = {"5OwnerHotkey": 1.0}
    self_stub = _make_stub(
        queued=queued, queued_source="epoch_manager", burn_returns=burn,
    )
    # First emit raises (queue path), second succeeds (burn path).
    self_stub._weights_emitter.emit_async = AsyncMock(
        side_effect=[RuntimeError("substrate timeout"), True],
    )

    await _run_n_iterations(self_stub, 2)

    # Two calls: first with queued mapping (failed), second with burn.
    assert self_stub._weights_emitter.emit_async.await_count == 2
    calls = self_stub._weights_emitter.emit_async.await_args_list
    assert calls[0].args[0] == queued
    assert calls[1].args[0] == burn

    # last_emit on tick 2 (the most recent) is the burn success.
    assert self_stub._last_emit_state["result"] == "ok"
    assert self_stub._last_emit_state["source"] == "burn_fallback"


@pytest.mark.asyncio
async def test_persist_called_on_every_emit_attempt():
    """``_persist_last_emit_state`` must run after EVERY emit attempt so
    a Watchtower restart between ticks preserves the attestation. This
    is the fix for the "leader flagged external after restart" symptom."""
    burn = {"5OwnerHotkey": 1.0}
    self_stub = _make_stub(queued=None, burn_returns=burn)

    await _run_n_iterations(self_stub, 1)

    self_stub._persist_last_emit_state.assert_called()


@pytest.mark.asyncio
async def test_persist_called_on_emit_failure():
    """Even a failed emit must persist — the workflow needs to see the
    error in /health.last_emit after a restart, otherwise we lose the
    failure signal."""
    burn = {"5OwnerHotkey": 1.0}
    self_stub = _make_stub(queued=None, burn_returns=burn)
    self_stub._weights_emitter.emit_async = AsyncMock(
        side_effect=RuntimeError("oh no"),
    )

    await _run_n_iterations(self_stub, 1)

    self_stub._persist_last_emit_state.assert_called()
    assert self_stub._last_emit_state["result"] == "error"
