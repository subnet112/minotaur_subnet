"""Tests for BittensorWeightCallback weight emission logic."""
import asyncio
import logging
from typing import Dict, Optional
from datetime import datetime, timezone

import pytest

from neurons.metagraph_manager import MetagraphSnapshot
from neurons.bittensor_validator import BittensorWeightCallback
from neurons.validation_engine import EpochResult, ValidationResult


class FakeMetagraphManager:
    def __init__(self, snapshot: Optional[MetagraphSnapshot] = None):
        self._snapshot = snapshot

    async def get_current_metagraph(self) -> Optional[MetagraphSnapshot]:
        return self._snapshot


class FakeOnchainEmitter:
    def __init__(self, should_succeed: bool = True):
        self._should_succeed = should_succeed
        self.last_weights: Optional[Dict[str, float]] = None

    async def emit_async(self, weights: Dict[str, float]) -> bool:
        self.last_weights = weights
        return self._should_succeed


def _make_epoch_result(weights: Dict[str, float]) -> EpochResult:
    return EpochResult(
        epoch_key="test-epoch",
        start_time=datetime.now(timezone.utc),
        end_time=datetime.now(timezone.utc),
        validation_results=[],
        weights=weights,
        stats={},
    )


def test_callback_skips_when_snapshot_missing():
    manager = FakeMetagraphManager(snapshot=None)
    emitter = FakeOnchainEmitter()
    callback = BittensorWeightCallback(manager, emitter, logging.getLogger())

    result = asyncio.run(callback({"hk1": 0.5}, _make_epoch_result({"hk1": 0.5})))

    assert result is False
    assert emitter.last_weights is None


def test_callback_skips_when_permit_missing():
    snapshot = MetagraphSnapshot(
        uid_for_hotkey={"hk1": 0},
        size=1,
        validator_permit=False,
        validator_uid=0,
    )
    manager = FakeMetagraphManager(snapshot)
    emitter = FakeOnchainEmitter()
    callback = BittensorWeightCallback(manager, emitter, logging.getLogger())

    result = asyncio.run(callback({"hk1": 0.5}, _make_epoch_result({"hk1": 0.5})))

    assert result is False
    assert emitter.last_weights is None


def test_callback_filters_unknown_hotkeys():
    snapshot = MetagraphSnapshot(
        uid_for_hotkey={"hk1": 0, "hk2": 1},
        size=2,
        validator_permit=True,
        validator_uid=0,
    )
    manager = FakeMetagraphManager(snapshot)
    emitter = FakeOnchainEmitter()
    callback = BittensorWeightCallback(manager, emitter, logging.getLogger())

    weights = {"hk1": 0.4, "hk2": 0.3, "unknown_hk": 0.3}
    result = asyncio.run(callback(weights, _make_epoch_result(weights)))

    assert result is True
    assert emitter.last_weights == {"hk1": 0.4, "hk2": 0.3}


def test_callback_returns_false_when_emitter_fails():
    snapshot = MetagraphSnapshot(
        uid_for_hotkey={"hk1": 0},
        size=1,
        validator_permit=True,
        validator_uid=0,
    )
    manager = FakeMetagraphManager(snapshot)
    emitter = FakeOnchainEmitter(should_succeed=False)
    callback = BittensorWeightCallback(manager, emitter, logging.getLogger())

    result = asyncio.run(callback({"hk1": 1.0}, _make_epoch_result({"hk1": 1.0})))

    assert result is False
    assert emitter.last_weights == {"hk1": 1.0}


def test_callback_returns_false_when_no_valid_hotkeys():
    snapshot = MetagraphSnapshot(
        uid_for_hotkey={"hk1": 0},
        size=1,
        validator_permit=True,
        validator_uid=0,
    )
    manager = FakeMetagraphManager(snapshot)
    emitter = FakeOnchainEmitter()
    callback = BittensorWeightCallback(manager, emitter, logging.getLogger())

    # All hotkeys are unknown
    result = asyncio.run(callback({"unknown": 1.0}, _make_epoch_result({"unknown": 1.0})))

    assert result is False
    assert emitter.last_weights is None

