"""DexCompareWorker — the always-on slow loop (leader only).

Mirrors the shape of ``harness/benchmark_worker.py`` (``run_loop`` / ``stop``).
Every cycle: pick a random historical order, quote Minotaur + all aggregators
for that trade, snapshot the gas price, and persist one comparison row. All
blocking work (store I/O, gas-price RPC, order listing) is pushed off the event
loop with ``asyncio.to_thread``.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Any

import aiohttp

from minotaur_subnet.blockchain.chains import get_web3

from .aggregators import build_aggregators
from .config import DexCompareConfig
from .minotaur_client import fetch_minotaur_quote
from .models import STATUS_WARMING_UP, ComparisonRow, TERMINAL_STATUSES
from .store import DexCompareStore
from .tokens_resolve import DecimalsCache, resolve_trade_tokens

logger = logging.getLogger(__name__)


class DexCompareWorker:
    def __init__(
        self,
        app_store: Any,
        store: DexCompareStore,
        config: DexCompareConfig,
    ) -> None:
        self._app_store = app_store
        self._store = store
        self._cfg = config
        self._running = False
        self._decimals = DecimalsCache()
        self._aggregators = build_aggregators(config)
        # Non-deterministic on purpose — unlike the consensus-seeded corpus in
        # order_sampler.py, we want independent random draws here.
        self._rng = random.Random()
        self._session: aiohttp.ClientSession | None = None

    # ── lifecycle ────────────────────────────────────────────────────────
    async def run_loop(self, interval: float | None = None) -> None:
        interval = interval if interval is not None else self._cfg.interval_seconds
        self._running = True
        # /quote needs block_loop.solver, which is wired late in startup.
        await self._interruptible_sleep(self._cfg.startup_delay_seconds)
        timeout = aiohttp.ClientTimeout(total=self._cfg.http_timeout)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                self._session = session
                logger.info(
                    "dex-compare worker started (interval=%.0fs, chains=%s, aggregators=%s)",
                    interval,
                    list(self._cfg.supported_chain_ids),
                    [a.name for a in self._aggregators],
                )
                while self._running:
                    try:
                        await self.run_once()
                    except Exception as exc:  # noqa: BLE001 — loop must never die
                        logger.exception("dex-compare loop error: %s", exc)
                    await self._interruptible_sleep(
                        interval + self._rng.uniform(0, self._cfg.jitter_seconds)
                    )
        finally:
            self._session = None

    def stop(self) -> None:
        self._running = False

    async def _interruptible_sleep(self, total: float) -> None:
        remaining = total
        while remaining > 0 and self._running:
            await asyncio.sleep(min(1.0, remaining))
            remaining -= 1.0

    # ── one cycle ────────────────────────────────────────────────────────
    async def run_once(self) -> int:
        """Run ONE comparison per enabled chain that has candidates.

        A single uniform draw over the whole corpus would starve minority chains
        (the order book is ~99% one chain), so we draw independently PER CHAIN —
        every enabled chain advances each cycle regardless of its share of the
        corpus. Returns the number of rows written this cycle.
        """
        orders = await asyncio.to_thread(self._app_store.list_orders)
        by_chain: dict[int, list[dict[str, Any]]] = {}
        for order in orders:
            if self._is_candidate(order):
                by_chain.setdefault(int(order["chain_id"]), []).append(order)
        if not by_chain:
            return 0

        written = 0
        # Iterate the configured chains (stable order); one random draw within each.
        for chain_id in self._cfg.supported_chain_ids:
            pool = by_chain.get(chain_id)
            if not pool:
                continue
            order = self._rng.choice(pool)
            try:
                if await self._run_one_comparison(order):
                    written += 1
            except Exception as exc:  # noqa: BLE001 — one chain must not kill the rest
                logger.exception(
                    "dex-compare comparison error (chain %s): %s", chain_id, exc,
                )

        # Occasional prune (~1/50 cycles) — keeps growth bounded off the hot path.
        if written and self._rng.random() < 0.02:
            cutoff = time.time() - self._cfg.retain_days * 86400
            deleted = await asyncio.to_thread(
                self._store.prune, cutoff, self._cfg.max_rows,
            )
            if deleted:
                logger.info("dex-compare pruned %d old rows", deleted)
        return written

    async def _run_one_comparison(self, order: dict[str, Any]) -> bool:
        """Quote Minotaur + all aggregators for one order and persist a row.

        Returns True if a row was written; False when the order can't be resolved
        or the solver is warming up (503).
        """
        trade = await resolve_trade_tokens(order, self._decimals)
        if trade is None:
            return False

        assert self._session is not None
        mino = await fetch_minotaur_quote(self._session, self._cfg, trade)
        if mino.status == STATUS_WARMING_UP:
            logger.info("dex-compare: solver warming up (503) — skipping")
            return False

        agg_outcomes = await self._fan_out(trade)
        gas_price = await self._snapshot_gas_price(trade.chain_id)

        outcomes = {"minotaur": mino}
        for outcome in agg_outcomes:
            outcomes[outcome.source] = outcome

        row = ComparisonRow(
            created_at=time.time(),
            trade=trade,
            gas_price_wei=gas_price,
            outcomes=outcomes,
        )
        await asyncio.to_thread(self._store.insert, row)
        logger.debug(
            "dex-compare recorded %s/%s on chain %d (mino=%s)",
            trade.input_symbol or trade.input_token,
            trade.output_symbol or trade.output_token,
            trade.chain_id,
            mino.status,
        )
        return True

    # ── helpers ──────────────────────────────────────────────────────────

    def _is_candidate(self, order: dict[str, Any]) -> bool:
        if str(order.get("status", "")).lower() not in TERMINAL_STATUSES:
            return False
        try:
            chain_id = int(order.get("chain_id"))
        except (TypeError, ValueError):
            return False
        if chain_id not in self._cfg.supported_chain_ids:
            return False
        params = order.get("params") or {}
        if not (
            params.get("input_token")
            and params.get("output_token")
            and params.get("input_amount") is not None
        ):
            return False
        # Cross-chain orders can't be quoted apples-to-apples by same-chain
        # aggregators — filter them out.
        if params.get("dest_chain_id") is not None:
            return False
        in_chain = params.get("input_chain_id")
        out_chain = params.get("output_chain_id")
        if in_chain is not None and out_chain is not None:
            try:
                if int(in_chain) != int(out_chain):
                    return False
            except (TypeError, ValueError):
                return False
        return True

    async def _fan_out(self, trade: Any) -> list[Any]:
        return await asyncio.gather(
            *(agg.quote(self._session, trade) for agg in self._aggregators),
            return_exceptions=False,  # clients never raise
        )

    async def _snapshot_gas_price(self, chain_id: int) -> str | None:
        try:
            w3 = get_web3(chain_id)
            gas_price = await asyncio.to_thread(lambda: w3.eth.gas_price)
            return str(int(gas_price))
        except Exception as exc:  # noqa: BLE001 — net-of-gas stats degrade gracefully
            logger.debug("gas-price snapshot failed on chain %d: %s", chain_id, exc)
            return None
