"""Unit tests for #228 follower order-sync (OrderSync)."""

import asyncio
from unittest.mock import MagicMock

from minotaur_subnet.blockloop.order_sync import OrderSync


def _run(coro):
    return asyncio.run(coro)


def _sync(*, is_follower=True, leader_url="http://leader:8080", orders=None, app_store=None):
    captured = {}

    async def fake_get(url, api_key):
        captured["url"] = url
        captured["api_key"] = api_key
        return orders if orders is not None else []

    s = OrderSync(
        app_store=app_store if app_store is not None else MagicMock(),
        leader_api_url=lambda: leader_url,
        is_follower=lambda: is_follower,
        internal_api_key=lambda: "secret-key",
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
    # hit the leader's internal endpoint with the internal key
    assert cap["url"] == "http://leader:8080/v1/internal/orders"
    assert cap["api_key"] == "secret-key"


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
        internal_api_key=lambda: "k", http_get=lambda u, k: asyncio.sleep(0, result=[]),
    )
    assert _run(s.sync_once()) == 0
