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


def to_float(value: Any) -> float | None:
    """Best-effort parse of a numeric/string value to float (None on failure)."""
    if value is None:
        return None
    try:
        return float(value)
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
    # Size-normalization metadata (set when the worker rescales the order to a
    # target USD notional). input_amount above is the amount actually QUOTED.
    notional_usd: float | None = None       # USD value the trade was scaled to
    original_input_amount: str | None = None  # the untouched historical amount
    # Which source produced this trade — "historical" | "cow_onchain".
    # None == legacy rows (predate the pluggable source == historical).
    trade_source: str | None = None


@dataclass
class QuoteOutcome:
    """The result of quoting one source for one trade."""

    source: str
    status: str
    output_raw: str | None = None      # base units of the OUTPUT token (GROSS, decimal string)
    gas_units: int | None = None       # estimated gas units for the swap
    fee_raw: str | None = None         # CoW feeAmount / Minotaur platform_fee_wei
    is_net_of_gas: bool = False        # True only for CoW (gasless/solver-pays-gas)
    dex: str | None = None             # protocol/route label
    latency_ms: int | None = None
    error: str | None = None
    # ── net-comparison fields (populated where the source provides them) ──
    output_after_fee_raw: str | None = None  # output net of the source's OWN protocol fee
    gas_native_wei: str | None = None        # gas cost in native (ETH) wei, if given directly (0x)
    protocol_fee_raw: str | None = None      # the source's protocol fee amount (informational)
    input_usd: float | None = None           # USD value of the input (Velora srcUSD)
    output_usd: float | None = None          # USD value of the output (Velora destUSD)
    gas_usd: float | None = None             # gas cost in USD (Velora gasCostUSD)
    price_impact_reached: bool = False       # source flagged excessive price impact

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "output_raw": self.output_raw,
            "output_after_fee_raw": self.output_after_fee_raw,
            "gas_units": self.gas_units,
            "gas_native_wei": self.gas_native_wei,
            "fee_raw": self.fee_raw,
            "protocol_fee_raw": self.protocol_fee_raw,
            "is_net_of_gas": self.is_net_of_gas,
            "input_usd": self.input_usd,
            "output_usd": self.output_usd,
            "gas_usd": self.gas_usd,
            "price_impact_reached": self.price_impact_reached,
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
    native_usd: float | None = None   # USD price of the chain's native token (for gas/fee conversion)
