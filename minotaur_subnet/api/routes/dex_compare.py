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
    response = build_stats_response(rows, window_days)
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
