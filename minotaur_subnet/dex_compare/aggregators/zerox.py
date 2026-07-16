"""0x Swap API (v2) client — requires an API key.

``GET /swap/allowance-holder/price`` is the indicative endpoint (no ``taker``
needed). Output is ``buyAmount`` (gross); ``gas`` is the estimated gas units.
Requires both the ``0x-api-key`` and ``0x-version: v2`` headers. Without a key
the source is reported as ``unsupported``.
"""

from __future__ import annotations

import logging

import aiohttp

from ..backoff import request_with_backoff
from ..models import QuoteOutcome, TradeDescriptor, to_int
from .base import AggregatorClient

logger = logging.getLogger(__name__)

ZEROX_CHAINS: frozenset[int] = frozenset({1, 8453, 42161, 10})


class ZeroxClient(AggregatorClient):
    name = "0x"

    def __init__(self, api_key: str | None, base_url: str, max_retries: int) -> None:
        self._api_key = api_key
        self._base = base_url.rstrip("/")
        self._max_retries = max_retries

    def is_configured(self) -> bool:
        return bool(self._api_key)

    def supports(self, chain_id: int) -> bool:
        return chain_id in ZEROX_CHAINS

    async def quote(
        self, session: aiohttp.ClientSession, trade: TradeDescriptor,
    ) -> QuoteOutcome:
        if not self.is_configured():
            return self._unsupported("no ZEROX_API_KEY configured")
        if not self.supports(trade.chain_id):
            return self._unsupported(f"chain {trade.chain_id} not supported")
        started = self._now()
        try:
            url = f"{self._base}/swap/allowance-holder/price"
            params = {
                "chainId": str(trade.chain_id),
                "sellToken": trade.input_token,
                "buyToken": trade.output_token,
                "sellAmount": trade.input_amount,
            }
            headers = {"0x-api-key": self._api_key or "", "0x-version": "v2"}
            result = await request_with_backoff(
                session, "GET", url, params=params, headers=headers,
                max_retries=self._max_retries,
            )
            latency = self._elapsed_ms(started)
            if not result.ok:
                return self._error(result.error, latency)
            data = result.data or {}
            # 0x v2 `buyAmount` is NET of the zeroExFee; `grossBuyAmount` is the
            # pre-fee gross. Use gross for the raw board, buyAmount for after-fee.
            buy = to_int(data.get("buyAmount"))
            if buy is None or buy <= 0:
                return self._failed("no buyAmount / no liquidity", latency)
            gross = to_int(data.get("grossBuyAmount"))
            out = gross if (gross is not None and gross >= buy) else buy
            # 0x returns a routing "source" list; best-effort protocol label.
            dex = None
            route = (data.get("route") or {}).get("fills")
            if isinstance(route, list) and route:
                dex = route[0].get("source")
            zerox_fee = (data.get("fees") or {}).get("zeroExFee") or {}
            return self._ok(
                str(out),                        # gross (grossBuyAmount when given)
                output_after_fee_raw=str(buy),   # buyAmount is net of zeroExFee
                gas_units=to_int(data.get("gas")),
                # totalNetworkFee = exact gas cost in NATIVE (ETH) wei.
                gas_native_wei=(str(to_int(data.get("totalNetworkFee")))
                                if data.get("totalNetworkFee") is not None else None),
                protocol_fee_raw=(str(zerox_fee.get("amount"))
                                  if zerox_fee.get("amount") is not None else None),
                is_net_of_gas=False,
                dex=dex or "0x",
                latency_ms=latency,
            )
        except Exception as exc:  # noqa: BLE001
            return self._error(f"{type(exc).__name__}: {exc}", self._elapsed_ms(started))
