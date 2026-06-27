"""Unit tests for AppIntentStore.count_orders_since (order-volume emission ramp)."""

import time

from minotaur_subnet.store.app_intent_store import AppIntentStore


def _store(tmp_path):
    return AppIntentStore(store_path=tmp_path / "store.db")


def _order(order_id: str, created_at: float, app_id: str = "app1") -> dict:
    return {
        "order_id": order_id,
        "app_id": app_id,
        "status": "FILLED",
        "created_at": created_at,
    }


def test_counts_only_orders_within_window(tmp_path):
    store = _store(tmp_path)
    now = time.time()
    store.save_order(_order("recent1", now - 100))
    store.save_order(_order("recent2", now - 3600))
    store.save_order(_order("old", now - 90_000))  # >24h ago

    assert store.count_orders_since(now - 86400.0) == 2


def test_empty_store_returns_zero(tmp_path):
    store = _store(tmp_path)
    assert store.count_orders_since(time.time() - 86400.0) == 0


def test_app_id_filter(tmp_path):
    store = _store(tmp_path)
    now = time.time()
    store.save_order(_order("a", now - 100, app_id="app1"))
    store.save_order(_order("b", now - 100, app_id="app2"))

    assert store.count_orders_since(now - 86400.0) == 2
    assert store.count_orders_since(now - 86400.0, app_id="app1") == 1
