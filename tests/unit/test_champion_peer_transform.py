"""Tests for the champion-consensus peer URL transform.

Champion-consensus broadcasts must target each validator's api port (:8080),
not its metagraph daemon axon (:9100, which does not serve the champion
routes). This is implemented via an optional ``peer_url_transform`` on
``ValidatorPeerNetwork`` (applied only to the protocol_config discovery
branch, never to pinned ``_peers_override`` URLs) plus the
``_champion_axon_to_api_url`` helper in ``api/startup.py``.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from minotaur_subnet.consensus.peer_network import (
    PeerEndpoint,
    ValidatorPeerNetwork,
)


def _proto_config(*peers):
    """Build a fake protocol_config exposing .peers with evm_address/axon_url."""
    return SimpleNamespace(peers=list(peers))


def _discovered_peer(evm_address: str, axon_url: str):
    return SimpleNamespace(evm_address=evm_address, axon_url=axon_url)


# ── ValidatorPeerNetwork.peer_url_transform ─────────────────────────────────

def test_transform_rewrites_only_discovery_branch():
    """Transform rewrites discovered axons but leaves pinned URLs verbatim."""
    me = "0xself"
    discovered = _proto_config(
        _discovered_peer("0xAAA", "http://peer-a:9100"),
        _discovered_peer("0xBBB", "http://peer-b:9100"),
    )
    # Pin one peer verbatim (operator-pinned :8080 URL must NOT be rewritten,
    # and must NOT be retargeted to :9999 by the transform).
    pinned = [PeerEndpoint(validator_id="0xCCC", url="http://pinned-c:8080")]

    net = ValidatorPeerNetwork(
        validator_id=me,
        private_key="0x" + "1" * 64,
        consensus=MagicMock(),
        peers=pinned,
        protocol_config=discovered,
        peer_url_transform=lambda u: u.replace(":9100", ":8080"),
    )

    by_id = {p.validator_id: p.url for p in net.peers}
    # Pinned URL untouched.
    assert by_id["0xCCC"] == "http://pinned-c:8080"
    # Discovered URLs rewritten :9100 -> :8080.
    assert by_id["0xAAA"] == "http://peer-a:8080"
    assert by_id["0xBBB"] == "http://peer-b:8080"


def test_transform_none_is_byte_identical():
    """peer_url_transform=None must produce the exact same discovered URLs."""
    me = "0xself"
    discovered = _proto_config(
        _discovered_peer("0xAAA", "http://peer-a:9100"),
        _discovered_peer("0xBBB", "http://peer-b:9100"),
    )

    baseline = ValidatorPeerNetwork(
        validator_id=me,
        private_key="0x" + "1" * 64,
        consensus=MagicMock(),
        protocol_config=discovered,
    )
    with_none = ValidatorPeerNetwork(
        validator_id=me,
        private_key="0x" + "1" * 64,
        consensus=MagicMock(),
        protocol_config=discovered,
        peer_url_transform=None,
    )

    base_map = {p.validator_id: p.url for p in baseline.peers}
    none_map = {p.validator_id: p.url for p in with_none.peers}
    assert base_map == none_map
    assert none_map == {
        "0xAAA": "http://peer-a:9100",
        "0xBBB": "http://peer-b:9100",
    }


def test_pinned_override_branch_never_transformed():
    """Even with a transform set, a pure pinned-override network is verbatim."""
    pinned = [
        PeerEndpoint(validator_id="0xCCC", url="http://pinned-c:9100"),
    ]
    net = ValidatorPeerNetwork(
        validator_id="0xself",
        private_key="0x" + "1" * 64,
        consensus=MagicMock(),
        peers=pinned,
        protocol_config=None,
        peer_url_transform=lambda u: u.replace(":9100", ":8080"),
    )
    # Operators pin verbatim :8080/:9100 URLs — the transform must not touch
    # the override branch.
    assert {p.validator_id: p.url for p in net.peers} == {
        "0xCCC": "http://pinned-c:9100",
    }


# ── _champion_axon_to_api_url ───────────────────────────────────────────────

def test_champion_axon_to_api_url_default_port():
    from minotaur_subnet.api.startup import _champion_axon_to_api_url

    assert (
        _champion_axon_to_api_url("http://1.2.3.4:9100")
        == "http://1.2.3.4:8080"
    )


def test_champion_axon_to_api_url_honors_env_port(monkeypatch):
    from minotaur_subnet.api.startup import _champion_axon_to_api_url

    monkeypatch.setenv("CHAMPION_CONSENSUS_PEER_PORT", "8085")
    assert (
        _champion_axon_to_api_url("http://1.2.3.4:9100")
        == "http://1.2.3.4:8085"
    )


def test_champion_axon_to_api_url_preserves_path_and_scheme(monkeypatch):
    from minotaur_subnet.api.startup import _champion_axon_to_api_url

    monkeypatch.delenv("CHAMPION_CONSENSUS_PEER_PORT", raising=False)
    # hostname-only axon, no explicit port still gets the api port + scheme.
    assert (
        _champion_axon_to_api_url("https://host.internal/identity")
        == "https://host.internal:8080/identity"
    )


def test_champion_axon_to_api_url_unparseable_returns_input(monkeypatch):
    from minotaur_subnet.api.startup import _champion_axon_to_api_url

    # A non-int env port forces int() to raise inside the try → original
    # string is returned unchanged (fail-safe, never None).
    monkeypatch.setenv("CHAMPION_CONSENSUS_PEER_PORT", "not-a-number")
    original = "http://1.2.3.4:9100"
    assert _champion_axon_to_api_url(original) == original
