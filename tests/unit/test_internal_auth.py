"""Tests for shared.internal_auth — signed-payload RPC between api ↔ validator.

This module gates ``/internal/weights/queue`` on the validator daemon
(and any future ``/internal/*`` route). A correct implementation must:

  - bind the signature to the exact (method, path, body, timestamp)
    tuple, so a sig for ``POST /internal/weights/queue`` cannot be
    replayed against ``POST /internal/foo`` or against a tampered body;
  - reject signatures that recover to anyone other than the expected
    signer address;
  - reject timestamps older than ``MAX_REQUEST_AGE_SECONDS``;
  - reject timestamps too far in the future (clock-skew bound).

We avoid testing the eth_account / eth_abi internals — those are
upstream and trusted. We test the *envelope*: that two parties holding
the same key agree, and that any mismatch in any field is rejected.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from minotaur_subnet.shared.internal_auth import (
    InvalidSignature,
    MAX_REQUEST_AGE_SECONDS,
    MAX_REQUEST_FUTURE_SKEW_SECONDS,
    derive_signer_address,
    sign_request,
    verify_request,
)


# Anvil's well-known account #0 — public test fixture, not a real key.
TEST_KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"


def test_sign_then_verify_round_trips():
    """Happy path: a signature produced with the same key/method/path/body
    that the receiver expects round-trips cleanly."""
    expected = derive_signer_address(TEST_KEY)
    body = b'{"mapping": {"5HOwner": 1.0}}'
    ts, sig = sign_request(
        TEST_KEY,
        method="POST",
        path="/internal/weights/queue",
        body=body,
    )
    # Must not raise.
    verify_request(
        method="POST",
        path="/internal/weights/queue",
        body=body,
        timestamp=ts,
        signature_hex=sig,
        expected_address=expected,
    )


def test_signer_mismatch_rejected():
    """A sig from a different key must not verify against the expected
    address. This is the property that makes internal-auth gate the
    endpoint — only holders of VALIDATOR_PRIVATE_KEY can post."""
    other_key = "0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d"
    expected = derive_signer_address(TEST_KEY)
    body = b'{"mapping": {"5HOwner": 1.0}}'
    ts, sig = sign_request(
        other_key,
        method="POST",
        path="/internal/weights/queue",
        body=body,
    )
    with pytest.raises(InvalidSignature, match="signer mismatch"):
        verify_request(
            method="POST",
            path="/internal/weights/queue",
            body=body,
            timestamp=ts,
            signature_hex=sig,
            expected_address=expected,
        )


def test_body_tamper_rejected():
    """A signature for body A must not verify against body B. Without
    this property, an attacker who observes a queue POST could swap
    in their own mapping while keeping the signature."""
    expected = derive_signer_address(TEST_KEY)
    body_signed = b'{"mapping": {"5HOwner": 1.0}}'
    body_tampered = b'{"mapping": {"5Attacker": 1.0}}'
    ts, sig = sign_request(
        TEST_KEY,
        method="POST",
        path="/internal/weights/queue",
        body=body_signed,
    )
    with pytest.raises(InvalidSignature, match="signer mismatch"):
        verify_request(
            method="POST",
            path="/internal/weights/queue",
            body=body_tampered,
            timestamp=ts,
            signature_hex=sig,
            expected_address=expected,
        )


def test_path_tamper_rejected():
    """A sig for /internal/weights/queue must not be replayable against
    a different /internal/* route. Especially important once we add
    more internal endpoints — a queue sig shouldn't authorize anything
    else."""
    expected = derive_signer_address(TEST_KEY)
    body = b'{"mapping": {"5HOwner": 1.0}}'
    ts, sig = sign_request(
        TEST_KEY,
        method="POST",
        path="/internal/weights/queue",
        body=body,
    )
    with pytest.raises(InvalidSignature, match="signer mismatch"):
        verify_request(
            method="POST",
            path="/internal/some-other-endpoint",
            body=body,
            timestamp=ts,
            signature_hex=sig,
            expected_address=expected,
        )


def test_method_tamper_rejected():
    """A POST sig must not verify as a GET (or any other method). Note
    POST/GET differ semantically — if we ever add a GET /internal/X
    handler that uses the same auth, a POST sig stolen elsewhere
    shouldn't authorize the GET."""
    expected = derive_signer_address(TEST_KEY)
    body = b'{"mapping": {"5HOwner": 1.0}}'
    ts, sig = sign_request(
        TEST_KEY,
        method="POST",
        path="/internal/weights/queue",
        body=body,
    )
    with pytest.raises(InvalidSignature, match="signer mismatch"):
        verify_request(
            method="GET",
            path="/internal/weights/queue",
            body=body,
            timestamp=ts,
            signature_hex=sig,
            expected_address=expected,
        )


def test_stale_timestamp_rejected():
    """Signatures older than MAX_REQUEST_AGE_SECONDS must be rejected.
    Bounds the replay window even if every other check is bypassed."""
    expected = derive_signer_address(TEST_KEY)
    body = b'{"mapping": {"5HOwner": 1.0}}'
    # Sign with a timestamp WELL outside the freshness window.
    old_ts = int(time.time()) - (MAX_REQUEST_AGE_SECONDS + 60)
    _, sig = sign_request(
        TEST_KEY,
        method="POST",
        path="/internal/weights/queue",
        body=body,
        timestamp=old_ts,
    )
    with pytest.raises(InvalidSignature, match="too old"):
        verify_request(
            method="POST",
            path="/internal/weights/queue",
            body=body,
            timestamp=old_ts,
            signature_hex=sig,
            expected_address=expected,
        )


def test_future_timestamp_within_skew_accepted():
    """A small future-skew timestamp (within
    MAX_REQUEST_FUTURE_SKEW_SECONDS) must be accepted, otherwise minor
    clock drift between containers will reject legitimate requests."""
    expected = derive_signer_address(TEST_KEY)
    body = b""
    future_ts = int(time.time()) + 2  # within tolerance
    _, sig = sign_request(
        TEST_KEY,
        method="POST",
        path="/internal/weights/queue",
        body=body,
        timestamp=future_ts,
    )
    verify_request(
        method="POST",
        path="/internal/weights/queue",
        body=body,
        timestamp=future_ts,
        signature_hex=sig,
        expected_address=expected,
    )


def test_future_timestamp_beyond_skew_rejected():
    """A timestamp far in the future (beyond
    MAX_REQUEST_FUTURE_SKEW_SECONDS) must be rejected — that's a sign
    of a clock-broken sender, not a routine clock-drift event."""
    expected = derive_signer_address(TEST_KEY)
    body = b""
    far_future = int(time.time()) + MAX_REQUEST_FUTURE_SKEW_SECONDS + 60
    _, sig = sign_request(
        TEST_KEY,
        method="POST",
        path="/internal/weights/queue",
        body=body,
        timestamp=far_future,
    )
    with pytest.raises(InvalidSignature, match="future"):
        verify_request(
            method="POST",
            path="/internal/weights/queue",
            body=body,
            timestamp=far_future,
            signature_hex=sig,
            expected_address=expected,
        )


def test_malformed_signature_rejected():
    """Non-hex / wrong-length signatures must surface as InvalidSignature
    (not bubble a raw eth_account error). The receiver maps any
    InvalidSignature to HTTP 403, so the request handler can't crash."""
    expected = derive_signer_address(TEST_KEY)
    body = b""
    ts = int(time.time())
    with pytest.raises(InvalidSignature, match="signature recovery failed"):
        verify_request(
            method="POST",
            path="/internal/weights/queue",
            body=body,
            timestamp=ts,
            signature_hex="0xnothex",
            expected_address=expected,
        )


def test_derive_signer_address_matches_account():
    """``derive_signer_address`` must return the checksummed address that
    matches what ``Account.sign_message`` would recover to. This is the
    invariant that makes "both sides hold the same env var → both
    derive the same address" actually true."""
    from eth_account import Account
    expected = Account.from_key(TEST_KEY).address
    assert derive_signer_address(TEST_KEY) == expected
