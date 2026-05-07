"""Monitoring routes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from minotaur_subnet.api import services as _tools

router = APIRouter(tags=["monitoring"])


def _store():
    from minotaur_subnet.api.server import store
    return store


@router.get("/apps/{app_id}/monitor")
def monitor_app(app_id: str) -> dict[str, Any]:
    """Get real-time execution monitoring data for an App Intent."""
    return _tools.monitor_app(_store(), app_id)
