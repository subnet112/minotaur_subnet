"""End-to-end tests for burn code - CRITICAL: ensures validator emits 100% to creator when no aggregator.

These tests verify the complete flow:
1. ValidationEngine detects unhealthy aggregator → emits {creator: 1.0}
2. BittensorWeightCallback receives weights → filters by metagraph → calls emitter
3. OnchainWeightsEmitter receives weights → would emit to chain

This is critical for running the validator without an aggregator (burn mode).
"""
import asyncio
from datetime import datetime, timezone
from typing import Dict, Optional
from unittest.mock import MagicMock, patch

import pytest

from neurons.validation_engine import ValidationEngine, ValidationResult, EpochResult
from neurons.bittensor_validator import BittensorWeightCallback
from neurons.metagraph_manager import MetagraphSnapshot


# =============================================================================
# Mock/Fake Classes
# =============================================================================

class FakeEventsClient:
    """Mock aggregator client that can simulate healthy/unhealthy states."""
    
    def __init__(self, healthy: bool = True):
        self._healthy = healthy
    
    async def fetch_pending_orders(self, validator_id: str):
        return []
    
    async def submit_validation(self, order_id: str, validator_id: str, success: bool, notes: str = ""):
        return True
    
    async def fetch_health(self):
        if self._healthy:
            return {"status": "healthy", "storage": {"healthy": True}}
        return None  # Unhealthy


class FakeSimulator:
    """Mock simulator that doesn't require Docker."""
    
    async def simulate(self, order: dict) -> bool:
        return True


class FakeMetagraphManager:
    """Mock metagraph manager with configurable snapshot."""
    
    def __init__(self, snapshot: Optional[MetagraphSnapshot] = None):
        self._snapshot = snapshot
    
    async def get_current_metagraph(self) -> Optional[MetagraphSnapshot]:
        return self._snapshot


class FakeOnchainEmitter:
    """Mock emitter that captures emitted weights."""
    
    def __init__(self, should_succeed: bool = True):
        self._should_succeed = should_succeed
        self.emitted_weights: Optional[Dict[str, float]] = None
        self.emit_called = False
    
    async def emit_async(self, weights: Dict[str, float]) -> bool:
        self.emit_called = True
        self.emitted_weights = weights.copy()
        return self._should_succeed


# =============================================================================
# Test: Full Burn Code Flow
# =============================================================================

def test_burn_code_full_flow_aggregator_unhealthy():
    """
    CRITICAL TEST: Verify complete burn code flow when aggregator is unhealthy.
    
    Flow:
    1. ValidationEngine has unhealthy aggregator
    2. compute_weights_for_epoch returns {creator: 1.0}
    3. BittensorWeightCallback receives these weights
    4. BittensorWeightCallback filters by metagraph (creator must be in metagraph)
    5. OnchainEmitter.emit_async is called with {creator: 1.0}
    """
    CREATOR_HOTKEY = "5FHneW46xGXgs5mUiveU4sbTyGBzmstUspZC92UhjJM694ty"
    
    # Step 1: Setup ValidationEngine with unhealthy aggregator
    engine = ValidationEngine(
        events_client=FakeEventsClient(healthy=False),
        validator_id="test-validator",
        simulator=FakeSimulator(),
        creator_miner_id=CREATOR_HOTKEY,
    )
    engine._aggregator_healthy = False  # Force unhealthy state
    
    # Step 2: Compute weights - should return {creator: 1.0}
    epoch_result = asyncio.run(
        engine.compute_weights_for_epoch("burn-test-epoch", [])
    )
    
    assert epoch_result.weights == {CREATOR_HOTKEY: 1.0}, \
        f"Expected {{creator: 1.0}}, got {epoch_result.weights}"
    assert epoch_result.stats.get("burn_fallback") is True, \
        "Expected burn_fallback=True in stats"
    
    # Step 3: Setup BittensorWeightCallback with metagraph containing creator
    snapshot = MetagraphSnapshot(
        uid_for_hotkey={CREATOR_HOTKEY: 0, "other-miner": 1},
        size=2,
        validator_permit=True,
        validator_uid=0,
    )
    metagraph_manager = FakeMetagraphManager(snapshot)
    emitter = FakeOnchainEmitter(should_succeed=True)
    
    callback = BittensorWeightCallback(
        metagraph_manager=metagraph_manager,
        onchain_emitter=emitter,
        logger=MagicMock(),
    )
    
    # Step 4: Call the callback with burn weights
    success = asyncio.run(callback(epoch_result.weights, epoch_result))
    
    # Step 5: Verify emitter was called with correct weights
    assert success is True, "Callback should return True on successful emission"
    assert emitter.emit_called is True, "Emitter.emit_async should have been called"
    assert emitter.emitted_weights == {CREATOR_HOTKEY: 1.0}, \
        f"Expected emitter to receive {{creator: 1.0}}, got {emitter.emitted_weights}"


def test_burn_code_full_flow_no_miners():
    """
    CRITICAL TEST: Verify burn code when there are no miners/validation results.
    
    Even with healthy aggregator, if there are no miners, should burn to creator.
    """
    CREATOR_HOTKEY = "5FHneW46xGXgs5mUiveU4sbTyGBzmstUspZC92UhjJM694ty"
    
    # Setup ValidationEngine with healthy aggregator but no results
    engine = ValidationEngine(
        events_client=FakeEventsClient(healthy=True),
        validator_id="test-validator",
        simulator=FakeSimulator(),
        creator_miner_id=CREATOR_HOTKEY,
    )
    engine._aggregator_healthy = True
    
    # Compute weights with empty results
    epoch_result = asyncio.run(
        engine.compute_weights_for_epoch("no-miners-epoch", [])
    )
    
    # Should fallback to creator
    assert CREATOR_HOTKEY in epoch_result.weights, \
        f"Creator should be in weights, got {epoch_result.weights}"
    assert epoch_result.weights[CREATOR_HOTKEY] == 1.0, \
        f"Creator should get 100%, got {epoch_result.weights[CREATOR_HOTKEY]}"
    
    # Verify callback flow
    snapshot = MetagraphSnapshot(
        uid_for_hotkey={CREATOR_HOTKEY: 0},
        size=1,
        validator_permit=True,
        validator_uid=0,
    )
    emitter = FakeOnchainEmitter()
    callback = BittensorWeightCallback(
        metagraph_manager=FakeMetagraphManager(snapshot),
        onchain_emitter=emitter,
        logger=MagicMock(),
    )
    
    success = asyncio.run(callback(epoch_result.weights, epoch_result))
    
    assert success is True
    assert emitter.emitted_weights == {CREATOR_HOTKEY: 1.0}


def test_burn_code_creator_must_be_in_metagraph():
    """
    CRITICAL TEST: Burn fails gracefully if creator is not in metagraph.
    
    This shouldn't happen in production (UID 0 is always creator), but we must handle it.
    """
    CREATOR_HOTKEY = "5FHneW46xGXgs5mUiveU4sbTyGBzmstUspZC92UhjJM694ty"
    
    # ValidationEngine emits burn weights
    engine = ValidationEngine(
        events_client=FakeEventsClient(healthy=False),
        validator_id="test-validator",
        simulator=FakeSimulator(),
        creator_miner_id=CREATOR_HOTKEY,
    )
    engine._aggregator_healthy = False
    
    epoch_result = asyncio.run(
        engine.compute_weights_for_epoch("burn-epoch", [])
    )
    
    assert epoch_result.weights == {CREATOR_HOTKEY: 1.0}
    
    # But metagraph doesn't contain creator (edge case)
    snapshot = MetagraphSnapshot(
        uid_for_hotkey={"other-hotkey": 0},  # Creator NOT in metagraph
        size=1,
        validator_permit=True,
        validator_uid=0,
    )
    emitter = FakeOnchainEmitter()
    callback = BittensorWeightCallback(
        metagraph_manager=FakeMetagraphManager(snapshot),
        onchain_emitter=emitter,
        logger=MagicMock(),
    )
    
    success = asyncio.run(callback(epoch_result.weights, epoch_result))
    
    # Should fail gracefully - creator not in metagraph
    assert success is False, "Should fail when creator not in metagraph"
    assert emitter.emit_called is False, "Emitter should NOT be called"


def test_burn_code_requires_validator_permit():
    """
    CRITICAL TEST: Burn code requires validator permit to emit.
    """
    CREATOR_HOTKEY = "5FHneW46xGXgs5mUiveU4sbTyGBzmstUspZC92UhjJM694ty"
    
    weights = {CREATOR_HOTKEY: 1.0}
    epoch_result = EpochResult(
        epoch_key="test",
        start_time=datetime.now(timezone.utc),
        end_time=datetime.now(timezone.utc),
        validation_results=[],
        weights=weights,
        stats={},
    )
    
    # Metagraph has creator but validator lacks permit
    snapshot = MetagraphSnapshot(
        uid_for_hotkey={CREATOR_HOTKEY: 0},
        size=1,
        validator_permit=False,  # NO PERMIT
        validator_uid=0,
    )
    emitter = FakeOnchainEmitter()
    callback = BittensorWeightCallback(
        metagraph_manager=FakeMetagraphManager(snapshot),
        onchain_emitter=emitter,
        logger=MagicMock(),
    )
    
    success = asyncio.run(callback(weights, epoch_result))
    
    assert success is False, "Should fail without validator permit"
    assert emitter.emit_called is False


def test_burn_code_no_creator_set_returns_empty():
    """
    TEST: When no creator_miner_id is set, weights should be empty.
    """
    engine = ValidationEngine(
        events_client=FakeEventsClient(healthy=False),
        validator_id="test-validator",
        simulator=FakeSimulator(),
        creator_miner_id=None,  # NO CREATOR SET
    )
    engine._aggregator_healthy = False
    
    epoch_result = asyncio.run(
        engine.compute_weights_for_epoch("no-creator-epoch", [])
    )
    
    assert epoch_result.weights == {}, \
        f"Without creator_miner_id, weights should be empty, got {epoch_result.weights}"


def test_burn_code_emitter_failure_returns_false():
    """
    TEST: If emitter fails, callback should return False.
    """
    CREATOR_HOTKEY = "5FHneW46xGXgs5mUiveU4sbTyGBzmstUspZC92UhjJM694ty"
    
    weights = {CREATOR_HOTKEY: 1.0}
    epoch_result = EpochResult(
        epoch_key="test",
        start_time=datetime.now(timezone.utc),
        end_time=datetime.now(timezone.utc),
        validation_results=[],
        weights=weights,
        stats={},
    )
    
    snapshot = MetagraphSnapshot(
        uid_for_hotkey={CREATOR_HOTKEY: 0},
        size=1,
        validator_permit=True,
        validator_uid=0,
    )
    emitter = FakeOnchainEmitter(should_succeed=False)  # EMITTER FAILS
    callback = BittensorWeightCallback(
        metagraph_manager=FakeMetagraphManager(snapshot),
        onchain_emitter=emitter,
        logger=MagicMock(),
    )
    
    success = asyncio.run(callback(weights, epoch_result))
    
    assert success is False, "Should return False when emitter fails"
    assert emitter.emit_called is True, "Emitter should have been called"
    assert emitter.emitted_weights == {CREATOR_HOTKEY: 1.0}, "Weights should have been passed to emitter"


def test_burn_code_weight_sum_is_one():
    """
    TEST: Burn weights must sum to 1.0 for valid chain submission.
    """
    CREATOR_HOTKEY = "5FHneW46xGXgs5mUiveU4sbTyGBzmstUspZC92UhjJM694ty"
    
    engine = ValidationEngine(
        events_client=FakeEventsClient(healthy=False),
        validator_id="test-validator",
        simulator=FakeSimulator(),
        creator_miner_id=CREATOR_HOTKEY,
    )
    engine._aggregator_healthy = False
    
    epoch_result = asyncio.run(
        engine.compute_weights_for_epoch("burn-epoch", [])
    )
    
    total_weight = sum(epoch_result.weights.values())
    assert abs(total_weight - 1.0) < 1e-9, \
        f"Burn weights must sum to 1.0, got {total_weight}"


def test_burn_code_multiple_epochs_consistent():
    """
    TEST: Multiple consecutive burn epochs should produce consistent results.
    """
    CREATOR_HOTKEY = "5FHneW46xGXgs5mUiveU4sbTyGBzmstUspZC92UhjJM694ty"
    
    engine = ValidationEngine(
        events_client=FakeEventsClient(healthy=False),
        validator_id="test-validator",
        simulator=FakeSimulator(),
        creator_miner_id=CREATOR_HOTKEY,
    )
    engine._aggregator_healthy = False
    
    results = []
    for i in range(5):
        epoch_result = asyncio.run(
            engine.compute_weights_for_epoch(f"burn-epoch-{i}", [])
        )
        results.append(epoch_result)
    
    # All epochs should produce same burn weights
    for i, result in enumerate(results):
        assert result.weights == {CREATOR_HOTKEY: 1.0}, \
            f"Epoch {i} should have burn weights, got {result.weights}"


# =============================================================================
# Integration Test: Simulated Real Flow
# =============================================================================

def test_burn_code_simulated_validator_startup():
    """
    INTEGRATION TEST: Simulate validator startup without aggregator.
    
    This simulates what happens when you run:
    python -m neurons.validator --netuid 1 --subtensor.network finney
    
    Without a running aggregator.
    """
    CREATOR_HOTKEY = "5FHneW46xGXgs5mUiveU4sbTyGBzmstUspZC92UhjJM694ty"
    VALIDATOR_HOTKEY = "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY"
    
    # 1. ValidationEngine starts, aggregator health check fails
    engine = ValidationEngine(
        events_client=FakeEventsClient(healthy=False),
        validator_id=VALIDATOR_HOTKEY,
        simulator=FakeSimulator(),
        creator_miner_id=CREATOR_HOTKEY,
    )
    
    # Simulate initial health check failure
    engine._aggregator_healthy = False
    
    # 2. First epoch runs - should emit burn weights
    epoch_result = asyncio.run(
        engine.compute_weights_for_epoch("epoch-1", [])
    )
    
    assert epoch_result.weights == {CREATOR_HOTKEY: 1.0}
    assert epoch_result.stats.get("error") == "aggregator_unhealthy"
    assert epoch_result.stats.get("burn_fallback") is True
    
    # 3. BittensorWeightCallback processes the weights
    metagraph = MetagraphSnapshot(
        uid_for_hotkey={
            CREATOR_HOTKEY: 0,      # UID 0 = creator
            VALIDATOR_HOTKEY: 1,    # UID 1 = our validator
            "miner-1": 2,
            "miner-2": 3,
        },
        size=4,
        validator_permit=True,
        validator_uid=1,
    )
    
    emitter = FakeOnchainEmitter()
    callback = BittensorWeightCallback(
        metagraph_manager=FakeMetagraphManager(metagraph),
        onchain_emitter=emitter,
        logger=MagicMock(),
    )
    
    success = asyncio.run(callback(epoch_result.weights, epoch_result))
    
    # 4. Verify chain emission
    assert success is True, "Burn emission should succeed"
    assert emitter.emit_called is True
    assert emitter.emitted_weights == {CREATOR_HOTKEY: 1.0}
    
    print("✅ BURN CODE TEST PASSED: Validator would emit 100% to creator (UID 0)")

