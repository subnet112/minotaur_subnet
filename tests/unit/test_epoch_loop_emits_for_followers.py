"""Regression test for the leader-gate weight-emission bug.

Pre-fix: ``AppIntentsValidator._epoch_loop`` only called ``weights_emitter.emit_async``
when ``self._is_leader`` was True. That's the subnet-team order-consensus
leader-election flag — which has nothing to do with whether a validator
should set weights on Bittensor (every validator does, that's Yuma's
whole design). Result: every non-leader validator on subnet 112 silently
stopped emitting weights to subtensor → lost dividends → looked dead on
the network even though their daemon was up and answering /identity.

This test pins the post-fix behaviour: follower validators MUST emit
weights too, with the same cadence the leader does (governed by
``ChampionWeights.maybe_emit``).
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


async def _run_one_iteration(self_stub) -> None:
    """Run ``_epoch_loop`` long enough to execute one iteration, then exit.

    The loop is ``while True`` so we patch ``asyncio.sleep`` to raise
    ``CancelledError`` on the second invocation, which lets the first
    iteration's body run fully before the loop terminates.
    """
    call_count = {"sleep": 0}

    async def fake_sleep(delay):
        call_count["sleep"] += 1
        if call_count["sleep"] >= 2:
            raise asyncio.CancelledError
        # First sleep returns instantly, letting maybe_emit / emit_async run.

    with patch("minotaur_subnet.validator.main.asyncio.sleep", new=fake_sleep):
        with pytest.raises(asyncio.CancelledError):
            await AppIntentsValidator._epoch_loop(self_stub)


@pytest.mark.asyncio
async def test_emits_when_validator_is_follower():
    """The fix in one sentence: follower validators emit weights too."""
    self_stub = MagicMock()
    self_stub.weights = MagicMock()
    self_stub.weights.maybe_emit = MagicMock(return_value={"5HOwnerHotkey": 1.0})
    self_stub._champion_miner_id = None
    self_stub._weights_emitter = MagicMock()
    self_stub._weights_emitter.emit_async = AsyncMock()
    self_stub._is_leader = False  # ← the case that was silently broken

    await _run_one_iteration(self_stub)

    self_stub._weights_emitter.emit_async.assert_awaited_once_with(
        {"5HOwnerHotkey": 1.0},
    )


@pytest.mark.asyncio
async def test_emits_when_validator_is_leader():
    """Leaders keep emitting too — symmetry; we removed the gate, not the call."""
    self_stub = MagicMock()
    self_stub.weights = MagicMock()
    self_stub.weights.maybe_emit = MagicMock(return_value={"5HOwnerHotkey": 1.0})
    self_stub._champion_miner_id = None
    self_stub._weights_emitter = MagicMock()
    self_stub._weights_emitter.emit_async = AsyncMock()
    self_stub._is_leader = True

    await _run_one_iteration(self_stub)

    self_stub._weights_emitter.emit_async.assert_awaited_once()


@pytest.mark.asyncio
async def test_skips_when_no_weights_due_to_rate_limit():
    """``ChampionWeights.maybe_emit`` returns None when the epoch hasn't
    elapsed yet. We must NOT call emit_async in that case — that's the
    rate-limiting layer."""
    self_stub = MagicMock()
    self_stub.weights = MagicMock()
    self_stub.weights.maybe_emit = MagicMock(return_value=None)  # rate-limited
    self_stub._champion_miner_id = None
    self_stub._weights_emitter = MagicMock()
    self_stub._weights_emitter.emit_async = AsyncMock()
    self_stub._is_leader = False

    await _run_one_iteration(self_stub)

    self_stub._weights_emitter.emit_async.assert_not_awaited()


@pytest.mark.asyncio
async def test_skips_when_emitter_unset():
    """Bittensor integration disabled → ``_weights_emitter`` is None and the
    loop must not try to dereference emit_async."""
    self_stub = MagicMock()
    self_stub.weights = MagicMock()
    self_stub.weights.maybe_emit = MagicMock(return_value={"5HOwnerHotkey": 1.0})
    self_stub._champion_miner_id = None
    self_stub._weights_emitter = None  # not wired
    self_stub._is_leader = False

    # If the loop tried to call .emit_async on None it'd raise AttributeError
    # which is NOT CancelledError, and the test would fail.
    await _run_one_iteration(self_stub)


@pytest.mark.asyncio
async def test_emission_exception_is_swallowed_not_killing_loop():
    """An emit_async exception must be logged but not crash the loop —
    one failed epoch shouldn't stop the validator from trying again next
    epoch. Pre-fix this protection was already in place; pin it as part
    of the contract."""
    self_stub = MagicMock()
    self_stub.weights = MagicMock()
    self_stub.weights.maybe_emit = MagicMock(return_value={"5HOwnerHotkey": 1.0})
    self_stub._champion_miner_id = None
    self_stub._weights_emitter = MagicMock()
    self_stub._weights_emitter.emit_async = AsyncMock(
        side_effect=RuntimeError("subtensor unreachable"),
    )
    self_stub._is_leader = False

    # Should still terminate via CancelledError (from our patched sleep),
    # not via the RuntimeError leaking out.
    await _run_one_iteration(self_stub)

    self_stub._weights_emitter.emit_async.assert_awaited_once()
