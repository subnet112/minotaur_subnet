"""DEX-compare service — benchmark the Minotaur solver against external DEX aggregators.

A leader-only background loop that replays real historical orders through both our
``/quote`` endpoint and the major DEX aggregators (CoW, Velora/Paraswap, 1inch, 0x),
persists every comparison to SQLite, and exposes per-chain stats. See
``minotaur_subnet/api/routes/dex_compare.py`` for the query endpoint and
``docs``/the plan for the methodology.
"""

from __future__ import annotations

from .config import DexCompareConfig, load_config
from .store import DexCompareStore
from .worker import DexCompareWorker

__all__ = ["DexCompareConfig", "load_config", "DexCompareStore", "DexCompareWorker"]
