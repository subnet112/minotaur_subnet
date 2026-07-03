"""Tests for the app-management signature-auth layer (app_auth.py).

The lifecycle endpoints execute with the leader's relayer key, so the API is
a proxy for a fund-moving key. This layer requires the caller to prove owner
authority — by wallet signature bound to the exact parameters — instead of
(or in addition to) the shared admin key. The load-bearing property: a
signature authorizing a float withdraw to address A / amount N cannot be
replayed for a different recipient, amount, action, app, or a second time.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from eth_account import Account

from minotaur_subnet.api.services import app_auth, developer_auth
from minotaur_subnet.shared.types import AppIntentConfig, AppIntentDefinition
from minotaur_subnet.store.app_intent_store import AppIntentStore

# Deterministic test wallets.
OWNER = Account.from_key("0x" + "11" * 32)          # app deployer
ADMIN_WALLET = Account.from_key("0x" + "22" * 32)   # operator admin signer
STRANGER = Account.from_key("0x" + "33" * 32)


def _store(tmp_path, deployer=OWNER.address) -> AppIntentStore:
    s = AppIntentStore(store_path=tmp_path / "s.db")
    s.save_app(AppIntentDefinition(
        app_id="app_x", name="dex", version="1.0.0", intent_type="swap",
        js_code="function score(){return 1;}", solidity_code="contract X {}",
        config=AppIntentConfig(supported_chains=[8453]),
        deployer=deployer,
    ))
    return s


def _signed_block(wallet, *, action, app_id, params_hash, nonce, deadline, signer=None):
    sig = developer_auth.sign_developer_auth(
        wallet.key.hex(), action=action, app_id=app_id,
        params_hash=params_hash, nonce=nonce, deadline=deadline,
    )
    return app_auth.AuthBlock(
        signer=signer if signer is not None else wallet.address,
        signature=sig, nonce=nonce, deadline=deadline,
    )


NOW = 1_000_000
SOON = NOW + 600


def _withdraw_hash(app_id="app_x", chain=8453, to="0x" + "77" * 20, amt=5):
    return app_auth.params_hash_for(
        developer_auth.ACTION_FLOAT_WITHDRAW, app_id, chain, to, amt,
    )


# ── happy path ───────────────────────────────────────────────────────────


def test_owner_signature_authorizes_and_consumes_nonce(tmp_path):
    s = _store(tmp_path)
    ph = _withdraw_hash()
    auth = _signed_block(OWNER, action=developer_auth.ACTION_FLOAT_WITHDRAW,
                         app_id="app_x", params_hash=ph, nonce=1, deadline=SOON)
    ok, err, signer = app_auth.authorize(
        s, "app_x", action=developer_auth.ACTION_FLOAT_WITHDRAW,
        params_hash=ph, auth=auth, admin_ok=False, now=NOW,
    )
    assert ok and signer == OWNER.address.lower(), err
    # nonce consumed → the SAME signature can't be reused.
    ok2, err2, _ = app_auth.authorize(
        s, "app_x", action=developer_auth.ACTION_FLOAT_WITHDRAW,
        params_hash=ph, auth=auth, admin_ok=False, now=NOW,
    )
    assert not ok2 and "nonce" in err2.lower()


def test_env_admin_signer_authorizes_any_app(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_ADMIN_SIGNERS", ADMIN_WALLET.address)
    s = _store(tmp_path)
    ph = _withdraw_hash()
    auth = _signed_block(ADMIN_WALLET, action=developer_auth.ACTION_FLOAT_WITHDRAW,
                         app_id="app_x", params_hash=ph, nonce=1, deadline=SOON)
    ok, err, signer = app_auth.authorize(
        s, "app_x", action=developer_auth.ACTION_FLOAT_WITHDRAW,
        params_hash=ph, auth=auth, admin_ok=False, now=NOW,
    )
    assert ok and signer == ADMIN_WALLET.address.lower(), err


# ── the drain vector: parameter binding ──────────────────────────────────


def test_signature_cannot_be_repointed_to_another_recipient(tmp_path):
    """A signature for withdraw→A/5 must fail when the request body says B/5."""
    s = _store(tmp_path)
    signed_for = _withdraw_hash(to="0x" + "aa" * 20, amt=5)
    auth = _signed_block(OWNER, action=developer_auth.ACTION_FLOAT_WITHDRAW,
                         app_id="app_x", params_hash=signed_for, nonce=1, deadline=SOON)
    # Server recomputes the hash from the ACTUAL request (attacker's address).
    actual = _withdraw_hash(to="0x" + "bb" * 20, amt=5)
    ok, err, _ = app_auth.authorize(
        s, "app_x", action=developer_auth.ACTION_FLOAT_WITHDRAW,
        params_hash=actual, auth=auth, admin_ok=False, now=NOW,
    )
    assert not ok and "match" in err.lower()


def test_signature_cannot_be_repointed_to_another_amount(tmp_path):
    s = _store(tmp_path)
    auth = _signed_block(OWNER, action=developer_auth.ACTION_FLOAT_WITHDRAW,
                         app_id="app_x", params_hash=_withdraw_hash(amt=1),
                         nonce=1, deadline=SOON)
    ok, _, _ = app_auth.authorize(
        s, "app_x", action=developer_auth.ACTION_FLOAT_WITHDRAW,
        params_hash=_withdraw_hash(amt=10_000), auth=auth, admin_ok=False, now=NOW,
    )
    assert not ok


def test_signature_cannot_be_replayed_as_a_different_action(tmp_path):
    """Signed for float_deposit, presented for float_withdraw → action tag
    domain-separates even at the same paramsHash."""
    s = _store(tmp_path)
    ph = _withdraw_hash()
    auth = _signed_block(OWNER, action=developer_auth.ACTION_FLOAT_DEPOSIT,
                         app_id="app_x", params_hash=ph, nonce=1, deadline=SOON)
    ok, _, _ = app_auth.authorize(
        s, "app_x", action=developer_auth.ACTION_FLOAT_WITHDRAW,
        params_hash=ph, auth=auth, admin_ok=False, now=NOW,
    )
    assert not ok


def test_signature_cannot_be_replayed_on_a_different_app(tmp_path):
    s = _store(tmp_path)
    s.save_app(AppIntentDefinition(
        app_id="app_y", name="y", version="1.0.0", intent_type="swap",
        js_code="x", solidity_code="x",
        config=AppIntentConfig(supported_chains=[8453]), deployer=OWNER.address,
    ))
    ph = _withdraw_hash(app_id="app_x")
    auth = _signed_block(OWNER, action=developer_auth.ACTION_FLOAT_WITHDRAW,
                         app_id="app_x", params_hash=ph, nonce=1, deadline=SOON)
    ok, _, _ = app_auth.authorize(
        s, "app_y", action=developer_auth.ACTION_FLOAT_WITHDRAW,
        params_hash=_withdraw_hash(app_id="app_y"), auth=auth, admin_ok=False, now=NOW,
    )
    assert not ok


# ── rejections ───────────────────────────────────────────────────────────


def test_stranger_signature_rejected(tmp_path):
    s = _store(tmp_path)
    ph = _withdraw_hash()
    auth = _signed_block(STRANGER, action=developer_auth.ACTION_FLOAT_WITHDRAW,
                         app_id="app_x", params_hash=ph, nonce=1, deadline=SOON)
    ok, err, _ = app_auth.authorize(
        s, "app_x", action=developer_auth.ACTION_FLOAT_WITHDRAW,
        params_hash=ph, auth=auth, admin_ok=False, now=NOW,
    )
    assert not ok and "not an allowed signer" in err


def test_expired_signature_rejected(tmp_path):
    s = _store(tmp_path)
    ph = _withdraw_hash()
    auth = _signed_block(OWNER, action=developer_auth.ACTION_FLOAT_WITHDRAW,
                         app_id="app_x", params_hash=ph, nonce=1, deadline=NOW - 1)
    ok, err, _ = app_auth.authorize(
        s, "app_x", action=developer_auth.ACTION_FLOAT_WITHDRAW,
        params_hash=ph, auth=auth, admin_ok=False, now=NOW,
    )
    assert not ok and "deadline" in err.lower()


def test_no_auth_at_all_rejected(tmp_path):
    s = _store(tmp_path)
    ok, err, _ = app_auth.authorize(
        s, "app_x", action=developer_auth.ACTION_FLOAT_WITHDRAW,
        params_hash=_withdraw_hash(), auth=app_auth.AuthBlock(),
        admin_ok=False, now=NOW,
    )
    assert not ok and "required" in err.lower()


# ── admin bypass + the mandatory switch ──────────────────────────────────


def test_admin_bypass_when_signatures_not_required(tmp_path):
    s = _store(tmp_path)
    ok, err, signer = app_auth.authorize(
        s, "app_x", action=developer_auth.ACTION_FLOAT_WITHDRAW,
        params_hash=_withdraw_hash(), auth=app_auth.AuthBlock(),
        admin_ok=True, now=NOW,
    )
    assert ok and signer == "admin", err


def test_admin_bypass_disabled_when_signature_required(tmp_path, monkeypatch):
    monkeypatch.setenv("REQUIRE_APP_ACTION_SIGNATURE", "1")
    s = _store(tmp_path)
    ok, err, _ = app_auth.authorize(
        s, "app_x", action=developer_auth.ACTION_FLOAT_WITHDRAW,
        params_hash=_withdraw_hash(), auth=app_auth.AuthBlock(),
        admin_ok=True, now=NOW,
    )
    assert not ok and "signature required" in err.lower()


def test_valid_signature_still_works_when_required(tmp_path, monkeypatch):
    monkeypatch.setenv("REQUIRE_APP_ACTION_SIGNATURE", "1")
    s = _store(tmp_path)
    ph = _withdraw_hash()
    auth = _signed_block(OWNER, action=developer_auth.ACTION_FLOAT_WITHDRAW,
                         app_id="app_x", params_hash=ph, nonce=1, deadline=SOON)
    ok, err, signer = app_auth.authorize(
        s, "app_x", action=developer_auth.ACTION_FLOAT_WITHDRAW,
        params_hash=ph, auth=auth, admin_ok=False, now=NOW,
    )
    assert ok, err


# ── reads don't consume nonces ───────────────────────────────────────────


def test_read_signature_does_not_consume_nonce(tmp_path):
    s = _store(tmp_path)
    ph = app_auth.params_hash_for(developer_auth.ACTION_ADMIN_STATE, "app_x", None)
    auth = _signed_block(OWNER, action=developer_auth.ACTION_ADMIN_STATE,
                         app_id="app_x", params_hash=ph, nonce=1, deadline=SOON)
    for _ in range(3):  # same read signature reusable within its deadline
        ok, err, _ = app_auth.authorize(
            s, "app_x", action=developer_auth.ACTION_ADMIN_STATE,
            params_hash=ph, auth=auth, admin_ok=False, consume_nonce=False, now=NOW,
        )
        assert ok, err
    assert s.get_developer_nonce("app_x", OWNER.address.lower()) == 0


def test_default_signer_falls_back_to_app_deployer(tmp_path):
    """Omitting signer defaults to the app's deployer."""
    s = _store(tmp_path)
    ph = _withdraw_hash()
    auth = _signed_block(OWNER, action=developer_auth.ACTION_FLOAT_WITHDRAW,
                         app_id="app_x", params_hash=ph, nonce=1, deadline=SOON,
                         signer="")  # no explicit signer
    ok, err, signer = app_auth.authorize(
        s, "app_x", action=developer_auth.ACTION_FLOAT_WITHDRAW,
        params_hash=ph, auth=auth, admin_ok=False, now=NOW,
    )
    assert ok and signer == OWNER.address.lower(), err
