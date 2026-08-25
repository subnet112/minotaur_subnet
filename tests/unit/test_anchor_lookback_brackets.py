"""Every chain's anchor lookback must actually confirm-bracket that chain.

`find_pin_block` refuses to pin unless a CONFIRMED block strictly after the
anchor exists. The confirmed tip trails head by `confirmations * block_time`
seconds; the anchor trails "now" by `lookback_epochs * epoch_seconds`. If the
first is larger, the bracket can NEVER be satisfied and the chain defers every
epoch, forever — silently, because a defer is a normal log line.

Chain 964 shipped with lookback_epochs=1 against ~12s blocks and 12
confirmations: 144s of lag against a 60s anchor. Measured on the leader
2026-08-25, it had deferred every epoch since boot and its benchmark fork was
9,562 blocks (~32h) stale, so anything deployed after the sidecar started was
invisible to scoring — and each validator's fork sat at ITS OWN boot block,
which is a determinism hazard, not just a staleness one.
"""
from __future__ import annotations

import pytest

from minotaur_subnet.chains import registry
from minotaur_subnet.consensus.round_anchor import (
    ForkPinUnavailable,
    find_pin_block,
    round_anchor_lookback_epochs,
)

EPOCH_SECONDS = 60
CONFIRMATIONS = 12

# Nominal block time per chain, in seconds. The lookback has to cover
# CONFIRMATIONS blocks of it.
BLOCK_SECONDS = {1: 12, 8453: 2, 964: 12}


@pytest.mark.parametrize("chain_id", sorted(BLOCK_SECONDS))
def test_lookback_covers_the_confirmation_lag(chain_id):
    lag = CONFIRMATIONS * BLOCK_SECONDS[chain_id]
    anchor_back = round_anchor_lookback_epochs(chain_id) * EPOCH_SECONDS
    assert anchor_back > lag, (
        f"chain {chain_id}: anchor is {anchor_back}s back but the confirmed tip "
        f"trails {lag}s — find_pin_block can never bracket it, so this chain "
        f"defers every epoch and its fork never re-pins"
    )


@pytest.mark.parametrize("chain_id", sorted(BLOCK_SECONDS))
def test_a_realistic_chain_actually_pins(chain_id):
    """Drive find_pin_block with a synthetic chain of the right shape."""
    bt = BLOCK_SECONDS[chain_id]
    head, now = 1_000_000, 1_800_000_000
    ts = lambda b: now - (head - b) * bt          # noqa: E731
    anchor = now - round_anchor_lookback_epochs(chain_id) * EPOCH_SECONDS
    pin = find_pin_block(anchor, head=head, block_timestamp=ts,
                         confirmations=CONFIRMATIONS)
    assert ts(pin) <= anchor
    assert pin <= head - CONFIRMATIONS


def test_964_specifically_used_to_defer_and_now_does_not():
    """The regression, stated as the arithmetic that caused it."""
    bt, head, now = 12, 1_000_000, 1_800_000_000
    ts = lambda b: now - (head - b) * bt          # noqa: E731

    # What shipped: a 1-epoch anchor against 144s of confirmation lag.
    with pytest.raises(ForkPinUnavailable, match="not yet confirmed-bracketed"):
        find_pin_block(now - 1 * EPOCH_SECONDS, head=head, block_timestamp=ts,
                       confirmations=CONFIRMATIONS)

    # 2 epochs (120s) still does not clear 144s — the near miss is why this
    # needs a test and not a nudge.
    with pytest.raises(ForkPinUnavailable, match="not yet confirmed-bracketed"):
        find_pin_block(now - 2 * EPOCH_SECONDS, head=head, block_timestamp=ts,
                       confirmations=CONFIRMATIONS)

    assert round_anchor_lookback_epochs(964) >= 3
    find_pin_block(now - round_anchor_lookback_epochs(964) * EPOCH_SECONDS,
                   head=head, block_timestamp=ts, confirmations=CONFIRMATIONS)


def test_964_matches_ethereum_because_the_profile_matches():
    """Same block time and same confirmations must mean the same lookback."""
    assert BLOCK_SECONDS[964] == BLOCK_SECONDS[1]
    assert round_anchor_lookback_epochs(964) == round_anchor_lookback_epochs(1)


def test_the_value_is_a_code_constant_not_env():
    """lookback_epochs folds into the pack hash, so it must be fleet-uniform."""
    assert registry.spec(964).lookback_epochs == 3
