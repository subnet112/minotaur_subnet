"""Tests for the deterministic JSON-RPC counting/budget proxy.

A local aiohttp test server stands in for the upstream Anvil fork, returning a
canned JSON-RPC result, so no real Anvil is needed. The proxy's data plane
(``/rpc/...``) and control plane (``/control/...``) are exercised end to end.

Coverage:
  (a) cost_table: single + batch cost, default cost, record is sorted/stable
  (b) OBSERVE mode forwards transparently + accumulates, never cuts
  (c) ENFORCE mode forwards under budget, then MINOTAUR_BUDGET_EXCEEDED at/over
      budget and stays exhausted for the session
  (d) batch request cost = sum + batch error shape
  (e) control open/reset/close/stats
  (f) concurrent calls to one session can't exceed budget (atomic spend)
"""

from __future__ import annotations

import asyncio
import json

import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from minotaur_subnet.harness.rpc_budget_proxy import cost_table
from minotaur_subnet.harness.rpc_budget_proxy.cost_table import (
    DEFAULT_COST,
    batch_cost,
    cost_table_record,
    request_cost,
)
from minotaur_subnet.harness.rpc_budget_proxy.proxy import (
    BUDGET_EXCEEDED_CODE,
    BUDGET_EXCEEDED_MESSAGE,
    BudgetProxy,
)


# ---------------------------------------------------------------------------
# (a) cost_table
# ---------------------------------------------------------------------------


def test_request_cost_single_and_default():
    assert request_cost("eth_call") == 1
    assert request_cost("eth_getLogs") == 2
    assert request_cost("eth_blockNumber") == 0
    assert request_cost("eth_chainId") == 0
    # unlisted -> default
    assert request_cost("eth_totallyMadeUp") == DEFAULT_COST == 1
    # malformed -> default (still consumes budget, never free)
    assert request_cost("") == DEFAULT_COST
    assert request_cost(None) == DEFAULT_COST  # type: ignore[arg-type]


def test_batch_cost_is_sum():
    methods = ["eth_call", "eth_getLogs", "eth_chainId", "eth_unknownMethod"]
    # 1 + 2 + 0 + 1 = 4
    assert batch_cost(methods) == 4
    assert batch_cost([]) == 0


def test_cost_table_record_is_sorted_and_stable():
    rec = cost_table_record()
    assert rec["version"] == cost_table.COST_TABLE_VERSION == "v1"
    assert rec["default"] == DEFAULT_COST
    method_keys = list(rec["methods"].keys())
    assert method_keys == sorted(method_keys), "methods must be sorted by name"
    # canonical + stable: identical across calls and JSON-serializable
    assert cost_table_record() == rec
    assert json.dumps(rec, sort_keys=True) == json.dumps(
        cost_table_record(), sort_keys=True
    )


# ---------------------------------------------------------------------------
# Fixtures: a stub upstream + a proxy wired to it.
# ---------------------------------------------------------------------------


class UpstreamStub:
    """Canned JSON-RPC upstream. Records every raw body it receives."""

    def __init__(self) -> None:
        self.received: list[bytes] = []
        # A response with deliberate whitespace so we can assert byte-for-byte
        # transparency (the proxy must not reserialize and normalize it).
        self.canned_text = '{"jsonrpc": "2.0",  "id": 1,   "result": "0xabc"}'

    async def handle(self, request: web.Request) -> web.Response:
        body = await request.read()
        self.received.append(body)
        return web.Response(
            text=self.canned_text, content_type="application/json"
        )


@pytest_asyncio.fixture
async def upstream():
    stub = UpstreamStub()
    app = web.Application()
    app.router.add_post("/", stub.handle)
    server = TestServer(app)
    await server.start_server()
    stub.url = str(server.make_url("/"))  # type: ignore[attr-defined]
    yield stub
    await server.close()


async def _make_proxy_client(upstream_url, *, mode="observe", budget=1000):
    proxy = BudgetProxy(
        {"eth": upstream_url, "base": upstream_url},
        default_mode=mode,
        default_budget=budget,
    )
    app = proxy.build_app()
    client = TestClient(TestServer(app))
    await client.start_server()
    return proxy, client


@pytest_asyncio.fixture
async def proxy_client(upstream):
    proxy, client = await _make_proxy_client(upstream.url)
    yield proxy, client, upstream
    await client.close()


def _rpc(method="eth_call", _id=1):
    return {"jsonrpc": "2.0", "id": _id, "method": method, "params": []}


# ---------------------------------------------------------------------------
# (b) OBSERVE mode: transparent forward + accumulate, never cuts
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_observe_forwards_transparently_and_accumulates(proxy_client):
    proxy, client, upstream = proxy_client
    await client.post("/control/open", json={"session_id": "s1", "budget": 5, "mode": "observe"})

    # eth_call costs 1; send 10 of them -> spent 10, > budget 5, but observe
    # NEVER cuts off.
    for _ in range(10):
        resp = await client.post("/rpc/s1/eth", json=_rpc("eth_call"))
        assert resp.status == 200
        # byte-for-byte transparency: response equals the canned upstream text
        text = await resp.text()
        assert text == upstream.canned_text

    sess = proxy.sessions["s1"]
    assert sess.spent == 10
    assert sess.exhausted is False
    assert len(upstream.received) == 10  # all forwarded


@pytest.mark.asyncio
async def test_observe_zero_cost_methods(proxy_client):
    proxy, client, _ = proxy_client
    await client.post("/control/open", json={"session_id": "z", "budget": 100, "mode": "observe"})
    for m in ("eth_chainId", "eth_blockNumber", "eth_gasPrice", "net_version"):
        await client.post("/rpc/z", json=_rpc(m))
    assert proxy.sessions["z"].spent == 0


# ---------------------------------------------------------------------------
# (c) ENFORCE mode: forward under budget, then MINOTAUR_BUDGET_EXCEEDED, sticky
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_enforce_cuts_off_at_budget_and_stays_exhausted(proxy_client):
    proxy, client, upstream = proxy_client
    await client.post(
        "/control/open", json={"session_id": "e1", "budget": 3, "mode": "enforce"}
    )

    # 3 eth_call (cost 1 each) succeed and forward; spent goes 1,2,3.
    for i in range(3):
        resp = await client.post("/rpc/e1", json=_rpc("eth_call", _id=i))
        body = await resp.json()
        assert "result" in body, f"call {i} should have forwarded"
    assert len(upstream.received) == 3
    assert proxy.sessions["e1"].spent == 3

    # 4th call: spent(3)+cost(1) > budget(3) -> rejected, NOT forwarded.
    resp = await client.post("/rpc/e1", json=_rpc("eth_call", _id=99))
    body = await resp.json()
    assert body["error"]["code"] == BUDGET_EXCEEDED_CODE
    assert body["error"]["message"] == BUDGET_EXCEEDED_MESSAGE
    assert body["id"] == 99  # echoes the request id
    assert proxy.sessions["e1"].exhausted is True
    assert len(upstream.received) == 3  # still 3 — 4th was NOT forwarded

    # Subsequent calls stay exhausted (sticky), even a free (cost 0) method.
    for m in ("eth_call", "eth_chainId"):
        resp = await client.post("/rpc/e1", json=_rpc(m, _id=7))
        body = await resp.json()
        assert body["error"]["message"] == BUDGET_EXCEEDED_MESSAGE
    assert len(upstream.received) == 3  # nothing new forwarded


@pytest.mark.asyncio
async def test_enforce_overshoot_single_heavy_call(proxy_client):
    """A single request whose cost alone exceeds remaining budget is cut."""
    proxy, client, upstream = proxy_client
    await client.post(
        "/control/open", json={"session_id": "h", "budget": 1, "mode": "enforce"}
    )
    # eth_getLogs costs 2 > budget 1 -> rejected immediately, never forwarded.
    resp = await client.post("/rpc/h", json=_rpc("eth_getLogs"))
    body = await resp.json()
    assert body["error"]["message"] == BUDGET_EXCEEDED_MESSAGE
    assert proxy.sessions["h"].exhausted is True
    assert proxy.sessions["h"].spent == 0  # cost of rejected call NOT spent
    assert len(upstream.received) == 0


# ---------------------------------------------------------------------------
# (d) batch cost = sum + batch error shape
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_batch_cost_accumulates_in_observe(proxy_client):
    proxy, client, _ = proxy_client
    await client.post("/control/open", json={"session_id": "b", "budget": 100, "mode": "observe"})
    batch = [_rpc("eth_call", 1), _rpc("eth_getLogs", 2), _rpc("eth_chainId", 3)]
    # 1 + 2 + 0 = 3
    await client.post("/rpc/b", json=batch)
    assert proxy.sessions["b"].spent == 3


@pytest.mark.asyncio
async def test_batch_budget_exceeded_error_shape(proxy_client):
    proxy, client, upstream = proxy_client
    await client.post(
        "/control/open", json={"session_id": "bb", "budget": 1, "mode": "enforce"}
    )
    # batch cost 1 + 2 = 3 > budget 1 -> array of errors, one per member id.
    batch = [_rpc("eth_call", "x"), _rpc("eth_getLogs", "y")]
    resp = await client.post("/rpc/bb", json=batch)
    body = await resp.json()
    assert isinstance(body, list)
    assert len(body) == 2
    assert {item["id"] for item in body} == {"x", "y"}
    for item in body:
        assert item["error"]["code"] == BUDGET_EXCEEDED_CODE
        assert item["error"]["message"] == BUDGET_EXCEEDED_MESSAGE
    assert len(upstream.received) == 0


# ---------------------------------------------------------------------------
# (e) control plane: open / reset / close / stats
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_control_open_replace_reset_close(proxy_client):
    proxy, client, _ = proxy_client

    # open
    r = await client.post(
        "/control/open", json={"session_id": "c", "budget": 10, "mode": "enforce"}
    )
    rec = await r.json()
    assert rec["budget"] == 10 and rec["mode"] == "enforce" and rec["spent"] == 0

    # spend a bit
    await client.post("/rpc/c", json=_rpc("eth_getLogs"))  # cost 2
    assert proxy.sessions["c"].spent == 2
    assert proxy.sessions["c"].peak == 2

    # reset clears spent + exhausted (but keeps peak)
    r = await client.post("/control/reset", json={"session_id": "c"})
    rec = await r.json()
    assert rec["spent"] == 0 and rec["exhausted"] is False
    assert proxy.sessions["c"].peak == 2  # peak retained across reset

    # open again REPLACES (spent back to 0, new budget)
    await client.post("/rpc/c", json=_rpc("eth_call"))  # spent 1
    r = await client.post(
        "/control/open", json={"session_id": "c", "budget": 42, "mode": "observe"}
    )
    rec = await r.json()
    assert rec["budget"] == 42 and rec["spent"] == 0 and rec["peak"] == 0

    # close returns final stats + deletes
    await client.post("/rpc/c", json=_rpc("eth_call"))  # spent 1
    r = await client.post("/control/close", json={"session_id": "c"})
    rec = await r.json()
    assert rec["session_id"] == "c" and rec["spent"] == 1 and rec["peak"] == 1
    assert "c" not in proxy.sessions

    # closing an unknown session -> 404
    r = await client.post("/control/close", json={"session_id": "nope"})
    assert r.status == 404


@pytest.mark.asyncio
async def test_control_stats_reports_all_sessions(proxy_client):
    proxy, client, _ = proxy_client
    await client.post("/control/open", json={"session_id": "s_a", "budget": 5, "mode": "observe"})
    await client.post("/control/open", json={"session_id": "s_b", "budget": 5, "mode": "observe"})
    await client.post("/rpc/s_a", json=_rpc("eth_getLogs"))  # cost 2
    r = await client.get("/control/stats")
    stats = (await r.json())["sessions"]
    assert stats["s_a"]["spent"] == 2
    assert stats["s_b"]["spent"] == 0


@pytest.mark.asyncio
async def test_unknown_session_forwards_to_anon_bucket(proxy_client):
    """A misconfigured run (no /control/open) must not break — forward + count."""
    proxy, client, upstream = proxy_client
    resp = await client.post("/rpc/never_opened", json=_rpc("eth_call"))
    assert resp.status == 200
    text = await resp.text()
    assert text == upstream.canned_text  # forwarded transparently
    assert len(upstream.received) == 1
    assert proxy.sessions["__anon__"].spent == 1


@pytest.mark.asyncio
async def test_unknown_chain_returns_400(proxy_client):
    proxy, client, _ = proxy_client
    await client.post("/control/open", json={"session_id": "ch", "budget": 5, "mode": "observe"})
    resp = await client.post("/rpc/ch/dogecoin", json=_rpc("eth_call"))
    assert resp.status == 400


# ---------------------------------------------------------------------------
# (f) concurrent calls to one session can't exceed budget (atomic spend)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_calls_cannot_exceed_budget(upstream):
    """Fire many concurrent requests at one enforce-session; exactly `budget`
    forward, the rest are cut. The atomic (no-await) spend decision prevents two
    concurrent calls from both slipping under budget.
    """
    # Slow the upstream so requests overlap inside the proxy.
    async def slow_handle(request: web.Request) -> web.Response:
        await request.read()
        await asyncio.sleep(0.02)
        upstream.received.append(b"x")
        return web.Response(text=upstream.canned_text, content_type="application/json")

    app = web.Application()
    app.router.add_post("/", slow_handle)
    upserver = TestServer(app)
    await upserver.start_server()
    slow_url = str(upserver.make_url("/"))

    proxy = BudgetProxy({"eth": slow_url}, default_mode="enforce")
    client = TestClient(TestServer(proxy.build_app()))
    await client.start_server()
    try:
        budget = 5
        await client.post(
            "/control/open",
            json={"session_id": "race", "budget": budget, "mode": "enforce"},
        )

        # 20 concurrent eth_call (cost 1) against a budget of 5.
        async def one():
            resp = await client.post("/rpc/race", json=_rpc("eth_call"))
            return await resp.json()

        results = await asyncio.gather(*[one() for _ in range(20)])
        ok = [r for r in results if "result" in r]
        rejected = [r for r in results if "error" in r]

        assert len(ok) == budget, f"exactly {budget} should forward, got {len(ok)}"
        assert len(rejected) == 20 - budget
        assert proxy.sessions["race"].spent == budget
        assert len(upstream.received) == budget  # only `budget` reached upstream
    finally:
        await client.close()
        await upserver.close()


# ---------------------------------------------------------------------------
# (g) block-pin: rewrite_table (pure)
# ---------------------------------------------------------------------------

from minotaur_subnet.harness.rpc_budget_proxy import rewrite_table as rt  # noqa: E402


def test_classify():
    assert rt.classify("eth_call") == "rewrite"
    assert rt.classify("eth_getStorageAt") == "rewrite"
    assert rt.classify("eth_blockNumber") == "blocknumber"
    assert rt.classify("eth_getLogs") == "getlogs"
    assert rt.classify("eth_sendRawTransaction") == "reject"
    assert rt.classify("anvil_setBalance") == "reject"
    assert rt.classify("evm_snapshot") == "reject"
    assert rt.classify("eth_chainId") == "passthrough"
    assert rt.classify(123) == "passthrough"


def test_rewrite_params_forces_and_pads():
    B = "0x3039"  # 12345
    assert rt.rewrite_params("eth_call", [{"to": "0x0"}, "latest"], B) == [{"to": "0x0"}, B]
    assert rt.rewrite_params("eth_call", [{"to": "0x0"}], B) == [{"to": "0x0"}, B]  # omitted -> padded+forced
    assert rt.rewrite_params("eth_getStorageAt", ["0xa", "0x1"], B) == ["0xa", "0x1", B]
    assert rt.rewrite_params("eth_getBlockByNumber", ["latest", True], B) == [B, True]
    f = rt.rewrite_params("eth_getLogs", [{"address": "0xa", "fromBlock": "0x0", "toBlock": "latest"}], B)
    assert f[0]["fromBlock"] == B and f[0]["toBlock"] == B


def test_rewrite_single_actions():
    B = "0x3039"
    assert rt.rewrite_single({"method": "eth_blockNumber", "params": []}, B) == ("blocknumber", B)
    assert rt.rewrite_single({"method": "anvil_setBalance", "params": []}, B)[0] == "reject"
    a, p = rt.rewrite_single({"method": "eth_call", "params": [{"to": "0x0"}]}, B)
    assert a == "forward" and p["params"] == [{"to": "0x0"}, B]


def test_rewrite_table_record_stable():
    r = rt.rewrite_table_record()
    assert r["version"] == "v1"
    assert r == rt.rewrite_table_record()
    assert list(r["block_param_index"]) == sorted(r["block_param_index"])


# ---------------------------------------------------------------------------
# (h) block-pin: proxy integration
# ---------------------------------------------------------------------------


async def _open_pinned(client, sid, block):
    await client.post("/control/open", json={
        "session_id": sid, "budget": 10 ** 9, "mode": "observe", "blocks": {"eth": block},
    })


def _call(params, method="eth_call"):
    return {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}


@pytest.mark.asyncio
async def test_pin_rewrites_block_tag(proxy_client):
    _, client, upstream = proxy_client
    await _open_pinned(client, "p", 12345)  # 0x3039
    await client.post("/rpc/p/eth", json=_call([{"to": "0xq"}, "latest"]))
    sent = json.loads(upstream.received[-1])
    assert sent["params"][1] == "0x3039"  # 'latest' forced to the pin


@pytest.mark.asyncio
async def test_pin_forces_block_when_omitted(proxy_client):
    _, client, upstream = proxy_client
    await _open_pinned(client, "p", 12345)
    await client.post("/rpc/p/eth", json=_call([{"to": "0xq"}]))  # NO block arg
    sent = json.loads(upstream.received[-1])
    assert sent["params"][1] == "0x3039"  # padded + pinned (can't dodge by omission)


@pytest.mark.asyncio
async def test_pin_intercepts_blocknumber(proxy_client):
    _, client, upstream = proxy_client
    await _open_pinned(client, "p", 12345)
    before = len(upstream.received)
    resp = await client.post("/rpc/p/eth", json=_call([], "eth_blockNumber"))
    body = await resp.json()
    assert body["result"] == "0x3039"        # answered with the pin
    assert len(upstream.received) == before  # NOT forwarded


@pytest.mark.asyncio
async def test_pin_rejects_state_changing(proxy_client):
    _, client, upstream = proxy_client
    await _open_pinned(client, "p", 12345)
    before = len(upstream.received)
    for m in ("eth_sendRawTransaction", "anvil_setBalance", "evm_revert"):
        resp = await client.post("/rpc/p/eth", json=_call([], m))
        body = await resp.json()
        assert "error" in body and "not allowed" in body["error"]["message"]
    assert len(upstream.received) == before  # none forwarded


@pytest.mark.asyncio
async def test_no_pin_is_transparent(proxy_client):
    _, client, upstream = proxy_client
    await client.post("/control/open", json={"session_id": "np", "budget": 10 ** 9})  # no blocks
    await client.post("/rpc/np/eth", json=_call([{"to": "0xq"}, "latest"]))
    sent = json.loads(upstream.received[-1])
    assert sent["params"][1] == "latest"  # NOT rewritten (byte-transparent)


@pytest.mark.asyncio
async def test_reset_repoints_blocks(proxy_client):
    _, client, upstream = proxy_client
    await _open_pinned(client, "p", 12345)
    await client.post("/control/reset", json={"session_id": "p", "blocks": {"eth": 999}})  # 0x3e7
    await client.post("/rpc/p/eth", json=_call([{"to": "0xq"}]))
    sent = json.loads(upstream.received[-1])
    assert sent["params"][1] == "0x3e7"  # re-pointed to the new round's block


# ---------------------------------------------------------------------------
# (h1) pinned-read cache: (chain, block, method, params) -> result
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pin_cache_serves_repeat_reads_locally(proxy_client):
    proxy, client, upstream = proxy_client
    await _open_pinned(client, "p", 12345)
    r1 = await client.post("/rpc/p/eth", json=_call([{"to": "0xq"}, "latest"]))
    assert (await r1.json())["result"] == "0xabc"
    before = len(upstream.received)
    r2 = await client.post(
        "/rpc/p/eth",
        json={"jsonrpc": "2.0", "id": 42, "method": "eth_call", "params": [{"to": "0xq"}, "latest"]},
    )
    body = await r2.json()
    assert body["result"] == "0xabc"  # replayed upstream value
    assert body["id"] == 42           # echoes THIS request's id
    assert len(upstream.received) == before  # not forwarded
    assert proxy._pin_cache.stats()["hits"] == 1
    # metering is unaffected by the cache: both eth_calls charged cost 1
    assert proxy.sessions["p"].spent == 2


@pytest.mark.asyncio
async def test_pin_cache_keyed_by_params(proxy_client):
    _, client, upstream = proxy_client
    await _open_pinned(client, "p", 12345)
    await client.post("/rpc/p/eth", json=_call([{"to": "0xq"}]))
    await client.post("/rpc/p/eth", json=_call([{"to": "0xOTHER"}]))
    assert len(upstream.received) == 2  # different reads both forwarded


@pytest.mark.asyncio
async def test_pin_cache_key_canonicalizes_param_spelling(proxy_client):
    _, client, upstream = proxy_client
    await _open_pinned(client, "p", 12345)
    # same read, dict keys spelled in different order + block written as
    # 'latest' vs explicit — the rewrite pins both to the same canonical key
    await client.post("/rpc/p/eth", json=_call([{"to": "0xq", "data": "0x1"}, "latest"]))
    await client.post("/rpc/p/eth", json=_call([{"data": "0x1", "to": "0xq"}, "0x3039"]))
    assert len(upstream.received) == 1


@pytest.mark.asyncio
async def test_pin_cache_shared_across_sessions_same_block(proxy_client):
    _, client, upstream = proxy_client
    await _open_pinned(client, "a", 12345)
    await _open_pinned(client, "b", 12345)
    await client.post("/rpc/a/eth", json=_call([{"to": "0xq"}]))
    before = len(upstream.received)
    await client.post("/rpc/b/eth", json=_call([{"to": "0xq"}]))
    assert len(upstream.received) == before  # b served from a's fetch


@pytest.mark.asyncio
async def test_pin_cache_misses_on_new_block(proxy_client):
    _, client, upstream = proxy_client
    await _open_pinned(client, "p", 12345)
    await client.post("/rpc/p/eth", json=_call([{"to": "0xq"}]))
    await client.post("/control/reset", json={"session_id": "p", "blocks": {"eth": 999}})
    await client.post("/rpc/p/eth", json=_call([{"to": "0xq"}]))
    assert len(upstream.received) == 2  # new block = new state = must re-fetch


@pytest.mark.asyncio
async def test_unpinned_reads_never_cached(proxy_client):
    _, client, upstream = proxy_client
    await client.post("/control/open", json={"session_id": "np", "budget": 100})  # no pin
    await client.post("/rpc/np/eth", json=_call([{"to": "0xq"}, "latest"]))
    await client.post("/rpc/np/eth", json=_call([{"to": "0xq"}, "latest"]))
    assert len(upstream.received) == 2  # 'latest' is a moving target — no cache


@pytest.mark.asyncio
async def test_pin_cache_skips_block_independent_methods(proxy_client):
    _, client, upstream = proxy_client
    await _open_pinned(client, "p", 12345)
    await client.post("/rpc/p/eth", json=_call([], method="eth_gasPrice"))
    await client.post("/rpc/p/eth", json=_call([], method="eth_gasPrice"))
    assert len(upstream.received) == 2  # non-constant passthrough: always forwards


@pytest.mark.asyncio
async def test_pin_cache_hit_still_consumes_budget(upstream):
    proxy, client = await _make_proxy_client(upstream.url, mode="enforce", budget=2)
    try:
        await client.post("/control/open", json={
            "session_id": "e", "budget": 2, "mode": "enforce", "blocks": {"eth": 12345},
        })
        await client.post("/rpc/e/eth", json=_call([{"to": "0xq"}]))  # forward, spent=1
        await client.post("/rpc/e/eth", json=_call([{"to": "0xq"}]))  # cache hit, spent=2
        resp = await client.post("/rpc/e/eth", json=_call([{"to": "0xq"}]))  # over budget
        body = await resp.json()
        assert body["error"]["message"] == BUDGET_EXCEEDED_MESSAGE
        assert proxy.sessions["e"].spent == 2  # hits metered exactly like forwards
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_pin_cache_does_not_cache_jsonrpc_errors():
    calls = []

    async def flaky(request: web.Request) -> web.Response:
        calls.append(await request.read())
        if len(calls) == 1:  # transient upstream error (e.g. rate limit)
            return web.json_response(
                {"jsonrpc": "2.0", "id": 1, "error": {"code": -32005, "message": "limit"}}
            )
        return web.json_response({"jsonrpc": "2.0", "id": 1, "result": "0xgood"})

    app = web.Application()
    app.router.add_post("/", flaky)
    server = TestServer(app)
    await server.start_server()
    try:
        _, client = await _make_proxy_client(str(server.make_url("/")))
        try:
            await _open_pinned(client, "p", 12345)
            await client.post("/rpc/p/eth", json=_call([{"to": "0xq"}]))  # error: NOT cached
            r2 = await client.post("/rpc/p/eth", json=_call([{"to": "0xq"}]))  # re-forwards
            assert (await r2.json())["result"] == "0xgood"
            r3 = await client.post("/rpc/p/eth", json=_call([{"to": "0xq"}]))  # now cached
            assert (await r3.json())["result"] == "0xgood"
            assert len(calls) == 2
        finally:
            await client.close()
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_pin_cache_evicts_oldest_block_group(proxy_client):
    from minotaur_subnet.harness.rpc_budget_proxy.proxy import PinCache

    proxy, client, upstream = proxy_client
    proxy._pin_cache = PinCache(max_blocks=2)
    for block in (100, 200, 300):  # 3 blocks through a 2-block cache
        await client.post("/control/open", json={
            "session_id": "p", "budget": 10 ** 9, "blocks": {"eth": block},
        })
        await client.post("/rpc/p/eth", json=_call([{"to": "0xq"}]))
    assert proxy._pin_cache.stats()["blocks"] == 2
    # block 100 was evicted: reading it again must re-forward
    await client.post("/control/open", json={
        "session_id": "p", "budget": 10 ** 9, "blocks": {"eth": 100},
    })
    before = len(upstream.received)
    await client.post("/rpc/p/eth", json=_call([{"to": "0xq"}]))
    assert len(upstream.received) == before + 1


# ---------------------------------------------------------------------------
# (h2) immutable per-chain constants (eth_chainId / net_version) cache
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chain_constant_cached_after_first_fetch(proxy_client):
    _, client, upstream = proxy_client
    await client.post("/control/open", json={"session_id": "c", "budget": 100})
    for i in range(5):
        resp = await client.post("/rpc/c/eth", json=_rpc("eth_chainId", _id=i))
        body = await resp.json()
        assert body["result"] == "0xabc"  # the (stubbed) upstream constant
        if i > 0:  # first call is the raw upstream forward (echoes the stub's id)
            assert body["id"] == i        # synthesized answers echo the request id
    assert len(upstream.received) == 1    # only the first call forwarded


@pytest.mark.asyncio
async def test_chain_constant_cache_keyed_per_chain_and_method(proxy_client):
    _, client, upstream = proxy_client
    await client.post("/control/open", json={"session_id": "c", "budget": 100})
    for chain in ("eth", "base"):
        for method in ("eth_chainId", "net_version"):
            await client.post(f"/rpc/c/{chain}", json=_rpc(method))
            await client.post(f"/rpc/c/{chain}", json=_rpc(method))
    assert len(upstream.received) == 4  # one fetch per (chain, method), repeats cached


@pytest.mark.asyncio
async def test_chain_constant_cache_shared_across_sessions(proxy_client):
    _, client, upstream = proxy_client
    await client.post("/control/open", json={"session_id": "a", "budget": 100})
    await client.post("/control/open", json={"session_id": "b", "budget": 100})
    await client.post("/rpc/a/eth", json=_rpc("eth_chainId"))
    await client.post("/rpc/b/eth", json=_rpc("eth_chainId"))
    assert len(upstream.received) == 1  # session b served from a's fetch


@pytest.mark.asyncio
async def test_chain_constant_served_under_pin(proxy_client):
    _, client, upstream = proxy_client
    await _open_pinned(client, "p", 12345)
    await client.post("/rpc/p/eth", json=_rpc("eth_chainId"))
    before = len(upstream.received)
    resp = await client.post("/rpc/p/eth", json=_rpc("eth_chainId"))
    body = await resp.json()
    assert body["result"] == "0xabc"
    assert len(upstream.received) == before  # cached, not forwarded


@pytest.mark.asyncio
async def test_chain_constant_still_budget_gated_when_exhausted(upstream):
    proxy, client = await _make_proxy_client(upstream.url, mode="enforce", budget=1)
    try:
        await client.post("/control/open", json={"session_id": "x", "budget": 1, "mode": "enforce"})
        await client.post("/rpc/x/eth", json=_rpc("eth_call"))   # spends the budget
        await client.post("/rpc/x/eth", json=_rpc("eth_call"))   # exceeds -> exhausted
        before = len(upstream.received)
        resp = await client.post("/rpc/x/eth", json=_rpc("eth_chainId"))
        body = await resp.json()
        assert body["error"]["message"] == "MINOTAUR_BUDGET_EXCEEDED"  # sticky
        assert len(upstream.received) == before  # not forwarded, not cache-served
    finally:
        await client.close()


# ---------------------------------------------------------------------------
# (h3) RPC_PROXY_RESPONSE_CACHE=0 kill switch: both caches off, old behavior
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_response_cache_kill_switch_restores_forward_everything(upstream):
    proxy = BudgetProxy(
        {"eth": upstream.url}, response_cache_enabled=False,
    )
    client = TestClient(TestServer(proxy.build_app()))
    await client.start_server()
    try:
        await _open_pinned(client, "p", 12345)
        # pinned reads: every repeat forwards (pin still applied)
        await client.post("/rpc/p/eth", json=_call([{"to": "0xq"}, "latest"]))
        await client.post("/rpc/p/eth", json=_call([{"to": "0xq"}, "latest"]))
        assert json.loads(upstream.received[-1])["params"][1] == "0x3039"
        # chain constants: every repeat forwards too
        await client.post("/rpc/p/eth", json=_rpc("eth_chainId"))
        await client.post("/rpc/p/eth", json=_rpc("eth_chainId"))
        assert len(upstream.received) == 4
        assert proxy._pin_cache.stats()["entries"] == 0  # nothing cached
    finally:
        await client.close()


def test_response_cache_env_switch(monkeypatch):
    from minotaur_subnet.harness.rpc_budget_proxy.proxy import (
        _response_cache_enabled_from_env,
    )
    monkeypatch.delenv("RPC_PROXY_RESPONSE_CACHE", raising=False)
    assert _response_cache_enabled_from_env() is True  # default: on
    for off in ("0", "false", "no", "OFF"):
        monkeypatch.setenv("RPC_PROXY_RESPONSE_CACHE", off)
        assert _response_cache_enabled_from_env() is False
    monkeypatch.setenv("RPC_PROXY_RESPONSE_CACHE", "1")
    assert _response_cache_enabled_from_env() is True


# ---------------------------------------------------------------------------
# (i) control-plane auth + session registry cap
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_control_token_guards_control_plane(upstream):
    proxy = BudgetProxy({"eth": upstream.url}, control_token="s3kr3t")
    client = TestClient(TestServer(proxy.build_app()))
    await client.start_server()
    try:
        assert (await client.post("/control/open", json={"session_id": "s"})).status == 403
        r = await client.post("/control/open", json={"session_id": "s"},
                              headers={"X-Control-Token": "nope"})
        assert r.status == 403
        r = await client.post("/control/open", json={"session_id": "s"},
                              headers={"X-Control-Token": "s3kr3t"})
        assert r.status == 200
        # data plane is NOT token-guarded (the untrusted solver uses it freely)
        assert (await client.post("/rpc/s/eth", json=_rpc("eth_call"))).status == 200
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_session_registry_capped(upstream):
    from minotaur_subnet.harness.rpc_budget_proxy import proxy as proxymod
    proxy = BudgetProxy({"eth": upstream.url})
    client = TestClient(TestServer(proxy.build_app()))
    await client.start_server()
    try:
        for i in range(proxymod.MAX_SESSIONS + 5):
            await client.post("/control/open", json={"session_id": f"s{i}"})
        assert len(proxy.sessions) <= proxymod.MAX_SESSIONS  # bounded
        assert "s0" not in proxy.sessions  # oldest evicted
        assert f"s{proxymod.MAX_SESSIONS + 4}" in proxy.sessions  # newest kept
    finally:
        await client.close()
