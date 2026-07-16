"""Endpoint tests for the ``rejection_class`` field + filter on GET /v1/orders.

Every order entry carries a structured ``rejection_class``, the response carries
a ``rejection_class_counts`` breakdown (over the app_id/status match set, before
the class filter), and ``?rejection_class=`` narrows the returned page. This is
what lets a dashboard compute an honest service-success rate (excluding
``duplicate``, which was already served) without string-matching ``error``.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from minotaur_subnet.orderbook.orderbook import IntentOrderBook, OrderStatus


_OWNER = "0xB763F651776690F7b142e5D40A7C096Aa963f04e"


@pytest.fixture
def client():
    from minotaur_subnet.api.routes import orders as order_routes

    ob = IntentOrderBook()
    order_routes.set_orderbook(ob)
    order_routes.set_app_store(None)  # OrderBook-only path (no store merge)

    # Seed a spread of terminal states across rejection classes.
    def _mk(error: str | None, status: OrderStatus):
        o = ob.submit(
            app_id="app_x", intent_function="swap", params={},
            submitted_by=_OWNER, chain_id=8453,
        )
        ob.update_order(o.order_id, status=status, error=error)
        return o.order_id

    _mk("Relayer submission failed: plan_hash already submitted (re-submittable after 30s)", OrderStatus.REJECTED)
    _mk("Relayer submission failed: plan_hash already submitted (re-submittable after 12s)", OrderStatus.REJECTED)
    _mk("Consensus not reached", OrderStatus.REJECTED)
    _mk("User cannot fund order (insufficient input-token balance/allowance) at settlement: x", OrderStatus.REJECTED)
    # A successful order — no class, must not appear in counts.
    ob.update_order(ob.submit(
        app_id="app_x", intent_function="swap", params={},
        submitted_by=_OWNER, chain_id=8453,
    ).order_id, status=OrderStatus.FILLED, tx_hash="0xdead")

    app = FastAPI()
    app.include_router(order_routes.router, prefix="/v1")
    return TestClient(app)


def test_every_entry_carries_rejection_class(client):
    body = client.get("/v1/orders").json()
    assert body["count"] == 5
    assert all("rejection_class" in o for o in body["orders"])
    # The filled order classifies to None.
    filled = [o for o in body["orders"] if o["status"] == "filled"]
    assert filled and filled[0]["rejection_class"] is None


def test_rejection_class_counts_breakdown(client):
    body = client.get("/v1/orders").json()
    assert body["rejection_class_counts"] == {"duplicate": 2, "infra": 1, "user": 1}


def test_filter_by_rejection_class(client):
    body = client.get("/v1/orders", params={"rejection_class": "duplicate"}).json()
    assert body["total"] == 2
    assert body["count"] == 2
    assert {o["rejection_class"] for o in body["orders"]} == {"duplicate"}

    infra = client.get("/v1/orders", params={"rejection_class": "infra"}).json()
    assert infra["total"] == 1
    assert infra["orders"][0]["rejection_class"] == "infra"


def test_counts_are_stable_under_filter(client):
    """The breakdown is computed BEFORE the class filter, so drilling into one
    class doesn't collapse the chart totals."""
    body = client.get("/v1/orders", params={"rejection_class": "duplicate"}).json()
    assert body["rejection_class_counts"] == {"duplicate": 2, "infra": 1, "user": 1}


def test_unknown_class_returns_empty_page_not_error(client):
    body = client.get("/v1/orders", params={"rejection_class": "nope"}).json()
    assert body["total"] == 0
    assert body["orders"] == []
    # Counts still reflect the real distribution.
    assert body["rejection_class_counts"] == {"duplicate": 2, "infra": 1, "user": 1}
