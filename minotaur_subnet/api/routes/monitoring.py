"""Monitoring routes."""

from __future__ import annotations

import os
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from minotaur_subnet.api import services as _tools
from minotaur_subnet.api.routes.apps import _require_admin

router = APIRouter(tags=["monitoring"])


def _store():
    from minotaur_subnet.api.server import store
    return store


@router.get("/apps/{app_id}/monitor")
def monitor_app(app_id: str) -> dict[str, Any]:
    """Get real-time execution monitoring data for an App Intent."""
    return _tools.monitor_app(_store(), app_id)


class ShadowVoteRequest(BaseModel):
    challenger_image: str


@router.post("/admin/shadow-vote", dependencies=[Depends(_require_admin)])
async def shadow_vote(body: ShadowVoteRequest) -> dict[str, Any]:
    """Trigger this validator's OBSERVE-ONLY shadow adopt-vote.

    Benchmarks the current champion (or the official genesis solver when none is
    adopted — the same store-backed resolution scoring uses, never an injectable
    env) and the given challenger on this validator's own diverse Stage-2 subset, applies the
    shared adoption rule, and returns this validator's vote. Never adopts, never
    touches the real champion or weights — it lets the fleet demonstrate the
    challenger-quorum decision (good->adopt / bad->reject by majority) without an
    organic champion. Admin-gated (spawns benchmarks) + requires
    ``CHALLENGER_QUORUM_MODE``.
    """
    if os.environ.get("CHALLENGER_QUORUM_MODE", "").strip().lower() not in (
        "1", "true", "yes", "on",
    ):
        raise HTTPException(status_code=503, detail="CHALLENGER_QUORUM_MODE not enabled")
    from minotaur_subnet.api.server_context import ctx
    worker = getattr(ctx, "benchmark_worker", None)
    if worker is None:
        raise HTTPException(status_code=503, detail="benchmark worker unavailable")
    return await worker.run_shadow_vote(body.challenger_image)
