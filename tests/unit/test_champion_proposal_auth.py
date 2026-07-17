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
    err = _verify_champion_proposal_signature(body.model_dump())
    assert err is not None
    assert "Missing proposer" in err


def test_sig_verify_env_opt_out_now_ignored(monkeypatch):
    """The CONSENSUS_REQUIRE_SIGNED_CHAMPION_PROPOSALS=0 bypass is removed.

    The EIP-712 signature is now the sole cross-validator auth (the shared
    internal API key gate was dropped from this route), so an unsigned
    proposal must STILL be rejected even with the legacy opt-out env set.
    """
    monkeypatch.setenv("CONSENSUS_REQUIRE_SIGNED_CHAMPION_PROPOSALS", "0")
    body = _make_body()  # empty proposer/signature
    err = _verify_champion_proposal_signature(body.model_dump())
    assert err is not None
    assert "Missing proposer" in err


def test_sig_verify_on_rejects_missing(monkeypatch):
    monkeypatch.setenv("CONSENSUS_REQUIRE_SIGNED_CHAMPION_PROPOSALS", "1")
    body = _make_body(proposer="", proposer_signature="")
    err = _verify_champion_proposal_signature(body.model_dump())
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

    assert _verify_champion_proposal_signature(body.model_dump()) is None


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

    assert _verify_champion_proposal_signature(body.model_dump()) is None


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

    err = _verify_champion_proposal_signature(body.model_dump())
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

    err = _verify_champion_proposal_signature(body.model_dump())
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


# ── End-to-end sign → parse → verify (the real quorum path) ──────────────────
# The tests above sign over body.model_dump() directly, so they CANNOT catch a
# mismatch between the leader's signed payload (PeerNetwork raw dict) and the
# verifier's canonical (model_dump). These exercise the real path.

def _proposal(**ov):
    from types import SimpleNamespace
    base = dict(
        round_id="round-1", committee_hash="committee", incumbent_image_id=None,
        candidate_submission_id="sub_x", candidate_image_id="a" * 64,
        benchmark_pack_hash="pack", shadow_case_log_hash=None, effective_epoch=6,
        commit_hash="c" * 40, nonce=123, deadline=456,
    )
    base.update(ov)
    return SimpleNamespace(**base)


def _peer_net():
    from eth_account import Account
    from minotaur_subnet.consensus.peer_network import ValidatorPeerNetwork
    acct = Account.create()
    net = ValidatorPeerNetwork(validator_id=acct.address, private_key=acct.key.hex(),
                               consensus=MagicMock(), peers=[])
    return net, acct


def test_champion_proposal_sign_then_verify_raw_wire_dict(monkeypatch, unlock_leader):
    """B2: leader builds+signs via PeerNetwork (raw dict); follower verifies over the
    RAW WIRE dict (request.json()), NOT the parsed model. This is the exact live quorum
    path — verifying the bytes the leader actually signed."""
    monkeypatch.setenv("CONSENSUS_REQUIRE_SIGNED_CHAMPION_PROPOSALS", "1")
    monkeypatch.delenv("CONSENSUS_ENFORCE_ONCHAIN_REGISTRY", raising=False)

    net, _acct = _peer_net()
    payload = net._build_champion_proposal_payload(
        _proposal(), close_epoch=7, quorum_required=2,
        decision_deadline_epoch=8, committee_block=9,
    )
    assert payload.get("proposer_signature")                    # the leader signed it
    assert _verify_champion_proposal_signature(payload) is None  # verify the raw wire dict


def test_raw_dict_verify_tolerates_non_model_field(monkeypatch, unlock_leader):
    """B2 DUAL-VERIFY PROOF (the inverse of the old structural guard): a signed-payload
    key that is NOT a ChampionConsensusProposalRequest field NO LONGER breaks verification
    — the follower verifies the raw wire bytes, so the leader can add a new field (B3's
    benchmark_anchor_epoch) without a staggered-rollout auth break. The old model_dump
    verifier would drop such a key and fail 'Signer mismatch' (the #378 outage)."""
    monkeypatch.setenv("CONSENSUS_REQUIRE_SIGNED_CHAMPION_PROPOSALS", "1")
    monkeypatch.delenv("CONSENSUS_ENFORCE_ONCHAIN_REGISTRY", raising=False)
    from eth_account import Account
    from eth_account.messages import encode_defunct
    from minotaur_subnet.api.routes.submissions.models import ChampionConsensusProposalRequest

    acct = Account.create()
    payload = {
        "round_id": "round-1", "proposer": acct.address,
        # a field the current request model does NOT declare — mimics a future B3 field
        # (or the #378 `timestamp`) present in the SIGNED canonical:
        "benchmark_anchor_epoch": 42,
    }
    assert "benchmark_anchor_epoch" not in ChampionConsensusProposalRequest.model_fields
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    payload["proposer_signature"] = Account.sign_message(
        encode_defunct(text=canonical), private_key=acct.key,
    ).signature.hex()
    # Raw-dict verify PASSES despite the non-model field (the model_dump path would fail).
    assert _verify_champion_proposal_signature(payload) is None
