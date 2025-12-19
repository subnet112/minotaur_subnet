import datetime as dt

import pytest

from neurons import events_validator


class DummyLogger:
    def __init__(self):
        self.messages = []

    def warning(self, msg):
        self.messages.append(msg)


@pytest.fixture(autouse=True)
def mock_signature(monkeypatch):
    monkeypatch.setattr(events_validator, "_verify_signature", lambda *args, **kwargs: True)


def _ts(offset_ms: int = 0) -> str:
    base = dt.datetime(2025, 1, 1, tzinfo=dt.timezone.utc)
    ts = base + dt.timedelta(milliseconds=offset_ms)
    return ts.isoformat().replace("+00:00", "Z")


def test_validate_events_accepts_valid_submission():
    logger = DummyLogger()
    raw_events = [
        {
            "type": "quote",
            "id": "evt-1",
            "request_ts": _ts(0),
            "context": {"constraints": {"ttl_ms": 1200}},
            "submissions": [
                {
                    "hotkey": "5F3sa2TJAWMqDhXG6jhV4N8ko9mVx7gVM9TZ",
                    "response_ts": _ts(500),
                    "latency_ms": 500,
                    "price": 3520.12,
                    "size": 5.0,
                    "signature": "ZmFrZV9zaWduYXR1cmU=",
                }
            ],
        }
    ]

    validated, stats = events_validator.validate_events(
        raw_events,
        allowed_hotkeys={"5F3sa2TJAWMqDhXG6jhV4N8ko9mVx7gVM9TZ"},
        logger=logger,
        default_ttl_ms=1500,
        max_response_latency_ms=2000,
    )

    assert len(validated) == 1
    assert stats["valid_events"] == 1
    assert stats["valid_submissions"] == 1
    assert stats["dropped_events"] == 0
    assert stats["dropped_submissions"] == 0


def test_validate_events_drops_invalid_latency_and_price(monkeypatch):
    logger = DummyLogger()
    hotkey = "5C9gnV7y3YB2d89goUnxWMg4RgzNc2idHbTJ"
    raw_events = [
        {
            "type": "quote",
            "id": "evt-ttl",
            "request_ts": _ts(0),
            "context": {"constraints": {"ttl_ms": 500}},
            "submissions": [
                {
                    "hotkey": hotkey,
                    "response_ts": _ts(1200),  # exceeds ttl
                    "latency_ms": 1200,
                    "price": 3500.0,
                    "size": 5.0,
                    "signature": "ZmFrZV9zaWdu",
                }
            ],
        },
        {
            "type": "quote",
            "id": "evt-price",
            "request_ts": _ts(0),
            "submissions": [
                {
                    "hotkey": hotkey,
                    "response_ts": _ts(100),
                    "latency_ms": 100,
                    "price": -1.0,
                    "size": 5.0,
                    "signature": "ZmFrZV9zaWdu",
                }
            ],
        },
    ]

    validated, stats = events_validator.validate_events(
        raw_events,
        allowed_hotkeys={hotkey},
        logger=logger,
        default_ttl_ms=1000,
        max_response_latency_ms=1000,
        min_price=0.01,
        min_size=1.0,
    )

    # Both events should be dropped since submissions invalid
    assert validated == []
    assert stats["valid_events"] == 0
    assert stats["dropped_events"] == 2
    assert stats["ttl_violations"] == 1
    assert stats["price_bounds"] >= 1

