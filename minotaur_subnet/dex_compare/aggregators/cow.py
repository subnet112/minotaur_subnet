"""CoW Swap (CoW Protocol) client — keyless.

NOTE: CoW's ``buyAmount`` is NET of gas — the solver pays on-chain gas and the
fee is deducted from ``sellAmountBeforeFee`` before the buy amount is computed.
We therefore set ``is_net_of_gas=True`` so the net-of-gas leaderboard does not
subtract gas from it a second time.
"""

from __future__ import annotations

import logging

import aiohttp

from ..backoff import request_with_backoff
from ..models import QuoteOutcome, TradeDescriptor, str_or_none, to_int
from .base import AggregatorClient

logger = logging.getLogger(__name__)

# CoW uses network-name slugs, not chain ids. Optimism is NOT supported.
COW_SLUGS: dict[int, str] = {1: "mainnet", 8453: "base", 42161: "arbitrum_one"}

# Any address works for an unattended price check (we never sign/place the order).
_PLACEHOLDER_TRADER = "0x0000000000000000000000000000000000000001"


class CowClient(AggregatorClient):
    name = "cow"

    def __init__(self, base_url: str, max_retries: int) -> None:
        self._base = base_url.rstrip("/")
        self._max_retries = max_retries

    def supports(self, chain_id: int) -> bool:
        return chain_id in COW_SLUGS

    async def quote(
        self, session: aiohttp.ClientSession, trade: TradeDescriptor,
    ) -> QuoteOutcome:
        if not self.supports(trade.chain_id):
            return self._unsupported(f"chain {trade.chain_id} not supported")
        started = self._now()
        try:
            url = f"{self._base}/{COW_SLUGS[trade.chain_id]}/api/v1/quote"
            body = {
                "sellToken": trade.input_token,
                "buyToken": trade.output_token,
                "sellAmountBeforeFee": trade.input_amount,
                "from": _PLACEHOLDER_TRADER,
                "kind": "sell",
            }
            result = await request_with_backoff(
                session, "POST", url, json_body=body, max_retries=self._max_retries,
            )
            latency = self._elapsed_ms(started)
            if not result.ok:
                return self._error(result.error, latency)
            quote = (result.data or {}).get("quote") or {}
            buy = to_int(quote.get("buyAmount"))
            if buy is None or buy <= 0:
                return self._failed("no buyAmount", latency)
            return self._ok(
                str(buy),
                # buyAmount is already the user's net receive (gasless, fee taken
                # from the sell side) — it IS the after-fee, net-of-gas output.
                output_after_fee_raw=str(buy),
                gas_units=None,  # gasless — no on-chain gas the taker pays
                gas_native_wei="0",
                fee_raw=str_or_none(quote.get("feeAmount")),
                protocol_fee_raw=str_or_none(quote.get("feeAmount")),
                is_net_of_gas=True,
                dex="cow",
                latency_ms=latency,
            )
        except Exception as exc:  # noqa: BLE001 — never propagate to gather
            return self._error(f"{type(exc).__name__}: {exc}", self._elapsed_ms(started))
