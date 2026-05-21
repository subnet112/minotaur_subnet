"""Unit tests for the validator identity attestation."""

from __future__ import annotations

import time

import pytest
from eth_account import Account

from minotaur_subnet.consensus.identity import (
    ValidatorIdentity,
    sign_identity,
    verify_identity,
)


KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
KEY_OTHER = "0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d"
HOTKEY = "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY"
AXON = "http://my-validator.example:9100"


def test_sign_verify_round_trip():
    ident = sign_identity(KEY, HOTKEY, AXON)
    assert ident.evm_address == Account.from_key(KEY).address
    recovered = verify_identity(ident)
    assert recovered is not None
    assert recovered.lower() == ident.evm_address.lower()


def test_signature_format():
    ident = sign_identity(KEY, HOTKEY, AXON)
    assert ident.signature.startswith("0x")
    # 65-byte ECDSA signature → 130 hex chars + 0x
    assert len(ident.signature) == 132
    assert ident.nonce.startswith("0x")
    # 32-byte nonce
    assert len(ident.nonce) == 66


def test_expired_signature_rejected():
    past = int(time.time()) - 3600
    ident = sign_identity(KEY, HOTKEY, AXON, ttl_seconds=60, now=past)
    assert verify_identity(ident) is None


def test_tampered_axon_url_rejected():
    ident = sign_identity(KEY, HOTKEY, AXON)
    ident.axon_url = "http://attacker.example:9100"
    assert verify_identity(ident) is None


def test_tampered_hotkey_rejected():
    ident = sign_identity(KEY, HOTKEY, AXON)
    ident.hotkey = "5OtherHotkey..."
    assert verify_identity(ident) is None


def test_tampered_evm_address_rejected():
    ident = sign_identity(KEY, HOTKEY, AXON)
    # Claim a different address than the one that actually signed
    ident.evm_address = Account.from_key(KEY_OTHER).address
    assert verify_identity(ident) is None


def test_tampered_expiry_rejected():
    ident = sign_identity(KEY, HOTKEY, AXON)
    # Extending the deadline would let a stale signature live forever —
    # this must fail verification because the struct hash changes.
    ident.expiry += 10_000
    assert verify_identity(ident) is None


def test_nonce_changes_signature():
    a = sign_identity(KEY, HOTKEY, AXON)
    b = sign_identity(KEY, HOTKEY, AXON)
    # Same inputs, different random nonces → different signatures.
    # Nonce is the freshness guarantee (the timestamp alone could collide).
    assert a.nonce != b.nonce
    assert a.signature != b.signature
    # Both still verify
    assert verify_identity(a) is not None
    assert verify_identity(b) is not None


def test_serialization_round_trip():
    ident = sign_identity(KEY, HOTKEY, AXON)
    as_dict = ident.to_dict()
    rehydrated = ValidatorIdentity.from_dict(as_dict)
    assert rehydrated == ident
    assert verify_identity(rehydrated) is not None


def test_now_override_lets_us_check_future_validity():
    # Signed at t=1000, ttl=60 → expires at t=1060.
    # At t=1059 it's still valid; at t=1061 it's expired.
    ident = sign_identity(KEY, HOTKEY, AXON, ttl_seconds=60, now=1000)
    assert verify_identity(ident, now=1059) is not None
    assert verify_identity(ident, now=1061) is None
