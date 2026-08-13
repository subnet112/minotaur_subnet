"""Monitoring routes."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from minotaur_subnet.api import services as _tools
from minotaur_subnet.api.routes.apps import _require_admin

logger = logging.getLogger(__name__)

router = APIRouter(tags=["monitoring"])


def _store():
    """The APP-INTENT store (apps, orders, monitoring).

    NOT the submission store — it has no ``_submissions``. Anything counting
    submissions wants ``routes.submissions.state.get_store()``.
    """
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
    unmeasured_n = 0
    surface_hit_n = 0
    scanned_n = 0
    total = 0
    floor = dsf.sdk_version_floor()
    # The SUBMISSION store, not ``_store()`` (that one is the AppIntentStore and
    # has no ``_submissions``). Reading the wrong store raised AttributeError
    # straight into the swallow below, so every field of ``last_24h`` was pinned
    # at zero and served — for 5 minutes at a time — as if it had been measured.
    # Zero submissions and zero-because-we-asked-the-wrong-object are opposite
    # readings, and the retirement gate gets this one as evidence.
    from minotaur_subnet.api.routes.submissions.state import get_store

    try:
        st = get_store()
        st._maybe_reload()
        subs = list(st._submissions.values())
    except Exception:
        # Still best-effort — a store hiccup must not 500 a public dashboard
        # read — but never silently: an empty window is now reported as the
        # degraded reading it is, not as a measurement.
        logger.warning("migration_status: submission store read failed", exc_info=True)
        subs = None
    for sub in subs or ():
        created = float(getattr(sub, "created_at", 0) or 0)
        if now - created > 86400:
            continue
        total += 1
        reported = getattr(sub, "sdk_version", None)
        counts[reported or "pre-marker"] = counts.get(reported or "pre-marker", 0) + 1
        # UNMEASURED is not the same reading as BELOW FLOOR, and only one of
        # them is evidence of a migration backlog.
        #
        # ``below_floor(None, floor)`` is True by construction — _parse_version
        # (None) is (0,) — which is right at the ENFORCEMENT gate
        # (screening_pipeline), where the value has already been read and a None
        # means a genuinely unmarked solver. It is wrong HERE. This window
        # includes submissions rejected BEFORE stage 2 ever ran, so their
        # sdk_version was never read at all.
        #
        # Live shape (2026-08-13): of 221 submissions in 24h, 55 had no
        # sdk_version — and 54 of the 55 were structural-dedup rejects from
        # operators whose OTHER submissions in the same window reported 1.1.0.
        # Actual below-floor solvers: zero. Lumping them together would have the
        # retirement gate read a permanent 55-strong migration backlog that is
        # really duplicate spam, and it would never shrink however completely
        # miners migrate.
        if reported is None:
            unmeasured_n += 1
        elif floor and dsf.below_floor(reported, floor):
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
            # True when the store read failed — the counts below are NOT a
            # measurement and must not be read as "nobody submitted".
            "degraded": subs is None,
            "submissions": total,
            "sdk_version_counts": counts,
            # Solvers that REPORTED a generation older than the floor — the
            # only figure that is evidence of a migration backlog.
            "below_floor": below_floor_n,
            # Submissions whose SDK generation was never read (rejected before
            # screening stage 2). Neither migrated nor unmigrated: unknown.
            "unmeasured": unmeasured_n,
            "surface_scanned": scanned_n,
            "surface_hits": surface_hit_n,
        },
        "docs": "docs/architecture/sdk-v2-migration.md",
    }
    # Never cache a degraded reading: a single store hiccup would otherwise be
    # served as the answer for the next 5 minutes.
    if subs is not None:
        _MIGRATION_CACHE["ts"] = now
        _MIGRATION_CACHE["payload"] = payload
    return payload
