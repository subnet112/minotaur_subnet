import math
from pathlib import Path

import pytest

from neurons import events_validator
from neurons.events_engine import EventsWeightsEngine
from neurons.state_store import StateStore


class StubEventsClient:
    def __init__(self, events):
        self._events = events
        self.calls = []


@pytest.fixture(autouse=True)
def mock_signature(monkeypatch):
    monkeypatch.setattr(events_validator, "_verify_signature", lambda *args, **kwargs: True)


@pytest.mark.asyncio
async def test_events_engine_pipeline(tmp_path: Path):
    events = [
        {
            "type": "quote",
            "id": "evt-1",
            "request_ts": "2025-01-01T00:00:00Z",
            "context": {"constraints": {"ttl_ms": 1500}},
            "submissions": [
                {
                    "hotkey": "hk1",
                    "response_ts": "2025-01-01T00:00:00.400Z",
                    "price": 100.0,
                    "size": 2.0,
                    "signature": "ZmFrZQ==",
                },
                {
                    "hotkey": "hk2",
                    "response_ts": "2025-01-01T00:00:00.300Z",
                    "price": 101.0,
                    "size": 1.5,
                    "signature": "ZmFrZQ==",
                },
            ],
        }
    ]

    client = StubEventsClient(events)
    state_store = StateStore(base_dir=str(tmp_path))
    engine = EventsWeightsEngine(
        events_client=client,
        state_store=state_store,
        validation_params={
            "default_ttl_ms": 1500,
            "max_response_latency_ms": 2000,
            "min_price": 0.01,
            "min_size": 0.1,
        },
    )

    weights, smoothed, stats = await engine.compute_weights_for_window(
        from_ts="2025-01-01T00:00:00Z",
        to_ts="2025-01-01T00:05:00Z",
        allowed_hotkeys={"hk1", "hk2"},
    )

    assert stats["valid_events"] == 1
    assert math.isclose(sum(weights.values()), 1.0, rel_tol=1e-6)
    assert set(weights.keys()) <= {"hk1", "hk2"}
    # ensure state store persisted smoothed scores
    engine.state_store.commit_epoch(epoch_index=0, to_ts="2025-01-01T00:05:00Z", last_scores=smoothed)
    assert state_store.get_last_scores()


@pytest.mark.asyncio
async def test_events_engine_handles_empty_events(tmp_path: Path):
    client = StubEventsClient([])
    state_store = StateStore(base_dir=str(tmp_path))
    engine = EventsWeightsEngine(
        events_client=client,
        state_store=state_store,
        validation_params={"default_ttl_ms": 1000},
    )

    weights, smoothed, stats = await engine.compute_weights_for_window(
        from_ts="2025-01-01T00:00:00Z",
        to_ts="2025-01-01T00:05:00Z",
        allowed_hotkeys={"hk"},
    )

    assert weights == {}
    assert isinstance(smoothed, dict)
    assert stats["total_events"] == 0

