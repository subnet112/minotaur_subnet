"""Order persistence for the block loop pipeline."""

from __future__ import annotations

import logging
from typing import Any

from minotaur_subnet.orderbook.orderbook import IntentOrderBook, Order, OrderStatus
from minotaur_subnet.store import AppIntentStore

logger = logging.getLogger(__name__)


class OrderPersistence:
    """Handles order persistence to/from the AppIntentStore.

    Args:
        app_store: Persistent store for app definitions and stats.
        orderbook: The Intent OrderBook.
    """

    def __init__(
        self,
        app_store: AppIntentStore,
        orderbook: IntentOrderBook,
    ) -> None:
        self.app_store = app_store
        self.orderbook = orderbook

    def sync(self, order_id: str) -> None:
        """Persist current order state to AppIntentStore (OB-11, OB-12)."""
        order = self.orderbook.get(order_id)
        if order is not None:
            try:
                self.app_store.save_order(order.to_dict())
            except Exception as exc:
                logger.warning("Failed to persist order %s: %s", order_id, exc)

    def load_open_orders(self, orderbook: IntentOrderBook) -> int:
        """Load persisted OPEN orders from store into the OrderBook (OB-12).

        Called on leader transition so the new leader can reprocess
        all outstanding orders. Returns the number of orders loaded.
        """
        stored = self.app_store.list_orders(status="open")
        loaded = 0
        for order_dict in stored:
            order_id = order_dict.get("order_id", "")
            # Skip if already in the OrderBook
            if orderbook.get(order_id) is not None:
                continue
            # Re-inject into the OrderBook as OPEN
            from minotaur_subnet.orderbook.orderbook import Order
            try:
                order = Order(
                    order_id=order_id,
                    app_id=order_dict.get("app_id", ""),
                    intent_function=order_dict.get("intent_function", "execute"),
                    params=order_dict.get("params", {}),
                    submitted_by=order_dict.get("submitted_by", ""),
                    chain_id=order_dict.get("chain_id", 1),
                    status=OrderStatus.OPEN,
                    perpetual=order_dict.get("perpetual", False),
                    max_executions=order_dict.get("max_executions", 1),
                    cooldown=order_dict.get("cooldown", 0.0),
                    deadline=order_dict.get("deadline", 0.0),
                    last_filled_at=order_dict.get("last_filled_at", 0.0),
                    execution_count=order_dict.get("execution_count", 0),
                )
                orderbook._orders[order_id] = order
                loaded += 1
            except Exception as exc:
                logger.warning("Failed to reload order %s: %s", order_id, exc)
        if loaded > 0:
            logger.info("Reloaded %d OPEN orders from store", loaded)
        return loaded
