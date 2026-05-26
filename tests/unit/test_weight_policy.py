"""Unit tests for weight emission bootstrap policy."""

from minotaur_subnet.weight_policy import (
    GENESIS_HOTKEY,
    build_bootstrap_or_champion_weights,
)
from minotaur_subnet.validator.main import ChampionWeights


def test_build_weights_burns_to_owner_without_champion():
    assert build_bootstrap_or_champion_weights(
        None,
        owner_hotkey="5Gowner",
    ) == {"5Gowner": 1.0}


def test_build_weights_burns_to_owner_for_genesis():
    assert build_bootstrap_or_champion_weights(
        GENESIS_HOTKEY,
        owner_hotkey="5Gowner",
    ) == {"5Gowner": 1.0}


def test_build_weights_routes_to_real_champion():
    assert build_bootstrap_or_champion_weights(
        "5Gminer",
        owner_hotkey="5Gowner",
    ) == {"5Gminer": 1.0}


def test_champion_weights_uses_owner_burn_before_real_champion():
    tracker = ChampionWeights(epoch_seconds=0, owner_hotkey="5Gowner")

    assert tracker.maybe_emit(None) == {"5Gowner": 1.0}
    assert tracker.get_weights(GENESIS_HOTKEY) == {"5Gowner": 1.0}
    assert tracker.get_weights("5Gminer") == {"5Gminer": 1.0}


def test_seed_epoch_clock_stale_emits_on_next_tick():
    """A stale validator (last emit > epoch_seconds ago) must emit on the
    FIRST maybe_emit call, not wait another full epoch."""
    tracker = ChampionWeights(epoch_seconds=1200, owner_hotkey="5Gowner")
    # 30 min since last emit, epoch is 20 min → clearly elapsed
    tracker.seed_epoch_clock_from_last_emit(1800.0)
    assert tracker.maybe_emit(None) == {"5Gowner": 1.0}


def test_seed_epoch_clock_recent_emit_still_waits():
    """A recently-emitted validator (within epoch_seconds) must STILL wait
    the remaining time — we honor the chain rate-limit, just from the
    correct anchor point on the chain rather than process start."""
    tracker = ChampionWeights(epoch_seconds=1200, owner_hotkey="5Gowner")
    # 5 min since last emit, epoch is 20 min → 15 min remaining
    tracker.seed_epoch_clock_from_last_emit(300.0)
    assert tracker.maybe_emit(None) is None


def test_seed_epoch_clock_negative_clamped_to_zero():
    """Clock-skew or buggy callers might pass negative seconds. Treat as
    'just emitted, wait full epoch' rather than crash."""
    tracker = ChampionWeights(epoch_seconds=1200, owner_hotkey="5Gowner")
    tracker.seed_epoch_clock_from_last_emit(-999.0)
    assert tracker.maybe_emit(None) is None  # behaves like a fresh start
