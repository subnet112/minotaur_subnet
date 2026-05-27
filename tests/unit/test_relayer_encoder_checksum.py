"""Regression test for the address-checksum normalization in encode_intent_order.

Live failure 2026-05-27 20:04 UTC: order ord_5bb76708b265476f reached
consensus cleanly (4 of 4 sigs), the PR #102 _LightOrder fix worked,
then the relayer crashed at the first web3 contract call with:

  Relayer submission failed: Failed after 2 attempts: ('web3.py only
  accepts checksum addresses. ... Or, if you must accept lower safety,
  use Web3.to_checksum_address(lower_case_address).',
  '0x0aea6ab70b384adc6493d40e927ce53a7cefe035')

The api was storing ``app_address`` lowercase in ``order.params``,
the encoder passed it through verbatim, and web3.py's strict
checksum check rejected the contract address on the very first call.

Fix: normalize at the encoder boundary so downstream call sites can
keep using the value as a plain string. Both ``app_address`` and
``submitted_by`` go through ``_safe_checksum``.

This file pins:
  - Lowercase app_address gets checksummed (the exact live failure)
  - Already-checksummed addresses round-trip unchanged
  - Empty / missing addresses fall back to zero-address default
  - Submitted_by gets the same treatment
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from minotaur_subnet.relayer.encoder import _safe_checksum, encode_intent_order


# ── Unit-level: _safe_checksum ─────────────────────────────────────────


def test_lowercase_address_gets_checksummed():
    """The exact live failure case: api stored 0x0aea6ab70b384adc...
    (all lowercase). Pre-fix this was passed through to web3.py which
    rejected with a strict-checksum error. Post-fix it gets normalized
    to the proper EIP-55 mixed-case form."""
    lower = "0x0aea6ab70b384adc6493d40e927ce53a7cefe035"
    checksummed = _safe_checksum(lower)
    # EIP-55 checksum is deterministic
    assert checksummed == "0x0AeA6Ab70B384ADC6493d40e927ce53A7cefE035"
    assert checksummed != lower
    # Verify it actually checksums correctly — the case pattern matches
    # what eth_utils.to_checksum_address produces.
    from eth_utils import to_checksum_address
    assert checksummed == to_checksum_address(lower)


def test_already_checksummed_address_is_idempotent():
    """A correctly-checksummed address should round-trip unchanged."""
    checksummed = "0x0AeA6Ab70B384ADC6493d40e927ce53A7cefE035"
    assert _safe_checksum(checksummed) == checksummed


def test_uppercase_address_gets_normalized():
    """An all-uppercase hex address should normalize to proper checksum."""
    upper = "0X0AEA6AB70B384ADC6493D40E927CE53A7CEFE035"
    result = _safe_checksum(upper)
    assert result == "0x0AeA6Ab70B384ADC6493d40e927ce53A7cefE035"


def test_empty_string_returns_default_zero_address():
    """Empty/falsy input shouldn't crash — return the zero-address
    default (matches the upstream .get(..., default) shape)."""
    assert _safe_checksum("") == "0x" + "00" * 20
    assert _safe_checksum(None) == "0x" + "00" * 20


def test_custom_default_used_for_empty_input():
    """Caller can override the default for empty input — useful for
    fields that should fail loudly elsewhere rather than silently
    becoming the zero address."""
    custom = "0xCAFE000000000000000000000000000000000001"
    assert _safe_checksum("", default=custom) == custom


def test_malformed_hex_passes_through_unchanged():
    """Garbage input (not valid hex, wrong length, etc.) should NOT be
    swallowed — let the downstream web3 call raise with a more
    specific error than we'd generate."""
    bad = "0xnothex"
    assert _safe_checksum(bad) == bad

    too_short = "0x1234"
    assert _safe_checksum(too_short) == too_short


# ── Integration: encode_intent_order normalizes addresses ──────────────


class _FakeOrder:
    """Minimal order shape encode_intent_order reads off."""

    def __init__(
        self, *, app_address: str, submitted_by: str,
        order_id: str = "ord_test",
        intent_selector: str = "d5bcb9b5",
        chain_id: int = 8453,
        deadline: int = 1779897596,
        perpetual: bool = False,
        max_executions: int = 1,
        cooldown: float = 0.0,
    ):
        self.order_id = order_id
        self.params = {
            "app_address": app_address,
            "intent_selector": intent_selector,
            "intent_params_hex": "",
        }
        self.submitted_by = submitted_by
        self.chain_id = chain_id
        self.deadline = deadline
        self.perpetual = perpetual
        self.max_executions = max_executions
        self.cooldown = cooldown


def test_encode_intent_order_normalizes_app_address():
    """End-to-end: lowercase app_address from order.params lands as
    checksummed in the encoded tuple. This is the bug from prod."""
    order = _FakeOrder(
        app_address="0x0aea6ab70b384adc6493d40e927ce53a7cefe035",
        submitted_by="0xB763F651776690F7b142e5D40A7C096Aa963f04e",
    )
    encoded = encode_intent_order(order)
    # Tuple shape: (orderId, app, intentSelector, intentParams,
    #               submittedBy, chainId, deadline, nonce, perpetual,
    #               maxExecutions, cooldown)
    app = encoded[1]
    assert app == "0x0AeA6Ab70B384ADC6493d40e927ce53A7cefE035"


def test_encode_intent_order_normalizes_submitted_by():
    """submitted_by also gets the checksum treatment. Pre-fix this
    field would also crash web3 if it arrived lowercase."""
    order = _FakeOrder(
        app_address="0x0AeA6Ab70B384ADC6493d40e927ce53A7cefE035",
        submitted_by="0xb763f651776690f7b142e5d40a7c096aa963f04e",  # lowercase
    )
    encoded = encode_intent_order(order)
    submitted_by = encoded[4]
    assert submitted_by == "0xB763F651776690F7b142e5D40A7C096Aa963f04e"


def test_encode_intent_order_handles_missing_app_address():
    """An order without app_address gets the zero-address default
    (preserves pre-fix behavior — encode_intent_order has always had
    this default; we just made sure checksum normalization works on
    the default too)."""
    order = _FakeOrder(
        app_address="",  # falsy
        submitted_by="0xB763F651776690F7b142e5D40A7C096Aa963f04e",
    )
    encoded = encode_intent_order(order)
    app = encoded[1]
    # Zero address is already in proper checksum form.
    assert app == "0x" + "00" * 20
