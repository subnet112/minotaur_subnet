"""1inch Classic Swap API (v6) client — requires an API key.

``GET /swap/{version}/{chainId}/quote`` is the wallet-free indicative endpoint.
Output is ``dstAmount`` (gross); ``gas`` is the estimated gas units. Without a
key the source is reported as ``unsupported`` (graceful, like the frontend).
"""

from __future__ import annotations

import logging

import aiohttp

from ..backoff import request_with_backoff
from ..models import QuoteOutcome, TradeDescriptor, to_int
from .base import AggregatorClient

logger = logging.getLogger(__name__)

ONEINCH_CHAINS: frozenset[int] = frozenset({1, 8453, 42161, 10})


class OneInchClient(AggregatorClient):
    name = "1inch"

    def __init__(
        self, api_key: str | None, base_url: str, version: str, max_retries: int,
    ) -> None:
        self._api_key = api_key
        self._base = base_url.rstrip("/")
        self._version = version.strip("/")
        self._max_retries = max_retries

    def is_configured(self) -> bool:
        return bool(self._api_key)

    def supports(self, chain_id: int) -> bool:
        return chain_id in ONEINCH_CHAINS

    async def quote(
        self, session: aiohttp.ClientSession, trade: TradeDescriptor,
    ) -> QuoteOutcome:
        if not self.is_configured():
            return self._unsupported("no ONEINCH_API_KEY configured")
        if not self.supports(trade.chain_id):
            return self._unsupported(f"chain {trade.chain_id} not supported")
        started = self._now()
        try:
            url = f"{self._base}/swap/{self._version}/{trade.chain_id}/quote"
            params = {
                "src": trade.input_token,
                "dst": trade.output_token,
                "amount": trade.input_amount,
                "includeGas": "true",   # v6 returns the `gas` estimate only when asked
            }
            headers = {"Authorization": f"Bearer {self._api_key}"}
            result = await request_with_backoff(
                session, "GET", url, params=params, headers=headers,
                max_retries=self._max_retries,
            )
            latency = self._elapsed_ms(started)
            if not result.ok:
                return self._error(result.error, latency)
            data = result.data or {}
            out = to_int(data.get("dstAmount"))
            if out is None or out <= 0:
                return self._failed("no dstAmount", latency)
            return self._ok(
                str(out),
                gas_units=to_int(data.get("gas")),
                is_net_of_gas=False,
                dex="1inch",
                latency_ms=latency,
            )
        except Exception as exc:  # noqa: BLE001
            return self._error(f"{type(exc).__name__}: {exc}", self._elapsed_ms(started))
