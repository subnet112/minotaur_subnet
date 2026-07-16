"""External DEX-aggregator clients."""

from __future__ import annotations

from ..config import DexCompareConfig
from .base import AggregatorClient
from .cow import CowClient
from .oneinch import OneInchClient
from .velora import VeloraClient
from .zerox import ZeroxClient

__all__ = [
    "AggregatorClient",
    "CowClient",
    "VeloraClient",
    "OneInchClient",
    "ZeroxClient",
    "build_aggregators",
]


def build_aggregators(cfg: DexCompareConfig) -> list[AggregatorClient]:
    """Instantiate every aggregator client from config (keys optional)."""
    return [
        CowClient(cfg.cow_base_url, cfg.max_retries),
        VeloraClient(cfg.velora_base_url, cfg.max_retries),
        OneInchClient(
            cfg.oneinch_api_key, cfg.oneinch_base_url, cfg.oneinch_version, cfg.max_retries,
        ),
        ZeroxClient(cfg.zerox_api_key, cfg.zerox_base_url, cfg.max_retries),
    ]
