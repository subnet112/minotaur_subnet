"""Chain discovery routes."""

from __future__ import annotations

import logging
import time
from typing import Any

from fastapi import APIRouter, HTTPException

from minotaur_subnet.api import services as _tools

logger = logging.getLogger(__name__)

router = APIRouter(tags=["chains"])

# How old a persisted token list may be before it's flagged ``stale`` in the
# response. Purely informational for clients — the background TokenListCache
# refresh is what actually keeps it current; we always serve the cached list.
_TOKEN_LIST_STALE_AFTER = 900.0  # 15 min

# Set by server.py at startup
_block_loop = None
_store = None


def set_block_loop(bl: Any) -> None:
    global _block_loop
    _block_loop = bl


def set_store(store: Any) -> None:
    global _store
    _store = store


@router.get("/chains")
def list_chains() -> dict[str, Any]:
    """List all chains the platform can deploy to and simulate on."""
    return _tools.list_chains()


@router.get("/chains/{chain_id}/tokens")
async def list_tokens(chain_id: int) -> dict[str, Any]:
    """List tokens the current solver can route on the given chain.

    Served from the persisted, background-refreshed cache (TokenListCache) so
    the response is instant and survives api restarts / champion swaps — token
    discovery is a slow on-chain scan and never runs on this request path. The
    list updates automatically when a new champion solver is adopted (the
    background refresher reads the live solver each tick).

    Cold fallback (only before the cache has ever been populated — e.g. the
    very first boot on a fresh store): compute once on-request so the endpoint
    still works, then persist for subsequent calls.
    """
    # Fast path: serve the persisted list instantly.
    if _store is not None:
        try:
            cached = _store.get_token_list(chain_id)
        except Exception as exc:
            logger.warning("Token cache read failed for chain %d: %s", chain_id, exc)
            cached = None
        if cached is not None:
            updated_at, tokens = cached
            return {
                "chain_id": chain_id,
                "tokens": tokens,
                "count": len(tokens),
                "updated_at": updated_at,
                "stale": (time.time() - updated_at) > _TOKEN_LIST_STALE_AFTER,
            }

    # Cold fallback: nothing persisted yet — compute once on this request.
    import inspect

    bl = _block_loop
    if bl is None or bl.solver is None:
        raise HTTPException(status_code=503, detail="No solver available")
    if not hasattr(bl.solver, "supported_tokens"):
        raise HTTPException(
            status_code=501,
            detail="Current solver does not support token discovery",
        )
    try:
        call = bl.solver.supported_tokens(chain_id)
        tokens = list((await call if inspect.isawaitable(call) else call) or [])
    except Exception as exc:
        logger.warning("Token discovery failed for chain %d: %s", chain_id, exc)
        raise HTTPException(status_code=500, detail=f"Token discovery failed: {exc}")

    if _store is not None and tokens:
        try:
            _store.save_token_list(chain_id, tokens, updated_at=time.time())
        except Exception:
            pass

    return {
        "chain_id": chain_id,
        "tokens": tokens,
        "count": len(tokens),
        "updated_at": time.time(),
        "stale": False,
    }
