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

from minotaur_subnet.relayer.encoder import (
    _safe_checksum,
    encode_execution_plan,
    encode_intent_order,
    hash_execution_plan,
)
from minotaur_subnet.shared.types import ExecutionPlan, Interaction


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


def test_mixed_case_wrong_checksum_is_repaired():
    """The 2026-07 live failure: a solver plan target arrives mixed-case with
    an INVALID EIP-55 checksum (its 20 bytes are valid — only the casing is
    wrong). Strict web3.py ``to_checksum_address`` RAISES on this rather than
    normalizing, so the pre-fix helper (which checksummed the string as-is,
    caught the raise, and returned it UNCHANGED) left the bad address in place
    and the relayer submit still failed at web3's checksum validation.

    Post-fix ``_safe_checksum`` lowercases first, so the canonical EIP-55 form
    comes back regardless of the web3 version's leniency.
    """
    from eth_utils import to_checksum_address

    # One nibble off the canonical checksum (…5E9bc… vs …5E9bC…) — same bytes.
    bad = "0x1601843c5E9bc251A3272907010AFa41Fa18347E"
    canonical = "0x1601843c5E9bC251A3272907010AFa41Fa18347E"
    assert canonical == to_checksum_address(bad.lower())  # sanity: same address
    assert bad != canonical                               # sanity: casing differs

    result = _safe_checksum(bad)
    assert result == canonical, (
        "mixed-case wrong-checksum address must be repaired to canonical EIP-55, "
        f"got {result!r}"
    )


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


# ── Plan targets: the actual live crash site ───────────────────────────

# A solver plan target with a valid 20 bytes but a WRONG EIP-55 checksum —
# the exact shape that failed 91 live submits on chain 8453.
_BAD_TARGET = "0x1601843c5E9bc251A3272907010AFa41Fa18347E"
_CANONICAL_TARGET = "0x1601843c5E9bC251A3272907010AFa41Fa18347E"


def _plan_with_target(target: str) -> ExecutionPlan:
    return ExecutionPlan(
        intent_id="intent_test",
        interactions=[Interaction(target=target, value="0", call_data="0x")],
        deadline=1779897596,
        nonce=0,
        metadata={},
    )


def test_encode_execution_plan_normalizes_mis_cased_target():
    """The plan target — the router/pool address from the miner's solver — is
    what web3 chokes on inside executeIntent. It must land canonical in the
    encoded call tuple, not verbatim."""
    plan = _plan_with_target(_BAD_TARGET)
    calls, _deadline, _nonce, _metadata = encode_execution_plan(plan)
    encoded_target = calls[0][0]
    assert encoded_target == _CANONICAL_TARGET


def test_hash_execution_plan_is_casing_invariant():
    """Normalizing the target must NOT change the signed plan hash: the address
    encodes to the same 20 bytes regardless of casing, so a bad-cased target
    and its canonical form hash identically. This is what makes the fix safe to
    apply on the consensus-critical hash path."""
    assert (
        hash_execution_plan(_plan_with_target(_BAD_TARGET))
        == hash_execution_plan(_plan_with_target(_CANONICAL_TARGET))
    )
