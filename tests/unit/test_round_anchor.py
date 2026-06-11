"""Tests for round-anchored fork-pin derivation (Option b, canonical/verifiable).

The whole mechanism rests on every validator computing the *identical* integer
from the same anchor, so these tests focus on: exact/at-or-before selection,
the confirmation + bracketing determinism guard, deferral (never guess), the
multi-chain map, and stable serialization for the pack hash.
"""

from __future__ import annotations

import pytest

from minotaur_subnet.consensus.round_anchor import (
    ForkPinUnavailable,
    derive_fork_pins,
    epoch_anchor_ts,
    find_pin_block,
    serialize_fork_pins,
)


# ── epoch_anchor_ts ───────────────────────────────────────────────────────────


def test_epoch_anchor_ts_is_epoch_times_seconds():
    assert epoch_anchor_ts(100, 60) == 6000
    assert epoch_anchor_ts(0, 60) == 0


def test_epoch_anchor_ts_rejects_nonpositive_seconds():
    with pytest.raises(ValueError):
        epoch_anchor_ts(100, 0)


class _Chain:
    """Synthetic chain: block b has timestamp t0 + b * spacing, head at `head`."""

    def __init__(self, *, t0: int = 1_000_000, spacing: int = 12, head: int = 10_000):
        self.t0 = t0
        self.spacing = spacing
        self.head = head

    def ts(self, block: int) -> int:
        return self.t0 + block * self.spacing


# ── find_pin_block ────────────────────────────────────────────────────────────


def test_exact_timestamp_returns_that_block():
    c = _Chain()
    block = 5000
    pin = find_pin_block(c.ts(block), head=c.head, block_timestamp=c.ts)
    assert pin == block


def test_between_blocks_returns_the_earlier_block():
    c = _Chain()
    # A timestamp one second after block 5000 but before 5001.
    pin = find_pin_block(c.ts(5000) + 1, head=c.head, block_timestamp=c.ts)
    assert pin == 5000


def test_confirmations_cap_eligible_range():
    c = _Chain(head=10_000)
    anchor = c.ts(9_999)  # within the confirmation margin of the head
    # Anchor is above the confirmed tip (head-5=9995) -> not bracketed -> defer.
    with pytest.raises(ForkPinUnavailable):
        find_pin_block(anchor, head=c.head, block_timestamp=c.ts, confirmations=5)
    # A comfortably-past anchor pins below the confirmed tip.
    pin = find_pin_block(c.ts(9_000), head=c.head, block_timestamp=c.ts, confirmations=5)
    assert pin == 9_000
    assert pin <= c.head - 5


def test_defer_when_anchor_at_or_after_confirmed_tip():
    c = _Chain(head=1_000)
    # Anchor exactly at the head timestamp: no confirmed block strictly after it.
    with pytest.raises(ForkPinUnavailable):
        find_pin_block(c.ts(1_000), head=c.head, block_timestamp=c.ts)
    # Anchor in the future relative to the chain: also defer.
    with pytest.raises(ForkPinUnavailable):
        find_pin_block(c.ts(1_000) + 99_999, head=c.head, block_timestamp=c.ts)


def test_defer_when_chain_too_short():
    c = _Chain(head=3)
    with pytest.raises(ForkPinUnavailable):
        find_pin_block(c.ts(1), head=c.head, block_timestamp=c.ts, confirmations=10)


def test_negative_confirmations_rejected():
    c = _Chain()
    with pytest.raises(ValueError):
        find_pin_block(c.ts(10), head=c.head, block_timestamp=c.ts, confirmations=-1)


def test_pin_is_stable_as_chain_grows():
    # The determinism property: once the anchor is bracketed, the pin does not
    # change as the chain extends. Two nodes seeing different heads (but the same
    # history + same anchor) derive the same block.
    spacing = 12
    anchor = 1_000_000 + 5_000 * spacing + 3  # mid-way past block 5000
    pins = {
        head: find_pin_block(
            anchor,
            head=head,
            block_timestamp=lambda b: 1_000_000 + b * spacing,
        )
        for head in (5_001, 6_000, 9_999, 50_000)
    }
    assert set(pins.values()) == {5_000}


def test_lo_floor_respected_and_defers_when_anchor_precedes_lo():
    c = _Chain()
    # Anchor before the lo block's timestamp -> nothing qualifies -> defer.
    with pytest.raises(ForkPinUnavailable):
        find_pin_block(c.ts(100), head=c.head, block_timestamp=c.ts, lo=200)


# ── derive_fork_pins ──────────────────────────────────────────────────────────


def test_multi_chain_map_keyed_by_chain_id():
    # Two chains with different block rates share one anchor timestamp.
    base = _Chain(t0=1_000_000, spacing=2, head=2_000_000)   # 8453: ~2s blocks
    btevm = _Chain(t0=1_000_000, spacing=12, head=400_000)   # 964: ~12s blocks
    anchor = 1_000_000 + 100_000  # +100k seconds from shared t0

    chains = {8453: base, 964: btevm}
    pins = derive_fork_pins(
        anchor,
        list(chains),
        head_of=lambda c: chains[c].head,
        block_timestamp_of=lambda c, b: chains[c].ts(b),
    )
    assert pins == {8453: 50_000, 964: 8_333}  # 100000/2 and floor(100000/12)


def test_derive_defers_if_any_chain_cannot_be_pinned():
    good = _Chain(t0=1_000_000, spacing=2, head=2_000_000)
    short = _Chain(t0=1_000_000, spacing=12, head=5)  # can't bracket the anchor
    anchor = 1_000_000 + 100_000
    chains = {8453: good, 964: short}
    with pytest.raises(ForkPinUnavailable) as exc:
        derive_fork_pins(
            anchor,
            list(chains),
            head_of=lambda c: chains[c].head,
            block_timestamp_of=lambda c, b: chains[c].ts(b),
        )
    assert "964" in str(exc.value)


# ── serialize_fork_pins ───────────────────────────────────────────────────────


def test_serialize_is_sorted_and_insertion_order_independent():
    a = serialize_fork_pins({8453: 46_904_887, 964: 5_012_345})
    b = serialize_fork_pins({964: 5_012_345, 8453: 46_904_887})
    assert a == b == "964:5012345|8453:46904887"


def test_serialize_empty():
    assert serialize_fork_pins({}) == ""
