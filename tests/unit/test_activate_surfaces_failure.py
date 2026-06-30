"""Internal round-sync handlers surface failures as actionable HTTP, never a bare 500.

The /internal/close|certify|activate broadcast handlers do real per-validator work —
most importantly the champion hot-swap that ``docker run``s the solver runtime. When
that throws (e.g. a missing docker network on the follower's host) it used to become a
bare HTTP 500 ("Internal Server Error"): the follower silently fell back to 100% burn
while the leader's reattest log showed only an opaque error. ``_raise_round_sync_failure``
now maps such a failure to a **503 carrying the cause** — and since the leader logs the
peer response body on rejection (``Peer … rejected … (HTTP 503): <detail>``), the reason
shows up FLEET-side without the follower's own logs. KeyError→404 / ValueError→409 and a
deliberate downstream HTTPException still pass through unchanged.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from minotaur_subnet.api.routes.submissions import routes as R
from minotaur_subnet.api.routes.submissions.models import (
    ActivateRoundRequest,
    CertifyRoundRequest,
    CloseRoundRequest,
)


async def _noop_auth(request):  # bypass the shared-key / signature auth
    return None


# ── the shared mapping helper ────────────────────────────────────────────────

def test_helper_passes_through_httpexception():
    orig = HTTPException(status_code=418, detail="i am a teapot")
    with pytest.raises(HTTPException) as ei:
        R._raise_round_sync_failure("round close", "r", orig)
    assert ei.value is orig  # unchanged, NOT re-wrapped into 503


def test_helper_keyerror_maps_404():
    with pytest.raises(HTTPException) as ei:
        R._raise_round_sync_failure("round close", "r", KeyError("Round not found: r"))
    assert ei.value.status_code == 404


def test_helper_valueerror_maps_409():
    with pytest.raises(HTTPException) as ei:
        R._raise_round_sync_failure("round close", "r", ValueError("wrong state"))
    assert ei.value.status_code == 409


def test_helper_other_maps_503_with_cause():
    with pytest.raises(HTTPException) as ei:
        R._raise_round_sync_failure(
            "champion activation", "r",
            RuntimeError("docker: network benchmark-sandbox not found"),
        )
    assert ei.value.status_code == 503
    assert "champion activation failed on this validator" in ei.value.detail
    assert "benchmark-sandbox" in ei.value.detail  # real cause carried through


# ── each broadcast handler routes failures through the helper ────────────────

@pytest.mark.asyncio
async def test_activate_handler_surfaces_503(monkeypatch):
    async def _boom(body):
        raise RuntimeError("docker: network benchmark-sandbox not found")

    monkeypatch.setattr(R, "_authorize_internal_round_sync", _noop_auth)
    monkeypatch.setattr(R, "_activate_solver_round_state", _boom)
    with pytest.raises(HTTPException) as ei:
        await R.internal_activate_solver_round(
            ActivateRoundRequest(round_id="r", activation_epoch=6, champion_changed=True),
            request=object(),
        )
    assert ei.value.status_code == 503
    assert "champion activation failed" in ei.value.detail
    assert "benchmark-sandbox" in ei.value.detail


@pytest.mark.asyncio
async def test_certify_handler_surfaces_503(monkeypatch):
    async def _boom(body):
        raise RuntimeError("kaboom in certify")

    monkeypatch.setattr(R, "_authorize_internal_round_sync", _noop_auth)
    monkeypatch.setattr(R, "_sync_certified_round_state", _boom)
    with pytest.raises(HTTPException) as ei:
        await R.internal_certify_solver_round(
            CertifyRoundRequest(round_id="r", effective_epoch=6, quorum_required=1),
            request=object(),
        )
    assert ei.value.status_code == 503
    assert "round certify failed" in ei.value.detail


@pytest.mark.asyncio
async def test_close_handler_surfaces_503(monkeypatch):
    def _boom(body):  # the close state fn is SYNC
        raise RuntimeError("kaboom in close")

    monkeypatch.setattr(R, "_authorize_internal_round_sync", _noop_auth)
    monkeypatch.setattr(R, "_sync_close_solver_round_state", _boom)
    with pytest.raises(HTTPException) as ei:
        await R.internal_close_solver_round(
            CloseRoundRequest(round_id="r", close_epoch=6),
            request=object(),
        )
    assert ei.value.status_code == 503
    assert "round close failed" in ei.value.detail


@pytest.mark.asyncio
async def test_activate_handler_keyerror_still_404(monkeypatch):
    async def _missing(body):
        raise KeyError("Round not found: r")

    monkeypatch.setattr(R, "_authorize_internal_round_sync", _noop_auth)
    monkeypatch.setattr(R, "_activate_solver_round_state", _missing)
    with pytest.raises(HTTPException) as ei:
        await R.internal_activate_solver_round(
            ActivateRoundRequest(round_id="r", activation_epoch=6),
            request=object(),
        )
    assert ei.value.status_code == 404
