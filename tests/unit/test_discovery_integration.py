"""Integration test: ProtocolConfig.peers drives ConsensusManager + PeerNetwork.

Verifies the read-through wiring: mutating ``protocol_config.peers`` (as
the refresh loop does) propagates to both ConsensusManager.validators and
ValidatorPeerNetwork.peers without restart.
"""

from __future__ import annotations

import pytest
from unittest.mock import MagicMock

from minotaur_subnet.consensus.manager import ConsensusManager
from minotaur_subnet.consensus.peer_discovery import PeerInfo
from minotaur_subnet.consensus.peer_network import (
    PeerEndpoint,
    ValidatorPeerNetwork,
)
from minotaur_subnet.consensus.protocol_config import ProtocolConfig


KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"


def _cfg(quorum_bps: int = 6666, peers: list[PeerInfo] | None = None) -> ProtocolConfig:
    return ProtocolConfig(
        quorum_bps=quorum_bps,
        rpc_url="",
        registry_address="",
        peers=list(peers or []),
    )


def _peer(suffix: int) -> PeerInfo:
    return PeerInfo(
        evm_address=f"0x{'a' * 39}{suffix}",
        hotkey=f"5Hotkey{suffix}",
        axon_url=f"http://peer-{suffix}:9100",
    )


class TestConsensusManagerDiscoveryMode:
    """ConsensusManager.validators reads through ProtocolConfig.peers."""

    def test_initial_peer_set_visible(self):
        cfg = _cfg(peers=[_peer(1), _peer(2)])
        cm = ConsensusManager(
            validator_id="0xself",
            private_key=KEY,
            protocol_config=cfg,
        )
        # discovery mode: validators = self + cfg.peers
        assert cm.validators[0] == "0xself"
        assert len(cm.validators) == 3
        assert all(any(v.endswith(s) for v in cm.validators) for s in ["1", "2"])

    def test_peer_added_at_runtime_is_picked_up(self):
        cfg = _cfg(peers=[_peer(1)])
        cm = ConsensusManager(
            validator_id="0xself",
            private_key=KEY,
            protocol_config=cfg,
        )
        assert len(cm.validators) == 2

        # Simulate discovery loop adding a new peer
        cfg.peers.append(_peer(2))
        assert len(cm.validators) == 3

    def test_peer_removed_at_runtime_drops_out(self):
        cfg = _cfg(peers=[_peer(1), _peer(2)])
        cm = ConsensusManager(
            validator_id="0xself",
            private_key=KEY,
            protocol_config=cfg,
        )
        assert len(cm.validators) == 3

        # Simulate discovery loop dropping a peer (e.g. removed from registry)
        cfg.peers[:] = [_peer(1)]
        assert len(cm.validators) == 2

    def test_explicit_validators_override_pins_set(self):
        cfg = _cfg(peers=[_peer(1), _peer(2)])
        pinned = ["0xself", "0xother"]
        cm = ConsensusManager(
            validator_id="0xself",
            private_key=KEY,
            protocol_config=cfg,
            validators=pinned,
        )
        # Override wins even when discovery has different peers
        assert cm.validators == pinned

        # And it stays pinned across discovery mutations
        cfg.peers.append(_peer(3))
        assert cm.validators == pinned

    def test_quorum_required_recalculates_on_peer_change(self):
        # 50% quorum with 3 total (self + 2 peers) → ceil(1.5) = 2
        cfg = _cfg(quorum_bps=5000, peers=[_peer(1), _peer(2)])
        cm = ConsensusManager(
            validator_id="0xself",
            private_key=KEY,
            protocol_config=cfg,
        )
        assert cm.quorum_required == 2

        # Add another peer: 50% of 4 = 2
        cfg.peers.append(_peer(3))
        assert cm.quorum_required == 2

        # 80% of 4 = 3.2, ceil = 4
        cfg.quorum_bps = 8000
        assert cm.quorum_required == 4


class TestValidatorPeerNetworkDiscoveryMode:
    """ValidatorPeerNetwork.peers reads through ProtocolConfig.peers."""

    def test_initial_peers_from_config(self):
        cfg = _cfg(peers=[_peer(1), _peer(2)])
        net = ValidatorPeerNetwork(
            validator_id="0xself",
            private_key=KEY,
            consensus=MagicMock(),
            protocol_config=cfg,
        )
        urls = [p.url for p in net.peers]
        assert "http://peer-1:9100" in urls
        assert "http://peer-2:9100" in urls

    def test_self_excluded_from_peers(self):
        cfg = _cfg(peers=[
            _peer(1),
            PeerInfo(
                evm_address="0xself",
                hotkey="5Self",
                axon_url="http://self:9100",
            ),
        ])
        net = ValidatorPeerNetwork(
            validator_id="0xself",
            private_key=KEY,
            consensus=MagicMock(),
            protocol_config=cfg,
        )
        # Self-entry filtered out even if present in cfg.peers
        urls = [p.url for p in net.peers]
        assert "http://self:9100" not in urls
        assert "http://peer-1:9100" in urls

    def test_discovery_mutation_visible_without_restart(self):
        cfg = _cfg(peers=[_peer(1)])
        net = ValidatorPeerNetwork(
            validator_id="0xself",
            private_key=KEY,
            consensus=MagicMock(),
            protocol_config=cfg,
        )
        assert len(net.peers) == 1

        # Refresh loop discovers a new peer
        cfg.peers.append(_peer(2))
        assert len(net.peers) == 2

    def test_explicit_peers_override_pins_set(self):
        cfg = _cfg(peers=[_peer(1), _peer(2)])
        pinned = [PeerEndpoint(validator_id="0xpinned", url="http://pin:9100")]
        net = ValidatorPeerNetwork(
            validator_id="0xself",
            private_key=KEY,
            consensus=MagicMock(),
            peers=pinned,
            protocol_config=cfg,
        )
        # Pinned wins
        urls = [p.url for p in net.peers]
        assert urls == ["http://pin:9100"]

        # And discovery mutations don't break it
        cfg.peers.append(_peer(99))
        assert [p.url for p in net.peers] == ["http://pin:9100"]

    def test_no_protocol_config_and_no_peers_returns_empty(self):
        net = ValidatorPeerNetwork(
            validator_id="0xself",
            private_key=KEY,
            consensus=MagicMock(),
        )
        assert net.peers == []
