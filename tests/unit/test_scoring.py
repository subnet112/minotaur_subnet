import os

import pytest

from neurons.scoring import DefaultScoringV1


@pytest.fixture(autouse=True)
def reset_env(monkeypatch):
    for key in list(os.environ.keys()):
        if key.startswith("SCORING_"):
            monkeypatch.delenv(key, raising=False)


def test_scoring_guardrails(monkeypatch):
    monkeypatch.setenv("SCORING_MIN_PARTICIPATIONS", "2")
    monkeypatch.setenv("SCORING_MIN_WINS", "1")
    monkeypatch.setenv("SCORING_SCORE_CAP", "0.8")
    monkeypatch.setenv("SCORING_MAX_REVERT_RATIO", "0.4")

    scoring = DefaultScoringV1()

    metrics = {
        "hotkey_a": {
            "participations": 5,
            "wins": 3,
            "filled_notional": 500.0,
            "p95_latency_ms": 120.0,
            "reverts": 0.0,
        },
        "hotkey_b": {
            "participations": 1,  # below min participations
            "wins": 1,
            "filled_notional": 800.0,
            "p95_latency_ms": 80.0,
            "reverts": 0.0,
        },
        "hotkey_c": {
            "participations": 4,
            "wins": 1,
            "filled_notional": 100.0,
            "p95_latency_ms": 90.0,
            "reverts": 1.0,  # revert ratio = 1.0 (> max)
        },
    }

    combined, smoothed = scoring.compute_scores(metrics, prev_scores={})

    assert combined.keys() == smoothed.keys()
    # hotkey_a should have positive score capped at 0.8
    assert 0 < combined["hotkey_a"] <= 0.8
    # guardrails zero out hotkey_b (insufficient participations)
    assert combined["hotkey_b"] == 0.0
    # hotkey_c zero due to revert ratio
    assert combined["hotkey_c"] == 0.0


def test_scoring_ema(monkeypatch):
    monkeypatch.setenv("SCORING_EMA_ALPHA", "0.3")
    scoring = DefaultScoringV1()

    metrics = {
        "hk": {
            "participations": 3,
            "wins": 2,
            "filled_notional": 100.0,
            "p95_latency_ms": 100.0,
            "reverts": 0.0,
        }
    }

    combined, smoothed = scoring.compute_scores(metrics, prev_scores={"hk": 0.6})
    assert combined["hk"] >= 0
    # EMA should blend prior 0.6 with new combined score
    expected = 0.3 * combined["hk"] + 0.7 * 0.6
    assert smoothed["hk"] == pytest.approx(expected, rel=1e-6)

