"""The round-anchor pin must fall back to a chain's EXPLICIT public endpoint.

Deriving the anchor is the most consensus-critical read a validator makes, but
it went through `_chain_rpc_env` -> `registry.live_rpc`, which is
archive-envs-ONLY and returns "" on any validator whose operator has not
configured that chain.

Measured on the fleet 2026-08-25 with a chain-964 app live: the leader pinned
{1, 964, 8453} while BOTH followers deferred with no pins at all. 964 already
carried `consensus_public_fallback="https://lite.chain.opentensor.ai"` in the
registry — the pin path simply never consulted it. `derive_fork_pins` fails if
ANY chain fails, so one unconfigured chain took the whole anchor down.

The paired half is the window: that lite endpoint retains ~397 blocks, so the
global 100k floor opens the binary search at head-50k and misses every time.
"""
from __future__ import annotations

import pytest

from minotaur_subnet.chains import registry
from minotaur_subnet.consensus.round_anchor import find_pin_block


def test_964_has_a_public_fallback_to_reach():
    assert registry.spec(964).consensus_public_fallback


def test_the_fallback_is_that_chain_never_the_local_node():
    """Why the fix uses the explicit fallback and NOT registry.consensus_rpc.

    consensus_rpc looks like the right ladder and is not: its last rung is the
    LOCAL NODE for every chain except 1. A validator with no Base archive would
    pin Base against its own anvil — a confident pin off the WRONG CHAIN, which
    is strictly worse than the defer it would replace. An explicit per-chain
    consensus_public_fallback is safe precisely because that value IS that chain.
    """
    assert registry.spec(8453).consensus_public_fallback is None
    assert registry.spec(1).consensus_public_fallback is None
    assert "opentensor" in registry.spec(964).consensus_public_fallback


def test_only_chains_with_an_explicit_fallback_gain_one():
    """The change must be inert for every chain that has none."""
    for cid in (1, 8453):
        assert registry.spec(cid).consensus_public_fallback is None


def test_the_window_is_per_chain_and_fits_the_shallow_endpoint():
    w = registry.pin_search_window(964)
    assert w <= 397, "must fit the lite endpoint's measured ~397-block retention"
    assert w >= 100, "must still clear the pin, which sits ~17 blocks back"


def test_other_chains_keep_the_generous_default():
    assert registry.pin_search_window(1) == 100_000
    assert registry.pin_search_window(8453) == 100_000
    assert registry.pin_search_window(999999) == 100_000   # unknown -> default


# ── the property that makes a per-chain window safe ──────────────────────────

HEAD, BLOCK_S, NOW, CONFIRMATIONS = 8_924_366, 12, 1_787_688_960, 12
RETAINED = 397          # what the public endpoint actually serves


def _ts(b: int) -> int:
    if b < HEAD - RETAINED:
        raise Exception(f"Block with id: '{hex(b)}' not found.")
    return NOW - (HEAD - b) * BLOCK_S


def test_the_global_window_would_miss_on_a_shallow_endpoint():
    """The bug this pairs with: 100k opens the search beyond what is served."""
    with pytest.raises(Exception, match="not found"):
        find_pin_block(NOW - 180, head=HEAD, block_timestamp=_ts,
                       confirmations=CONFIRMATIONS, lo=HEAD - 100_000)


@pytest.mark.parametrize("window", [100, 200, 300])
def test_every_fitting_window_gives_the_identical_pin(window):
    """Measured against the live endpoint: 100/200/300 all returned head-17.

    This is why a per-chain window cannot fragment the fleet — two validators on
    different windows still agree, so long as both fit.
    """
    a = find_pin_block(NOW - 180, head=HEAD, block_timestamp=_ts,
                       confirmations=CONFIRMATIONS, lo=HEAD - window)
    b = find_pin_block(NOW - 180, head=HEAD, block_timestamp=_ts,
                       confirmations=CONFIRMATIONS, lo=HEAD - 300)
    assert a == b


def test_a_window_too_small_for_the_anchor_fails_loud():
    """The one way to size it wrong must raise, never return a wrong pin."""
    from minotaur_subnet.consensus.round_anchor import ForkPinUnavailable
    with pytest.raises(ForkPinUnavailable):
        find_pin_block(NOW - 180, head=HEAD, block_timestamp=_ts,
                       confirmations=CONFIRMATIONS, lo=HEAD - 5)
