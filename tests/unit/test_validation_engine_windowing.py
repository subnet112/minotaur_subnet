import datetime as dt

from neurons.validation_engine import ValidationEngine, ValidationResult


class DummyEventsClient:
    async def fetch_pending_orders(self, validator_id):
        return []

    async def submit_validation(self, order_id, validator_id, success, notes=""):
        return True


class DummySimulator:
    def __init__(self):
        self.container_pool_size = 0


def _result_at(ts: dt.datetime, miner_id: str) -> ValidationResult:
    result = ValidationResult(
        order_id="order",
        solver_id="solver",
        miner_id=miner_id,
        success=True,
    )
    result.timestamp = ts
    return result


def test_validation_history_windowing(monkeypatch):
    monkeypatch.setenv("VALIDATION_HISTORY_RETENTION_SECONDS", "5")

    engine = ValidationEngine(
        events_client=DummyEventsClient(),
        validator_id="validator",
        simulator=DummySimulator(),
    )

    now = dt.datetime.now(dt.timezone.utc)
    old = now - dt.timedelta(seconds=10)
    within = now - dt.timedelta(seconds=2)

    engine._append_validation_results([
        _result_at(old, "hk-old"),
        _result_at(within, "hk-within"),
    ])

    # Adding a new result should prune the old one
    engine._append_validation_results([
        _result_at(now, "hk-new"),
    ])

    window_start = (now - dt.timedelta(seconds=3)).isoformat()
    window_end = (now + dt.timedelta(seconds=1)).isoformat()

    results = engine.get_results_for_window(window_start, window_end)
    miner_ids = {r.miner_id for r in results}

    assert "hk-old" not in miner_ids
    assert "hk-within" in miner_ids
    assert "hk-new" in miner_ids