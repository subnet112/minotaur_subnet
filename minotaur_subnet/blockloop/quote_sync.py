"""Quote-case sync for follower validators (quote-demand benchmark corpus).

The quote CASE store (``AppIntentStore.list_quotes``) is populated only where the
``/quote`` endpoint is actually served — in practice the leader (frontend + the
DEX-compare worker both quote against it). Followers need that same quote set to
build an identical Stage-2 quote draw (``order_sampler.sample_historical_quotes``)
and thus an identical benchmark pack hash, or they diverge the moment the
BENCHMARK_QUOTE_CORPUS flag is on.

This loop mirrors :class:`minotaur_subnet.blockloop.order_sync.OrderSync` exactly:
each FOLLOWER periodically pulls the leader's full quote set over the public
``/v1/quotes`` route and upserts it into its own ``app_store``. ``save_quote`` is
an UPSERT keyed by the content-addressed ``quote_id``, so re-pulling is idempotent
and self-healing, and following the *current* elected leader handles leader
changes naturally. The leader does not sync (it is the source).

Runs unconditionally (it is cheap and side-effect-free) so that by the time the
corpus flag is flipped ON fleet-wide, every follower already holds the full quote
history — no cold-start divergence.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

# /v1/quotes is paginated (max limit 500). The sync needs FULL records (the params
# blob) to rebuild a usable corpus, so it pages through with full=1 at max size.
_SYNC_PAGE_SIZE = 500


class QuoteSync:
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
                logger.warning("Quote sync loop error: %r", exc)
            await asyncio.sleep(self._interval)

    async def sync_once(self) -> int:
        """Pull the leader's quote cases and upsert them locally. Returns the count.

        No-ops (returns 0) on the leader itself, when no leader URL resolves, or
        when the app store cannot store quotes.
        """
        if (
            self._app_store is None
            or not hasattr(self._app_store, "save_quote")
            or not self._is_follower()
        ):
            return 0
        url = (self._leader_api_url() or "").rstrip("/")
        if not url:
            return 0
        # /v1/quotes is PUBLIC (quote cases carry no PII — a quote never had a
        # submitted_by or signature). Page at the endpoint's max limit; the
        # seen-set both dedupes and terminates against a pre-pagination leader
        # that ignores limit/offset and returns the whole set on every page.
        quotes: list[dict[str, Any]] = []
        seen: set[str] = set()
        page_offset = 0
        while True:
            page = await self._http_get(
                f"{url}/v1/quotes?full=1&limit={_SYNC_PAGE_SIZE}&offset={page_offset}"
            )
            fresh = [
                q for q in page
                if isinstance(q, dict) and q.get("quote_id") and q["quote_id"] not in seen
            ]
            if not fresh:
                break
            quotes.extend(fresh)
            seen.update(q["quote_id"] for q in fresh)
            if len(page) < _SYNC_PAGE_SIZE:
                break
            page_offset += _SYNC_PAGE_SIZE
        if not quotes:
            return 0
        n = 0
        for quote in quotes:
            if not isinstance(quote, dict) or not quote.get("quote_id"):
                continue
            try:
                self._app_store.save_quote(quote)
                n += 1
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("Quote sync: save_quote failed for %s: %s",
                               quote.get("quote_id"), exc)
        if n:
            logger.info("Quote sync: upserted %d quote cases from leader %s", n, url)
        return n

    @staticmethod
    async def _default_http_get(url: str) -> list[dict[str, Any]]:
        import aiohttp
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=20)) as resp:
                if resp.status != 200:
                    logger.warning("Quote sync: leader %s returned HTTP %s", url, resp.status)
                    return []
                data = await resp.json()
        return data.get("quotes", []) if isinstance(data, dict) else []
