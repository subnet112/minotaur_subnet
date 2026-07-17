"""B3: benchmark_anchor_epoch is plumbed through the PROPOSAL and CERTIFY paths.

This closes the last PACK_HASH_MISMATCH gap for quorum>1: a follower that adopts a
round via a proposal/certify (because it missed the close broadcast) now anchors its
fork pin to the leader's real round-open epoch instead of falling back to opened_epoch.

Adding the field to the proposal model is signature-safe ONLY because B2 moved
proposal-sig verification to the raw wire dict — B3 MUST ship after B1+B2 are fleet-wide.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from minotaur_subnet.api.routes.submissions.models import (
    CertifyRoundRequest,
    ChampionConsensusProposalRequest,
)
from minotaur_subnet.api.routes.submissions.round_manager import (
    _certify_round_sync_payload,
)
from minotaur_subnet.harness.round_store import RoundState, RoundStatus


def _proposal(**ov):
    base = dict(
        round_id="round-e100-n1", committee_hash="c", incumbent_image_id=None,
        candidate_submission_id="sub_x", candidate_image_id="a" * 64,
        benchmark_pack_hash="pack", shadow_case_log_hash=None, effective_epoch=6,
        commit_hash="c" * 40, nonce=1, deadline=2,
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


# ── models accept the field ──────────────────────────────────────────────────

def test_certify_request_accepts_anchor():
    assert CertifyRoundRequest(round_id="r", effective_epoch=6,
                               benchmark_anchor_epoch=77).benchmark_anchor_epoch == 77
    # absent on a pre-B3 leader → None (follower falls back to opened_epoch)
    assert CertifyRoundRequest(round_id="r", effective_epoch=6).benchmark_anchor_epoch is None


def test_proposal_request_accepts_anchor():
    body = ChampionConsensusProposalRequest(
        round_id="r", candidate_submission_id="s", candidate_image_id="i",
        effective_epoch=6, benchmark_anchor_epoch=77)
    assert body.benchmark_anchor_epoch == 77
    assert ChampionConsensusProposalRequest(
        round_id="r", candidate_submission_id="s", candidate_image_id="i",
        effective_epoch=6).benchmark_anchor_epoch is None


# ── payload builders carry the field ─────────────────────────────────────────

def test_certify_sync_payload_carries_anchor():
    state = RoundState(round_id="round-e100-n1", status=RoundStatus.CERTIFIED,
                       opened_epoch=100, effective_epoch=6, benchmark_anchor_epoch=77)
    payload = _certify_round_sync_payload(state)
    assert payload["benchmark_anchor_epoch"] == 77
    # and it round-trips through the request model the follower parses
    body = CertifyRoundRequest(**{k: v for k, v in payload.items()
                                  if k in CertifyRoundRequest.model_fields})
    assert body.benchmark_anchor_epoch == 77


def test_proposal_payload_carries_anchor():
    net, _ = _peer_net()
    payload = net._build_champion_proposal_payload(_proposal(), benchmark_anchor_epoch=77)
    assert payload["benchmark_anchor_epoch"] == 77
    body = ChampionConsensusProposalRequest(**{k: v for k, v in payload.items()
                                               if k in ChampionConsensusProposalRequest.model_fields})
    assert body.benchmark_anchor_epoch == 77


def test_proposal_signed_roundtrip_preserves_anchor(monkeypatch):
    """Leader builds+signs the proposal WITH the anchor; the follower's verify passes and
    the parsed model carries it. (In this pre-B2 worktree the model_dump verifier already
    round-trips because the model now declares the field; post-B2 it verifies the raw dict.)"""
    monkeypatch.setenv("CONSENSUS_REQUIRE_SIGNED_CHAMPION_PROPOSALS", "1")
    monkeypatch.delenv("CONSENSUS_ENFORCE_ONCHAIN_REGISTRY", raising=False)
    from minotaur_subnet.validator import metagraph_sync as ms
    monkeypatch.setattr(ms, "LOCKED_LEADER_EVM_ADDRESS", "")
    from minotaur_subnet.api.routes.submissions.routes import (
        _verify_champion_proposal_signature,
    )

    net, _acct = _peer_net()
    payload = net._build_champion_proposal_payload(
        _proposal(), close_epoch=7, quorum_required=2, benchmark_anchor_epoch=77)
    assert payload["benchmark_anchor_epoch"] == 77 and payload.get("proposer_signature")
    body = ChampionConsensusProposalRequest(**payload)
    # verifier signature: pre-B2 takes the model, post-B2 the raw dict — accept either.
    import inspect
    arg = body if "body" in inspect.signature(_verify_champion_proposal_signature).parameters else payload
    assert _verify_champion_proposal_signature(arg) is None
    assert body.benchmark_anchor_epoch == 77


# ── adopt path forwards the field ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_prepare_forwards_anchor_to_adopt(monkeypatch):
    from minotaur_subnet.api.routes.submissions import champion_consensus as cc

    captured: dict = {}
    monkeypatch.setattr(
        cc, "_adopt_leader_round_if_behind",
        lambda round_id, **kw: captured.update(kw),
    )
    # First get_round → None (behind → adopt), then a CLOSED state so prepare returns.
    state = RoundState(round_id="r", status=RoundStatus.CLOSED, opened_epoch=1,
                       benchmark_anchor_epoch=77)
    seq = [None, state, state]
    fake_store = SimpleNamespace(
        get_round=lambda rid: seq.pop(0) if seq else state,
        get_current_round=lambda: None,
    )
    monkeypatch.setattr(cc, "get_round_store", lambda: fake_store)

    await cc._maybe_prepare_round_for_certification(
        "r", close_epoch=1, effective_epoch=6, benchmark_anchor_epoch=77,
    )
    assert captured.get("benchmark_anchor_epoch") == 77
