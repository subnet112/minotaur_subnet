"""Dataclasses shared across the DEX-compare service."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Canonical source ordering. "minotaur" is always first; the rest are the
# external aggregators. Kept as a tuple so it's a stable, hashable constant.
SOURCES: tuple[str, ...] = ("minotaur", "cow", "velora", "1inch", "0x")
AGGREGATOR_SOURCES: tuple[str, ...] = ("cow", "velora", "1inch", "0x")

# Order statuses that represent real terminal demand — the corpus we sample.
TERMINAL_STATUSES: frozenset[str] = frozenset({"filled", "rejected", "expired"})

# QuoteOutcome.status values.
#   ok          — a usable positive output was returned
#   failed      — the source ran but produced no route / zero output
#   error       — an HTTP/parse/transport error (after backoff)
#   unsupported — source can't quote this (chain unsupported or no API key)
#   warming_up  — Minotaur only: /quote returned 503 (solver not ready yet)
STATUS_OK = "ok"
STATUS_FAILED = "failed"
STATUS_ERROR = "error"
STATUS_UNSUPPORTED = "unsupported"
STATUS_WARMING_UP = "warming_up"


def to_int(value: Any) -> int | None:
    """Best-effort parse of a wei/gas value (int or decimal string) to int."""
    if value is None:
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        try:
            # Some APIs return floats for gas; truncate.
            return int(float(value))
        except (TypeError, ValueError):
            return None


def str_or_none(value: Any) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    return s or None


@dataclass
class TradeDescriptor:
    """A fully-resolved swap ready to be quoted by every source."""

    order_id: str
    app_id: str
    intent_function: str
    chain_id: int
    input_token: str        # resolved ERC-20 address (wrapped native if sentinel)
    output_token: str
    input_amount: str       # base units, decimal string
    input_decimals: int
    output_decimals: int
    input_symbol: str | None
    output_symbol: str | None
    input_is_native: bool   # resolved token == wrapped native for the chain
    output_is_native: bool


@dataclass
class QuoteOutcome:
    """The result of quoting one source for one trade."""

    source: str
    status: str
    output_raw: str | None = None      # base units of the OUTPUT token (decimal string)
    gas_units: int | None = None       # estimated gas units for the swap
    fee_raw: str | None = None         # CoW feeAmount / Minotaur platform_fee_wei
    is_net_of_gas: bool = False        # True only for CoW (gasless/solver-pays-gas)
    dex: str | None = None             # protocol/route label
    latency_ms: int | None = None
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "output_raw": self.output_raw,
            "gas_units": self.gas_units,
            "fee_raw": self.fee_raw,
            "is_net_of_gas": self.is_net_of_gas,
            "dex": self.dex,
            "latency_ms": self.latency_ms,
            "error": self.error,
        }


@dataclass
class ComparisonRow:
    """One recorded comparison across all sources for a single trade."""

    created_at: float
    trade: TradeDescriptor
    gas_price_wei: str | None
    outcomes: dict[str, QuoteOutcome] = field(default_factory=dict)
