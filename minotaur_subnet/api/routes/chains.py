"""Chain discovery routes."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException

from minotaur_subnet.api import services as _tools

logger = logging.getLogger(__name__)

router = APIRouter(tags=["chains"])

# Set by server.py at startup
_block_loop = None


def set_block_loop(bl: Any) -> None:
    global _block_loop
    _block_loop = bl


@router.get("/chains")
def list_chains() -> dict[str, Any]:
    """List all chains the platform can deploy to and simulate on."""
    return _tools.list_chains()


@router.get("/chains/{chain_id}/tokens")
async def list_tokens(chain_id: int) -> dict[str, Any]:
    """List tokens the current solver can route on the given chain.

    Returns tokens discovered from on-chain pool data. The list updates
    automatically when a new champion solver is adopted.

    Supports both sync solvers (legacy in-process BaselineSwapSolver) and
    async solvers (DockerRuntimeSolver) — the latter forwards the request
    over the harness protocol to the live champion container.
    """
    import inspect

    bl = _block_loop
    if bl is None or bl.solver is None:
        raise HTTPException(
            status_code=503,
            detail="No solver available",
        )

    if not hasattr(bl.solver, "supported_tokens"):
        raise HTTPException(
            status_code=501,
            detail="Current solver does not support token discovery",
        )

    try:
        call = bl.solver.supported_tokens(chain_id)
        tokens = await call if inspect.isawaitable(call) else call
    except Exception as exc:
        logger.warning("Token discovery failed for chain %d: %s", chain_id, exc)
        raise HTTPException(
            status_code=500,
            detail=f"Token discovery failed: {exc}",
        )

    return {
        "chain_id": chain_id,
        "tokens": list(tokens or []),
        "count": len(tokens or []),
    }
