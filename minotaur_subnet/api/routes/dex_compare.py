"""Query endpoints for the DEX-compare service (leader only).

The worker persists comparisons to SQLite; these read-only endpoints aggregate
them into per-chain stats for the frontend. If the service isn't running on this
node (followers), the store is unset and the endpoints report that plainly.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from fastapi import APIRouter, Query

from minotaur_subnet.dex_compare.blindspots import build_blindspots_response
from minotaur_subnet.dex_compare.stats import build_stats_response

router = APIRouter(tags=["dex-compare"])

# Set at startup by the API (leader only). None on followers / when disabled.
_store: Any = None


def set_store(store: Any) -> None:
    """Wire the DexCompareStore so the endpoints can read it (server startup)."""
    global _store
    _store = store


@router.get("/dex-compare/stats")
async def dex_compare_stats(
    window_days: int = Query(30, ge=1, le=365),
    chain_id: int | None = Query(None),
    source: str | None = Query(None, description="filter by trade source: historical | cow_onchain"),
) -> dict[str, Any]:
    """Per-chain comparison of the Minotaur solver vs external DEX aggregators."""
    if _store is None:
        return {
            "enabled": False,
            "chains": [],
            "note": "dex-compare service is not running on this node",
        }
    since = time.time() - window_days * 86400
    rows = await asyncio.to_thread(_store.fetch_since, chain_id, since, None)
    # isinstance guard: when this handler is called directly (tests, not via
    # FastAPI) an unset Query param is a truthy Query sentinel, not None.
    if isinstance(source, str) and source:
        # NULL trade_source == legacy == "historical".
        rows = [r for r in rows if (r.get("trade_source") or "historical") == source]
    # Aggregate off the event loop — a wide window is thousands of rows of pure-
    # Python grouping; running it inline blocks the loop and 502s concurrent
    # requests under load.
    response = await asyncio.to_thread(build_stats_response, rows, window_days)
    response["enabled"] = True
    return response


@router.get("/dex-compare/blindspots")
async def dex_compare_blindspots(
    window_days: int = Query(14, ge=2, le=365),
    recent_days: int | None = Query(
        None, ge=1, le=365, description="recent sub-window in days; default window_days // 2"
    ),
    chain_id: int | None = Query(None),
    limit: int = Query(20, ge=1, le=200),
) -> dict[str, Any]:
    """Real (cow_onchain) pairs the solver can't route (open) and pairs that were
    unservable earlier in the window but are now servable (covered)."""
    if _store is None:
        return {"enabled": False, "chains": []}
    # isinstance guard: called directly (tests) an unset Query param is a Query
    # sentinel, not None (same pattern as /stats' `source`).
    rd = recent_days if isinstance(recent_days, int) else max(1, window_days // 2)
    since = time.time() - window_days * 86400
    rows = await asyncio.to_thread(_store.fetch_since, chain_id, since, None)
    # Aggregate off the event loop (see /stats) — the two-window per-pair scan is
    # the same order of work and must not block the loop.
    response = await asyncio.to_thread(build_blindspots_response, rows, window_days, rd, limit)
    response["enabled"] = True
    return response


@router.get("/dex-compare/samples")
async def dex_compare_samples(
    chain_id: int | None = Query(None),
    limit: int = Query(50, ge=1, le=500),
) -> dict[str, Any]:
    """Most-recent raw comparison rows (for a frontend live feed / debugging)."""
    if _store is None:
        return {"enabled": False, "samples": [], "count": 0}
    rows = await asyncio.to_thread(_store.fetch_since, chain_id, 0.0, limit)
    return {"enabled": True, "samples": rows, "count": len(rows)}
