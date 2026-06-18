"""Persistent, background-refreshed cache of the solver's supported-token list.

Token discovery (``solver.supported_tokens(chain_id)``) is a slow on-chain
scan — factory pool enumeration plus per-token ``symbol``/``decimals`` RPC
calls, bounded by the 180s harness timeout. Computing it on the request path
(even with the solver's in-memory 5-min cache) means a user reload hits a cold
recompute and the token selector is empty for up to a minute.

This moves discovery OFF the request path: a background task refreshes the list
per chain on a timer and persists it (``AppIntentStore.token_lists``). The
``/v1/chains/{id}/tokens`` endpoint then serves the last persisted list
instantly — surviving api restarts and champion swaps — while this refreshes
it in the background. The solver is read from the BlockLoop on each tick, so a
newly adopted champion's token set is picked up automatically.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from typing import Any

logger = logging.getLogger(__name__)


class TokenListCache:
    """Background refresher + persistence for per-chain supported-token lists."""

    def __init__(
        self,
        store: Any,
        block_loop: Any,
        chain_ids: list[int],
        refresh_interval: float = 300.0,
    ) -> None:
        self._store = store
        # Hold the BlockLoop (not the solver) so we always read the *current*
        # champion's solver — it can be hot-swapped under us on adoption.
        self._bl = block_loop
        self._chain_ids = [int(c) for c in chain_ids]
        self._interval = max(30.0, float(refresh_interval))
        self._stopped = False
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        """Refresh once now (off the request path), then loop on the interval."""
        await self.refresh_all()
        self._task = asyncio.create_task(self._run_loop())

    async def stop(self) -> None:
        self._stopped = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass

    async def _run_loop(self) -> None:
        while not self._stopped:
            try:
                await asyncio.sleep(self._interval)
                if self._stopped:
                    break
                await self.refresh_all()
            except asyncio.CancelledError:
                return
            except Exception as exc:  # never let the loop die
                logger.warning("Token cache refresh tick failed: %s", exc)

    async def refresh_all(self) -> None:
        for chain_id in self._chain_ids:
            try:
                await self.refresh_chain(chain_id)
            except Exception as exc:
                logger.warning(
                    "Token cache refresh failed for chain %d: %s", chain_id, exc,
                )

    async def refresh_chain(self, chain_id: int) -> int:
        """Recompute + persist the token list for one chain. Returns token count.

        Returns -1 (and persists nothing) when no solver is available or it
        doesn't support discovery — leaving any previously-persisted list intact
        so the endpoint keeps serving the last good value.
        """
        solver = getattr(self._bl, "solver", None) if self._bl is not None else None
        fn = getattr(solver, "supported_tokens", None) if solver is not None else None
        if fn is None:
            return -1

        call = fn(chain_id)
        tokens = await call if inspect.isawaitable(call) else call
        tokens = list(tokens or [])

        # Don't overwrite a good cached list with an empty result (transient RPC
        # failure inside the solver returning []). Only persist non-empty, or
        # when there's nothing cached yet.
        if not tokens and self._store.get_token_list(chain_id) is not None:
            logger.info(
                "Token cache: chain %d returned 0 tokens; keeping previous list",
                chain_id,
            )
            return 0

        self._store.save_token_list(chain_id, tokens, updated_at=time.time())
        logger.info(
            "Token cache refreshed + persisted for chain %d: %d tokens",
            chain_id, len(tokens),
        )
        return len(tokens)
