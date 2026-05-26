"""Unit tests for weight emission bootstrap policy."""

from unittest.mock import MagicMock

from minotaur_subnet.weight_policy import (
    GENESIS_HOTKEY,
    build_bootstrap_or_champion_weights,
    lookup_subnet_owner_from_chain,
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


def test_maybe_emit_empty_does_not_advance_epoch_clock(monkeypatch):
    """When weights end up empty (no champion AND no resolvable owner_hotkey)
    the epoch clock must NOT advance — otherwise a late-resolving owner
    (env set after startup, slow chain query) would have to wait a full
    additional epoch_seconds before next attempt."""
    # No owner_hotkey set anywhere — empty weights inevitable
    monkeypatch.delenv("SUBNET_OWNER_HOTKEY", raising=False)
    monkeypatch.delenv("OWNER_HOTKEY", raising=False)
    tracker = ChampionWeights(epoch_seconds=1200, owner_hotkey="")
    # Seed the clock so we'd otherwise emit
    tracker.seed_epoch_clock_from_last_emit(9999.0)

    epoch_start_before = tracker._epoch_start
    result = tracker.maybe_emit(None)

    assert result is None  # empty → None (not the empty dict)
    assert tracker._epoch_start == epoch_start_before  # clock did NOT advance


def test_lookup_subnet_owner_from_chain_returns_value():
    """Pull SubnetOwnerHotkey storage out of subtensor — happy path."""
    sub = MagicMock()
    result = MagicMock()
    result.value = "5E1ohAszHfhyQUEtz6mvCCkW4pYHsinPjxXS938fAZ2jFvCt"
    sub.query_subtensor = MagicMock(return_value=result)

    owner = lookup_subnet_owner_from_chain(sub, 112)

    assert owner == "5E1ohAszHfhyQUEtz6mvCCkW4pYHsinPjxXS938fAZ2jFvCt"
    sub.query_subtensor.assert_called_once_with("SubnetOwnerHotkey", params=[112])


def test_lookup_subnet_owner_from_chain_empty_on_error():
    """Network/RPC failure must return '' — caller falls back gracefully
    instead of crashing the daemon at startup."""
    sub = MagicMock()
    sub.query_subtensor = MagicMock(side_effect=RuntimeError("rpc down"))

    owner = lookup_subnet_owner_from_chain(sub, 112)

    assert owner == ""


def test_maybe_emit_recovers_after_owner_hotkey_set_late(monkeypatch):
    """Operator-self-heal flow: daemon starts with no owner, emits None
    every tick, then operator sets the hotkey at runtime. The very NEXT
    maybe_emit should emit (no extra wait)."""
    monkeypatch.delenv("SUBNET_OWNER_HOTKEY", raising=False)
    monkeypatch.delenv("OWNER_HOTKEY", raising=False)
    tracker = ChampionWeights(epoch_seconds=1200, owner_hotkey="")
    tracker.seed_epoch_clock_from_last_emit(9999.0)
    assert tracker.maybe_emit(None) is None  # initially empty

    # Operator sets the hotkey
    tracker.owner_hotkey = "5Gowner"

    # Next tick must emit immediately (clock wasn't advanced by the empty call)
    assert tracker.maybe_emit(None) == {"5Gowner": 1.0}
