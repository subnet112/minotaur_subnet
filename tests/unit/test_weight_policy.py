"""Unit tests for weight emission bootstrap policy."""

from unittest.mock import MagicMock

import minotaur_subnet.weight_policy as weight_policy
from minotaur_subnet.weight_policy import (
    CHAMPION_MINER_WEIGHT_FRACTION,
    GENESIS_HOTKEY,
    apply_champion_burn_ramp,
    build_bootstrap_or_champion_weights,
    lookup_subnet_owner_from_chain,
    resolve_subnet_owner_hotkey,
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


def test_build_weights_ramps_champion_with_owner_burn():
    # Once a real miner champion exists: the champion gets the fixed fraction
    # (0.10), the owner burns the rest (0.90).
    assert build_bootstrap_or_champion_weights(
        "5Gminer",
        owner_hotkey="5Gowner",
    ) == {
        "5Gowner": 1 - CHAMPION_MINER_WEIGHT_FRACTION,
        "5Gminer": CHAMPION_MINER_WEIGHT_FRACTION,
    }


def test_build_weights_full_to_champion_when_owner_unresolvable(monkeypatch):
    # No resolvable owner anywhere -> can't burn -> route fully to the champion.
    monkeypatch.delenv("SUBNET_OWNER_HOTKEY", raising=False)
    monkeypatch.delenv("OWNER_HOTKEY", raising=False)
    assert build_bootstrap_or_champion_weights(
        "5Gminer",
        owner_hotkey=None,
    ) == {"5Gminer": 1.0}


def test_build_weights_full_to_champion_when_champion_is_owner():
    # Champion IS the owner -> no point splitting to itself; 100%.
    assert build_bootstrap_or_champion_weights(
        "5Gowner",
        owner_hotkey="5Gowner",
    ) == {"5Gowner": 1.0}


def test_champion_ramp_split_sums_to_one():
    w = build_bootstrap_or_champion_weights("5Gminer", owner_hotkey="5Gowner")
    assert abs(sum(w.values()) - 1.0) < 1e-9
    assert w["5Gminer"] == CHAMPION_MINER_WEIGHT_FRACTION


def test_apply_burn_ramp_caps_miners_preserves_ratios():
    # A ranked multi-miner distribution: miners collectively get the champion
    # fraction, the owner the rest, and the relative split AMONG miners is preserved.
    ramped = apply_champion_burn_ramp(
        {"m1": 0.6, "m2": 0.3, "m3": 0.1}, owner_hotkey="5Gowner"
    )
    assert abs(sum(ramped.values()) - 1.0) < 1e-9
    assert abs(ramped["5Gowner"] - (1 - CHAMPION_MINER_WEIGHT_FRACTION)) < 1e-9
    assert abs(
        (ramped["m1"] + ramped["m2"] + ramped["m3"]) - CHAMPION_MINER_WEIGHT_FRACTION
    ) < 1e-9
    assert abs(ramped["m1"] / ramped["m2"] - 2.0) < 1e-9  # 0.6/0.3 preserved


def test_apply_burn_ramp_noop_without_owner(monkeypatch):
    monkeypatch.delenv("SUBNET_OWNER_HOTKEY", raising=False)
    monkeypatch.delenv("OWNER_HOTKEY", raising=False)
    assert apply_champion_burn_ramp({"m1": 1.0}, owner_hotkey=None) == {"m1": 1.0}


def test_apply_burn_ramp_drops_owner_and_still_burns():
    # Footgun fix: the owner appearing in the miner set must NOT skip the burn.
    # The owner is the burn target, not a miner — it is dropped, the remaining
    # miner re-normalized, and the burn still applies.
    ramped = apply_champion_burn_ramp(
        {"5Gowner": 0.7, "m1": 0.3}, owner_hotkey="5Gowner"
    )
    assert ramped == {
        "5Gowner": 1 - CHAMPION_MINER_WEIGHT_FRACTION,
        "m1": CHAMPION_MINER_WEIGHT_FRACTION,
    }


def test_apply_burn_ramp_owner_among_many_miners_burn_not_skipped():
    # The actual footgun: an owner submission landing among MULTIPLE ranked miners
    # must not disable the burn for everyone. Owner dropped; the remaining miners
    # share the champion fraction with their relative ratio preserved.
    ramped = apply_champion_burn_ramp(
        {"5Gowner": 0.5, "m1": 0.3, "m2": 0.2}, owner_hotkey="5Gowner"
    )
    assert abs(sum(ramped.values()) - 1.0) < 1e-9
    assert abs(ramped["5Gowner"] - (1 - CHAMPION_MINER_WEIGHT_FRACTION)) < 1e-9
    assert abs((ramped["m1"] + ramped["m2"]) - CHAMPION_MINER_WEIGHT_FRACTION) < 1e-9
    assert abs(ramped["m1"] / ramped["m2"] - 1.5) < 1e-9  # 0.3/0.2 preserved


def test_champion_weights_uses_owner_burn_before_real_champion():
    tracker = ChampionWeights(epoch_seconds=0, owner_hotkey="5Gowner")

    assert tracker.maybe_emit(None) == {"5Gowner": 1.0}
    assert tracker.get_weights(GENESIS_HOTKEY) == {"5Gowner": 1.0}
    assert tracker.get_weights("5Gminer") == {
        "5Gowner": 1 - CHAMPION_MINER_WEIGHT_FRACTION,
        "5Gminer": CHAMPION_MINER_WEIGHT_FRACTION,
    }


def test_close_epoch_now_bypasses_wall_clock():
    """The tempo-aligned scheduler decides timing from the CHAIN clock —
    close_epoch_now must return weights even when epoch_seconds hasn't
    elapsed (where maybe_emit would return None), and still reset the
    wall clock so a later fallback spaces itself from this emit."""
    tracker = ChampionWeights(epoch_seconds=1200, owner_hotkey="5Gowner")
    tracker.seed_epoch_clock_from_last_emit(0.0)  # just emitted

    assert tracker.maybe_emit("5Gminer") is None  # wall clock vetoes
    result = tracker.close_epoch_now("5Gminer")
    assert result == {
        "5Gowner": 1 - CHAMPION_MINER_WEIGHT_FRACTION,
        "5Gminer": CHAMPION_MINER_WEIGHT_FRACTION,
    }
    # History recorded like a normal epoch close.
    assert tracker.get_history()[-1]["champion"] == "5Gminer"
    # Clock reset → the wall-clock path still waits a full epoch from now.
    assert tracker.maybe_emit("5Gminer") is None


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


def test_resolve_subnet_owner_chain_primary(monkeypatch):
    """The chain is authoritative: when the on-chain lookup returns a value it
    wins, even if the env owner is also set."""
    monkeypatch.setenv("SUBNET_OWNER_HOTKEY", "5Genv")
    monkeypatch.setattr(
        weight_policy, "lookup_subnet_owner_from_chain", lambda sub, nuid: "5Gchain"
    )
    sub = MagicMock()
    assert resolve_subnet_owner_hotkey(sub, 112) == "5Gchain"


def test_resolve_subnet_owner_falls_back_to_env(monkeypatch):
    """When the chain lookup returns '', fall back to the env owner."""
    monkeypatch.setenv("SUBNET_OWNER_HOTKEY", "5Genv")
    monkeypatch.setattr(
        weight_policy, "lookup_subnet_owner_from_chain", lambda sub, nuid: ""
    )
    sub = MagicMock()
    assert resolve_subnet_owner_hotkey(sub, 112) == "5Genv"


def test_resolve_subnet_owner_env_only_without_subtensor(monkeypatch):
    """With no subtensor/netuid wired, resolve straight from env."""
    monkeypatch.setenv("SUBNET_OWNER_HOTKEY", "5Genv")
    monkeypatch.setattr(
        weight_policy,
        "lookup_subnet_owner_from_chain",
        lambda sub, nuid: (_ for _ in ()).throw(AssertionError("should not query chain")),
    )
    assert resolve_subnet_owner_hotkey(None, None) == "5Genv"


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


def test_ramp_with_full_fraction_routes_everything_to_miner():
    # 1.0 fraction → no burn; the champion takes the whole emission.
    ramped = apply_champion_burn_ramp(
        {"5Gminer": 1.0}, owner_hotkey="5Gowner", miner_fraction=1.0
    )
    assert ramped == {"5Gminer": 1.0, "5Gowner": 0.0}


def test_ramp_is_idempotent_in_fraction():
    # Re-applying the split with a new fraction re-targets the aggregate share
    # cleanly — the emission path relies on this.
    floor = apply_champion_burn_ramp({"5Gminer": 1.0}, owner_hotkey="5Gowner")
    assert floor == {
        "5Gminer": CHAMPION_MINER_WEIGHT_FRACTION,
        "5Gowner": 1 - CHAMPION_MINER_WEIGHT_FRACTION,
    }
    rescaled = apply_champion_burn_ramp(
        floor, owner_hotkey="5Gowner", miner_fraction=0.5
    )
    assert abs(rescaled["5Gminer"] - 0.5) < 1e-9
    assert abs(rescaled["5Gowner"] - 0.5) < 1e-9


def test_ramp_partial_fraction_preserves_multi_miner_ratio():
    ramped = apply_champion_burn_ramp(
        {"m1": 0.6, "m2": 0.3, "m3": 0.1},
        owner_hotkey="5Gowner",
        miner_fraction=0.5,
    )
    assert abs(sum(ramped.values()) - 1.0) < 1e-9
    assert abs(ramped["5Gowner"] - 0.5) < 1e-9
    assert abs((ramped["m1"] + ramped["m2"] + ramped["m3"]) - 0.5) < 1e-9
    assert abs(ramped["m1"] / ramped["m2"] - 2.0) < 1e-9  # 0.6/0.3 preserved
