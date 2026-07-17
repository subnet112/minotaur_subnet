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

from dataclasses import replace

from minotaur_subnet.blockchain.tokens import WRAPPED_NATIVE_TOKEN

from .aggregators import build_aggregators
from .config import DexCompareConfig
from .minotaur_client import fetch_minotaur_quote
from .models import STATUS_WARMING_UP, ComparisonRow, TradeDescriptor
from .sources import build_source, is_candidate
from .store import DexCompareStore
from .tokens_resolve import DecimalsCache, resolve_trade_tokens

logger = logging.getLogger(__name__)

# USDC per chain — the stable used to price the native token for gas/fee conversion.
_USDC = {
    1: "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
    8453: "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
    42161: "0xaf88d065e77c8cC2239327C5EDb3A432268e5831",
    10: "0x0b2C639c533813f4Aa9D7837CAf62653d097Ff85",
}


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
        self._velora = next((a for a in self._aggregators if a.name == "velora"), None)
        # Non-deterministic on purpose — unlike the consensus-seeded corpus in
        # order_sampler.py, we want independent random draws here.
        self._rng = random.Random()
        # Pluggable trade source: "historical" (our orders) | "cow_onchain"
        # (real executed CoW trades). Selected by config.source.
        self._source = build_source(config, app_store, self._rng)
        self._session: aiohttp.ClientSession | None = None
        # (chain, token) -> (usd_per_base_unit, monotonic_ts) — for size normalization.
        self._price_cache: dict[tuple[int, str], tuple[float, float]] = {}
        # chain -> (native_usd, monotonic_ts) — ETH/native price for gas/fee conversion.
        self._native_cache: dict[int, tuple[float, float]] = {}

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
        """Run ONE comparison per enabled chain that has a sampled trade.

        The trade SOURCE yields at most one order-shaped trade per chain (a
        random draw), so every enabled chain advances each cycle regardless of
        corpus share. Returns the number of rows written this cycle.
        """
        orders = await self._source.sample(self._cfg.supported_chain_ids)
        if not orders:
            return 0

        written = 0
        for order in orders:
            try:
                if await self._run_one_comparison(order):
                    written += 1
            except Exception as exc:  # noqa: BLE001 — one trade must not kill the rest
                logger.exception("dex-compare comparison error: %s", exc)

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
        trade.trade_source = self._source.name

        assert self._session is not None
        # Rescale the (typically dust-sized) historical order to a realistic USD
        # notional so gas/fees don't dominate. CoW trades are already real,
        # liquid executed sizes — never rescale them.
        if self._cfg.normalize_size and self._source.name != "cow_onchain":
            trade = await self._normalize(trade)

        mino = await fetch_minotaur_quote(self._session, self._cfg, trade)
        if mino.status == STATUS_WARMING_UP:
            logger.info("dex-compare: solver warming up (503) — skipping")
            return False

        agg_outcomes = await self._fan_out(trade)
        gas_price = await self._snapshot_gas_price(trade.chain_id)
        native_usd = await self._native_usd(trade.chain_id)

        outcomes = {"minotaur": mino}
        for outcome in agg_outcomes:
            outcomes[outcome.source] = outcome

        row = ComparisonRow(
            created_at=time.time(),
            trade=trade,
            gas_price_wei=gas_price,
            outcomes=outcomes,
            native_usd=native_usd,
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

    # ── size normalization ───────────────────────────────────────────────
    async def _normalize(self, trade: TradeDescriptor) -> TradeDescriptor:
        """Rescale the input to ~target_usd. Returns the trade unchanged (with
        notional_usd left None) when the input token can't be priced."""
        upbu = await self._input_price(trade)   # USD per input base-unit
        if not upbu or upbu <= 0:
            return trade
        scaled = int(self._cfg.target_usd / upbu)
        if scaled <= 0:
            return trade
        return replace(
            trade,
            input_amount=str(scaled),
            notional_usd=self._cfg.target_usd,
            original_input_amount=trade.input_amount,
        )

    async def _input_price(self, trade: TradeDescriptor) -> float | None:
        """USD value of one input base-unit, via Velora's srcUSD (cached per token)."""
        key = (trade.chain_id, trade.input_token.lower())
        hit = self._price_cache.get(key)
        now = time.monotonic()
        if hit and now - hit[1] < self._cfg.price_cache_ttl:
            return hit[0]
        if self._velora is None or not self._velora.supports(trade.chain_id):
            return None
        try:
            amt = int(trade.input_amount)
        except (TypeError, ValueError):
            return None
        if amt <= 0:
            return None
        try:
            outcome = await self._velora.quote(self._session, trade)
        except Exception:  # noqa: BLE001
            return None
        if outcome.status == "ok" and outcome.input_usd and outcome.input_usd > 0:
            upbu = outcome.input_usd / amt
            self._price_cache[key] = (upbu, now)
            return upbu
        return None

    async def _native_usd(self, chain_id: int) -> float | None:
        """USD price of the chain's native token, via Velora WETH->USDC (cached)."""
        hit = self._native_cache.get(chain_id)
        now = time.monotonic()
        if hit and now - hit[1] < self._cfg.price_cache_ttl:
            return hit[0]
        weth = WRAPPED_NATIVE_TOKEN.get(chain_id)
        usdc = _USDC.get(chain_id)
        if not (weth and usdc) or self._velora is None or not self._velora.supports(chain_id):
            return None
        probe = TradeDescriptor(
            order_id="", app_id="", intent_function="swap", chain_id=chain_id,
            input_token=weth, output_token=usdc, input_amount=str(10 ** 18),
            input_decimals=18, output_decimals=6, input_symbol="WETH",
            output_symbol="USDC", input_is_native=True, output_is_native=False,
        )
        try:
            outcome = await self._velora.quote(self._session, probe)
        except Exception:  # noqa: BLE001
            return None
        if outcome.status == "ok" and outcome.input_usd and outcome.input_usd > 0:
            self._native_cache[chain_id] = (outcome.input_usd, now)  # srcUSD of 1 WETH = ETH price
            return outcome.input_usd
        return None

    # ── helpers ──────────────────────────────────────────────────────────

    def _is_candidate(self, order: dict[str, Any]) -> bool:
        # Candidate logic now lives in sources.is_candidate (shared with the
        # HistoricalOrderSource). Kept as a thin delegator for back-compat.
        return is_candidate(order, self._cfg.supported_chain_ids)

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
