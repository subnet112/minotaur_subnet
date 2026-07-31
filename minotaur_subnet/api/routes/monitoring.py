"""Monitoring routes."""

from __future__ import annotations

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
    organic champion. The REAL protection is the admin-auth dependency
    (``_require_admin``, spawns benchmarks); the observability gate
    (``CHALLENGER_QUORUM_MODE``, DEFAULT ON via ``_challenger_quorum_mode``) lets it
    be silenced as a break-glass, so the fleet test needs no per-validator config.
    """
    from minotaur_subnet.harness.benchmark_worker import _challenger_quorum_mode

    if not _challenger_quorum_mode():
        raise HTTPException(status_code=503, detail="CHALLENGER_QUORUM_MODE disabled (break-glass)")
    from minotaur_subnet.api.server_context import ctx
    worker = getattr(ctx, "benchmark_worker", None)
    if worker is None:
        raise HTTPException(status_code=503, detail="benchmark worker unavailable")
    return await worker.run_shadow_vote(body.challenger_image)


class RevertChampionRequest(BaseModel):
    reason: str = ""


@router.post("/admin/revert-champion", dependencies=[Depends(_require_admin)])
async def revert_champion(body: RevertChampionRequest) -> dict[str, Any]:
    """Emergency rollback: revert the live champion to the PREVIOUS one.

    A one-step undo of the most recent adoption (NOT genesis). Forces the swap
    past the ``DISABLE_CHAMPION_ADOPTION`` gate — reverting to an already-vetted
    prior champion is always safe — and re-routes the next weight emission to it.
    Pair with ``DISABLE_CHAMPION_ADOPTION=1`` to stop the bad champion being
    re-adopted, then revert. Admin-gated.

    Returns 409 if there is no previous champion to revert to (or it's already
    active / unresolvable).
    """
    from minotaur_subnet.api.routes.submissions.state import get_epoch_manager

    manager = get_epoch_manager()
    if manager is None:
        raise HTTPException(status_code=503, detail="epoch manager unavailable")
    try:
        return await manager.revert_to_previous_champion(reason=body.reason)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


# ── Migration ladder status (public, cached) ────────────────────────────────
#
# One truthful source for "where does the SDK migration stand" so the miner
# dashboard renders live state (floor, modes, adoption) instead of copy that
# goes stale. Store scan is cached: the numbers move on submission cadence,
# not request cadence.

_MIGRATION_CACHE: dict[str, Any] = {"ts": 0.0, "payload": None}
_MIGRATION_CACHE_TTL_S = 300.0


@router.get("/migration/status")
def migration_status() -> dict[str, Any]:
    """Public migration-ladder status for dashboards.

    No auth: everything here is already public knowledge (env-derived policy
    + aggregate counts over the public submissions list).
    """
    import time as _time

    now = _time.time()
    if (
        _MIGRATION_CACHE["payload"] is not None
        and now - _MIGRATION_CACHE["ts"] < _MIGRATION_CACHE_TTL_S
    ):
        return _MIGRATION_CACHE["payload"]

    from minotaur_subnet.harness import deprecated_surface as dsf
    from minotaur_subnet.sdk.intent_solver import _SNAPSHOT_RETIREMENT_TARGET
    from minotaur_subnet.sdk.version import SDK_VERSION

    counts: dict[str, int] = {}
    below_floor_n = 0
    surface_hit_n = 0
    scanned_n = 0
    total = 0
    floor = dsf.sdk_version_floor()
    try:
        st = _store()
        st._maybe_reload()
        subs = list(st._submissions.values())
    except Exception:
        subs = []
    for sub in subs:
        created = float(getattr(sub, "created_at", 0) or 0)
        if now - created > 86400:
            continue
        total += 1
        v = getattr(sub, "sdk_version", None) or "pre-marker"
        counts[v] = counts.get(v, 0) + 1
        if floor and dsf.below_floor(getattr(sub, "sdk_version", None), floor):
            below_floor_n += 1
        hits = getattr(sub, "deprecated_surface_hits", None)
        if hits is not None:
            scanned_n += 1
            if hits:
                surface_hit_n += 1

    payload = {
        "current_sdk_version": SDK_VERSION,
        "deprecated_symbols": list(dsf.DEPRECATED_WIRE_SYMBOLS),
        "retirement_target": _SNAPSHOT_RETIREMENT_TARGET,
        "retirement_is_evidence_gated": True,
        "sdk_version_floor": floor,
        "sdk_version_floor_enforced": dsf.sdk_floor_enforced(),
        "deprecated_surface_mode": dsf.deprecated_surface_mode(),
        "last_24h": {
            "submissions": total,
            "sdk_version_counts": counts,
            "below_floor": below_floor_n,
            "surface_scanned": scanned_n,
            "surface_hits": surface_hit_n,
        },
        "docs": "docs/architecture/sdk-v2-migration.md",
    }
    _MIGRATION_CACHE["ts"] = now
    _MIGRATION_CACHE["payload"] = payload
    return payload
