"""Tests for the miner cost-awareness gate."""

from __future__ import annotations

import pytest
from unittest.mock import patch

from minotaur_subnet.miner.agent import cost_gate
from minotaur_subnet.miner.agent.cost_gate import CostGate, CostGateState


@pytest.fixture
def state_dir(tmp_path):
    return tmp_path


@pytest.fixture
def gate(state_dir):
    return CostGate(
        miner_id="alpha",
        state_dir=state_dir,
        plateau_k=3,
        plateau_min_delta=0.01,
        plateau_cooldown_seconds=60,
        token_budget_per_day=1000,
    )


# ── Token budget ──────────────────────────────────────────────────────────

def test_token_budget_blocks_run_when_exceeded(gate):
    # Start: no budget used. Behind-and-challenged scenario so rank rules
    # don't fire either way.
    d1 = gate.should_run_this_cycle(
        champion=None, my_best_score=0.1,
        top_rival_score=0.9, new_submissions_since_ours=3,
    )
    assert d1.should_run is True

    # Exceed the budget — now should_run must be False regardless of rank
    gate.record_token_usage(1200)
    assert gate.state.token_budget_used == 1200
    d2 = gate.should_run_this_cycle(
        champion=None, my_best_score=0.1,
        top_rival_score=0.9, new_submissions_since_ours=3,
    )
    assert d2.should_run is False
    assert d2.reason == "TOKEN_BUDGET"


def test_token_budget_rolls_at_utc_midnight(gate):
    gate.state.token_budget_date = "2024-01-01"
    gate.state.token_budget_used = 999_999  # way over
    # Today is NOT 2024-01-01, so the first call should roll the day
    d = gate.should_run_this_cycle(
        champion=None, my_best_score=0.1,
        top_rival_score=0.9, new_submissions_since_ours=3,
    )
    assert d.should_run is True
    assert gate.state.token_budget_used == 0
    assert gate.state.token_budget_date != "2024-01-01"


def test_token_usage_persists_across_instances(state_dir):
    g1 = CostGate(miner_id="alpha", state_dir=state_dir, token_budget_per_day=500)
    g1.record_token_usage(300)

    g2 = CostGate(miner_id="alpha", state_dir=state_dir, token_budget_per_day=500)
    # g2 should have loaded the persisted state
    assert g2.state.token_budget_used == 300


# ── Plateau detection ─────────────────────────────────────────────────────

def test_plateau_enters_after_k_cycles_without_improvement(gate):
    # K=3; four flat-zero cycles needed because the first counts the 0→0
    # no-delta as a flat cycle too. Actually: from initial last=0.0, best=0.0
    # yields delta=0 < min_delta, so each flat cycle increments. K=3 trips
    # on the third flat cycle.
    gate.record_cycle(best_score=0.0, submitted=False)
    gate.record_cycle(best_score=0.0, submitted=False)
    gate.record_cycle(best_score=0.0, submitted=False)
    assert gate.state.plateau_entered_at is not None

    d = gate.should_run_this_cycle(
        champion=None, my_best_score=0.0,
        top_rival_score=0.9, new_submissions_since_ours=5,
    )
    assert d.should_run is False
    assert d.reason == "PLATEAU"


def test_plateau_cleared_on_real_improvement(gate):
    # First cycle: delta = 0.5 - 0.0 = 0.5 → improvement, counters reset.
    # Cycles 2-4: delta = 0 → counter increments. After cycle 4 (3 flats),
    # plateau trips.
    gate.record_cycle(best_score=0.5, submitted=False)  # improvement
    gate.record_cycle(best_score=0.5, submitted=False)  # flat 1
    gate.record_cycle(best_score=0.5, submitted=False)  # flat 2
    gate.record_cycle(best_score=0.5, submitted=False)  # flat 3 → plateau
    assert gate.state.plateau_entered_at is not None

    # A cycle with real improvement clears the plateau
    gate.record_cycle(best_score=0.6, submitted=True)
    assert gate.state.plateau_entered_at is None
    assert gate.state.cycles_without_improvement == 0


def test_plateau_cooldown_expires(gate, monkeypatch):
    # Enter plateau
    gate.record_cycle(best_score=0.0, submitted=False)
    gate.record_cycle(best_score=0.0, submitted=False)
    gate.record_cycle(best_score=0.0, submitted=False)
    assert gate.state.plateau_entered_at is not None
    entered_at = gate.state.plateau_entered_at

    # Skip forward past the cooldown window
    monkeypatch.setattr(cost_gate, "_now", lambda: entered_at + 61)
    d = gate.should_run_this_cycle(
        champion=None, my_best_score=0.0,
        top_rival_score=0.9, new_submissions_since_ours=5,
    )
    assert d.should_run is True
    # Cooldown elapsed → plateau flag cleared so we try again
    assert gate.state.plateau_entered_at is None


# ── Champion + rank checks ────────────────────────────────────────────────

def test_champion_unchallenged_skips(gate):
    champion = {"miner_id": "alpha", "hotkey": "5Foo"}
    d = gate.should_run_this_cycle(
        champion=champion, my_best_score=0.9,
        top_rival_score=0.6, new_submissions_since_ours=0,
    )
    assert d.should_run is False
    assert d.reason == "CHAMPION_UNCHALLENGED"


def test_champion_runs_when_rival_ahead(gate):
    champion = {"miner_id": "alpha", "hotkey": "5Foo"}
    # Rival has higher score — we must iterate even as champion
    d = gate.should_run_this_cycle(
        champion=champion, my_best_score=0.6,
        top_rival_score=0.95, new_submissions_since_ours=2,
    )
    assert d.should_run is True


def test_top_ranked_but_new_submissions_means_run(gate):
    """We're top but somebody just submitted — our next cycle might need to react."""
    d = gate.should_run_this_cycle(
        champion=None, my_best_score=0.8,
        top_rival_score=0.7, new_submissions_since_ours=1,
    )
    assert d.should_run is True


def test_top_ranked_unchallenged_skips(gate):
    d = gate.should_run_this_cycle(
        champion=None, my_best_score=0.8,
        top_rival_score=0.7, new_submissions_since_ours=0,
    )
    assert d.should_run is False
    assert d.reason == "TOP_RANKED_UNCHALLENGED"


def test_behind_and_not_champion_runs(gate):
    d = gate.should_run_this_cycle(
        champion={"miner_id": "charlie"}, my_best_score=0.4,
        top_rival_score=0.7, new_submissions_since_ours=1,
    )
    assert d.should_run is True


def test_empty_state_doesnt_trigger_top_ranked_rule(gate):
    """Fresh miner with no scores yet must still iterate — my_best_score=0 with
    top_rival_score=0 should NOT pretend we're 'top ranked unchallenged'."""
    d = gate.should_run_this_cycle(
        champion=None, my_best_score=0.0,
        top_rival_score=0.0, new_submissions_since_ours=0,
    )
    assert d.should_run is True
