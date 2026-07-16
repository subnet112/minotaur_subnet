"""Unit tests for GET /orders/{id}/signing-payload and build_order_signing_payload.

The signing payload is the missing sibling of /prepare-permit: it hands the client
the exact EIP-712 typed data + digest to sign for an order (order_id is minted
server-side, so this must come AFTER submit). Crucially it carries the perpetual
terms and the uint256-max sentinel nonce, so a client can't sign the wrong nonce.
"""

import time

import pytest
from eth_account import Account
from eth_hash.auto import keccak

from minotaur_subnet.api.routes import orders as orders_mod
from minotaur_subnet.api.routes._signature_verify import (
    build_order_signing_payload,
    verify_user_order_signature,
)
from minotaur_subnet.consensus.eip712 import build_domain_separator, sign_user_order
from minotaur_subnet.orderbook.orderbook import IntentOrderBook
from minotaur_subnet.relayer.encoder import _SENTINEL_NONCE

OWNER_KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
OWNER = Account.from_key(OWNER_KEY).address
APP = "0x0aea6ab70b384adc6493d40e927ce53a7cefe035"
SEL = "d5bcb9b5"
PARAMS_HEX = "deadbeef" * 24


def _perp_order(ob):
    return ob.submit(
        app_id="app_x", intent_function="swap", submitted_by=OWNER,
        params={"app_address": APP, "intent_params_hex": PARAMS_HEX, "intent_selector": SEL},
        chain_id=8453, deadline=int(time.time()) + 7 * 24 * 3600,
        perpetual=True, max_executions=100, cooldown=3600,
    )


def test_payload_carries_perpetual_terms_and_sentinel_nonce():
    order = _perp_order(IntentOrderBook())
    p = build_order_signing_payload(order)
    msg = p["message"]
    assert msg["perpetual"] is True
    assert msg["maxExecutions"] == "100"
    assert msg["cooldown"] == "3600"
    # No user_nonce pinned → the payload tells the client to sign the sentinel.
    assert msg["nonce"] == str(_SENTINEL_NONCE)
    assert p["primaryType"] == "IntentOrder"
    assert p["domain"]["verifyingContract"] == APP
    assert p["digest"].startswith("0x") and len(p["digest"]) == 66


def test_payload_digest_roundtrips_with_server_verifier():
    # A signature over exactly what the payload advertises (sentinel nonce +
    # perpetual terms) must pass the server verifier — proving the client can't
    # be misled into an invalid signature.
    order = _perp_order(IntentOrderBook())
    sig = sign_user_order(
        OWNER_KEY, keccak(order.order_id.encode()), APP, bytes.fromhex(SEL),
        bytes.fromhex(PARAMS_HEX), order.submitted_by, order.chain_id,
        int(order.deadline), _SENTINEL_NONCE, order.perpetual,
        order.max_executions, int(order.cooldown),
        build_domain_separator(order.chain_id, APP),
    )
    assert verify_user_order_signature(order, "0x" + sig.hex()) is True


def test_route_returns_payload(monkeypatch):
    ob = IntentOrderBook()
    order = _perp_order(ob)
    monkeypatch.setattr(orders_mod, "_orderbook", ob)
    out = orders_mod.get_order_signing_payload(order.order_id)
    assert out["order_id"] == order.order_id
    assert out["perpetual"] is True
    assert out["message"]["nonce"] == str(_SENTINEL_NONCE)
    assert out["digest"].startswith("0x")


def test_route_404_when_missing(monkeypatch):
    monkeypatch.setattr(orders_mod, "_orderbook", IntentOrderBook())
    with pytest.raises(orders_mod.HTTPException) as ei:
        orders_mod.get_order_signing_payload("ord_missing")
    assert ei.value.status_code == 404


def test_route_409_when_app_address_unresolved(monkeypatch):
    ob = IntentOrderBook()
    order = ob.submit(app_id="a", intent_function="swap", submitted_by=OWNER,
                      params={}, chain_id=8453, deadline=int(time.time()) + 3600)
    monkeypatch.setattr(orders_mod, "_orderbook", ob)
    with pytest.raises(orders_mod.HTTPException) as ei:
        orders_mod.get_order_signing_payload(order.order_id)
    assert ei.value.status_code == 409
