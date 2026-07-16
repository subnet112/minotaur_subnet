"""Consensus resilience to peer-set flaps (2026-07-16 incident).

The incident: a CPU-stalled leader timed out one round of identity probes
(including a probe of ITSELF), ``_refresh_peers`` zeroed ``peers``, and an
order proposed one second later was broadcast to nobody, waited out the
full consensus window, and was terminally rejected ("Consensus not
reached") with ~58 minutes of order deadline left.

Three defenses, tested here:
1. ``ProtocolConfig._refresh_peers`` eviction hysteresis — a verified peer
   survives up to ``peer_eviction_misses``-1 consecutive failed probe
   rounds (de-authorization on chain still evicts immediately).
2. ``OrderProcessor._defer_or_reject_consensus`` — a failed consensus round
   requeues the order OPEN for the next tick while attempts and order
   deadline allow, instead of terminal rejection.
3. ``broadcast_proposal`` logs a warning instead of silently returning []
   on an empty peer list.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from minotaur_subnet.consensus.peer_discovery import PeerInfo
from minotaur_subnet.consensus.protocol_config import ProtocolConfig


def _peer(n: int) -> PeerInfo:
    return PeerInfo(
        evm_address=f"0x{n:040x}",
        hotkey=f"hk{n}",
        axon_url=f"http://10.0.0.{n}:9100",
    )


def _config(**kwargs) -> ProtocolConfig:
    cfg = ProtocolConfig(
        quorum_bps=6666,
        rpc_url="http://localhost:1",
        registry_address="0x" + "aa" * 20,
        my_evm_address="0x" + "ff" * 20,
        metagraph_provider=lambda: [],  # patched discover_peers ignores it
        **kwargs,
    )
    return cfg


async def _refresh_with(cfg: ProtocolConfig, discovered: list[PeerInfo], authorized: list[str]):
    """Run one _refresh_peers cycle with discovery + registry stubbed."""
    async def _fake_metagraph():
        return []
    cfg.metagraph_provider = _fake_metagraph

    async def _fake_discover(**_kw):
        return list(discovered)

    with patch(
        "minotaur_subnet.consensus.protocol_config.discover_peers",
        side_effect=_fake_discover,
    ), patch(
        "minotaur_subnet.consensus.protocol_config._read_validators",
        return_value=list(authorized),
    ):
        await cfg._refresh_peers(session=MagicMock())


# ── 1. Eviction hysteresis ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_peer_survives_transient_probe_blackout():
    """One (and two) all-fail probe rounds must NOT empty the peer set —
    exactly the incident shape: probes lie under local CPU load."""
    p1, p2 = _peer(1), _peer(2)
    auth = [p1.evm_address, p2.evm_address]
    cfg = _config(peer_eviction_misses=3)

    await _refresh_with(cfg, [p1, p2], auth)
    assert len(cfg.peers) == 2

    await _refresh_with(cfg, [], auth)  # blackout round 1
    assert len(cfg.peers) == 2, "peers must survive a single failed probe round"

    await _refresh_with(cfg, [], auth)  # blackout round 2
    assert len(cfg.peers) == 2, "peers must survive below-threshold streaks"

    await _refresh_with(cfg, [], auth)  # round 3 — threshold reached
    assert cfg.peers == [], "3 consecutive misses must finally evict"


@pytest.mark.asyncio
async def test_reappearing_peer_resets_streak():
    p1 = _peer(1)
    auth = [p1.evm_address]
    cfg = _config(peer_eviction_misses=3)

    await _refresh_with(cfg, [p1], auth)
    await _refresh_with(cfg, [], auth)
    await _refresh_with(cfg, [], auth)
    assert len(cfg.peers) == 1
    # Peer answers again — streak must reset...
    await _refresh_with(cfg, [p1], auth)
    assert cfg._peer_missing_streaks == {}
    # ...so two more misses still don't evict.
    await _refresh_with(cfg, [], auth)
    await _refresh_with(cfg, [], auth)
    assert len(cfg.peers) == 1


@pytest.mark.asyncio
async def test_deauthorized_peer_evicted_immediately():
    """On-chain de-authorization is authoritative — no retention grace."""
    p1, p2 = _peer(1), _peer(2)
    cfg = _config(peer_eviction_misses=3)

    await _refresh_with(cfg, [p1, p2], [p1.evm_address, p2.evm_address])
    assert len(cfg.peers) == 2

    # p2 fails its probe AND is gone from getValidators() → evicted now.
    await _refresh_with(cfg, [p1], [p1.evm_address])
    assert [p.evm_address for p in cfg.peers] == [p1.evm_address]


@pytest.mark.asyncio
async def test_new_and_returning_peers_added_immediately():
    p1, p2 = _peer(1), _peer(2)
    auth = [p1.evm_address, p2.evm_address]
    cfg = _config(peer_eviction_misses=3)

    await _refresh_with(cfg, [p1], auth)
    assert len(cfg.peers) == 1
    await _refresh_with(cfg, [p1, p2], auth)
    assert len(cfg.peers) == 2, "additions must not be dampened"


@pytest.mark.asyncio
async def test_evicted_peer_streak_pruned():
    """After eviction the streak entry is dropped, so a comeback starts
    with a clean slate instead of instant re-eviction."""
    p1 = _peer(1)
    auth = [p1.evm_address]
    cfg = _config(peer_eviction_misses=2)

    await _refresh_with(cfg, [p1], auth)
    await _refresh_with(cfg, [], auth)
    await _refresh_with(cfg, [], auth)
    assert cfg.peers == []
    assert cfg._peer_missing_streaks == {}
    await _refresh_with(cfg, [p1], auth)
    await _refresh_with(cfg, [], auth)
    assert len(cfg.peers) == 1


# ── 2. Order-processor defer/retry ───────────────────────────────────────


def _processor():
    from minotaur_subnet.blockloop.order_processor import OrderProcessor

    proc = OrderProcessor.__new__(OrderProcessor)
    proc.orderbook = MagicMock()
    proc.order_persistence = MagicMock()
    proc._consensus_retries = {}
    return proc


def _order(deadline_offset_s: float = 3600.0):
    return SimpleNamespace(
        order_id="ord_flap",
        deadline=time.time() + deadline_offset_s,
    )


def test_consensus_failure_requeues_open_with_deadline_left():
    from minotaur_subnet.orderbook.orderbook import OrderStatus

    proc = _processor()
    proc._defer_or_reject_consensus(_order(), "Consensus not reached")

    kwargs = proc.orderbook.update_order.call_args.kwargs
    assert kwargs["status"] == OrderStatus.OPEN
    assert "retry 1/" in kwargs["error"]
    proc.order_persistence.sync.assert_called_once_with("ord_flap")
    assert proc._consensus_retries["ord_flap"][0] == 1


def test_consensus_failure_terminal_after_max_attempts():
    from minotaur_subnet.orderbook.orderbook import OrderStatus

    proc = _processor()
    order = _order()
    for _ in range(proc._CONSENSUS_RETRY_MAX):
        proc._defer_or_reject_consensus(order, "Consensus not reached")
        assert proc.orderbook.update_order.call_args.kwargs["status"] == OrderStatus.OPEN

    proc._defer_or_reject_consensus(order, "Consensus not reached")
    kwargs = proc.orderbook.update_order.call_args.kwargs
    assert kwargs["status"] == OrderStatus.REJECTED
    assert "attempt(s)" in kwargs["error"]
    assert "ord_flap" not in proc._consensus_retries


def test_consensus_failure_terminal_when_deadline_short():
    from minotaur_subnet.orderbook.orderbook import OrderStatus

    proc = _processor()
    proc._defer_or_reject_consensus(
        _order(deadline_offset_s=30.0), "Consensus not reached",
    )
    kwargs = proc.orderbook.update_order.call_args.kwargs
    assert kwargs["status"] == OrderStatus.REJECTED


def test_terminal_rejection_carries_consensus_result():
    from minotaur_subnet.orderbook.orderbook import OrderStatus

    proc = _processor()
    result = SimpleNamespace(reached=False, approvals=[], quorum=2, collected=1)
    proc._defer_or_reject_consensus(
        _order(deadline_offset_s=30.0), "Consensus not reached",
        consensus_result=result,
    )
    kwargs = proc.orderbook.update_order.call_args.kwargs
    assert kwargs["status"] == OrderStatus.REJECTED
    assert "consensus_result" in kwargs


# ── 3. Empty-peer broadcast warns instead of silently no-oping ──────────


@pytest.mark.asyncio
async def test_broadcast_proposal_empty_peers_warns(caplog):
    from minotaur_subnet.consensus.peer_network import ValidatorPeerNetwork

    net = ValidatorPeerNetwork(
        validator_id="0x" + "ff" * 20,
        private_key="0x" + "11" * 32,
        consensus=MagicMock(),
        peers=[],
    )
    import logging
    with caplog.at_level(logging.WARNING):
        out = await net.broadcast_proposal(
            order_id="ord_flap",
            plan=MagicMock(),
            score=1.0,
            plan_hash="0x" + "00" * 32,
        )
    assert out == []
    assert any("peer list is EMPTY" in r.message for r in caplog.records)
