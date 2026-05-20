"""Unit tests for peer_discovery — mocked HTTP + mocked metagraph."""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest
import pytest_asyncio
from aiohttp import web
from eth_account import Account

from minotaur_subnet.consensus.identity import sign_identity
from minotaur_subnet.consensus.peer_discovery import (
    MetagraphPeer,
    PeerInfo,
    discover_peers,
)


# Three deterministic Anvil-style keys for the mock validators.
KEY_A = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
KEY_B = "0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d"
KEY_C = "0x5de4111afa1a4b94908f83103eb1f1706367c2e68ca870fc3fb9a804cdab365a"

EVM_A = Account.from_key(KEY_A).address
EVM_B = Account.from_key(KEY_B).address
EVM_C = Account.from_key(KEY_C).address

HK_A = "5HotkeyA"
HK_B = "5HotkeyB"
HK_C = "5HotkeyC"


async def _serve_identity(app_state: dict, request: web.Request) -> web.Response:
    """Return a configurable /identity response per test."""
    return web.json_response(app_state["payload"])


async def _serve_500(_request: web.Request) -> web.Response:
    return web.Response(status=500)


@pytest_asyncio.fixture
async def fake_validator():
    """Spin up a tiny aiohttp server that returns a configurable /identity."""

    async def _make(payload_factory, *, status=200):
        state = {"payload": None}

        async def handler(request: web.Request) -> web.Response:
            if status != 200:
                return web.Response(status=status)
            return web.json_response(state["payload"])

        app = web.Application()
        app.router.add_get("/identity", handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        state["payload"] = payload_factory(f"http://127.0.0.1:{port}")
        url = f"http://127.0.0.1:{port}"
        return url, state, runner

    servers = []

    async def make(payload_factory, *, status=200):
        url, state, runner = await _make(payload_factory, status=status)
        servers.append(runner)
        return url, state

    yield make

    for r in servers:
        await r.cleanup()


@pytest.mark.asyncio
async def test_discovers_authorized_peer(fake_validator):
    # Peer A serves a valid identity
    url_a, _ = await fake_validator(
        lambda axon_url: sign_identity(KEY_A, HK_A, axon_url).to_dict()
    )
    metagraph = [MetagraphPeer(hotkey=HK_A, axon_url=url_a)]

    peers = await discover_peers(
        metagraph_peers=metagraph,
        authorized_evm_addresses=[EVM_A],
        my_evm_address=EVM_B,
    )
    assert len(peers) == 1
    assert peers[0].evm_address.lower() == EVM_A.lower()
    assert peers[0].hotkey == HK_A
    assert peers[0].axon_url == url_a


@pytest.mark.asyncio
async def test_unauthorized_evm_rejected(fake_validator):
    # Peer signs validly but its EVM is not in the on-chain registry
    url_a, _ = await fake_validator(
        lambda axon_url: sign_identity(KEY_A, HK_A, axon_url).to_dict()
    )
    metagraph = [MetagraphPeer(hotkey=HK_A, axon_url=url_a)]

    peers = await discover_peers(
        metagraph_peers=metagraph,
        authorized_evm_addresses=[EVM_C],  # A is NOT in the registry
        my_evm_address=EVM_B,
    )
    assert peers == []


@pytest.mark.asyncio
async def test_hotkey_mismatch_rejected(fake_validator):
    # /identity claims hotkey A, but metagraph says hotkey B is at that axon
    url_a, _ = await fake_validator(
        lambda axon_url: sign_identity(KEY_A, HK_A, axon_url).to_dict()
    )
    metagraph = [MetagraphPeer(hotkey=HK_B, axon_url=url_a)]

    peers = await discover_peers(
        metagraph_peers=metagraph,
        authorized_evm_addresses=[EVM_A],
        my_evm_address=EVM_B,
    )
    assert peers == []


@pytest.mark.asyncio
async def test_axon_url_mismatch_rejected(fake_validator):
    # /identity signs URL X, but we probe URL Y (and metagraph says Y)
    url_a, _ = await fake_validator(
        lambda axon_url: sign_identity(KEY_A, HK_A, "http://elsewhere:9100").to_dict()
    )
    metagraph = [MetagraphPeer(hotkey=HK_A, axon_url=url_a)]

    peers = await discover_peers(
        metagraph_peers=metagraph,
        authorized_evm_addresses=[EVM_A],
        my_evm_address=EVM_B,
    )
    assert peers == []


@pytest.mark.asyncio
async def test_self_excluded_from_results(fake_validator):
    url_a, _ = await fake_validator(
        lambda axon_url: sign_identity(KEY_A, HK_A, axon_url).to_dict()
    )
    metagraph = [MetagraphPeer(hotkey=HK_A, axon_url=url_a)]

    peers = await discover_peers(
        metagraph_peers=metagraph,
        authorized_evm_addresses=[EVM_A],
        my_evm_address=EVM_A,  # we're A — exclude ourselves
    )
    assert peers == []


@pytest.mark.asyncio
async def test_offline_peer_silently_skipped(fake_validator):
    url_a, _ = await fake_validator(
        lambda axon_url: sign_identity(KEY_A, HK_A, axon_url).to_dict()
    )
    # Real server for A, dead URL for B
    metagraph = [
        MetagraphPeer(hotkey=HK_A, axon_url=url_a),
        MetagraphPeer(hotkey=HK_B, axon_url="http://127.0.0.1:1"),  # nobody listening
    ]

    peers = await discover_peers(
        metagraph_peers=metagraph,
        authorized_evm_addresses=[EVM_A, EVM_B],
        my_evm_address=EVM_C,
        probe_timeout_seconds=0.5,
    )
    # A is discovered, B is silently dropped
    assert len(peers) == 1
    assert peers[0].evm_address.lower() == EVM_A.lower()


@pytest.mark.asyncio
async def test_http_500_skipped(fake_validator):
    url, _ = await fake_validator(lambda _u: {}, status=500)
    metagraph = [MetagraphPeer(hotkey=HK_A, axon_url=url)]

    peers = await discover_peers(
        metagraph_peers=metagraph,
        authorized_evm_addresses=[EVM_A],
        my_evm_address=EVM_B,
    )
    assert peers == []


@pytest.mark.asyncio
async def test_no_axon_urls_returns_empty():
    metagraph = [MetagraphPeer(hotkey=HK_A, axon_url="")]
    peers = await discover_peers(
        metagraph_peers=metagraph,
        authorized_evm_addresses=[EVM_A],
        my_evm_address=EVM_B,
    )
    assert peers == []


@pytest.mark.asyncio
async def test_multiple_peers_all_discovered(fake_validator):
    url_a, _ = await fake_validator(
        lambda axon_url: sign_identity(KEY_A, HK_A, axon_url).to_dict()
    )
    url_b, _ = await fake_validator(
        lambda axon_url: sign_identity(KEY_B, HK_B, axon_url).to_dict()
    )
    metagraph = [
        MetagraphPeer(hotkey=HK_A, axon_url=url_a),
        MetagraphPeer(hotkey=HK_B, axon_url=url_b),
    ]

    peers = await discover_peers(
        metagraph_peers=metagraph,
        authorized_evm_addresses=[EVM_A, EVM_B],
        my_evm_address=EVM_C,
    )
    addrs = sorted(p.evm_address.lower() for p in peers)
    assert addrs == sorted([EVM_A.lower(), EVM_B.lower()])
