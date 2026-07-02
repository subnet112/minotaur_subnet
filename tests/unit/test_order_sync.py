"""Unit tests for #228 follower order-sync (OrderSync)."""

import asyncio
from unittest.mock import MagicMock

from minotaur_subnet.blockloop.order_sync import OrderSync


def _run(coro):
    return asyncio.run(coro)


def _sync(*, is_follower=True, leader_url="http://leader:8080", orders=None, app_store=None):
    captured = {}

    async def fake_get(url):
        captured["url"] = url
        return orders if orders is not None else []

    s = OrderSync(
        app_store=app_store if app_store is not None else MagicMock(),
        leader_api_url=lambda: leader_url,
        is_follower=lambda: is_follower,
        http_get=fake_get,
    )
    return s, captured


def test_follower_pulls_and_upserts():
    store = MagicMock()
    orders = [{"order_id": "a", "status": "filled"}, {"order_id": "b", "status": "rejected"}]
    s, cap = _sync(orders=orders, app_store=store)
    n = _run(s.sync_once())
    assert n == 2
    # upserted both (including the rejected one — the #228 point)
    saved = [c.args[0]["order_id"] for c in store.save_order.call_args_list]
    assert saved == ["a", "b"]
    # hit the leader's PUBLIC order book (no auth — anyone can read it),
    # asking for FULL records (the list view defaults to a slim summary)
    assert cap["url"] == "http://leader:8080/v1/orders?full=1&limit=500&offset=0"


def test_paginates_until_short_page():
    # 600 orders on a paginating leader: two fetches (500 + 100), all upserted.
    store = MagicMock()
    pages = {
        0: [{"order_id": f"a{i}"} for i in range(500)],
        500: [{"order_id": f"b{i}"} for i in range(100)],
    }
    urls = []

    async def fake_get(url):
        urls.append(url)
        return pages.get(int(url.rsplit("offset=", 1)[1]), [])

    s = OrderSync(
        app_store=store,
        leader_api_url=lambda: "http://leader:8080",
        is_follower=lambda: True,
        http_get=fake_get,
    )
    assert _run(s.sync_once()) == 600
    assert len(urls) == 2
    assert "offset=0" in urls[0] and "offset=500" in urls[1]
    assert all("full=1" in u for u in urls)


def test_pre_pagination_leader_terminates():
    # A leader that predates pagination ignores limit/offset and returns the
    # SAME full set on every fetch. The seen-set dedupes it and the loop stops
    # on the first fetch that yields nothing new — no infinite loop, no
    # duplicate upserts. 502 > page size, so the loop does attempt a 2nd page.
    store = MagicMock()
    full = [{"order_id": f"o{i}"} for i in range(502)]
    calls = []

    async def fake_get(url):
        calls.append(url)
        return list(full)

    s = OrderSync(
        app_store=store,
        leader_api_url=lambda: "http://leader:8080",
        is_follower=lambda: True,
        http_get=fake_get,
    )
    assert _run(s.sync_once()) == 502
    assert len(calls) == 2  # second fetch returned nothing new → stop
    assert store.save_order.call_count == 502


def test_leader_does_not_sync():
    store = MagicMock()
    s, cap = _sync(is_follower=False, orders=[{"order_id": "a"}], app_store=store)
    assert _run(s.sync_once()) == 0
    store.save_order.assert_not_called()
    assert "url" not in cap  # never even fetched


def test_no_leader_url_noops():
    store = MagicMock()
    s, _ = _sync(leader_url=None, orders=[{"order_id": "a"}], app_store=store)
    assert _run(s.sync_once()) == 0
    store.save_order.assert_not_called()


def test_skips_malformed_orders():
    store = MagicMock()
    orders = [{"order_id": "a"}, {"no_id": True}, "junk", {"order_id": ""}]
    s, _ = _sync(orders=orders, app_store=store)
    assert _run(s.sync_once()) == 1  # only the one valid order
    assert store.save_order.call_count == 1


def test_continues_past_save_error():
    store = MagicMock()
    store.save_order.side_effect = [RuntimeError("db"), None]
    orders = [{"order_id": "a"}, {"order_id": "b"}]
    s, _ = _sync(orders=orders, app_store=store)
    n = _run(s.sync_once())  # must not raise
    assert n == 1 and store.save_order.call_count == 2  # tried both, counted the success


def test_no_app_store_noops():
    s = OrderSync(
        app_store=None, leader_api_url=lambda: "http://x", is_follower=lambda: True,
        http_get=lambda u: asyncio.sleep(0, result=[]),
    )
    assert _run(s.sync_once()) == 0
