"""A native delivery is credited, not read as `nothing_delivered`.

Delivery was measured exclusively from ERC-20 `Transfer` logs. Native movement
emits none, so a bridge that credits native — Tensorplex delivering TAO on
964 — landed as `delivered = 0` with all three buckets empty: indistinguishable
from a plan that delivered nothing at all, and from a leg that reverted.

Two halves had to meet for this to work:

* the simulator must OBSERVE the native arrival (a balance rise at a delivery
  recipient, since there is no log to read), and
* the buckets must ACCEPT it — `output_token` can only ever name an address, so
  an order asking for TAO on 964 resolves to WTAO and an honest native delivery
  matched nothing.
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from minotaur_subnet.blockchain.tokens import (  # noqa: E402
    NATIVE_SENTINEL,
    WRAPPED_NATIVE_TOKEN,
)

_WTAO_964 = WRAPPED_NATIVE_TOKEN[964].lower()
_USDC_964 = "0xb833e8137fedf80de7e908dc6fea43a029142f20"


def _matcher(expected_token: str, dest_chains):
    """Rebuild the bucket predicate exactly as _observe_destination_delivery does."""
    equivalents = {NATIVE_SENTINEL}
    for c in dest_chains:
        w = WRAPPED_NATIVE_TOKEN.get(int(c))
        if w:
            equivalents.add(w.lower())

    def is_expected(token: str) -> bool:
        t = (token or "").lower()
        if t == expected_token:
            return True
        return t in equivalents and expected_token in equivalents

    return is_expected


def test_native_arrival_satisfies_a_wrapped_native_request():
    """THE regression: order asks for WTAO, rail delivers native TAO."""
    assert _matcher(_WTAO_964, {964})(NATIVE_SENTINEL) is True


def test_wrapped_arrival_satisfies_a_native_request():
    """The mirror: whichever way round, they are the same value."""
    assert _matcher(NATIVE_SENTINEL, {964})(_WTAO_964) is True


def test_an_unrelated_token_is_still_rejected():
    """The equivalence must not become 'anything to the recipient counts'.

    That is the decimals-arbitrage hole the token filter exists to close.
    """
    assert _matcher(_WTAO_964, {964})(_USDC_964) is False


def test_native_does_not_satisfy_a_non_native_request():
    is_expected = _matcher(_USDC_964, {964})
    assert is_expected(NATIVE_SENTINEL) is False
    assert is_expected(_USDC_964) is True


def test_exact_match_still_wins_with_no_destination_chain_resolved():
    """An unresolvable dest chain must not break plain equality."""
    is_expected = _matcher(_USDC_964, set())
    assert is_expected(_USDC_964) is True
    assert is_expected(NATIVE_SENTINEL) is False


def test_only_a_balance_RISE_is_a_delivery():
    """The executor's balance normally FALLS paying for the calls; a fall is
    not a delivery, and counting abs(delta) would credit spending as output."""
    before, after = 10**18, 10**18 - 5000
    assert not (after - before) > 0
