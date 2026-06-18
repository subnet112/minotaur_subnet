"""Chain discovery routes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from minotaur_subnet.api import services as _tools

router = APIRouter(tags=["chains"])


@router.get("/chains")
def list_chains() -> dict[str, Any]:
    """List all chains the platform can deploy to and simulate on."""
    return _tools.list_chains()
