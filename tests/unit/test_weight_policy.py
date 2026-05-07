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
