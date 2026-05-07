"""Unit tests for MetagraphSync, PeerInfo, and leader election."""

import asyncio
import time
from unittest.mock import MagicMock, patch

import pytest

from minotaur_subnet.validator.metagraph_sync import (
    MetagraphSync,
    MetagraphState,
    PeerInfo,
    SubnetEpochInfo,
    elect_leader,
    _hotkey_to_evm,
    _build_subnet_epoch_info,
    _resolve_subnet_epoch_info,
)


# ── Leader Election Tests ────────────────────────────────────────────────────


class TestElectLeader:
    """Test deterministic leader election."""

    def test_highest_stake_wins(self):
        peers = [
            PeerInfo(uid=0, hotkey="hk_a", stake=100.0, evm_address="0xaaa"),
            PeerInfo(uid=1, hotkey="hk_b", stake=200.0, evm_address="0xbbb"),
            PeerInfo(uid=2, hotkey="hk_c", stake=50.0, evm_address="0xccc"),
        ]
        leader = elect_leader(peers)
        assert leader is not None
        assert leader.hotkey == "hk_b"
        assert leader.stake == 200.0

    def test_tiebreaker_by_hotkey_ascending(self):
        peers = [
            PeerInfo(uid=0, hotkey="hk_c", stake=100.0, evm_address="0xccc"),
            PeerInfo(uid=1, hotkey="hk_a", stake=100.0, evm_address="0xaaa"),
            PeerInfo(uid=2, hotkey="hk_b", stake=100.0, evm_address="0xbbb"),
        ]
        leader = elect_leader(peers)
        assert leader is not None
        assert leader.hotkey == "hk_a"  # Lexicographically smallest

    def test_no_staked_peers_returns_none(self):
        peers = [
            PeerInfo(uid=0, hotkey="hk_a", stake=0.0, evm_address="0xaaa"),
            PeerInfo(uid=1, hotkey="hk_b", stake=0.0, evm_address="0xbbb"),
        ]
        leader = elect_leader(peers)
        assert leader is None

    def test_empty_peer_list(self):
        leader = elect_leader([])
        assert leader is None

    def test_single_staked_peer(self):
        peers = [
            PeerInfo(uid=0, hotkey="hk_a", stake=0.0, evm_address="0xaaa"),
            PeerInfo(uid=1, hotkey="hk_b", stake=50.0, evm_address="0xbbb"),
        ]
        leader = elect_leader(peers)
        assert leader is not None
        assert leader.hotkey == "hk_b"

    def test_deterministic_across_calls(self):
        """Same input always produces same leader."""
        peers = [
            PeerInfo(uid=0, hotkey="hk_a", stake=100.0, evm_address="0xaaa"),
            PeerInfo(uid=1, hotkey="hk_b", stake=100.0, evm_address="0xbbb"),
        ]
        results = [elect_leader(peers) for _ in range(10)]
        assert all(r.hotkey == results[0].hotkey for r in results)

    def test_leader_changes_when_stake_changes(self):
        peers = [
            PeerInfo(uid=0, hotkey="hk_a", stake=100.0, evm_address="0xaaa"),
            PeerInfo(uid=1, hotkey="hk_b", stake=200.0, evm_address="0xbbb"),
        ]
        leader1 = elect_leader(peers)
        assert leader1.hotkey == "hk_b"

        # B loses stake, A becomes leader
        peers[1] = PeerInfo(uid=1, hotkey="hk_b", stake=50.0, evm_address="0xbbb")
        leader2 = elect_leader(peers)
        assert leader2.hotkey == "hk_a"


# ── PeerInfo Tests ────────────────────────────────────────────────────────────


class TestPeerInfo:
    def test_basic_construction(self):
        p = PeerInfo(uid=0, hotkey="5Grw...", stake=100.0, evm_address="0xabc")
        assert p.uid == 0
        assert p.hotkey == "5Grw..."
        assert p.stake == 100.0
        assert p.axon_url == ""

    def test_with_axon_url(self):
        p = PeerInfo(uid=0, hotkey="5Grw...", stake=100.0, evm_address="0xabc",
                     axon_url="http://1.2.3.4:9100")
        assert p.axon_url == "http://1.2.3.4:9100"


# ── MetagraphState Tests ─────────────────────────────────────────────────────


class TestMetagraphState:
    def test_frozen_leader(self):
        peers = [PeerInfo(uid=0, hotkey="hk_a", stake=100.0, evm_address="0xaaa")]
        state = MetagraphState(
            block=1,
            peers=peers,
            validators=peers,
            leader=peers[0],
            my_uid=0,
            my_role="leader",
        )
        assert state.leader.hotkey == "hk_a"
        assert state.my_role == "leader"
        assert state.block == 1

    def test_unregistered_role(self):
        state = MetagraphState(
            block=1, peers=[], validators=[], leader=None,
            my_uid=None, my_role="unregistered",
        )
        assert state.my_role == "unregistered"
        assert state.my_uid is None


class TestSubnetEpochInfo:
    def test_build_subnet_epoch_info_uses_tempo_plus_one_cycle(self):
        info = _build_subnet_epoch_info(
            block=725,
            tempo_blocks=360,
            blocks_since_last_step=5,
        )
        assert info == SubnetEpochInfo(
            tempo_blocks=360,
            epoch_length_blocks=361,
            blocks_since_last_step=5,
            last_step_block=720,
            epoch_index=1,
        )

    def test_resolve_subnet_epoch_info_from_direct_queries(self):
        subtensor = MagicMock()

        def _query_subtensor(*args, **kwargs):
            name = kwargs.get("name") or (args[0] if args else "")
            if name == "Tempo":
                return 360
            if name == "BlocksSinceLastStep":
                return 5
            return None

        subtensor.query_subtensor.side_effect = _query_subtensor
        subtensor.blocks_since_last_step.return_value = 5
        # Prevent MagicMock fallback attributes from leaking into
        # _resolve_subnet_epoch_info's subnet_info getter loop.
        # int(MagicMock()) returns 1, which corrupts last_step_block.
        subtensor.get_subnet_info = None
        subtensor.get_all_subnets_info = None
        subtensor.all_subnets = None
        subtensor.get_subnet_hyperparameters = None

        info = _resolve_subnet_epoch_info(subtensor, netuid=1, block=725)
        assert info is not None
        assert info.tempo_blocks == 360
        assert info.epoch_length_blocks == 361
        assert info.last_step_block == 720
        assert info.epoch_index == 1


# ── EVM Derivation Tests ─────────────────────────────────────────────────────


class TestHotkeyToEvm:
    def test_returns_valid_hex(self):
        addr = _hotkey_to_evm("5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY")
        assert addr.startswith("0x")
        assert len(addr) == 42

    def test_deterministic(self):
        addr1 = _hotkey_to_evm("5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY")
        addr2 = _hotkey_to_evm("5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY")
        assert addr1 == addr2

    def test_different_hotkeys_different_addresses(self):
        addr1 = _hotkey_to_evm("hotkey_a")
        addr2 = _hotkey_to_evm("hotkey_b")
        assert addr1 != addr2


# ── MetagraphSync Tests ──────────────────────────────────────────────────────


class TestMetagraphSync:
    def test_init_defaults(self):
        ms = MetagraphSync(
            subtensor_url="ws://localhost:9944",
            netuid=1,
            my_hotkey="5Grw...",
        )
        assert ms.state is None
        assert ms.is_leader is False
        assert ms.poll_interval == 60.0

    def test_is_leader_when_no_state(self):
        ms = MetagraphSync("ws://localhost:9944", 1, "5Grw...")
        assert ms.is_leader is False

    def test_is_leader_with_leader_state(self):
        ms = MetagraphSync("ws://localhost:9944", 1, "5Grw...")
        peers = [PeerInfo(uid=0, hotkey="5Grw...", stake=100.0, evm_address="0xaaa")]
        ms._state = MetagraphState(
            block=1, peers=peers, validators=peers, leader=peers[0],
            my_uid=0, my_role="leader",
        )
        assert ms.is_leader is True

    def test_is_follower_with_follower_state(self):
        ms = MetagraphSync("ws://localhost:9944", 1, "my_hotkey")
        peers = [
            PeerInfo(uid=0, hotkey="other_hotkey", stake=200.0, evm_address="0xbbb"),
            PeerInfo(uid=1, hotkey="my_hotkey", stake=100.0, evm_address="0xaaa"),
        ]
        ms._state = MetagraphState(
            block=1, peers=peers, validators=peers, leader=peers[0],
            my_uid=1, my_role="follower",
        )
        assert ms.is_leader is False
