"""Tests for the EIP-712 round-lifecycle auth migration.

The cross-validator solver-round lifecycle endpoints
(``/v1/solver/round/internal/{close,certify,abort,activate}``) used to
authenticate via the shared ``SOLVER_ROUND_INTERNAL_API_KEY`` secret. They now
accept the SAME EIP-712 leader signature used by the champion-consensus
proposal route, while remaining backward-compatible (shared-key fallback) for a
staggered rollout.

These tests cover:
  1. sign -> verify ROUNDTRIP (leader-signed payload verifies, passes lock/registry);
  2. canonical-string PARITY (signer's canonical == verifier's canonical);
  3. FORGE rejection (tamper / wrong signer -> 401; invalid sig does NOT fall
     through to the shared-key path);
  4. BACKWARD-COMPAT (no sig + matching shared key -> OK; no sig +
     REQUIRE_SIGNED_ROUND_LIFECYCLE -> 401; no sig + no key -> 503).
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from eth_account import Account
from eth_account.messages import encode_defunct
from fastapi import HTTPException

from minotaur_subnet.api.routes.submissions import routes as routes_module
from minotaur_subnet.api.routes.submissions.routes import (
    _authorize_internal_round,
    _verify_internal_round_signature,
)
from minotaur_subnet.api.routes.submissions.round_manager import (
    _sign_internal_round_payload,
)
from minotaur_subnet.validator import metagraph_sync as metagraph_sync_module


# ── Helpers ──────────────────────────────────────────────────────────────────


class _FakeRequest:
    """Minimal Request stand-in: caches a body dict + headers for the helper.

    ``_authorize_internal_round`` only touches ``await request.json()`` and
    ``request.headers.get(...)`` (via the shared-key fallback).
    """

    def __init__(self, body: dict | None, headers: dict | None = None):
        self._body = body
        self.headers = headers or {}

    async def json(self):
        if self._body is None:
            raise ValueError("no body")
        return self._body


def _representative_close_payload() -> dict:
    """A realistic close-sync payload (mirrors _close_round_sync_payload)."""
    return {
        "round_id": "round-e29707897",
        "close_epoch": 29707897,
        "benchmark_pack_hash": "0xpack",
        "committee_block": 47842476,
        "committee_hash": "0xcomm",
        "quorum_required": 2,
        "decision_deadline_epoch": 29707920,
        "effective_epoch": 29707930,
    }


def _make_network_with_key(private_key: str):
    """A peer-network stand-in exposing only ``.private_key``."""
    net = MagicMock()
    net.private_key = private_key
    return net


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Clear the env knobs that affect the auth path so tests are isolated."""
    for var in (
        "SOLVER_ROUND_INTERNAL_API_KEY",
        "SUBMISSIONS_API_KEY",
        "REQUIRE_SIGNED_ROUND_LIFECYCLE",
        "CONSENSUS_ENFORCE_ONCHAIN_REGISTRY",
    ):
        monkeypatch.delenv(var, raising=False)
    yield


@pytest.fixture
def unlock_leader(monkeypatch):
    """Clear the locked-leader so the registry path is exercised."""
    monkeypatch.setattr(metagraph_sync_module, "LOCKED_LEADER_EVM_ADDRESS", "")
    yield


# ── (1) sign -> verify ROUNDTRIP ─────────────────────────────────────────────


def test_sign_then_verify_roundtrip_unlocked_registry(monkeypatch, unlock_leader):
    """A leader-signed payload verifies; recovered==proposer; registry path passes."""
    # No on-chain enforcement -> registry check is skipped.
    monkeypatch.setattr(
        "minotaur_subnet.consensus.validator_registry_cache.enforce_enabled",
        lambda: False,
    )
    acct = Account.create()
    net = _make_network_with_key(acct.key.hex())

    signed = _sign_internal_round_payload(net, _representative_close_payload())
    assert signed["proposer"].lower() == acct.address.lower()
    assert signed["proposer_signature"]

    assert _verify_internal_round_signature(signed) is None


def test_sign_then_verify_roundtrip_registry_enforced(monkeypatch, unlock_leader):
    """With on-chain enforcement on, a registered signer passes."""
    monkeypatch.setattr(
        "minotaur_subnet.consensus.validator_registry_cache.enforce_enabled",
        lambda: True,
    )
    monkeypatch.setattr(
        "minotaur_subnet.consensus.validator_registry_cache.is_on_chain_validator",
        lambda signer, chain_id: chain_id == 964,
    )
    acct = Account.create()
    net = _make_network_with_key(acct.key.hex())
    signed = _sign_internal_round_payload(net, _representative_close_payload())
    assert _verify_internal_round_signature(signed) is None


def test_sign_then_verify_locked_leader(monkeypatch):
    """When LOCKED_LEADER is set, the matching signer passes (registry untouched)."""
    acct = Account.create()
    monkeypatch.setattr(
        metagraph_sync_module, "LOCKED_LEADER_EVM_ADDRESS", acct.address,
    )
    net = _make_network_with_key(acct.key.hex())
    signed = _sign_internal_round_payload(net, _representative_close_payload())
    assert _verify_internal_round_signature(signed) is None


def test_authorize_accepts_signed_payload(monkeypatch, unlock_leader):
    """End-to-end through _authorize_internal_round: a signed body authorizes."""
    monkeypatch.setattr(
        "minotaur_subnet.consensus.validator_registry_cache.enforce_enabled",
        lambda: False,
    )
    acct = Account.create()
    net = _make_network_with_key(acct.key.hex())
    signed = _sign_internal_round_payload(net, _representative_close_payload())

    # No shared key set, no REQUIRE flag — signature alone must authorize.
    req = _FakeRequest(body=signed)
    _run(_authorize_internal_round(req))  # must not raise


# ── (2) canonical-string PARITY ──────────────────────────────────────────────


def test_signer_and_verifier_canonical_match():
    """The canonical the SIGNER signs == the canonical the VERIFIER recovers over.

    Guards against serialization drift between the leader-side signing in
    round_manager and the follower-side verification in routes.
    """
    acct = Account.create()
    net = _make_network_with_key(acct.key.hex())
    payload = _representative_close_payload()
    signed = _sign_internal_round_payload(net, payload)

    # Reconstruct what the SIGNER canonicalized: signed dict minus the sig.
    signer_view = dict(signed)
    signer_view.pop("proposer_signature", None)
    signer_canonical = json.dumps(signer_view, sort_keys=True, separators=(",", ":"))

    # Reconstruct what the VERIFIER canonicalizes: raw dict minus the sig.
    verifier_view = dict(signed)
    verifier_view.pop("proposer_signature", None)
    verifier_canonical = json.dumps(
        verifier_view, sort_keys=True, separators=(",", ":")
    )

    assert signer_canonical == verifier_canonical

    # And that exact canonical is what the signature recovers over.
    recovered = Account.recover_message(
        encode_defunct(text=verifier_canonical),
        signature=signed["proposer_signature"],
    )
    assert recovered.lower() == acct.address.lower()


# ── (3) FORGE rejection ──────────────────────────────────────────────────────


def test_tampered_payload_rejected(monkeypatch, unlock_leader):
    """Tampering with a signed field after signing breaks recovery -> mismatch."""
    monkeypatch.setattr(
        "minotaur_subnet.consensus.validator_registry_cache.enforce_enabled",
        lambda: False,
    )
    acct = Account.create()
    net = _make_network_with_key(acct.key.hex())
    signed = _sign_internal_round_payload(net, _representative_close_payload())

    # Flip a value AFTER signing — the recovered signer no longer matches the
    # declared proposer.
    signed["close_epoch"] = 99999999
    err = _verify_internal_round_signature(signed)
    assert err is not None
    assert "Signer mismatch" in err


def test_wrong_signer_declared_rejected(monkeypatch, unlock_leader):
    """Signature recovers to X but proposer field declares Y -> reject."""
    monkeypatch.setattr(
        "minotaur_subnet.consensus.validator_registry_cache.enforce_enabled",
        lambda: False,
    )
    real = Account.create()
    fake = Account.create()
    net = _make_network_with_key(real.key.hex())
    signed = _sign_internal_round_payload(net, _representative_close_payload())
    # Lie about who signed it.
    signed["proposer"] = fake.address
    err = _verify_internal_round_signature(signed)
    assert err is not None
    assert "Signer mismatch" in err


def test_locked_leader_rejects_other_signer(monkeypatch):
    """A valid sig from a non-locked address is rejected under the lock."""
    locked = Account.create()
    other = Account.create()
    monkeypatch.setattr(
        metagraph_sync_module, "LOCKED_LEADER_EVM_ADDRESS", locked.address,
    )
    net = _make_network_with_key(other.key.hex())
    signed = _sign_internal_round_payload(net, _representative_close_payload())
    err = _verify_internal_round_signature(signed)
    assert err is not None
    assert "locked leader" in err


def test_present_but_invalid_sig_does_not_fall_through_to_shared_key(
    monkeypatch, unlock_leader
):
    """CRITICAL: a forged/invalid sig must 401 — NOT be retryable as a key.

    Even when a MATCHING shared key header is supplied, a present-but-invalid
    signature must hard-fail rather than fall through to the shared-key path.
    """
    monkeypatch.setattr(
        "minotaur_subnet.consensus.validator_registry_cache.enforce_enabled",
        lambda: False,
    )
    # Valid shared key is configured + supplied in the header.
    monkeypatch.setenv("SOLVER_ROUND_INTERNAL_API_KEY", "shared-secret")

    real = Account.create()
    fake = Account.create()
    net = _make_network_with_key(real.key.hex())
    signed = _sign_internal_round_payload(net, _representative_close_payload())
    signed["proposer"] = fake.address  # break the sig

    req = _FakeRequest(
        body=signed,
        headers={"x-solver-round-internal-key": "shared-secret"},
    )
    with pytest.raises(HTTPException) as exc:
        _run(_authorize_internal_round(req))
    assert exc.value.status_code == 401
    assert "signature" in exc.value.detail.lower()


# ── (4) BACKWARD-COMPAT ──────────────────────────────────────────────────────


def test_no_sig_matching_shared_key_authorized(monkeypatch):
    """No signature + matching shared-key header -> authorized (legacy path)."""
    monkeypatch.setenv("SOLVER_ROUND_INTERNAL_API_KEY", "shared-secret")
    body = _representative_close_payload()  # no proposer / proposer_signature
    req = _FakeRequest(
        body=body,
        headers={"x-solver-round-internal-key": "shared-secret"},
    )
    _run(_authorize_internal_round(req))  # must not raise


def test_no_sig_wrong_shared_key_rejected(monkeypatch):
    """No signature + wrong shared-key header -> 401 (legacy path)."""
    monkeypatch.setenv("SOLVER_ROUND_INTERNAL_API_KEY", "shared-secret")
    req = _FakeRequest(
        body=_representative_close_payload(),
        headers={"x-solver-round-internal-key": "WRONG"},
    )
    with pytest.raises(HTTPException) as exc:
        _run(_authorize_internal_round(req))
    assert exc.value.status_code == 401


def test_no_sig_require_signed_flag_rejects(monkeypatch):
    """No signature + REQUIRE_SIGNED_ROUND_LIFECYCLE=1 -> 401 (rollout complete)."""
    monkeypatch.setenv("SOLVER_ROUND_INTERNAL_API_KEY", "shared-secret")
    monkeypatch.setenv("REQUIRE_SIGNED_ROUND_LIFECYCLE", "1")
    req = _FakeRequest(
        body=_representative_close_payload(),
        headers={"x-solver-round-internal-key": "shared-secret"},
    )
    with pytest.raises(HTTPException) as exc:
        _run(_authorize_internal_round(req))
    assert exc.value.status_code == 401
    assert "signed round-lifecycle" in exc.value.detail.lower()


def test_no_sig_no_key_set_fail_closed_503(monkeypatch):
    """No signature + no key configured -> 503 (existing fail-closed semantics)."""
    # _clean_env already dropped both key vars.
    req = _FakeRequest(
        body=_representative_close_payload(),
        headers={},
    )
    with pytest.raises(HTTPException) as exc:
        _run(_authorize_internal_round(req))
    assert exc.value.status_code == 503


# ── Leader-side defensiveness ────────────────────────────────────────────────


def test_sign_without_key_returns_unsigned():
    """No signing key -> payload returned unsigned (legacy broadcast)."""
    net = MagicMock()
    net.private_key = ""
    payload = _representative_close_payload()
    out = _sign_internal_round_payload(net, payload)
    assert "proposer_signature" not in out
    assert out == payload


def test_sign_never_raises_on_bad_key():
    """A garbage key must NOT raise into the broadcast path; returns unsigned."""
    net = MagicMock()
    net.private_key = "0xnotakey"
    payload = _representative_close_payload()
    out = _sign_internal_round_payload(net, payload)
    # Signing failed -> original payload, no signature, no exception.
    assert "proposer_signature" not in out
