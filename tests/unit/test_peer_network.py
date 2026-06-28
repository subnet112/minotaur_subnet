"""Unit tests for ValidatorPeerNetwork and peer parsing."""

import asyncio
import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from minotaur_subnet.consensus.peer_network import (
    ValidatorPeerNetwork,
    PeerEndpoint,
    parse_peers_env,
)
from minotaur_subnet.shared.types import SignedApproval


# ── PeerEndpoint Tests ────────────────────────────────────────────────────────


class TestPeerEndpoint:
    def test_construction(self):
        p = PeerEndpoint(validator_id="0xabc", url="http://host:9100")
        assert p.validator_id == "0xabc"
        assert p.url == "http://host:9100"


# ── parse_peers_env Tests ─────────────────────────────────────────────────────


class TestParsePeersEnv:
    def test_empty_string(self):
        assert parse_peers_env("") == []
        assert parse_peers_env("   ") == []

    def test_single_peer(self):
        peers = parse_peers_env("0xabc@http://host:9100")
        assert len(peers) == 1
        assert peers[0].validator_id == "0xabc"
        assert peers[0].url == "http://host:9100"

    def test_multiple_peers(self):
        peers = parse_peers_env("0xabc@http://host1:9100,0xdef@http://host2:9100")
        assert len(peers) == 2
        assert peers[0].validator_id == "0xabc"
        assert peers[1].validator_id == "0xdef"

    def test_whitespace_handling(self):
        peers = parse_peers_env("  0xabc@http://host1:9100 , 0xdef@http://host2:9100  ")
        assert len(peers) == 2
        assert peers[0].validator_id == "0xabc"
        assert peers[1].url == "http://host2:9100"

    def test_invalid_entry_skipped(self):
        peers = parse_peers_env("0xabc@http://host:9100,invalid_no_at")
        assert len(peers) == 1
        assert peers[0].validator_id == "0xabc"

    def test_empty_entries_skipped(self):
        peers = parse_peers_env("0xabc@http://host:9100,,")
        assert len(peers) == 1


# ── ValidatorPeerNetwork Tests ────────────────────────────────────────────────


class TestValidatorPeerNetwork:
    def test_init_defaults(self):
        consensus = MagicMock()
        vpn = ValidatorPeerNetwork(
            validator_id="0xabc",
            private_key="0x1234",
            consensus=consensus,
        )
        assert vpn.validator_id == "0xabc"
        assert vpn.peers == []
        assert vpn.timeout == 10.0

    def test_request_headers_include_default_headers(self):
        consensus = MagicMock()
        vpn = ValidatorPeerNetwork(
            validator_id="0xabc",
            private_key="0x1234",
            consensus=consensus,
            default_headers={"x-solver-round-internal-key": "secret"},
        )
        headers = vpn._request_headers()
        assert headers["Content-Type"] == "application/json"
        assert headers["x-solver-round-internal-key"] == "secret"

    def test_set_peers_excludes_self(self):
        consensus = MagicMock()
        vpn = ValidatorPeerNetwork(
            validator_id="0xabc",
            private_key="0x1234",
            consensus=consensus,
        )
        vpn.set_peers([
            PeerEndpoint(validator_id="0xabc", url="http://self:9100"),
            PeerEndpoint(validator_id="0xdef", url="http://peer:9100"),
        ])
        assert len(vpn.peers) == 1
        assert vpn.peers[0].validator_id == "0xdef"

    def test_set_peers_empty(self):
        consensus = MagicMock()
        vpn = ValidatorPeerNetwork(
            validator_id="0xabc",
            private_key="0x1234",
            consensus=consensus,
        )
        vpn.set_peers([])
        assert vpn.peers == []

    @pytest.mark.asyncio
    async def test_broadcast_no_peers(self):
        consensus = MagicMock()
        vpn = ValidatorPeerNetwork(
            validator_id="0xabc",
            private_key="0x1234",
            consensus=consensus,
        )
        result = await vpn.broadcast_proposal(
            order_id="order1",
            plan=MagicMock(interactions=[], intent_id="test", deadline=0, nonce=0, metadata={}),
            score=0.8,
            plan_hash="0x1234",
        )
        assert result == []

    def test_build_proposal_payload(self):
        consensus = MagicMock()
        vpn = ValidatorPeerNetwork(
            validator_id="0xabc",
            private_key="0x1234",
            consensus=consensus,
        )

        plan = MagicMock()
        plan.intent_id = "test_intent"
        plan.interactions = []
        plan.deadline = 12345
        plan.nonce = 1
        plan.metadata = {"key": "value"}

        payload = vpn._build_proposal_payload(
            order_id="order1",
            plan=plan,
            score=0.85,
            plan_hash="0xabcd",
            app_id="app1",
        )

        assert payload["order_id"] == "order1"
        assert payload["score"] == 0.85
        assert payload["plan_hash"] == "0xabcd"
        assert payload["app_id"] == "app1"
        assert payload["proposer"] == "0xabc"
        assert payload["plan"]["intent_id"] == "test_intent"
        assert payload["plan"]["deadline"] == 12345

    def test_build_proposal_payload_no_interactions(self):
        consensus = MagicMock()
        vpn = ValidatorPeerNetwork(
            validator_id="0xabc",
            private_key="0x1234",
            consensus=consensus,
        )

        # Object without interactions attribute
        plan = "not_a_plan"
        payload = vpn._build_proposal_payload(
            "order1", plan, 0.5, "0x1234", "app1",
        )
        assert payload["plan"] == {}

    def test_build_champion_proposal_payload(self):
        consensus = MagicMock()
        vpn = ValidatorPeerNetwork(
            validator_id="0xabc",
            private_key="0x1234",
            consensus=consensus,
        )
        proposal = MagicMock()
        proposal.round_id = "round-e42-n1"
        proposal.committee_hash = "0x" + "ab" * 32
        proposal.incumbent_image_id = "sha256:" + "1" * 64
        proposal.candidate_submission_id = "sub-final"
        proposal.candidate_image_id = "sha256:" + "2" * 64
        proposal.benchmark_pack_hash = "0x" + "cd" * 32
        proposal.shadow_case_log_hash = "0x" + "ef" * 32
        proposal.effective_epoch = 43

        payload = vpn._build_champion_proposal_payload(
            proposal,
            close_epoch=42,
            quorum_required=2,
            decision_deadline_epoch=44,
            committee_block=123,
        )

        assert payload["round_id"] == "round-e42-n1"
        assert payload["candidate_submission_id"] == "sub-final"
        assert payload["candidate_image_id"] == "sha256:" + "2" * 64
        assert payload["close_epoch"] == 42
        assert payload["quorum_required"] == 2
        assert payload["decision_deadline_epoch"] == 44
        assert payload["committee_block"] == 123

    @pytest.mark.asyncio
    async def test_start_creates_session(self):
        consensus = MagicMock()
        vpn = ValidatorPeerNetwork(
            validator_id="0xabc",
            private_key="0x1234",
            consensus=consensus,
        )
        await vpn.start()
        assert vpn._session is not None
        await vpn.stop()

    @pytest.mark.asyncio
    async def test_stop_closes_session(self):
        consensus = MagicMock()
        vpn = ValidatorPeerNetwork(
            validator_id="0xabc",
            private_key="0x1234",
            consensus=consensus,
        )
        await vpn.start()
        assert vpn._session is not None
        await vpn.stop()
        assert vpn._session is None

    def test_init_with_peers(self):
        consensus = MagicMock()
        peers = [
            PeerEndpoint(validator_id="0xdef", url="http://peer:9100"),
        ]
        vpn = ValidatorPeerNetwork(
            validator_id="0xabc",
            private_key="0x1234",
            consensus=consensus,
            peers=peers,
        )
        assert len(vpn.peers) == 1

    def test_custom_timeout(self):
        consensus = MagicMock()
        vpn = ValidatorPeerNetwork(
            validator_id="0xabc",
            private_key="0x1234",
            consensus=consensus,
            timeout=30.0,
        )
        assert vpn.timeout == 30.0


class TestPeerUrlSource:
    """The peers property prefers a discovered peer's advertised api_url over
    the axon→API port transform (the order-sync leader-URL fix)."""

    def _net(self, discovered_peers):
        from types import SimpleNamespace
        return ValidatorPeerNetwork(
            validator_id="0xSelf",
            private_key="0x1234",
            consensus=MagicMock(),
            protocol_config=SimpleNamespace(peers=discovered_peers),
            # mimic the prod axon(:9100) → api(:8080) transform
            peer_url_transform=lambda axon: axon.replace(":9100", ":8080"),
        )

    def test_advertised_api_url_wins_over_transform(self):
        from minotaur_subnet.consensus.peer_discovery import PeerInfo
        vpn = self._net([
            PeerInfo(evm_address="0xPeer1", hotkey="hk1",
                     axon_url="http://1.2.3.4:9100",
                     api_url="https://api.example.com"),
        ])
        urls = {p.validator_id: p.url for p in vpn.peers}
        assert urls["0xPeer1"] == "https://api.example.com"

    def test_falls_back_to_transform_without_api_url(self):
        from minotaur_subnet.consensus.peer_discovery import PeerInfo
        vpn = self._net([
            PeerInfo(evm_address="0xPeer2", hotkey="hk2",
                     axon_url="http://5.6.7.8:9100"),  # no api_url advertised
        ])
        urls = {p.validator_id: p.url for p in vpn.peers}
        assert urls["0xPeer2"] == "http://5.6.7.8:8080"


# ── Restart recovery: fleet-aborted detection in champion broadcast ──────────
# After a restart the leader can replay a round the fleet already aborted, then
# loop a doomed cert until the deadline. broadcast_champion_proposal now exposes
# `last_champion_broadcast["fleet_aborted"]` so the coordinator can abort+advance.

import asyncio as _asyncio
from types import SimpleNamespace as _SNS
from unittest.mock import AsyncMock as _AsyncMock

from minotaur_subnet.consensus.peer_network import ValidatorPeerNetwork as _VPN, PeerEndpoint as _PE


def _champ_vpn(monkeypatch, send_impl):
    peers = [_PE(validator_id="0xp1", url="http://p1:9100"),
             _PE(validator_id="0xp2", url="http://p2:9100")]
    vpn = _VPN(validator_id="0xself", private_key="", consensus=None, peers=peers)
    vpn._session = _SNS(closed=False)                      # skip real start()/HTTP
    monkeypatch.setattr(vpn, "start", _AsyncMock())
    monkeypatch.setattr(vpn, "_build_champion_proposal_payload",
                        lambda *a, **k: {"round_id": "round-1"})
    monkeypatch.setattr(vpn, "_send_champion_proposal", send_impl)
    return vpn


def test_broadcast_fleet_aborted_when_all_responders_round_wrong_state(monkeypatch):
    async def _send(peer, payload, *, path, dissent_sink=None):
        if dissent_sink is not None:
            dissent_sink.append("ROUND_WRONG_STATE")   # the fleet holds an aborted round
        return None
    vpn = _champ_vpn(monkeypatch, _send)
    approvals = _asyncio.run(vpn.broadcast_champion_proposal(_SNS(round_id="round-1")))
    assert approvals == []
    assert vpn.last_champion_broadcast["fleet_aborted"] is True
    assert vpn.last_champion_broadcast["round_id"] == "round-1"


def test_broadcast_not_fleet_aborted_when_an_approval_exists(monkeypatch):
    async def _send(peer, payload, *, path, dissent_sink=None):
        if peer.validator_id == "0xp1":
            return object()                              # an approval
        if dissent_sink is not None:
            dissent_sink.append("ROUND_WRONG_STATE")
        return None
    vpn = _champ_vpn(monkeypatch, _send)
    approvals = _asyncio.run(vpn.broadcast_champion_proposal(_SNS(round_id="round-1")))
    assert len(approvals) == 1
    assert vpn.last_champion_broadcast["fleet_aborted"] is False


def test_broadcast_not_fleet_aborted_on_mixed_dissents(monkeypatch):
    codes = iter(["ROUND_WRONG_STATE", "SCORE_BELOW_THRESHOLD"])
    async def _send(peer, payload, *, path, dissent_sink=None):
        if dissent_sink is not None:
            dissent_sink.append(next(codes))             # one wrong-state, one real disagreement
        return None
    vpn = _champ_vpn(monkeypatch, _send)
    _asyncio.run(vpn.broadcast_champion_proposal(_SNS(round_id="round-1")))
    # a non-stale dissent reason means real disagreement, not a stale round → don't trip
    assert vpn.last_champion_broadcast["fleet_aborted"] is False


def test_broadcast_not_fleet_aborted_on_network_errors_only(monkeypatch):
    async def _send(peer, payload, *, path, dissent_sink=None):
        raise ConnectionError("peer down")               # network error, not a dissent
    vpn = _champ_vpn(monkeypatch, _send)
    _asyncio.run(vpn.broadcast_champion_proposal(_SNS(round_id="round-1")))
    # unreachable peers are not dissents → no fleet-aborted verdict
    assert vpn.last_champion_broadcast["fleet_aborted"] is False
    assert vpn.last_champion_broadcast["dissent_codes"] == []
