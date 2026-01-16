import math
from pathlib import Path

import asyncio

from neurons.events_engine import EventsWeightsEngine
from neurons.state_store import StateStore


class StubEventsClient:
    def __init__(self, orders, miners_stats):
        self._orders = orders
        self._miners_stats = miners_stats

    async def fetch_pending_orders(self, validator_id: str):
        return list(self._orders)

    async def submit_validation(self, order_id: str, validator_id: str, success: bool, notes: str = ""):
        return True

    async def fetch_miners_stats(self):
        return list(self._miners_stats)


class DummySimulator:
    async def simulate_order(self, order):
        return True, None


def test_events_engine_pipeline(tmp_path: Path):
    orders = [
        {
            "orderId": "order-1",
            "quoteDetails": {"solverId": "solver-1"},
        }
    ]

    miners_stats = [
        {"minerId": "hk1", "solverIds": ["solver-1"]},
    ]

    client = StubEventsClient(orders, miners_stats)
    state_store = StateStore(base_dir=str(tmp_path))
    engine = EventsWeightsEngine(
        events_client=client,
        state_store=state_store,
        validator_id="validator-1",
        simulator=DummySimulator(),
    )

    weights, smoothed, stats = asyncio.run(
        engine.compute_weights_for_window(
            from_ts="2025-01-01T00:00:00Z",
            to_ts="2025-01-01T00:05:00Z",
            allowed_hotkeys={"hk1"},
        )
    )

    assert stats["valid_miners"] == 1
    assert math.isclose(sum(weights.values()), 1.0, rel_tol=1e-6)
    assert set(weights.keys()) <= {"hk1"}
    # ensure state store persisted last scores (float values)
    engine.state_store.commit_epoch(epoch_index=0, to_ts="2025-01-01T00:05:00Z", last_scores=weights)
    assert state_store.get_last_scores() == weights


def test_events_engine_handles_empty_events(tmp_path: Path):
    client = StubEventsClient([], [])
    state_store = StateStore(base_dir=str(tmp_path))
    engine = EventsWeightsEngine(
        events_client=client,
        state_store=state_store,
        validator_id="validator-1",
        simulator=DummySimulator(),
    )

    weights, smoothed, stats = asyncio.run(
        engine.compute_weights_for_window(
            from_ts="2025-01-01T00:00:00Z",
            to_ts="2025-01-01T00:05:00Z",
            allowed_hotkeys={"hk"},
        )
    )

    assert weights == {}
    assert isinstance(smoothed, dict)
    assert stats["total_simulations"] == 0

