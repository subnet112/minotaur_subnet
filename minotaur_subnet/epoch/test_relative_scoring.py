"""Unit tests for the SHADOW relative per-order scoring path.

Covers the two env gates (defaults + overrides) and the pure
``evaluate_relative_adoption`` decision across the full verdict matrix.
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from minotaur_subnet.epoch.relative_scoring import (
    MIN_VALID_OUTPUT,
    RELATIVE_TOL,
    evaluate_relative_adoption,
    relative_scoring_active,
    relative_scoring_shadow_enabled,
)
from minotaur_subnet.harness.orchestrator import BenchmarkResult


def _r(intent_id: str, shadow_score):
    """A real BenchmarkResult carrying only intent_id + shadow_score."""
    return BenchmarkResult(intent_id=intent_id, shadow_score=shadow_score)


# ── env gates ────────────────────────────────────────────────────────────────


def test_shadow_gate_defaults_on(monkeypatch):
    monkeypatch.delenv("RELATIVE_SCORING_SHADOW", raising=False)
    assert relative_scoring_shadow_enabled() is True


def test_shadow_gate_off_values(monkeypatch):
    for val in ("0", "false", "no", "off", "OFF", "False"):
        monkeypatch.setenv("RELATIVE_SCORING_SHADOW", val)
        assert relative_scoring_shadow_enabled() is False, val


def test_shadow_gate_garbage_stays_on(monkeypatch):
    # Anything that is not an explicit off-value keeps the observe-only path ON.
    monkeypatch.setenv("RELATIVE_SCORING_SHADOW", "yes")
    assert relative_scoring_shadow_enabled() is True
    monkeypatch.setenv("RELATIVE_SCORING_SHADOW", "garbage")
    assert relative_scoring_shadow_enabled() is True


def test_active_gate_defaults_off(monkeypatch):
    monkeypatch.delenv("RELATIVE_SCORING_ENABLED", raising=False)
    assert relative_scoring_active() is False


def test_active_gate_on_values(monkeypatch):
    for val in ("1", "true", "yes", "on", "ON", "Yes"):
        monkeypatch.setenv("RELATIVE_SCORING_ENABLED", val)
        assert relative_scoring_active() is True, val


def test_active_gate_garbage_stays_off(monkeypatch):
    monkeypatch.setenv("RELATIVE_SCORING_ENABLED", "maybe")
    assert relative_scoring_active() is False


# ── evaluate_relative_adoption ───────────────────────────────────────────────


def test_clean_win_adopts():
    champ = [_r("o1", 100.0), _r("o2", 200.0)]
    chal = [_r("o1", 120.0), _r("o2", 250.0)]
    res = evaluate_relative_adoption(champ, chal)
    assert res["adopt"] is True
    assert res["n_wins"] == 2
    assert res["n_regressions"] == 0
    assert res["scenarios_compared"] == 2
    assert {o["verdict"] for o in res["per_order"]} == {"win"}


def test_all_matched_no_win_does_not_adopt():
    # Identical outputs everywhere -> all "matched", no win -> no adopt.
    champ = [_r("o1", 100.0), _r("o2", 200.0)]
    chal = [_r("o1", 100.0), _r("o2", 200.0)]
    res = evaluate_relative_adoption(champ, chal)
    assert res["adopt"] is False
    assert res["n_wins"] == 0
    assert res["n_matched"] == 2
    assert res["n_regressions"] == 0


def test_single_regression_vetoes_many_wins():
    champ = [_r("o1", 100.0), _r("o2", 100.0), _r("o3", 100.0)]
    chal = [_r("o1", 200.0), _r("o2", 200.0), _r("o3", 50.0)]  # o3 regresses
    res = evaluate_relative_adoption(champ, chal)
    assert res["adopt"] is False
    assert res["n_wins"] == 2
    assert res["n_regressions"] == 1
    verdicts = {o["intent_id"]: o["verdict"] for o in res["per_order"]}
    assert verdicts["o3"] == "regression"


def test_blind_spot_cover_counts_as_win():
    # Champion delivered nothing on o2 (blind spot); challenger covers it.
    champ = [_r("o1", 100.0), _r("o2", None)]
    chal = [_r("o1", 100.0), _r("o2", 500.0)]
    res = evaluate_relative_adoption(champ, chal)
    assert res["adopt"] is True
    assert res["n_blind_spots"] == 1
    assert res["n_wins"] == 0  # o1 only matched
    verdicts = {o["intent_id"]: o["verdict"] for o in res["per_order"]}
    assert verdicts["o2"] == "blind_spot_cover"


def test_dropped_is_a_regression():
    # Champion delivered on o2; challenger drops it (no value) -> regression veto.
    champ = [_r("o1", 100.0), _r("o2", 300.0)]
    chal = [_r("o1", 200.0), _r("o2", None)]
    res = evaluate_relative_adoption(champ, chal)
    assert res["adopt"] is False
    assert res["n_regressions"] == 1
    verdicts = {o["intent_id"]: o["verdict"] for o in res["per_order"]}
    assert verdicts["o2"] == "dropped"


def test_tolerance_band_is_matched_not_regression():
    # Just inside the noise band below the champion -> "matched", not a regression,
    # so it does NOT veto (but is not a win either).
    champ = [_r("o1", 1000.0), _r("o2", 1000.0)]
    within = 1000.0 * (1.0 - RELATIVE_TOL / 2.0)  # 0.25% below -> inside ±0.5% band
    chal = [_r("o1", within), _r("o2", 1100.0)]   # o2 is a clear win
    res = evaluate_relative_adoption(champ, chal)
    verdicts = {o["intent_id"]: o["verdict"] for o in res["per_order"]}
    assert verdicts["o1"] == "matched"
    assert verdicts["o2"] == "win"
    assert res["n_regressions"] == 0
    assert res["adopt"] is True


def test_just_outside_band_is_regression():
    champ = [_r("o1", 1000.0)]
    below = 1000.0 * (1.0 - RELATIVE_TOL * 2.0)  # 1% below -> outside the band
    chal = [_r("o1", below)]
    res = evaluate_relative_adoption(champ, chal)
    assert res["per_order"][0]["verdict"] == "regression"
    assert res["adopt"] is False


def test_both_no_value_is_skipped():
    champ = [_r("o1", None), _r("o2", 0.0)]
    chal = [_r("o1", None), _r("o2", MIN_VALID_OUTPUT)]
    res = evaluate_relative_adoption(champ, chal)
    assert res["scenarios_compared"] == 0
    assert res["adopt"] is False
    assert {o["verdict"] for o in res["per_order"]} == {"skip"}


def test_accepts_per_intent_dicts():
    # The report/manager paths pass stored per_intent dicts, not BenchmarkResults.
    champ = [{"intent_id": "o1", "shadow_score": 100.0}]
    chal = [{"intent_id": "o1", "shadow_score": 150.0}]
    res = evaluate_relative_adoption(champ, chal)
    assert res["adopt"] is True
    assert res["per_order"][0]["ratio"] == 1.5
