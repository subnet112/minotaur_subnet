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
    round_anchored_pin_enabled,
    serialize_fork_pins,
)


# ── round_anchored_pin_enabled (fleet-uniform gate, DEFAULT ON) ───────────────


def test_gate_default_on_when_unset(monkeypatch):
    monkeypatch.delenv("ROUND_ANCHORED_PIN", raising=False)
    assert round_anchored_pin_enabled() is True


@pytest.mark.parametrize("val", ["0", "false", "FALSE", "no", "off", " Off "])
def test_gate_disabled_by_explicit_off_values(monkeypatch, val):
    monkeypatch.setenv("ROUND_ANCHORED_PIN", val)
    assert round_anchored_pin_enabled() is False


@pytest.mark.parametrize("val", ["1", "true", "on", "yes", "", "garbage", "2"])
def test_gate_enabled_for_anything_but_off_values(monkeypatch, val):
    # Critical fail-safe: a typo (e.g. "garbage"/empty) must NOT silently disable
    # one validator and split it off the fleet — only the explicit off-set does.
    monkeypatch.setenv("ROUND_ANCHORED_PIN", val)
    assert round_anchored_pin_enabled() is True


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


# ── round_anchor_ts (confirmation-margin lookback) ────────────────────────────


def test_round_anchor_ts_applies_one_epoch_lookback():
    from minotaur_subnet.consensus.round_anchor import (
        ROUND_ANCHOR_LOOKBACK_EPOCHS,
        epoch_anchor_ts,
        round_anchor_ts,
    )
    # The fork-pin anchor is one round-epoch earlier than the round's (close) epoch,
    # so the ~24s confirmation margin can confirm-bracket it by close instead of
    # deferring forever. Still a pure function of the epoch (no chain read).
    assert ROUND_ANCHOR_LOOKBACK_EPOCHS == 1
    # epoch 100, epoch_seconds 60 -> anchor at (100-1)*60 = 5940 (epoch 99's boundary)
    assert round_anchor_ts(100, 60) == epoch_anchor_ts(99, 60) == 5940
    # exactly one epoch_seconds earlier than the naive close-epoch anchor
    assert round_anchor_ts(100, 60) == epoch_anchor_ts(100, 60) - 60


# ── #632: per-chain lookback (slow chains anchor deeper so they bracket at open) ──

from minotaur_subnet.consensus.round_anchor import (  # noqa: E402
    ROUND_ANCHOR_LOOKBACK_EPOCHS,
    round_anchor_lookback_epochs,
    round_anchor_ts,
    round_anchor_ts_for_chain,
)

BASE, ETH = 8453, 1
_EPOCH_SECONDS, _EPOCH = 60, 100


def _fleet_chains():
    """Base (~2s blocks) + Ethereum (~12s blocks) at a common wall-clock == the
    round-open timestamp (E*epoch_seconds), so only `lookback` epochs of buffer
    exist — the exact condition that froze the leader on 2026-07-08."""
    now = _EPOCH * _EPOCH_SECONDS  # 6000
    chains = {
        BASE: _Chain(t0=0, spacing=2, head=now // 2),    # 3000
        ETH:  _Chain(t0=0, spacing=12, head=now // 12),  # 500
    }
    head_of = lambda c: chains[c].head
    ts_of = lambda c, b: chains[c].ts(b)
    return chains, head_of, ts_of


def test_lookback_is_per_chain():
    assert round_anchor_lookback_epochs(BASE) == ROUND_ANCHOR_LOOKBACK_EPOCHS == 1
    assert round_anchor_lookback_epochs(ETH) == 3
    assert round_anchor_lookback_epochs(999999) == ROUND_ANCHOR_LOOKBACK_EPOCHS  # unknown → default


def test_per_chain_anchor_ts_matches_default_for_base():
    # Base uses the default lookback → its anchor is byte-identical to round_anchor_ts
    # (so the Base pin, and the Base-only pack hash, are unchanged).
    assert round_anchor_ts_for_chain(BASE, _EPOCH, _EPOCH_SECONDS) == round_anchor_ts(_EPOCH, _EPOCH_SECONDS)
    # Ethereum anchors 3 epochs back instead of 1.
    assert round_anchor_ts_for_chain(ETH, _EPOCH, _EPOCH_SECONDS) == (_EPOCH - 3) * _EPOCH_SECONDS


def test_shared_anchor_defers_ethereum_the_pre_fix_freeze():
    # Reproduces the incident: with ONE shared anchor (1 epoch back), Ethereum's
    # 12-confirmation tip is still BEFORE the anchor → the whole round defers.
    _, head_of, ts_of = _fleet_chains()
    anchor = round_anchor_ts(_EPOCH, _EPOCH_SECONDS)  # 5940
    with pytest.raises(ForkPinUnavailable) as ei:
        derive_fork_pins(anchor, [BASE, ETH], head_of=head_of,
                         block_timestamp_of=ts_of, confirmations=12)
    assert "chain 1" in str(ei.value)


def test_per_chain_anchor_pins_both_chains_the_fix():
    # With the per-chain anchor, Ethereum anchors deep enough to bracket → both pin.
    _, head_of, ts_of = _fleet_chains()
    anchor = round_anchor_ts(_EPOCH, _EPOCH_SECONDS)
    pins = derive_fork_pins(
        anchor, [BASE, ETH], head_of=head_of, block_timestamp_of=ts_of,
        confirmations=12,
        anchor_ts_of=lambda c: round_anchor_ts_for_chain(c, _EPOCH, _EPOCH_SECONDS),
    )
    assert set(pins) == {BASE, ETH}
    # Deterministic exact blocks: highest b with ts(b) <= that chain's anchor.
    assert pins[BASE] == 2970   # 2*2970 = 5940 (anchor 5940)
    assert pins[ETH] == 485     # 12*485 = 5820 (anchor 5820)


def test_base_pin_unchanged_by_the_fix_pack_hash_stability():
    # The Base pin must be identical whether derived alone (scalar, old path) or
    # under the per-chain map — otherwise the default Base-only pack hash would
    # shift and force a needless fleet re-sync.
    _, head_of, ts_of = _fleet_chains()
    anchor = round_anchor_ts(_EPOCH, _EPOCH_SECONDS)
    base_only = derive_fork_pins(anchor, [BASE], head_of=head_of,
                                 block_timestamp_of=ts_of, confirmations=12)
    per_chain = derive_fork_pins(
        anchor, [BASE, ETH], head_of=head_of, block_timestamp_of=ts_of, confirmations=12,
        anchor_ts_of=lambda c: round_anchor_ts_for_chain(c, _EPOCH, _EPOCH_SECONDS),
    )
    assert base_only[BASE] == per_chain[BASE]


def test_no_anchor_ts_of_is_byte_identical_scalar_backcompat():
    # Omitting anchor_ts_of → every chain uses the scalar anchor (pre-#632).
    _, head_of, ts_of = _fleet_chains()
    anchor = round_anchor_ts(_EPOCH, _EPOCH_SECONDS)
    a = derive_fork_pins(anchor, [BASE], head_of=head_of, block_timestamp_of=ts_of, confirmations=12)
    b = derive_fork_pins(anchor, [BASE], head_of=head_of, block_timestamp_of=ts_of,
                         confirmations=12, anchor_ts_of=None)
    assert a == b
