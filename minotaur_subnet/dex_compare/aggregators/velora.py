"""Velora (formerly Paraswap) v6 Market API client — keyless.

``GET /prices`` is the indicative endpoint and needs no wallet. Output is
``priceRoute.destAmount`` (gross, before the taker's gas); ``priceRoute.gasCost``
is the estimated gas in units.
"""

from __future__ import annotations

import logging

import aiohttp

from ..backoff import request_with_backoff
from ..models import QuoteOutcome, TradeDescriptor, to_float, to_int
from .base import AggregatorClient


def _to_float(value):
    return to_float(value)

logger = logging.getLogger(__name__)

VELORA_NETWORKS: frozenset[int] = frozenset({1, 8453, 42161, 10})


class VeloraClient(AggregatorClient):
    name = "velora"

    def __init__(self, base_url: str, max_retries: int) -> None:
        self._base = base_url.rstrip("/")
        self._max_retries = max_retries

    def supports(self, chain_id: int) -> bool:
        return chain_id in VELORA_NETWORKS

    async def quote(
        self, session: aiohttp.ClientSession, trade: TradeDescriptor,
    ) -> QuoteOutcome:
        if not self.supports(trade.chain_id):
            return self._unsupported(f"chain {trade.chain_id} not supported")
        started = self._now()
        try:
            url = f"{self._base}/prices"
            params = {
                "srcToken": trade.input_token,
                "destToken": trade.output_token,
                "amount": trade.input_amount,
                "srcDecimals": str(trade.input_decimals),
                "destDecimals": str(trade.output_decimals),
                "side": "SELL",
                "network": str(trade.chain_id),
            }
            result = await request_with_backoff(
                session, "GET", url, params=params, max_retries=self._max_retries,
            )
            latency = self._elapsed_ms(started)
            if not result.ok:
                return self._error(result.error, latency)
            route = (result.data or {}).get("priceRoute") or {}
            dest = to_int(route.get("destAmount"))
            if dest is None or dest <= 0:
                return self._failed("no destAmount", latency)
            # destAmountAfterFee = output net of Velora's (default "anon") partner
            # fee. Fall back to gross when absent so we never overstate.
            after_fee = to_int(route.get("destAmountAfterFee"))
            dex = None
            best = route.get("bestRoute")
            if isinstance(best, list) and best:
                swaps = (best[0] or {}).get("swaps")
                if isinstance(swaps, list) and swaps:
                    exch = (swaps[0].get("swapExchanges") or [{}])[0]
                    dex = exch.get("exchange")
            return self._ok(
                str(dest),
                output_after_fee_raw=str(after_fee) if after_fee is not None else None,
                gas_units=to_int(route.get("gasCost")),
                is_net_of_gas=False,
                input_usd=_to_float(route.get("srcUSD")),
                output_usd=_to_float(route.get("destUSD")),
                gas_usd=_to_float(route.get("gasCostUSD")),
                price_impact_reached=bool(route.get("maxImpactReached")),
                dex=dex or "velora",
                latency_ms=latency,
            )
        except Exception as exc:  # noqa: BLE001
            return self._error(f"{type(exc).__name__}: {exc}", self._elapsed_ms(started))
