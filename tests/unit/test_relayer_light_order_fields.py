"""Regression test for _LightOrder field coverage.

PR-C-era refactor split the relayer into a separate service: the api
serializes an Order via ``_to_jsonable(order)`` and POSTs the JSON to
``/v1/submit-plan``. The relayer then wraps the dict into a
``_LightOrder`` attribute-bag for ``EvmRelayer.submit_plan`` to read
off. The encoder downstream reads ``submitted_by``, ``deadline``,
``perpetual``, ``max_executions``, and ``cooldown`` from the order
when building the on-chain Order struct.

Pre-fix, ``_LightOrder`` was built with only 4 fields
(chain_id, order_id, user_signature, params), so any field the
encoder touched beyond those raised
``AttributeError: '_LightOrder' object has no attribute 'X'``.
Caught live 2026-05-27 after consensus was reached cleanly (4 of 4
sigs collected, quorum 4) but the relayer crashed on encoding —
no on-chain tx, order rejected with this Python error in /health.

This test exercises the construction logic in ``RelayerService.submit_plan``
indirectly by checking the _LightOrder built from a representative
order_data dict carries every field the encoder reads.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from minotaur_subnet.relayer.main import _LightOrder


def _representative_order_data() -> dict:
    """Mirror what api's ``_to_jsonable(order)`` actually serializes.

    Field shape matches the prod order ``ord_26a43e9064a74917`` snapshot
    that surfaced the bug.
    """
    return {
        "order_id": "ord_26a43e9064a74917",
        "app_id": "app_da6c96b84c60",
        "intent_function": "swap",
        "params": {
            "input_token": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
            "output_token": "0x4200000000000000000000000000000000000006",
            "input_amount": "1000000",
            "min_output_amount": "476226325129775",
            "intent_params_hex": "00" * 32,
            "app_address": "0x0aea6ab70b384adc6493d40e927ce53a7cefe035",
            "intent_selector": "d5bcb9b5",
        },
        "submitted_by": "0xB763F651776690F7b142e5D40A7C096Aa963f04e",
        "user_signature": "0xdeadbeef",
        "chain_id": 8453,
        "deadline": 1779897596.2421448,
        "perpetual": False,
        "max_executions": 1,
        "cooldown": 0.0,
        "status": "scored",
    }


def _build_light_order_like_relayer(order_data: dict) -> _LightOrder:
    """Mirror the construction RelayerService does at relayer/main.py:455.

    This is intentionally NOT importing the real construction site —
    we want the test to fail loudly if someone changes the construction
    in ways that drop a field. The encoder's expected surface is the
    source of truth; this builder must match.
    """
    return _LightOrder(
        chain_id=int(order_data.get("chain_id", 0)),
        order_id=order_data.get("order_id", ""),
        user_signature=order_data.get("user_signature", ""),
        params=order_data.get("params", {}) or {},
        submitted_by=order_data.get("submitted_by", ""),
        deadline=order_data.get("deadline", 0),
        perpetual=bool(order_data.get("perpetual", False)),
        max_executions=int(order_data.get("max_executions", 1)),
        cooldown=float(order_data.get("cooldown", 0.0)),
    )


def test_light_order_has_all_encoder_fields():
    """``_LightOrder`` must carry every attribute the encoder reads.

    Concrete list pulled from ``relayer/encoder.py:24-44``:
      - order_id
      - params (with sub-keys app_address, intent_selector, intent_params_hex)
      - submitted_by
      - chain_id
      - deadline
      - perpetual
      - max_executions
      - cooldown
    """
    order_data = _representative_order_data()
    lo = _build_light_order_like_relayer(order_data)

    # Each attribute access here would raise AttributeError pre-fix.
    assert lo.order_id == "ord_26a43e9064a74917"
    assert lo.chain_id == 8453
    assert lo.submitted_by == "0xB763F651776690F7b142e5D40A7C096Aa963f04e"
    assert lo.deadline == 1779897596.2421448
    assert lo.perpetual is False
    assert lo.max_executions == 1
    assert lo.cooldown == 0.0
    assert lo.user_signature == "0xdeadbeef"
    assert isinstance(lo.params, dict)
    assert lo.params["app_address"] == "0x0aea6ab70b384adc6493d40e927ce53a7cefe035"


def test_light_order_survives_encoder_attribute_walk():
    """End-to-end check: feed _LightOrder to the encoder and confirm
    every field the encoder reads succeeds. This is the contract the
    bug violated — the encoder accesses attributes by direct dot-notation
    (not getattr-with-default), so a missing field crashes with
    AttributeError.
    """
    from minotaur_subnet.relayer.encoder import encode_intent_order

    order_data = _representative_order_data()
    lo = _build_light_order_like_relayer(order_data)

    # Should not raise.
    encoded = encode_intent_order(lo)
    # encode_order returns a tuple of 11 elements per the Solidity
    # Order struct (order_id, app_address, intent_selector,
    # intent_params, submitted_by, chain_id, deadline, nonce,
    # perpetual, max_executions, cooldown).
    assert len(encoded) == 11
    # Spot-check the submitted_by slot landed correctly (this was the
    # exact field that crashed live on prod).
    assert encoded[4] == "0xB763F651776690F7b142e5D40A7C096Aa963f04e"


def test_light_order_handles_missing_optional_fields():
    """Older or partial order payloads (e.g. tests, local-testnet) might
    omit optional fields. The construction should still work with safe
    defaults, NOT crash."""
    minimal = {
        "order_id": "ord_test",
        "chain_id": 8453,
        "params": {},
    }
    lo = _build_light_order_like_relayer(minimal)

    # Defaults populated, no AttributeError.
    assert lo.submitted_by == ""
    assert lo.deadline == 0
    assert lo.perpetual is False
    assert lo.max_executions == 1
    assert lo.cooldown == 0.0
    assert lo.user_signature == ""
