"""Tests for burn fallback behavior when aggregator is unavailable or no miners."""
import asyncio
from datetime import datetime, timezone

import pytest

from neurons.validation_engine import ValidationEngine, ValidationResult, EpochResult


class DummyEventsClient:
    async def fetch_pending_orders(self, validator_id):
        return []

    async def submit_validation(self, order_id, validator_id, success, notes=""):
        return True

    async def fetch_health(self):
        return {"status": "healthy", "storage": {"healthy": True}}


class FakeSimulator:
    async def simulate(self, order):
        return True


def test_burn_fallback_when_aggregator_unhealthy():
    """When aggregator is unhealthy, should emit 100% to creator."""
    creator_id = "5FHneW46xGXgs5mUiveU4sbTyGBzmstUspZC92UhjJM694ty"

    engine = ValidationEngine(
        events_client=DummyEventsClient(),
        validator_id="validator",
        simulator=FakeSimulator(),
        creator_miner_id=creator_id,
    )

    # Simulate unhealthy aggregator
    engine._aggregator_healthy = False

    result = asyncio.run(
        engine.compute_weights_for_epoch("test-epoch", [])
    )

    assert result.weights == {creator_id: 1.0}
    assert result.stats.get("burn_fallback") is True
    assert result.stats.get("error") == "aggregator_unhealthy"


def test_burn_fallback_when_no_validation_results():
    """When no validation results, should emit 100% to creator."""
    creator_id = "5FHneW46xGXgs5mUiveU4sbTyGBzmstUspZC92UhjJM694ty"

    engine = ValidationEngine(
        events_client=DummyEventsClient(),
        validator_id="validator",
        simulator=FakeSimulator(),
        creator_miner_id=creator_id,
    )

    # Aggregator is healthy but no validation results
    engine._aggregator_healthy = True

    result = asyncio.run(
        engine.compute_weights_for_epoch("test-epoch", [])
    )

    # Should fallback to 100% burn to creator
    assert creator_id in result.weights
    assert result.weights[creator_id] == 1.0


def test_burn_fallback_when_no_miner_scores():
    """When miners exist but have zero scores, should still distribute."""
    creator_id = "5FHneW46xGXgs5mUiveU4sbTyGBzmstUspZC92UhjJM694ty"

    engine = ValidationEngine(
        events_client=DummyEventsClient(),
        validator_id="validator",
        simulator=FakeSimulator(),
        creator_miner_id=creator_id,
        burn_percentage=0.0,  # No burn configured
    )

    engine._aggregator_healthy = True

    # Create validation results where all fail (score = 0)
    results = [
        ValidationResult(
            order_id="order-1",
            solver_id="solver-1",
            miner_id="miner-a",
            success=False,
        ),
    ]
    results[0].timestamp = datetime.now(timezone.utc)

    result = asyncio.run(
        engine.compute_weights_for_epoch("test-epoch", results)
    )

    # Miners exist but have no successful validations
    # Should get equal distribution (1 miner = 100%)
    assert "miner-a" in result.weights


def test_no_burn_fallback_when_no_creator_set():
    """When no creator_miner_id, should return empty weights."""
    engine = ValidationEngine(
        events_client=DummyEventsClient(),
        validator_id="validator",
        simulator=FakeSimulator(),
        creator_miner_id=None,  # No creator set
    )

    engine._aggregator_healthy = False

    result = asyncio.run(
        engine.compute_weights_for_epoch("test-epoch", [])
    )

    # No creator = empty weights
    assert result.weights == {}


def test_normal_operation_with_miners():
    """Normal operation with miners should distribute normally."""
    creator_id = "creator-hotkey"

    engine = ValidationEngine(
        events_client=DummyEventsClient(),
        validator_id="validator",
        simulator=FakeSimulator(),
        creator_miner_id=creator_id,
        burn_percentage=0.2,  # 20% burn
    )

    engine._aggregator_healthy = True

    results = [
        ValidationResult(
            order_id="order-1",
            solver_id="solver-1",
            miner_id="miner-a",
            success=True,
        ),
        ValidationResult(
            order_id="order-2",
            solver_id="solver-2",
            miner_id="miner-b",
            success=True,
        ),
    ]
    for r in results:
        r.timestamp = datetime.now(timezone.utc)

    result = asyncio.run(
        engine.compute_weights_for_epoch("test-epoch", results)
    )

    # Should have miners + creator
    assert "miner-a" in result.weights
    assert "miner-b" in result.weights
    assert creator_id in result.weights

    # Creator should get 20% burn
    assert abs(result.weights[creator_id] - 0.2) < 1e-6

    # Total should sum to 1.0
    assert abs(sum(result.weights.values()) - 1.0) < 1e-6

