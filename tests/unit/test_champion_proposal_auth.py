"""Tests for champion-proposal EIP-712 auth + rate limit."""

from __future__ import annotations

import json
import time
from unittest.mock import MagicMock, patch

import pytest

from minotaur_subnet.api.routes.submissions.routes import (
    _verify_champion_proposal_signature,
    _champion_proposal_rate_limit_check,
    _CHAMPION_PROPOSAL_LAST_SEEN,
)
from minotaur_subnet.validator import metagraph_sync as metagraph_sync_module


@pytest.fixture
def unlock_leader(monkeypatch):
    """Clear the leader lock so any-validator acceptance is exercised."""
    monkeypatch.setattr(metagraph_sync_module, "LOCKED_LEADER_EVM_ADDRESS", "")
    yield


def _make_body(**overrides):
    body = MagicMock()
    body.round_id = overrides.get("round_id", "round-1")
    body.proposer = overrides.get("proposer", "")
    body.proposer_signature = overrides.get("proposer_signature", "")
    body.model_dump = MagicMock(return_value={
        "round_id": body.round_id,
        "proposer": body.proposer,
        "proposer_signature": body.proposer_signature,
    })
    return body


# ── Signature verification ─────────────────────────────────────────────────

def test_sig_verify_on_by_default(monkeypatch):
    """PR-2 flipped the default from opt-in to opt-out (audit C2)."""
    monkeypatch.delenv("CONSENSUS_REQUIRE_SIGNED_CHAMPION_PROPOSALS", raising=False)
    body = _make_body()  # empty proposer/signature
    err = _verify_champion_proposal_signature(body)
    assert err is not None
    assert "Missing proposer" in err


def test_sig_verify_opt_out_via_env(monkeypatch):
    """Operators can still bypass with =0 for emergency incident handling."""
    monkeypatch.setenv("CONSENSUS_REQUIRE_SIGNED_CHAMPION_PROPOSALS", "0")
    body = _make_body()
    assert _verify_champion_proposal_signature(body) is None


def test_sig_verify_on_rejects_missing(monkeypatch):
    monkeypatch.setenv("CONSENSUS_REQUIRE_SIGNED_CHAMPION_PROPOSALS", "1")
    body = _make_body(proposer="", proposer_signature="")
    err = _verify_champion_proposal_signature(body)
    assert err is not None
    assert "Missing proposer" in err


def test_sig_verify_on_accepts_real_signature(monkeypatch, unlock_leader):
    """A real ECDSA sig over the canonical payload from a consistent signer passes."""
    monkeypatch.setenv("CONSENSUS_REQUIRE_SIGNED_CHAMPION_PROPOSALS", "1")
    monkeypatch.delenv("CONSENSUS_ENFORCE_ONCHAIN_REGISTRY", raising=False)

    from eth_account import Account
    from eth_account.messages import encode_defunct

    acct = Account.create()
    body = _make_body(proposer=acct.address)
    payload = body.model_dump()
    payload.pop("proposer_signature", None)
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    signed = Account.sign_message(encode_defunct(text=canonical), private_key=acct.key)
    body.proposer_signature = signed.signature.hex()
    # model_dump must include the updated signature so the helper strips it
    body.model_dump.return_value["proposer_signature"] = body.proposer_signature

    assert _verify_champion_proposal_signature(body) is None


def test_sig_verify_locked_leader_accepts_matching_signer(monkeypatch):
    """When LOCKED_LEADER_EVM_ADDRESS is set, only that address is accepted."""
    monkeypatch.setenv("CONSENSUS_REQUIRE_SIGNED_CHAMPION_PROPOSALS", "1")
    monkeypatch.delenv("CONSENSUS_ENFORCE_ONCHAIN_REGISTRY", raising=False)

    from eth_account import Account
    from eth_account.messages import encode_defunct

    acct = Account.create()
    monkeypatch.setattr(
        metagraph_sync_module, "LOCKED_LEADER_EVM_ADDRESS", acct.address,
    )

    body = _make_body(proposer=acct.address)
    payload = body.model_dump()
    payload.pop("proposer_signature", None)
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    signed = Account.sign_message(encode_defunct(text=canonical), private_key=acct.key)
    body.proposer_signature = signed.signature.hex()
    body.model_dump.return_value["proposer_signature"] = body.proposer_signature

    assert _verify_champion_proposal_signature(body) is None


def test_sig_verify_locked_leader_rejects_other_signer(monkeypatch):
    """A signature from any non-locked address is rejected even if registered."""
    monkeypatch.setenv("CONSENSUS_REQUIRE_SIGNED_CHAMPION_PROPOSALS", "1")
    monkeypatch.delenv("CONSENSUS_ENFORCE_ONCHAIN_REGISTRY", raising=False)

    from eth_account import Account
    from eth_account.messages import encode_defunct

    locked = Account.create()
    other = Account.create()
    monkeypatch.setattr(
        metagraph_sync_module, "LOCKED_LEADER_EVM_ADDRESS", locked.address,
    )

    body = _make_body(proposer=other.address)
    payload = body.model_dump()
    payload.pop("proposer_signature", None)
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    signed = Account.sign_message(encode_defunct(text=canonical), private_key=other.key)
    body.proposer_signature = signed.signature.hex()
    body.model_dump.return_value["proposer_signature"] = body.proposer_signature

    err = _verify_champion_proposal_signature(body)
    assert err is not None
    assert "locked leader" in err


def test_sig_verify_on_rejects_wrong_signer(monkeypatch):
    """Signature recovers to X but proposer declares Y — reject."""
    monkeypatch.setenv("CONSENSUS_REQUIRE_SIGNED_CHAMPION_PROPOSALS", "1")
    monkeypatch.delenv("CONSENSUS_ENFORCE_ONCHAIN_REGISTRY", raising=False)

    from eth_account import Account
    from eth_account.messages import encode_defunct

    real_signer = Account.create()
    fake_declarer = Account.create()
    body = _make_body(proposer=fake_declarer.address)
    payload = body.model_dump()
    payload.pop("proposer_signature", None)
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    signed = Account.sign_message(encode_defunct(text=canonical), private_key=real_signer.key)
    body.proposer_signature = signed.signature.hex()
    body.model_dump.return_value["proposer_signature"] = body.proposer_signature

    err = _verify_champion_proposal_signature(body)
    assert err is not None
    assert "Signer mismatch" in err


# ── Rate limit ─────────────────────────────────────────────────────────────

def test_rate_limit_first_passes(monkeypatch):
    monkeypatch.delenv("CHAMPION_PROPOSAL_MIN_INTERVAL_SECONDS", raising=False)
    _CHAMPION_PROPOSAL_LAST_SEEN.clear()
    body = _make_body(proposer="0x" + "aa" * 20, round_id="r1")
    assert _champion_proposal_rate_limit_check(body) is None


def test_rate_limit_second_within_window_rejected(monkeypatch):
    monkeypatch.setenv("CHAMPION_PROPOSAL_MIN_INTERVAL_SECONDS", "10")
    _CHAMPION_PROPOSAL_LAST_SEEN.clear()
    body = _make_body(proposer="0x" + "bb" * 20, round_id="r2")
    assert _champion_proposal_rate_limit_check(body) is None
    err = _champion_proposal_rate_limit_check(body)
    assert err is not None
    assert "rate-limited" in err


def test_rate_limit_can_be_disabled(monkeypatch):
    monkeypatch.setenv("CHAMPION_PROPOSAL_MIN_INTERVAL_SECONDS", "0")
    _CHAMPION_PROPOSAL_LAST_SEEN.clear()
    body = _make_body(proposer="0x" + "cc" * 20, round_id="r3")
    # Many rapid calls pass.
    for _ in range(5):
        assert _champion_proposal_rate_limit_check(body) is None


def test_rate_limit_segments_by_signer_and_round(monkeypatch):
    monkeypatch.setenv("CHAMPION_PROPOSAL_MIN_INTERVAL_SECONDS", "10")
    _CHAMPION_PROPOSAL_LAST_SEEN.clear()

    b1 = _make_body(proposer="0x" + "dd" * 20, round_id="r4")
    b2 = _make_body(proposer="0x" + "ee" * 20, round_id="r4")  # different signer
    b3 = _make_body(proposer="0x" + "dd" * 20, round_id="r5")  # different round

    assert _champion_proposal_rate_limit_check(b1) is None
    assert _champion_proposal_rate_limit_check(b2) is None
    assert _champion_proposal_rate_limit_check(b3) is None
    # Now re-sending b1 immediately is blocked.
    assert _champion_proposal_rate_limit_check(b1) is not None
