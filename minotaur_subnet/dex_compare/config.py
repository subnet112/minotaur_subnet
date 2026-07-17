"""Configuration for the DEX-compare service — all env-driven, all with defaults."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env_true(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "").strip() or default)
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip() or default)
    except (TypeError, ValueError):
        return default


def _env_str(name: str, default: str) -> str:
    return (os.environ.get(name) or "").strip() or default


def _env_int_map(name: str, default: dict[int, int]) -> dict[int, int]:
    """Parse ``"1:1500,8453:3000"`` into ``{1: 1500, 8453: 3000}`` (int keys+vals)."""
    raw = os.environ.get(name)
    if not raw or not raw.strip():
        return dict(default)
    out: dict[int, int] = {}
    for pair in raw.split(","):
        if ":" not in pair:
            continue
        k, v = pair.split(":", 1)
        k, v = k.strip(), v.strip()
        if k.lstrip("-").isdigit() and v.lstrip("-").isdigit():
            out[int(k)] = int(v)
    return out or dict(default)


def _env_str_map(name: str, default: dict[int, str]) -> dict[int, str]:
    """Parse ``"1:0xapp,8453:0xapp2"`` into ``{1: "0xapp", 8453: "0xapp2"}``."""
    raw = os.environ.get(name)
    if not raw or not raw.strip():
        return dict(default)
    out: dict[int, str] = {}
    for pair in raw.split(","):
        if ":" not in pair:
            continue
        k, v = pair.split(":", 1)
        k, v = k.strip(), v.strip()
        if k.lstrip("-").isdigit() and v:
            out[int(k)] = v
    return out or dict(default)


@dataclass(frozen=True)
class DexCompareConfig:
    """Immutable runtime config for the DEX-compare worker + store."""

    enabled: bool
    interval_seconds: float
    jitter_seconds: float
    startup_delay_seconds: float
    api_base_url: str
    slippage_bps: int
    http_timeout: float
    max_retries: int
    retain_days: int
    max_rows: int
    supported_chain_ids: tuple[int, ...]
    store_path: str

    # ── trade-size normalization ─────────────────────────────────────────
    # Historical orders are ~99% dust ($1 trades) where fixed gas+fee dwarf the
    # output. When enabled, each order is rescaled to ~target_usd (priced via
    # Velora's srcUSD, cached) so gas/fees become negligible and the comparison
    # reflects routing quality, not fixed costs.
    normalize_size: bool
    target_usd: float
    price_cache_ttl: float          # seconds to cache a token's USD price
    max_price_impact_bps: int       # flag rows above this (Velora maxImpactReached always flags)

    # Aggregator config — keys optional; an absent key means that source is
    # reported as "unsupported" (never a hard failure).
    cow_base_url: str
    velora_base_url: str
    oneinch_api_key: str | None
    oneinch_base_url: str
    oneinch_version: str
    zerox_api_key: str | None
    zerox_base_url: str

    # ── trade source (pluggable) ─────────────────────────────────────────
    # "historical" replays our own terminal orders (default). "cow_onchain"
    # samples REAL executed trades from CoW GPv2Settlement events — a neutral,
    # liquid corpus that also yields a coverage metric (trades we can't serve).
    # All fields below have defaults so existing constructions need no changes.
    source: str = "historical"
    # Optional per-chain app surface to requote CoW trades through. Empty ->
    # borrow a chain-matching app_id from a recent order automatically.
    cow_app_ids: dict = field(default_factory=dict)
    # Block lookback per chain (block times differ: ~12s ETH vs ~2s Base).
    cow_lookback_blocks: dict = field(default_factory=lambda: {1: 1500, 8453: 3000})
    cow_lookback_default: int = 1500        # fallback lookback for unlisted chains
    cow_max_block_span: int = 2000          # per-request getLogs chunk cap
    cow_min_block_span: int = 100           # floor when adaptively halving on range caps
    cow_dedup_by_pair: bool = False         # OFF -> uniform over distinct trades (recommended)


def load_config() -> DexCompareConfig:
    """Build a :class:`DexCompareConfig` from the process environment."""
    # Lazy import to avoid a route<->package import cycle at module load.
    from minotaur_subnet.api.routes.submissions.state import _resolve_persist_path

    store_path = (
        _resolve_persist_path("dex_compare.db", "DEX_COMPARE_STORE_PATH")
        or "/data/dex_compare.db"
    )

    chains_raw = _env_str("DEX_COMPARE_CHAINS", "8453")
    supported = tuple(
        int(c.strip()) for c in chains_raw.split(",") if c.strip().lstrip("-").isdigit()
    ) or (8453,)

    return DexCompareConfig(
        enabled=_env_true("ENABLE_DEX_COMPARE", False),
        interval_seconds=_env_float("DEX_COMPARE_INTERVAL", 90.0),
        jitter_seconds=_env_float("DEX_COMPARE_JITTER", 30.0),
        startup_delay_seconds=_env_float("DEX_COMPARE_STARTUP_DELAY", 60.0),
        api_base_url=_env_str("DEX_COMPARE_API_BASE", "http://127.0.0.1:8080"),
        slippage_bps=_env_int("DEX_COMPARE_SLIPPAGE_BPS", 50),
        http_timeout=_env_float("DEX_COMPARE_HTTP_TIMEOUT", 20.0),
        max_retries=_env_int("DEX_COMPARE_MAX_RETRIES", 4),
        retain_days=_env_int("DEX_COMPARE_RETAIN_DAYS", 90),
        max_rows=_env_int("DEX_COMPARE_MAX_ROWS", 500_000),
        supported_chain_ids=supported,
        store_path=store_path,
        normalize_size=_env_true("DEX_COMPARE_NORMALIZE", True),
        target_usd=_env_float("DEX_COMPARE_TARGET_USD", 5000.0),
        price_cache_ttl=_env_float("DEX_COMPARE_PRICE_TTL", 600.0),
        max_price_impact_bps=_env_int("DEX_COMPARE_MAX_IMPACT_BPS", 300),
        cow_base_url=_env_str("COW_BASE_URL", "https://api.cow.fi"),
        velora_base_url=_env_str("VELORA_BASE_URL", "https://api.velora.xyz"),
        oneinch_api_key=os.environ.get("ONEINCH_API_KEY") or None,
        oneinch_base_url=_env_str("ONEINCH_BASE_URL", "https://api.1inch.dev"),
        oneinch_version=_env_str("ONEINCH_VERSION", "v6.0"),
        zerox_api_key=os.environ.get("ZEROX_API_KEY") or None,
        zerox_base_url=_env_str("ZEROX_BASE_URL", "https://api.0x.org"),
        source=_env_str("DEX_COMPARE_SOURCE", "historical"),
        cow_app_ids=_env_str_map("DEX_COMPARE_COW_APP_ID", {}),
        cow_lookback_blocks=_env_int_map("DEX_COMPARE_COW_LOOKBACK_BLOCKS", {1: 1500, 8453: 3000}),
        cow_lookback_default=_env_int("DEX_COMPARE_COW_LOOKBACK", 1500),
        cow_max_block_span=_env_int("DEX_COMPARE_COW_MAX_SPAN", 2000),
        cow_min_block_span=_env_int("DEX_COMPARE_COW_MIN_SPAN", 100),
        cow_dedup_by_pair=_env_true("DEX_COMPARE_COW_DEDUP_PAIR", False),
    )
