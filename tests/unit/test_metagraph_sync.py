"""Unit tests for MetagraphSync, PeerInfo, and leader election."""

import asyncio
import time
from unittest.mock import MagicMock, patch

import pytest

from minotaur_subnet.validator import metagraph_sync as metagraph_sync_module
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


@pytest.fixture
def unlock_leader(monkeypatch):
    """Clear LOCKED_LEADER_HOTKEY for tests that exercise stake-based election."""
    monkeypatch.setattr(metagraph_sync_module, "LOCKED_LEADER_HOTKEY", "")
    yield


# ── Leader Election Tests (locked leader) ────────────────────────────────────


class TestElectLeaderLocked:
    """Locked-leader election: only LOCKED_LEADER_HOTKEY is eligible."""

    def test_returns_locked_hotkey_regardless_of_stake(self, monkeypatch):
        monkeypatch.setattr(metagraph_sync_module, "LOCKED_LEADER_HOTKEY", "hk_locked")
        peers = [
            PeerInfo(uid=0, hotkey="hk_a", stake=1_000_000.0, evm_address="0xaaa"),
            PeerInfo(uid=1, hotkey="hk_locked", stake=1.0, evm_address="0xlll"),
            PeerInfo(uid=2, hotkey="hk_c", stake=500.0, evm_address="0xccc"),
        ]
        leader = elect_leader(peers)
        assert leader is not None
        assert leader.hotkey == "hk_locked"

    def test_returns_none_when_locked_hotkey_absent(self, monkeypatch):
        monkeypatch.setattr(metagraph_sync_module, "LOCKED_LEADER_HOTKEY", "hk_locked")
        peers = [
            PeerInfo(uid=0, hotkey="hk_a", stake=100.0, evm_address="0xaaa"),
            PeerInfo(uid=1, hotkey="hk_b", stake=200.0, evm_address="0xbbb"),
        ]
        assert elect_leader(peers) is None

    def test_returns_locked_hotkey_even_with_zero_stake(self, monkeypatch):
        monkeypatch.setattr(metagraph_sync_module, "LOCKED_LEADER_HOTKEY", "hk_locked")
        peers = [
            PeerInfo(uid=0, hotkey="hk_locked", stake=0.0, evm_address="0xlll"),
        ]
        leader = elect_leader(peers)
        assert leader is not None
        assert leader.hotkey == "hk_locked"


# ── Leader Election Tests (stake-based, unlocked) ───────────────────────────


class TestElectLeader:
    """Stake-based election (exercised by clearing the lock)."""

    def test_highest_stake_wins(self, unlock_leader):
        peers = [
            PeerInfo(uid=0, hotkey="hk_a", stake=100.0, evm_address="0xaaa"),
            PeerInfo(uid=1, hotkey="hk_b", stake=200.0, evm_address="0xbbb"),
            PeerInfo(uid=2, hotkey="hk_c", stake=50.0, evm_address="0xccc"),
        ]
        leader = elect_leader(peers)
        assert leader is not None
        assert leader.hotkey == "hk_b"
        assert leader.stake == 200.0

    def test_tiebreaker_by_hotkey_ascending(self, unlock_leader):
        peers = [
            PeerInfo(uid=0, hotkey="hk_c", stake=100.0, evm_address="0xccc"),
            PeerInfo(uid=1, hotkey="hk_a", stake=100.0, evm_address="0xaaa"),
            PeerInfo(uid=2, hotkey="hk_b", stake=100.0, evm_address="0xbbb"),
        ]
        leader = elect_leader(peers)
        assert leader is not None
        assert leader.hotkey == "hk_a"  # Lexicographically smallest

    def test_no_staked_peers_returns_none(self, unlock_leader):
        peers = [
            PeerInfo(uid=0, hotkey="hk_a", stake=0.0, evm_address="0xaaa"),
            PeerInfo(uid=1, hotkey="hk_b", stake=0.0, evm_address="0xbbb"),
        ]
        leader = elect_leader(peers)
        assert leader is None

    def test_empty_peer_list(self, unlock_leader):
        leader = elect_leader([])
        assert leader is None

    def test_single_staked_peer(self, unlock_leader):
        peers = [
            PeerInfo(uid=0, hotkey="hk_a", stake=0.0, evm_address="0xaaa"),
            PeerInfo(uid=1, hotkey="hk_b", stake=50.0, evm_address="0xbbb"),
        ]
        leader = elect_leader(peers)
        assert leader is not None
        assert leader.hotkey == "hk_b"

    def test_deterministic_across_calls(self, unlock_leader):
        """Same input always produces same leader."""
        peers = [
            PeerInfo(uid=0, hotkey="hk_a", stake=100.0, evm_address="0xaaa"),
            PeerInfo(uid=1, hotkey="hk_b", stake=100.0, evm_address="0xbbb"),
        ]
        results = [elect_leader(peers) for _ in range(10)]
        assert all(r.hotkey == results[0].hotkey for r in results)

    def test_leader_changes_when_stake_changes(self, unlock_leader):
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


# ── Locked-leader constants are env-overridable ─────────────────────────────

_PROD_LOCKED_LEADER_HOTKEY = "5E1ohAszHfhyQUEtz6mvCCkW4pYHsinPjxXS938fAZ2jFvCt"
_PROD_LOCKED_LEADER_EVM = "0x34883C5f753AA36f1A9AA5BFCD2f51FaEA1166A5"


class TestLockedLeaderEnvOverride:
    """LOCKED_LEADER_* are module-load constants read from env with the prod
    values as defaults. Tested via importlib.reload under monkeypatched env."""

    def test_unset_env_defaults_to_prod_values(self):
        # The currently-imported module reflects an unset-env load (conftest /
        # test env does not set these) → must equal the hardcoded prod values.
        assert (
            metagraph_sync_module.LOCKED_LEADER_HOTKEY
            == _PROD_LOCKED_LEADER_HOTKEY
        )
        assert (
            metagraph_sync_module.LOCKED_LEADER_EVM_ADDRESS
            == _PROD_LOCKED_LEADER_EVM
        )

    def test_env_override_applied_on_module_load(self, monkeypatch):
        import importlib

        monkeypatch.setenv("LOCKED_LEADER_HOTKEY", "5Hcustomhotkey")
        monkeypatch.setenv("LOCKED_LEADER_EVM_ADDRESS", "0xCustomLeaderAddr")
        try:
            importlib.reload(metagraph_sync_module)
            assert metagraph_sync_module.LOCKED_LEADER_HOTKEY == "5Hcustomhotkey"
            assert (
                metagraph_sync_module.LOCKED_LEADER_EVM_ADDRESS
                == "0xCustomLeaderAddr"
            )
        finally:
            # Restore the default-env module state for other tests.
            monkeypatch.delenv("LOCKED_LEADER_HOTKEY", raising=False)
            monkeypatch.delenv("LOCKED_LEADER_EVM_ADDRESS", raising=False)
            importlib.reload(metagraph_sync_module)

    def test_explicit_empty_env_clears_the_lock(self, monkeypatch):
        import importlib

        monkeypatch.setenv("LOCKED_LEADER_HOTKEY", "")
        monkeypatch.setenv("LOCKED_LEADER_EVM_ADDRESS", "")
        try:
            importlib.reload(metagraph_sync_module)
            assert metagraph_sync_module.LOCKED_LEADER_HOTKEY == ""
            assert metagraph_sync_module.LOCKED_LEADER_EVM_ADDRESS == ""
        finally:
            monkeypatch.delenv("LOCKED_LEADER_HOTKEY", raising=False)
            monkeypatch.delenv("LOCKED_LEADER_EVM_ADDRESS", raising=False)
            importlib.reload(metagraph_sync_module)
