"""Unit tests for relayer ABI encoder helpers."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from eth_hash.auto import keccak

from minotaur_subnet.relayer.encoder import encode_intent_order


def test_encode_intent_order_freezes_current_tuple_shape():
    order = SimpleNamespace(
        order_id="ord_test_123",
        submitted_by="0x" + "aa" * 20,
        chain_id=1,
        deadline=1234567890,
        perpetual=False,
        max_executions=1,
        cooldown=0,
        params={
            "app_address": "0x" + "bb" * 20,
            "intent_selector": "d5bcb9b5",
            "intent_params_hex": "00ff",
            "user_nonce": 7,
        },
    )

    encoded = encode_intent_order(order)

    assert len(encoded) == 11
    assert encoded[0] == keccak(b"ord_test_123")
    assert encoded[1].lower() == "0x" + "bb" * 20
    assert encoded[2] == bytes.fromhex("d5bcb9b5")
    assert encoded[3] == bytes.fromhex("00ff")
    assert encoded[4].lower() == "0x" + "aa" * 20
    assert encoded[5] == 1
    assert encoded[6] == 1234567890
    assert encoded[7] == 7
    assert encoded[8] is False
    assert encoded[9] == 1
    assert encoded[10] == 0
