"""Unit tests for /apps/{app_id}/prepare-permit and its digest builder (#3)."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import minotaur_subnet.api.routes.orders as orders_mod
import minotaur_subnet.blockchain.chains as chains_mod
from minotaur_subnet.blockchain.token_approval import build_permit_digest

OWNER = "0x00000000000000000000000000000000000000C0"
TOKEN = "0x00000000000000000000000000000000000000B0"
APP = "0x00000000000000000000000000000000000000A0"


def _w3_supporting_2612(nonce=7):
    w3 = MagicMock()
    # First call = DOMAIN_SEPARATOR() (32 bytes), second = nonces(owner) (32 bytes)
    w3.eth.call.side_effect = [b"\x11" * 32, nonce.to_bytes(32, "big")]
    return w3


def test_build_permit_digest_happy():
    out = build_permit_digest(_w3_supporting_2612(nonce=7), TOKEN, OWNER, APP, 1000, deadline=9999999999)
    assert out is not None
    assert out["nonce"] == 7
    assert out["deadline"] == 9999999999
    assert out["value"] == 1000
    assert out["digest"].startswith("0x") and len(out["digest"]) == 66
    assert out["domain_separator"] == "0x" + "11" * 32


def test_build_permit_digest_deterministic():
    a = build_permit_digest(_w3_supporting_2612(3), TOKEN, OWNER, APP, 500, deadline=123)
    b = build_permit_digest(_w3_supporting_2612(3), TOKEN, OWNER, APP, 500, deadline=123)
    assert a["digest"] == b["digest"]  # same inputs → same digest


def test_build_permit_digest_non_2612_returns_none():
    w3 = MagicMock()
    w3.eth.call.side_effect = Exception("execution reverted")  # no DOMAIN_SEPARATOR
    assert build_permit_digest(w3, TOKEN, OWNER, APP, 1000) is None


def test_build_permit_digest_bad_domain_len_returns_none():
    w3 = MagicMock()
    w3.eth.call.return_value = b"\x11" * 20  # not 32 bytes
    assert build_permit_digest(w3, TOKEN, OWNER, APP, 1000) is None


# ── Route ────────────────────────────────────────────────────────────────────

def _req(**kw):
    base = dict(token=TOKEN, owner=OWNER, value=1000, chain_id=8453, deadline=0)
    base.update(kw)
    return SimpleNamespace(**base)


def _store_with_deployment(contract=APP):
    store = MagicMock()
    store.get_deployment.return_value = SimpleNamespace(contract_address=contract)
    return store


def test_prepare_permit_happy(monkeypatch):
    monkeypatch.setattr(orders_mod, "_app_store", _store_with_deployment())
    monkeypatch.setattr(chains_mod, "get_web3", lambda cid: _w3_supporting_2612(nonce=2))
    out = orders_mod.prepare_permit("app_1", _req())
    assert out["spender"] == APP
    assert out["nonce"] == 2
    assert out["digest"].startswith("0x")
    assert out["order_params"]["permit_value"] == "1000"
    assert out["order_params"]["permit_deadline"] == out["deadline"]


def test_prepare_permit_non_2612_is_400(monkeypatch):
    w3 = MagicMock()
    w3.eth.call.side_effect = Exception("no permit")
    monkeypatch.setattr(orders_mod, "_app_store", _store_with_deployment())
    monkeypatch.setattr(chains_mod, "get_web3", lambda cid: w3)
    with pytest.raises(orders_mod.HTTPException) as ei:
        orders_mod.prepare_permit("app_1", _req())
    assert ei.value.status_code == 400


def test_prepare_permit_no_deployment_is_404(monkeypatch):
    store = MagicMock()
    store.get_deployment.return_value = None
    monkeypatch.setattr(orders_mod, "_app_store", store)
    with pytest.raises(orders_mod.HTTPException) as ei:
        orders_mod.prepare_permit("app_1", _req())
    assert ei.value.status_code == 404


def test_prepare_permit_zero_value_is_400(monkeypatch):
    monkeypatch.setattr(orders_mod, "_app_store", _store_with_deployment())
    monkeypatch.setattr(chains_mod, "get_web3", lambda cid: _w3_supporting_2612())
    with pytest.raises(orders_mod.HTTPException) as ei:
        orders_mod.prepare_permit("app_1", _req(value=0))
    assert ei.value.status_code == 400
