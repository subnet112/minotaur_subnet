"""Regression tests for the single-prompt ``PATCH /orders/{id}/signature``.

Before 2026-05-26: the audit M4 fix required a SECOND signature on this
route — an EIP-191 ``owner_signature`` over an ``AttachSig`` action —
because the server was skipping EIP-712 verification of the
``user_signature`` itself server-side. Frontends therefore prompted the
user twice in MetaMask: once for the EIP-712 IntentOrder, once for the
EIP-191 AttachSig action.

After: the EIP-712 ``user_signature`` is verified server-side at attach
time by ECDSA-recovering the signer and comparing to ``submitted_by``.
That closes the same gap the EIP-191 closed (garbage bytes don't
recover; sigs for a different order have a different orderId/paramsHash
in the typehash and recover to a different signer). No second wallet
prompt needed.

The legacy ``owner_signature`` body field stays accepted-but-ignored for
one-version backward compat with frontends that haven't yet shipped the
single-prompt change.
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from eth_account import Account
from eth_hash.auto import keccak
from fastapi import HTTPException

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from minotaur_subnet.api.routes import orders as orders_mod
from minotaur_subnet.consensus.eip712 import (
    build_domain_separator,
    sign_user_order,
)
from minotaur_subnet.orderbook.orderbook import IntentOrderBook


# Deterministic Anvil key — no real secrets.
OWNER_KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
OWNER_ADDR = Account.from_key(OWNER_KEY).address

ATTACKER_KEY = "0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d"
ATTACKER_ADDR = Account.from_key(ATTACKER_KEY).address

APP_ADDR = "0x0aea6ab70b384adc6493d40e927ce53a7cefe035"  # any 20-byte address
CHAIN_ID = 8453
INTENT_SELECTOR = bytes.fromhex("d5bcb9b5")  # arbitrary 4-byte selector
INTENT_PARAMS_HEX = "deadbeef" * 24  # 96-byte stand-in for ABI-encoded swap params


@pytest.fixture
def fresh_orderbook():
    """Clean IntentOrderBook with one signed-pending order, registered
    on the orders module so the route under test can find it."""
    ob = IntentOrderBook()
    order = ob.submit(
        app_id="app_test",
        intent_function="swap",
        submitted_by=OWNER_ADDR,
        params={
            "app_address": APP_ADDR,
            "intent_params_hex": INTENT_PARAMS_HEX,
            "intent_selector": INTENT_SELECTOR.hex(),
            "user_nonce": 0,
        },
        chain_id=CHAIN_ID,
        deadline=int(time.time()) + 3600,
    )
    # Wire into the module singleton the route reads from.
    prev_ob = orders_mod._orderbook
    prev_store = orders_mod._app_store
    orders_mod._orderbook = ob
    orders_mod._app_store = None  # don't persist in tests
    yield ob, order
    orders_mod._orderbook = prev_ob
    orders_mod._app_store = prev_store


def _sign_user_order_for(order, *, signer_key: str, intent_params_hex: str | None = None) -> str:
    """Build an EIP-712 user_signature that matches what the server will
    reconstruct in ``verify_user_order_signature``."""
    params_hex = intent_params_hex if intent_params_hex is not None else order.params["intent_params_hex"]
    intent_params = bytes.fromhex(params_hex.replace("0x", ""))
    order_id_bytes = keccak(order.order_id.encode())
    domain_sep = build_domain_separator(order.chain_id, APP_ADDR)
    sig = sign_user_order(
        signer_key,
        order_id_bytes,
        APP_ADDR,
        INTENT_SELECTOR,
        intent_params,
        order.submitted_by,
        order.chain_id,
        int(order.deadline),
        int(order.params.get("user_nonce", 0)),
        order.perpetual,
        order.max_executions,
        int(order.cooldown),
        domain_sep,
    )
    return "0x" + sig.hex()


def _mock_request(body: dict):
    req = MagicMock()
    req.json = AsyncMock(return_value=body)
    return req


def _call_attach(order_id: str, body: dict):
    return asyncio.run(orders_mod.attach_signature(order_id, _mock_request(body)))


# ── happy path: EIP-712 sig alone (no EIP-191) ────────────────────────────


def test_attach_accepts_valid_eip712_with_no_owner_signature(fresh_orderbook):
    """Single-prompt flow: frontend sends just ``user_signature``. Server
    ECDSA-recovers from the IntentOrder typehash and confirms it equals
    submitted_by. Accept."""
    ob, order = fresh_orderbook
    sig = _sign_user_order_for(order, signer_key=OWNER_KEY)

    result = _call_attach(order.order_id, {"user_signature": sig})
    assert result == {"order_id": order.order_id, "signature_attached": True}

    # And the sig is actually stored on the order.
    refetched = ob.get(order.order_id)
    assert refetched.user_signature == sig


# ── sentinel-nonce default (real-frontend shape) ──────────────────────────
#
# Frontends in the own-wallet flow don't carry ``user_nonce`` in
# ``order.params`` — only the managed-wallet code path fills it in (see
# routes/orders.py:_fetch_user_nonce). The relayer encodes the missing
# nonce as the uint256-max sentinel (relayer/encoder.py:_resolve_nonce),
# and the on-chain EIP712Verifier reads that sentinel out of calldata.
# The server-side EIP-712 verifier MUST resolve the same way; otherwise
# a frontend that correctly signs over ``nonce = SENTINEL`` recovers to
# a different signer here and every swap fails at PATCH /signature
# even though the contract would accept the exact same bytes.

_SENTINEL_NONCE = 2**256 - 1


@pytest.fixture
def fresh_orderbook_no_user_nonce():
    """Order WITHOUT a ``user_nonce`` key in params — mirrors the shape
    a real frontend POSTs in the own-wallet flow."""
    ob = IntentOrderBook()
    order = ob.submit(
        app_id="app_test",
        intent_function="swap",
        submitted_by=OWNER_ADDR,
        params={
            "app_address": APP_ADDR,
            "intent_params_hex": INTENT_PARAMS_HEX,
            "intent_selector": INTENT_SELECTOR.hex(),
            # user_nonce deliberately absent
        },
        chain_id=CHAIN_ID,
        deadline=int(time.time()) + 3600,
    )
    prev_ob = orders_mod._orderbook
    prev_store = orders_mod._app_store
    orders_mod._orderbook = ob
    orders_mod._app_store = None
    yield ob, order
    orders_mod._orderbook = prev_ob
    orders_mod._app_store = prev_store


def _sign_with_explicit_nonce(order, signer_key: str, *, nonce: int) -> str:
    """Like _sign_user_order_for but with an explicit nonce, so tests can
    sign over the SENTINEL value (or any other value) regardless of
    what's stored on the order."""
    intent_params = bytes.fromhex(order.params["intent_params_hex"].replace("0x", ""))
    order_id_bytes = keccak(order.order_id.encode())
    domain_sep = build_domain_separator(order.chain_id, APP_ADDR)
    sig = sign_user_order(
        signer_key,
        order_id_bytes,
        APP_ADDR,
        INTENT_SELECTOR,
        intent_params,
        order.submitted_by,
        order.chain_id,
        int(order.deadline),
        nonce,
        order.perpetual,
        order.max_executions,
        int(order.cooldown),
        domain_sep,
    )
    return "0x" + sig.hex()


def test_attach_accepts_sentinel_nonce_when_params_omit_user_nonce(fresh_orderbook_no_user_nonce):
    """Real-frontend shape: no ``user_nonce`` in params, sig built over
    ``nonce = 2**256 - 1``. The verifier must default to the same
    sentinel and accept."""
    ob, order = fresh_orderbook_no_user_nonce
    sig = _sign_with_explicit_nonce(order, OWNER_KEY, nonce=_SENTINEL_NONCE)

    result = _call_attach(order.order_id, {"user_signature": sig})
    assert result == {"order_id": order.order_id, "signature_attached": True}

    refetched = ob.get(order.order_id)
    assert refetched.user_signature == sig


def test_attach_rejects_nonce_zero_sig_when_params_omit_user_nonce(fresh_orderbook_no_user_nonce):
    """Regression guard. Pre-fix the verifier defaulted missing
    ``user_nonce`` to 0, so a sig signed over ``nonce = 0`` would
    accidentally pass — even though the on-chain calldata carries
    ``nonce = SENTINEL`` and would recover a different signer. With the
    correct default a ``nonce = 0`` sig must NOT recover to
    submitted_by, and the route must 403."""
    _, order = fresh_orderbook_no_user_nonce
    sig = _sign_with_explicit_nonce(order, OWNER_KEY, nonce=0)

    with pytest.raises(HTTPException) as exc:
        _call_attach(order.order_id, {"user_signature": sig})
    assert exc.value.status_code == 403
    assert "does not recover" in exc.value.detail


# ── attack surfaces the EIP-191 used to cover ─────────────────────────────


def test_attach_rejects_random_garbage_bytes(fresh_orderbook):
    """Pre-fix: server stored whatever bytes you sent. Post-fix: garbage
    that doesn't ECDSA-recover to submitted_by is rejected 403."""
    _, order = fresh_orderbook
    garbage = "0x" + "ab" * 65

    with pytest.raises(HTTPException) as exc:
        _call_attach(order.order_id, {"user_signature": garbage})
    assert exc.value.status_code == 403
    assert "does not recover" in exc.value.detail


def test_attach_rejects_sig_from_attacker_key(fresh_orderbook):
    """A well-formed EIP-712 sig that recovers to a NON-owner address is
    rejected. This is what blocks "anyone PATCHes anyone else's order"
    griefing — the original M4 threat model."""
    _, order = fresh_orderbook
    sig = _sign_user_order_for(order, signer_key=ATTACKER_KEY)

    with pytest.raises(HTTPException) as exc:
        _call_attach(order.order_id, {"user_signature": sig})
    assert exc.value.status_code == 403


def test_attach_rejects_sig_for_different_order_content(fresh_orderbook):
    """Sig signed over DIFFERENT intent params doesn't reconstruct the
    same typehash, so recovery against this order's params yields a
    different signer (or fails). Rejected — blocks cross-order replay
    even if attacker has a legitimate sig from the same user for a
    different intent."""
    _, order = fresh_orderbook
    different_params_hex = "deadbeef" * 16  # 64 hex chars = 32 bytes
    sig = _sign_user_order_for(order, signer_key=OWNER_KEY, intent_params_hex=different_params_hex)

    with pytest.raises(HTTPException) as exc:
        _call_attach(order.order_id, {"user_signature": sig})
    assert exc.value.status_code == 403


def test_attach_rejects_empty_signature(fresh_orderbook):
    _, order = fresh_orderbook
    with pytest.raises(HTTPException) as exc:
        _call_attach(order.order_id, {"user_signature": ""})
    assert exc.value.status_code == 400
    assert "required" in exc.value.detail.lower()


def test_attach_404s_unknown_order():
    orders_mod._orderbook = IntentOrderBook()
    with pytest.raises(HTTPException) as exc:
        _call_attach("ord_missing", {"user_signature": "0xab" * 65})
    assert exc.value.status_code == 404


# ── backward compat with the legacy 2-prompt frontend ─────────────────────


def test_attach_ignores_legacy_owner_signature_field(fresh_orderbook):
    """A frontend that hasn't yet shipped the single-prompt change still
    sends ``owner_signature`` + ``deadline`` in the body. The new route
    silently ignores both as long as the EIP-712 sig itself verifies.
    Tests this by sending obvious garbage in owner_signature — must not
    affect the outcome."""
    _, order = fresh_orderbook
    sig = _sign_user_order_for(order, signer_key=OWNER_KEY)

    result = _call_attach(
        order.order_id,
        {
            "user_signature": sig,
            "owner_signature": "0x" + "ee" * 65,  # would fail EIP-191 if checked
            "deadline": 0,  # would fail EIP-191 deadline window if checked
        },
    )
    assert result["signature_attached"] is True


# ── env-override escape hatch (parity with the legacy gate) ───────────────


def test_attach_env_override_skips_verification(fresh_orderbook, monkeypatch):
    """``REQUIRE_ORDER_OWNER_SIG=0`` was the legacy one-off-incident
    escape hatch on the EIP-191 path. Keep the same env semantics on the
    EIP-712 path so operators don't lose a known knob."""
    monkeypatch.setenv("REQUIRE_ORDER_OWNER_SIG", "0")
    _, order = fresh_orderbook
    # Garbage that would normally 403 — under the override it stores.
    garbage = "0x" + "00" * 65
    result = _call_attach(order.order_id, {"user_signature": garbage})
    assert result["signature_attached"] is True
