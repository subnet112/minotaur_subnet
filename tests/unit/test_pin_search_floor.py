"""The fork-pin search must never read a block the upstream cannot serve.

`find_pin_block` opens with `block_timestamp(lo)`. With `lo` defaulting to 0
that is a GENESIS read, and not every upstream serves genesis: the leader's
chain-964 node is pruned (measured 2026-08-25 — blocks 0, 1 and 1e6 return
BlockNotFound; 5e6 and above are fine). The read raised, `derive_fork_pins`
failed, and NO chain got pinned:

    fork-pins: derivation failed for epoch N: Block with id: '0x0' not found.

Ethereum and Base never hit it because their upstreams serve genesis — which is
why this survived until chain 964 first entered the pin set.
"""
from __future__ import annotations

import pytest

from minotaur_subnet.consensus.round_anchor import (
    ForkPinUnavailable,
    derive_fork_pins,
    find_pin_block,
)

HEAD = 8_922_226
BLOCK_S = 12
NOW = 1_787_662_284
PRUNED_BELOW = 5_000_000          # what the 964 node actually serves
CONFIRMATIONS = 12


def _ts(b: int) -> int:
    """Timestamps for a synthetic 964, refusing pruned blocks like the real node."""
    if b < PRUNED_BELOW:
        raise Exception(f"Block with id: '{hex(b)}' not found.")
    return NOW - (HEAD - b) * BLOCK_S


def test_a_genesis_floor_breaks_on_a_pruned_upstream():
    """The bug, stated directly: lo=0 reads a block that does not exist."""
    with pytest.raises(Exception, match="not found"):
        find_pin_block(NOW - 180, head=HEAD, block_timestamp=_ts,
                       confirmations=CONFIRMATIONS, lo=0)


def test_a_tip_relative_floor_pins_fine():
    lo = HEAD - 100_000
    pin = find_pin_block(NOW - 180, head=HEAD, block_timestamp=_ts,
                         confirmations=CONFIRMATIONS, lo=lo)
    assert lo <= pin <= HEAD - CONFIRMATIONS
    assert _ts(pin) <= NOW - 180


@pytest.mark.parametrize("lo", [HEAD - 1_000_000, HEAD - 100_000,
                                HEAD - 10_000, HEAD - 1_000])
def test_the_floor_cannot_move_the_pin(lo):
    """Every floor below the answer must give the IDENTICAL answer.

    This is what makes a tip-relative floor safe across validators whose heads
    differ by a few blocks.
    """
    lo = max(lo, PRUNED_BELOW)
    anchor = NOW - 180
    assert find_pin_block(anchor, head=HEAD, block_timestamp=_ts,
                          confirmations=CONFIRMATIONS, lo=lo) == find_pin_block(
        anchor, head=HEAD, block_timestamp=_ts,
        confirmations=CONFIRMATIONS, lo=PRUNED_BELOW)


def test_two_nodes_with_different_heads_still_agree():
    """The determinism property the whole module exists to protect.

    Anchored 300s back, not 180s: at exactly 180s the anchor lands ON the
    trailing node's confirmed tip, and the bracketing rule (which wants a
    confirmed block STRICTLY after the anchor) makes that node defer while the
    leading one pins. That is fail-safe rather than divergent — a defer, never a
    different pin — but it is a real edge, and it is why the anchor lookback
    carries margin instead of sitting on the boundary.
    """
    anchor = NOW - 300
    a = find_pin_block(anchor, head=HEAD, block_timestamp=_ts,
                       confirmations=CONFIRMATIONS, lo=HEAD - 100_000)
    b = find_pin_block(anchor, head=HEAD - 3, block_timestamp=_ts,
                       confirmations=CONFIRMATIONS, lo=HEAD - 3 - 100_000)
    assert a == b


def test_a_trailing_node_defers_rather_than_pinning_differently():
    """Pin the edge above explicitly: the failure mode is a DEFER, not a split."""
    anchor = NOW - 180
    find_pin_block(anchor, head=HEAD, block_timestamp=_ts,
                   confirmations=CONFIRMATIONS, lo=HEAD - 100_000)
    with pytest.raises(ForkPinUnavailable, match="not yet confirmed-bracketed"):
        find_pin_block(anchor, head=HEAD - 3, block_timestamp=_ts,
                       confirmations=CONFIRMATIONS, lo=HEAD - 3 - 100_000)


def test_a_floor_above_the_anchor_fails_loud_not_wrong():
    """The one way a floor can be wrong must be an exception, never a bad pin.

    Two distinct guards catch it depending on how high the floor is, and both
    raise ForkPinUnavailable: above the confirmed tip it is "chain too short",
    below that but after the anchor it is "anchor precedes lo block".
    """
    with pytest.raises(ForkPinUnavailable, match="chain too short"):
        find_pin_block(NOW - 100_000, head=HEAD, block_timestamp=_ts,
                       confirmations=CONFIRMATIONS, lo=HEAD - 10)
    with pytest.raises(ForkPinUnavailable, match="precedes lo block"):
        find_pin_block(NOW - 100_000, head=HEAD, block_timestamp=_ts,
                       confirmations=CONFIRMATIONS, lo=HEAD - 1_000)


def test_derive_fork_pins_threads_the_floor_per_chain():
    """A pruned chain must not take the other chains down with it."""
    served = {1: 0, 8453: 0, 964: PRUNED_BELOW}

    def ts_of(chain_id, b):
        if b < served[chain_id]:
            raise Exception(f"Block with id: '{hex(b)}' not found.")
        return NOW - (HEAD - b) * BLOCK_S

    pins = derive_fork_pins(
        NOW - 180, [1, 8453, 964],
        head_of=lambda c: HEAD,
        block_timestamp_of=ts_of,
        confirmations=CONFIRMATIONS,
        lo_of=lambda c: max(0, HEAD - 100_000),
    )
    assert set(pins) == {1, 8453, 964}
    assert all(p <= HEAD - CONFIRMATIONS for p in pins.values())


def test_the_window_constant_is_far_above_any_anchor():
    from minotaur_subnet.api.startup import _PIN_SEARCH_WINDOW_BLOCKS
    # The pin sits an epoch or two back — a few blocks. The floor must be orders
    # of magnitude below that, or it could clip a legitimate pin.
    assert _PIN_SEARCH_WINDOW_BLOCKS >= 50_000
