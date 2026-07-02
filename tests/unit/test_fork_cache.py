"""Tests for the anvil fork read-through cache (fork_cache).

A stub upstream stands in for the archive provider. The cache must serve ONLY
provably-immutable responses (explicit-block state reads, hash-keyed lookups,
chain constants) and forward everything that names moving state.
"""
from __future__ import annotations

import json
from typing import Any

import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from minotaur_subnet.harness.rpc_budget_proxy.fork_cache import ForkCache, cache_key


class UpstreamStub:
    def __init__(self) -> None:
        self.received: list[Any] = []

    async def handle(self, request: web.Request) -> web.Response:
        body = json.loads(await request.read())
        self.received.append(body)
        if isinstance(body, list):
            return web.json_response(
                [{"jsonrpc": "2.0", "id": m.get("id"), "result": f"0xr{m.get('id')}"}
                 for m in body]
            )
        return web.json_response(
            {"jsonrpc": "2.0", "id": body.get("id"), "result": "0xcafe"}
        )


@pytest_asyncio.fixture
async def cache_client():
    stub = UpstreamStub()
    up_app = web.Application()
    up_app.router.add_post("/", stub.handle)
    up_server = TestServer(up_app)
    await up_server.start_server()

    fc = ForkCache({"base": str(up_server.make_url("/")), "eth": str(up_server.make_url("/"))})
    client = TestClient(TestServer(fc.build_app()))
    await client.start_server()
    yield fc, client, stub
    await client.close()
    await up_server.close()


def _rpc(method, params, _id=1):
    return {"jsonrpc": "2.0", "id": _id, "method": method, "params": params}


# ── cache_key classification ────────────────────────────────────────────────

def test_cache_key_explicit_block_only():
    assert cache_key("base", _rpc("eth_getStorageAt", ["0xA", "0x1", "0x2ddd7c6"])) is not None
    assert cache_key("base", _rpc("eth_getStorageAt", ["0xA", "0x1", "latest"])) is None
    assert cache_key("base", _rpc("eth_getStorageAt", ["0xA", "0x1"])) is None  # absent == latest
    assert cache_key("base", _rpc("eth_getCode", ["0xA", "pending"])) is None
    assert cache_key("base", _rpc("eth_getBlockByNumber", ["0x100", False])) is not None
    assert cache_key("base", _rpc("eth_getBlockByNumber", ["safe", False])) is None


def test_cache_key_hash_and_constants_and_moving():
    assert cache_key("base", _rpc("eth_getBlockByHash", ["0xabc", True])) is not None
    assert cache_key("base", _rpc("eth_getTransactionReceipt", ["0xabc"])) is not None
    assert cache_key("base", _rpc("eth_chainId", [])) is not None
    assert cache_key("base", _rpc("eth_blockNumber", [])) is None   # moving head
    assert cache_key("base", _rpc("eth_gasPrice", [])) is None      # moving price
    assert cache_key("base", _rpc("anvil_reset", [{}])) is None     # unknown/never


def test_cache_key_block_by_number_bool_variant_distinct():
    a = cache_key("base", _rpc("eth_getBlockByNumber", ["0x100", False]))
    b = cache_key("base", _rpc("eth_getBlockByNumber", ["0x100", True]))
    assert a != b  # full-tx variant is a different response


# ── end-to-end behavior ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_explicit_block_read_cached(cache_client):
    fc, client, stub = cache_client
    req = _rpc("eth_getStorageAt", ["0xA", "0x1", "0x2ddd7c6"])
    r1 = await client.post("/base", json=req)
    assert (await r1.json())["result"] == "0xcafe"
    r2 = await client.post("/base", json=dict(req, id=42))
    body = await r2.json()
    assert body["result"] == "0xcafe" and body["id"] == 42
    assert len(stub.received) == 1  # second served locally
    assert fc.hits == 1


@pytest.mark.asyncio
async def test_latest_reads_always_forward(cache_client):
    _, client, stub = cache_client
    req = _rpc("eth_getStorageAt", ["0xA", "0x1", "latest"])
    await client.post("/base", json=req)
    await client.post("/base", json=req)
    assert len(stub.received) == 2


@pytest.mark.asyncio
async def test_chains_do_not_share_entries(cache_client):
    _, client, stub = cache_client
    req = _rpc("eth_getCode", ["0xA", "0x100"])
    await client.post("/base", json=req)
    await client.post("/eth", json=req)
    assert len(stub.received) == 2  # same params, different chain -> both forwarded


@pytest.mark.asyncio
async def test_batch_forwarded_then_served_locally(cache_client):
    fc, client, stub = cache_client
    batch = [
        _rpc("eth_getStorageAt", ["0xA", "0x1", "0x100"], _id=1),
        _rpc("eth_getStorageAt", ["0xA", "0x2", "0x100"], _id=2),
    ]
    r1 = await client.post("/base", json=batch)
    assert [m["result"] for m in await r1.json()] == ["0xr1", "0xr2"]
    r2 = await client.post("/base", json=batch)  # every member now cached
    assert [m["result"] for m in await r2.json()] == ["0xr1", "0xr2"]
    assert len(stub.received) == 1
    assert fc.hits == 2


@pytest.mark.asyncio
async def test_batch_with_moving_member_always_forwards(cache_client):
    _, client, stub = cache_client
    batch = [
        _rpc("eth_getStorageAt", ["0xA", "0x1", "0x100"], _id=1),
        _rpc("eth_blockNumber", [], _id=2),  # moving -> whole batch forwards
    ]
    await client.post("/base", json=batch)
    await client.post("/base", json=batch)
    assert len(stub.received) == 2


@pytest.mark.asyncio
async def test_error_responses_not_cached():
    calls = []

    async def flaky(request: web.Request) -> web.Response:
        calls.append(await request.read())
        if len(calls) == 1:
            return web.json_response(
                {"jsonrpc": "2.0", "id": 1, "error": {"code": -32005, "message": "limit"}}
            )
        return web.json_response({"jsonrpc": "2.0", "id": 1, "result": "0xgood"})

    up = web.Application()
    up.router.add_post("/", flaky)
    server = TestServer(up)
    await server.start_server()
    try:
        fc = ForkCache({"base": str(server.make_url("/"))})
        client = TestClient(TestServer(fc.build_app()))
        await client.start_server()
        try:
            req = _rpc("eth_getCode", ["0xA", "0x100"])
            await client.post("/base", json=req)  # 429-ish error: NOT cached
            r2 = await client.post("/base", json=req)  # re-forwards, succeeds
            assert (await r2.json())["result"] == "0xgood"
            r3 = await client.post("/base", json=req)  # now cached
            assert (await r3.json())["result"] == "0xgood"
            assert len(calls) == 2
        finally:
            await client.close()
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_lru_eviction_bounded(cache_client):
    fc, client, stub = cache_client
    fc.max_entries = 3
    for i in range(5):
        await client.post("/base", json=_rpc("eth_getStorageAt", ["0xA", hex(i), "0x100"]))
    assert len(fc._cache) == 3  # bounded
    before = len(stub.received)
    await client.post("/base", json=_rpc("eth_getStorageAt", ["0xA", "0x0", "0x100"]))
    assert len(stub.received) == before + 1  # oldest evicted -> re-fetched


@pytest.mark.asyncio
async def test_disable_env_forwards_everything(cache_client):
    fc, client, stub = cache_client
    fc.disabled = True
    req = _rpc("eth_getStorageAt", ["0xA", "0x1", "0x100"])
    await client.post("/base", json=req)
    await client.post("/base", json=req)
    assert len(stub.received) == 2  # kill switch: transparent passthrough


@pytest.mark.asyncio
async def test_unknown_chain_400_and_health_stats(cache_client):
    fc, client, _ = cache_client
    assert (await client.post("/nope", json=_rpc("eth_chainId", []))).status == 400
    assert (await client.get("/health")).status == 200
    stats = await (await client.get("/stats")).json()
    assert set(stats) >= {"hits", "misses", "uncacheable", "entries", "disabled"}


def test_empty_upstreams_filtered():
    fc = ForkCache({"eth": "", "base": "http://x"})
    assert list(fc.upstreams) == ["base"]  # unset env -> chain absent, fails loud
    import pytest as _pytest
    with _pytest.raises(ValueError):
        ForkCache({"eth": ""})
