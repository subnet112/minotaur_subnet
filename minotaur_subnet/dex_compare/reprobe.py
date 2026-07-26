"""Active re-probe of open blindspots (leader only).

Blindspot detection is passive: a pair flips open→covered only when the CoW
corpus happens to re-sample it, so long-tail pairs lag by days — and a champion
change (the event that actually closes gaps) isn't reflected until the market
re-trades every pair. Two mechanisms close that lag:

* **steady drip** — every worker cycle, requote the N least-recently-probed
  open pairs against OUR solver only (no aggregator calls; the question is just
  "can we route this now?").
* **on-adoption sweep** — when the active champion's ``submission_id`` changes
  (polled from ``GET /v1/solver/champion`` over the same loopback the quotes
  use), queue EVERY open pair and drain ``sweep_batch`` of them per cycle, so
  the whole board re-reads within a few cycles of an adoption.

Probe rows are stamped ``trade_source="reprobe"``: blindspot classification
counts them alongside ``cow_onchain`` rows, while the stats endpoint (coverage,
leaderboards, counts) excludes them entirely — probes are synthetic re-asks of
known demand, not new demand.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import aiohttp

from .blindspots import build_blindspots_response
from .config import DexCompareConfig
from .minotaur_client import fetch_minotaur_quote
from .models import STATUS_WARMING_UP, ComparisonRow
from .store import DexCompareStore
from .tokens_resolve import DecimalsCache, SymbolCache, resolve_trade_tokens

logger = logging.getLogger(__name__)

# Same detection window the /dex-compare/blindspots endpoint defaults to.
_WINDOW_DAYS = 14
_RECENT_DAYS = 7
# Pause between consecutive probes in one cycle — each probe is a fork-sim on
# the shared anvil; a sweep must not starve real /quote traffic.
_INTER_PROBE_SLEEP = 2.0

PairKey = tuple[int, str, str]  # (chain_id, input_token, output_token)


class BlindspotReprober:
    def __init__(
        self,
        store: DexCompareStore,
        cfg: DexCompareConfig,
        decimals: DecimalsCache,
        symbols: SymbolCache,
    ) -> None:
        self._store = store
        self._cfg = cfg
        self._decimals = decimals
        self._symbols = symbols
        self._last_probed: dict[PairKey, float] = {}
        self._sweep_queue: list[PairKey] = []
        self._last_champion: str | None = None

    # ── champion change detection ────────────────────────────────────────
    async def _champion_id(self, session: aiohttp.ClientSession) -> str | None:
        url = f"{self._cfg.api_base_url.rstrip('/')}/v1/solver/champion"
        try:
            async with session.get(url) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
        except Exception:  # noqa: BLE001 — champion polling is best-effort
            return None
        sid = data.get("submission_id")
        return str(sid) if sid else None

    # ── open-pair discovery ──────────────────────────────────────────────
    def _open_pairs(self) -> dict[PairKey, dict[str, Any]]:
        """Current open blindspots, each with the newest row's quote surface
        (app_id / intent_function / input_amount) to requote through."""
        since = time.time() - _WINDOW_DAYS * 86400
        rows = self._store.fetch_since(None, since, None)
        response = build_blindspots_response(rows, _WINDOW_DAYS, _RECENT_DAYS, limit=10_000)
        open_keys = {
            (int(chain["chain_id"]), p["input"], p["output"])
            for chain in response["chains"]
            for p in chain["open"]
        }
        surfaces: dict[PairKey, dict[str, Any]] = {}
        for row in rows:  # newest-first: first surface seen per pair wins
            key = (int(row["chain_id"]), row.get("input_token"), row.get("output_token"))
            if key in open_keys and key not in surfaces and row.get("app_id"):
                surfaces[key] = {
                    "app_id": row["app_id"],
                    "intent_function": row.get("intent_function") or "swap",
                    "input_amount": row.get("input_amount"),
                }
        return surfaces

    # ── probing ──────────────────────────────────────────────────────────
    async def _probe(
        self, session: aiohttp.ClientSession, key: PairKey, surface: dict[str, Any],
    ) -> bool:
        """Requote one open pair against the solver and persist a reprobe row.
        Returns False when the solver is warming up (abort the batch)."""
        chain_id, input_token, output_token = key
        order = {
            "order_id": "",
            "app_id": surface["app_id"],
            "chain_id": chain_id,
            "intent_function": surface["intent_function"],
            "params": {
                "input_token": input_token,
                "output_token": output_token,
                "input_amount": surface["input_amount"],
            },
        }
        trade = await resolve_trade_tokens(order, self._decimals, self._symbols)
        self._last_probed[key] = time.time()  # even unresolvable pairs rotate on
        if trade is None:
            return True
        trade.trade_source = "reprobe"
        mino = await fetch_minotaur_quote(session, self._cfg, trade)
        if mino.status == STATUS_WARMING_UP:
            return False
        row = ComparisonRow(
            created_at=time.time(),
            trade=trade,
            gas_price_wei=None,          # minotaur-only probe; no net math needed
            outcomes={"minotaur": mino},
            native_usd=None,
        )
        await asyncio.to_thread(self._store.insert, row)
        logger.info(
            "dex-compare reprobe: chain %d %s->%s -> %s",
            chain_id,
            trade.input_symbol or input_token,
            trade.output_symbol or output_token,
            mino.status,
        )
        return True

    # ── one tick, called once per worker cycle ───────────────────────────
    async def tick(self, session: aiohttp.ClientSession) -> int:
        """Probe a batch of open blindspots. Returns rows written."""
        if self._cfg.reprobe_per_cycle <= 0:
            return 0

        champion = await self._champion_id(session)
        surfaces = await asyncio.to_thread(self._open_pairs)

        if champion and self._last_champion and champion != self._last_champion:
            # Adoption: the whole open board is stale — sweep everything.
            self._sweep_queue = list(surfaces)
            logger.info(
                "dex-compare reprobe: champion changed (%s -> %s) — sweeping %d open pairs",
                self._last_champion, champion, len(self._sweep_queue),
            )
        if champion:
            self._last_champion = champion

        if self._sweep_queue:
            batch_keys = [k for k in self._sweep_queue[: self._cfg.reprobe_sweep_batch]]
            self._sweep_queue = self._sweep_queue[self._cfg.reprobe_sweep_batch:]
        else:
            batch_keys = sorted(surfaces, key=lambda k: self._last_probed.get(k, 0.0))[
                : self._cfg.reprobe_per_cycle
            ]

        written = 0
        for i, key in enumerate(batch_keys):
            surface = surfaces.get(key)
            if surface is None:  # pair closed/aged out since the sweep was queued
                continue
            if i > 0:
                await asyncio.sleep(_INTER_PROBE_SLEEP)
            try:
                if not await self._probe(session, key, surface):
                    logger.info("dex-compare reprobe: solver warming up — batch aborted")
                    break
                written += 1
            except Exception as exc:  # noqa: BLE001 — one pair must not kill the batch
                logger.exception("dex-compare reprobe error for %s: %s", key, exc)
        return written
