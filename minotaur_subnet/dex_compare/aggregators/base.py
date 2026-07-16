"""AggregatorClient ABC — one implementation per external DEX aggregator."""

from __future__ import annotations

import time
from abc import ABC, abstractmethod

import aiohttp

from ..models import (
    STATUS_ERROR,
    STATUS_FAILED,
    STATUS_OK,
    STATUS_UNSUPPORTED,
    QuoteOutcome,
    TradeDescriptor,
)


class AggregatorClient(ABC):
    """Base class: each subclass fetches one quote for one trade.

    ``quote()`` must NEVER raise — every failure path returns a
    :class:`QuoteOutcome`, so one source's error/429/timeout can't disturb the
    others when they run under ``asyncio.gather``.
    """

    #: stable source key, matches ``models.SOURCES``
    name: str = "aggregator"

    @abstractmethod
    def supports(self, chain_id: int) -> bool:
        """Whether this aggregator can quote on ``chain_id``."""

    def is_configured(self) -> bool:
        """Whether the client has what it needs (e.g. an API key). Default True."""
        return True

    @abstractmethod
    async def quote(
        self, session: aiohttp.ClientSession, trade: TradeDescriptor,
    ) -> QuoteOutcome:
        """Fetch a sell-side quote. Never raises."""

    # ── outcome helpers ──────────────────────────────────────────────────
    def _unsupported(self, reason: str) -> QuoteOutcome:
        return QuoteOutcome(self.name, STATUS_UNSUPPORTED, error=reason)

    def _error(self, reason: str | None, latency_ms: int | None = None) -> QuoteOutcome:
        return QuoteOutcome(self.name, STATUS_ERROR, latency_ms=latency_ms, error=reason)

    def _failed(self, reason: str, latency_ms: int | None = None) -> QuoteOutcome:
        return QuoteOutcome(self.name, STATUS_FAILED, latency_ms=latency_ms, error=reason)

    def _ok(
        self,
        output_raw: str,
        *,
        output_after_fee_raw: str | None = None,
        gas_units: int | None = None,
        gas_native_wei: str | None = None,
        fee_raw: str | None = None,
        protocol_fee_raw: str | None = None,
        is_net_of_gas: bool = False,
        input_usd: float | None = None,
        output_usd: float | None = None,
        gas_usd: float | None = None,
        price_impact_reached: bool = False,
        dex: str | None = None,
        latency_ms: int | None = None,
    ) -> QuoteOutcome:
        return QuoteOutcome(
            self.name,
            STATUS_OK,
            output_raw=output_raw,
            # default the after-fee output to gross when the source has no fee
            output_after_fee_raw=output_after_fee_raw if output_after_fee_raw is not None else output_raw,
            gas_units=gas_units,
            gas_native_wei=gas_native_wei,
            fee_raw=fee_raw,
            protocol_fee_raw=protocol_fee_raw,
            is_net_of_gas=is_net_of_gas,
            input_usd=input_usd,
            output_usd=output_usd,
            gas_usd=gas_usd,
            price_impact_reached=price_impact_reached,
            dex=dex or self.name,
            latency_ms=latency_ms,
        )

    @staticmethod
    def _now() -> float:
        return time.monotonic()

    @staticmethod
    def _elapsed_ms(started: float) -> int:
        return int((time.monotonic() - started) * 1000)
