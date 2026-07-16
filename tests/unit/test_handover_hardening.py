"""Handover hardening (2026-07-16 scandinavia activation incident):

1. Lifecycle broadcasts wait (bounded) for peer discovery instead of firing
   into an empty peer set — the certify broadcast at api-boot+27s reached
   nobody and both followers 409'd the activation.
2. A positively-null champion (api answered, champion=None) is debounced
   before burning when our own persisted last successful emit was
   champion-sourced — the validator ticked inside the activation window and
   committed a 100% owner burn for the tempo.
"""
import asyncio
import time

import pytest

from types import SimpleNamespace

from minotaur_subnet.consensus.peer_network import ValidatorPeerNetwork
from minotaur_subnet.validator.main import AppIntentsValidator as Validator


# ── 1. lifecycle broadcast peer wait ─────────────────────────────────────────

class _Cfg:
    def __init__(self, n_validators: int):
        self.on_chain_validators = [f"0x{i:040x}" for i in range(n_validators)]
        self.peers = []


def _network(cfg) -> ValidatorPeerNetwork:
    net = ValidatorPeerNetwork.__new__(ValidatorPeerNetwork)
    net.protocol_config = cfg
    net.validator_id = "0x" + "e" * 40
    net._peers_override = None
    net._peer_url_transform = None
    return net


def _peers_of(net):
    return net.peers


@pytest.mark.asyncio
async def test_lifecycle_wait_returns_once_discovery_catches_up(monkeypatch):
    monkeypatch.setenv("LIFECYCLE_BROADCAST_PEER_WAIT_SECONDS", "10")
    cfg = _Cfg(4)
    net = _network(cfg)

    async def _populate():
        await asyncio.sleep(0.5)
        # discovery-sourced peers are PeerInfo-shaped (evm_address + url)
        cfg.peers = [
            SimpleNamespace(evm_address="0x" + "a" * 40, url="http://a:9100", axon_url="http://a:9100"),
            SimpleNamespace(evm_address="0x" + "b" * 40, url="http://b:9100", axon_url="http://b:9100"),
        ]

    task = asyncio.create_task(_populate())
    t0 = time.monotonic()
    peers = await net._await_peers_for_lifecycle("test")
    await task
    assert len(peers) == 2
    assert time.monotonic() - t0 < 8, "should return as soon as peers appear"


@pytest.mark.asyncio
async def test_lifecycle_wait_bounded_then_falls_through(monkeypatch):
    monkeypatch.setenv("LIFECYCLE_BROADCAST_PEER_WAIT_SECONDS", "1")
    net = _network(_Cfg(4))
    t0 = time.monotonic()
    peers = await net._await_peers_for_lifecycle("test")
    assert peers == []
    assert time.monotonic() - t0 < 5


@pytest.mark.asyncio
async def test_lifecycle_wait_skipped_single_node(monkeypatch):
    # A single-validator (dev/local) setup must never stall round syncs.
    monkeypatch.setenv("LIFECYCLE_BROADCAST_PEER_WAIT_SECONDS", "60")
    net = _network(_Cfg(1))
    t0 = time.monotonic()
    assert await net._await_peers_for_lifecycle("test") == []
    assert time.monotonic() - t0 < 1


@pytest.mark.asyncio
async def test_lifecycle_wait_disabled_by_zero(monkeypatch):
    monkeypatch.setenv("LIFECYCLE_BROADCAST_PEER_WAIT_SECONDS", "0")
    net = _network(_Cfg(4))
    t0 = time.monotonic()
    assert await net._await_peers_for_lifecycle("test") == []
    assert time.monotonic() - t0 < 1


# ── 2. null-champion burn debounce ───────────────────────────────────────────

def _validator_with_last_emit(source: str, age_s: float):
    v = Validator.__new__(Validator)
    v._last_successful_emit_state = {
        "source": source,
        "attempted_at": time.time() - age_s,
        "result": "ok",
    }
    v._null_champion_since = None
    return v


def test_debounce_defers_when_champion_recent(monkeypatch):
    monkeypatch.setenv("NULL_CHAMPION_BURN_GRACE_SECONDS", "600")
    v = _validator_with_last_emit("champion", age_s=120)
    now = time.time()
    assert v._should_defer_null_champion_burn(now) is True          # first sight
    assert v._should_defer_null_champion_burn(now + 300) is True    # inside grace
    assert v._should_defer_null_champion_burn(now + 601) is False   # grace over


def test_debounce_burns_immediately_without_champion_history(monkeypatch):
    monkeypatch.setenv("NULL_CHAMPION_BURN_GRACE_SECONDS", "600")
    v = _validator_with_last_emit("burn", age_s=60)
    assert v._should_defer_null_champion_burn(time.time()) is False


def test_debounce_burns_when_champion_memory_stale(monkeypatch):
    monkeypatch.setenv("NULL_CHAMPION_BURN_GRACE_SECONDS", "600")
    monkeypatch.setenv("NULL_CHAMPION_MEMORY_SECONDS", "7200")
    v = _validator_with_last_emit("champion", age_s=8000)
    assert v._should_defer_null_champion_burn(time.time()) is False


def test_debounce_disabled_by_zero_grace(monkeypatch):
    monkeypatch.setenv("NULL_CHAMPION_BURN_GRACE_SECONDS", "0")
    v = _validator_with_last_emit("champion", age_s=60)
    assert v._should_defer_null_champion_burn(time.time()) is False
