"""Tests for ``ValidatorPeerNetwork.peers`` union-mode semantics.

The property now returns the union of (pinned override) and
(discovered protocol_config peers), deduped by validator_id with the
pinned override winning on conflict. Self is filtered from both
sources.

This enables the deployment pattern where team-internal peers are
pinned via ``ORDER_CONSENSUS_PEERS`` env (they aren't on the
Bittensor metagraph) AND externally-reachable third-party
validators are auto-discovered. Pre-existing mutually-exclusive
behavior is preserved when only one source has entries.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from minotaur_subnet.consensus.peer_network import (
    PeerEndpoint,
    ValidatorPeerNetwork,
)


def _make_vpn(my_id: str = "0xMe", protocol_config=None) -> ValidatorPeerNetwork:
    return ValidatorPeerNetwork(
        validator_id=my_id,
        private_key="0x" + "11" * 32,
        consensus=MagicMock(),
        protocol_config=protocol_config,
    )


def _pc(*peers):
    """Build a minimal protocol_config-like with a peers list. peer_discovery
    returns objects with ``.evm_address`` + ``.axon_url``."""
    return SimpleNamespace(
        peers=[SimpleNamespace(evm_address=evm, axon_url=url) for evm, url in peers],
    )


# ── Mutually-exclusive backward-compat ──────────────────────────────────


def test_pinned_only_returns_pinned():
    """Pre-existing behavior: set_peers() alone (no protocol_config) → returns
    pinned only, same as before."""
    vpn = _make_vpn()
    vpn.set_peers([
        PeerEndpoint(validator_id="0xA", url="http://a:9100"),
        PeerEndpoint(validator_id="0xB", url="http://b:9100"),
    ])
    assert [p.validator_id for p in vpn.peers] == ["0xA", "0xB"]


def test_discovered_only_returns_discovered():
    """No set_peers() called → returns just the discovery results, same as before."""
    vpn = _make_vpn(protocol_config=_pc(("0xC", "http://c:9100"), ("0xD", "http://d:9100")))
    ids = [p.validator_id for p in vpn.peers]
    assert ids == ["0xC", "0xD"]


def test_neither_source_returns_empty():
    """No override + no protocol_config (the test_init_defaults case)."""
    vpn = _make_vpn()
    assert vpn.peers == []


# ── New union semantics ─────────────────────────────────────────────────


def test_union_pinned_plus_discovered():
    """The third-party use case: 2 in-cluster peers pinned + 1 discovered
    third party → all 3 broadcast targets."""
    pc = _pc(("0xC", "http://c:9100"))  # the discovered third party
    vpn = _make_vpn(protocol_config=pc)
    vpn.set_peers([
        PeerEndpoint(validator_id="0xA", url="http://a:9100"),
        PeerEndpoint(validator_id="0xB", url="http://b:9100"),
    ])
    ids = [p.validator_id.lower() for p in vpn.peers]
    assert ids == ["0xa", "0xb", "0xc"]


def test_union_pinned_takes_precedence_on_conflict():
    """Operator pinning 0xC@http://override:9100 should win even if discovery
    reports the same EVM at http://stale:9100. Lets URL overrides stick."""
    pc = _pc(("0xC", "http://stale.example:9100"))
    vpn = _make_vpn(protocol_config=pc)
    vpn.set_peers([
        PeerEndpoint(validator_id="0xC", url="http://override:9100"),
    ])
    peers = vpn.peers
    assert len(peers) == 1
    assert peers[0].url == "http://override:9100"


def test_union_dedupe_is_case_insensitive():
    """Pinned 0xabc + discovered 0xABC → one entry (the pinned)."""
    pc = _pc(("0xABC", "http://discovered:9100"))
    vpn = _make_vpn(protocol_config=pc)
    vpn.set_peers([PeerEndpoint(validator_id="0xabc", url="http://pinned:9100")])
    assert len(vpn.peers) == 1
    assert vpn.peers[0].url == "http://pinned:9100"


def test_self_filtered_from_both_sources():
    """Self should never appear regardless of which source claims it."""
    pc = _pc(
        ("0xMe", "http://self-discovered:9100"),
        ("0xPeer", "http://peer:9100"),
    )
    vpn = _make_vpn(my_id="0xMe", protocol_config=pc)
    vpn.set_peers([
        PeerEndpoint(validator_id="0xMe", url="http://self-pinned:9100"),
        PeerEndpoint(validator_id="0xPinnedPeer", url="http://pinned-peer:9100"),
    ])
    ids = [p.validator_id.lower() for p in vpn.peers]
    assert "0xme" not in ids
    assert sorted(ids) == ["0xpeer", "0xpinnedpeer"]


def test_union_self_filtered_case_insensitive():
    """Self filter must be case-insensitive — discovery may report
    checksummed addresses while validator_id is lowercase."""
    pc = _pc(("0xMe", "http://discovered:9100"))
    vpn = _make_vpn(my_id="0xme", protocol_config=pc)
    assert vpn.peers == []


def test_union_empty_pinned_falls_through_to_discovered():
    """set_peers([]) installs an empty override — discovery results should
    still be returned via the union."""
    pc = _pc(("0xC", "http://c:9100"))
    vpn = _make_vpn(protocol_config=pc)
    vpn.set_peers([])
    assert [p.validator_id for p in vpn.peers] == ["0xC"]


def test_union_preserves_order_pinned_first_then_discovered():
    """Ordering matters for some downstream tests; lock it in: pinned
    appear first in their pin order, then discovered in their discovery
    order."""
    pc = _pc(
        ("0xD1", "http://d1:9100"),
        ("0xD2", "http://d2:9100"),
    )
    vpn = _make_vpn(protocol_config=pc)
    vpn.set_peers([
        PeerEndpoint(validator_id="0xP1", url="http://p1:9100"),
        PeerEndpoint(validator_id="0xP2", url="http://p2:9100"),
    ])
    ids = [p.validator_id for p in vpn.peers]
    assert ids == ["0xP1", "0xP2", "0xD1", "0xD2"]
