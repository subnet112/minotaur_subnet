"""Order-book sync for follower validators (#228 cross-validator corpus).

Only the leader runs the BlockLoop and persists orders — including the FAILED
ones (rejected/expired) that never reach the chain. Followers need that full order
set to build a representative Stage-2 benchmark corpus (the diverse-subset
adoption vote), but the leader only broadcasts *successful* plan proposals and
there is no event for failed orders, so a follower's store would otherwise be
empty.

This loop has each FOLLOWER periodically pull the leader's full order set over the
authenticated internal route and upsert it into its own ``app_store``.
``save_order`` is an UPSERT keyed by ``order_id``, so re-pulling is idempotent and
self-healing, and always following the *current* elected leader handles leader
changes naturally. The leader does not sync (it is the source).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)


class OrderSync:
    def __init__(
        self,
        *,
        app_store: Any,
        leader_api_url: Callable[[], str | None],
        is_follower: Callable[[], bool],
        interval: float = 30.0,
        http_get: Callable[[str], Awaitable[list[dict[str, Any]]]] | None = None,
    ) -> None:
        self._app_store = app_store
        self._leader_api_url = leader_api_url      # () -> current leader's API base, or None
        self._is_follower = is_follower            # () -> True only when this node is a follower
        self._interval = interval
        self._http_get = http_get or self._default_http_get

    async def run_loop(self) -> None:
        while True:
            try:
                await self.sync_once()
            except Exception as exc:  # never let the loop die
                # %r, not %s: connection-level exceptions (TimeoutError,
                # ServerDisconnectedError, ClientConnectorError) have an empty
                # str(), so %s logged a blank line and hid the real cause.
                logger.warning("Order sync loop error: %r", exc)
            await asyncio.sleep(self._interval)

    async def sync_once(self) -> int:
        """Pull the leader's orders and upsert them locally. Returns the count.

        No-ops (returns 0) on the leader itself, when no leader URL resolves, or
        when the app store is unavailable.
        """
        if self._app_store is None or not self._is_follower():
            return 0
        url = (self._leader_api_url() or "").rstrip("/")
        if not url:
            return 0
        # The order book is PUBLIC (no auth) — /v1/orders already strips
        # user_signature. Followers pull it to build their benchmark corpus.
        orders = await self._http_get(f"{url}/v1/orders")
        if not orders:
            return 0
        n = 0
        for order in orders:
            if not isinstance(order, dict) or not order.get("order_id"):
                continue
            try:
                self._app_store.save_order(order)
                n += 1
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("Order sync: save_order failed for %s: %s",
                               order.get("order_id"), exc)
        if n:
            logger.info("Order sync: upserted %d orders from leader %s", n, url)
        return n

    @staticmethod
    async def _default_http_get(url: str) -> list[dict[str, Any]]:
        import aiohttp
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=20)) as resp:
                if resp.status != 200:
                    logger.warning("Order sync: leader %s returned HTTP %s", url, resp.status)
                    return []
                data = await resp.json()
        return data.get("orders", []) if isinstance(data, dict) else []
