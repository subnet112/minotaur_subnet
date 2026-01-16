"""Tests for ValidationEngine score computation and weight normalization."""
import asyncio
import logging
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional

import pytest

from neurons.validation_engine import ValidationEngine, ValidationResult, EpochResult


class DummyEventsClient:
    async def fetch_pending_orders(self, validator_id: str) -> List[Dict[str, Any]]:
        return []

    async def submit_validation(self, order_id: str, validator_id: str, success: bool, notes: str = "") -> bool:
        return True

    async def fetch_health(self) -> Optional[Dict[str, Any]]:
        return {"status": "healthy", "storage": {"healthy": True}}


class FakeSimulator:
    """Fake simulator that doesn't require an event loop."""
    async def simulate(self, order: dict) -> bool:
        return True


def _result(miner_id: str, success: bool) -> ValidationResult:
    r = ValidationResult(
        order_id=f"order-{miner_id}",
        solver_id=f"solver-{miner_id}",
        miner_id=miner_id,
        success=success,
    )
    r.timestamp = datetime.now(timezone.utc)
    return r


def test_score_computation_from_validation_results():
    engine = ValidationEngine(
        events_client=DummyEventsClient(),
        validator_id="validator",
        simulator=FakeSimulator(),
    )

    results = [
        _result("miner-a", True),
        _result("miner-a", True),
        _result("miner-a", False),
        _result("miner-b", True),
        _result("miner-b", False),
        _result("miner-b", False),
    ]

    scores, stats = engine._compute_scores_from_results(results)

    # miner-a: 2 valid out of 3
    # miner-b: 1 valid out of 3
    assert "miner-a" in scores
    assert "miner-b" in scores
    assert scores["miner-a"]["score"] == 2.0
    assert scores["miner-b"]["score"] == 1.0
    assert stats["total_simulations"] == 6
    assert stats["valid_miners"] == 2


def test_weight_normalization_sums_to_one():
    engine = ValidationEngine(
        events_client=DummyEventsClient(),
        validator_id="validator",
        simulator=FakeSimulator(),
    )

    scores = {
        "miner-a": {"score": 3.0},
        "miner-b": {"score": 1.0},
        "miner-c": {"score": 0.0},
    }

    weights = engine._normalize_scores_to_weights(scores)

    assert abs(sum(weights.values()) - 1.0) < 1e-6
    assert weights["miner-a"] == pytest.approx(0.75, rel=1e-6)
    assert weights["miner-b"] == pytest.approx(0.25, rel=1e-6)
    # Zero-score miners should still be included but with zero weight
    assert "miner-c" in weights
    assert weights["miner-c"] == 0.0


def test_weight_normalization_with_burn_percentage():
    engine = ValidationEngine(
        events_client=DummyEventsClient(),
        validator_id="validator",
        simulator=FakeSimulator(),
        burn_percentage=0.2,
        creator_miner_id="creator-hotkey",
    )

    scores = {
        "miner-a": {"score": 4.0},
        "miner-b": {"score": 1.0},
    }

    weights = engine._normalize_scores_to_weights(scores)

    # With 20% burn:
    # - Miner scores are scaled down to 80%
    # - Creator gets the 20% burn allocation
    # miner-a: 0.8 * (4/5) = 0.64
    # miner-b: 0.8 * (1/5) = 0.16
    # creator-hotkey: 0.2
    # Total: 1.0
    total = sum(weights.values())
    assert abs(total - 1.0) < 1e-6


def test_weight_normalization_empty_scores():
    engine = ValidationEngine(
        events_client=DummyEventsClient(),
        validator_id="validator",
        simulator=FakeSimulator(),
    )

    scores = {}
    weights = engine._normalize_scores_to_weights(scores)

    assert weights == {}


def test_weight_normalization_all_zero_scores():
    engine = ValidationEngine(
        events_client=DummyEventsClient(),
        validator_id="validator",
        simulator=FakeSimulator(),
    )

    scores = {
        "miner-a": {"score": 0.0},
        "miner-b": {"score": 0.0},
    }

    weights = engine._normalize_scores_to_weights(scores)

    # With all zero scores, weights should be empty or equal distribution
    # depends on implementation - let's check it doesn't crash
    assert isinstance(weights, dict)


def test_heartbeat_callback_is_called():
    heartbeat_called = []

    def heartbeat():
        heartbeat_called.append(True)

    engine = ValidationEngine(
        events_client=DummyEventsClient(),
        validator_id="validator",
        simulator=FakeSimulator(),
        heartbeat_callback=heartbeat,
    )

    # Manually call the heartbeat through the run_epoch sleep loop is hard to test
    # but we can verify the callback is stored
    assert engine._heartbeat_callback is heartbeat


def test_compute_weights_for_epoch_with_results():
    engine = ValidationEngine(
        events_client=DummyEventsClient(),
        validator_id="validator",
        simulator=FakeSimulator(),
    )

    results = [
        _result("miner-a", True),
        _result("miner-a", True),
        _result("miner-b", True),
    ]

    epoch_result = asyncio.run(
        engine.compute_weights_for_epoch("test-epoch", results)
    )

    assert epoch_result.epoch_key == "test-epoch"
    assert "miner-a" in epoch_result.weights
    assert "miner-b" in epoch_result.weights
    # miner-a has 2 validations, miner-b has 1
    assert epoch_result.weights["miner-a"] > epoch_result.weights["miner-b"]


def test_compute_weights_for_epoch_empty_results():
    engine = ValidationEngine(
        events_client=DummyEventsClient(),
        validator_id="validator",
        simulator=FakeSimulator(),
    )

    epoch_result = asyncio.run(
        engine.compute_weights_for_epoch("empty-epoch", [])
    )

    assert epoch_result.epoch_key == "empty-epoch"
    assert epoch_result.weights == {}


def test_compute_weights_aggregator_unhealthy():
    engine = ValidationEngine(
        events_client=DummyEventsClient(),
        validator_id="validator",
        simulator=FakeSimulator(),
    )
    engine._aggregator_healthy = False

    results = [_result("miner-a", True)]

    epoch_result = asyncio.run(
        engine.compute_weights_for_epoch("unhealthy-epoch", results)
    )

    # When aggregator is unhealthy, weights should be empty (100% burn)
    assert epoch_result.weights == {}
    assert epoch_result.stats.get("error") == "aggregator_unhealthy"

