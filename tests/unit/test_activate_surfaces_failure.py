"""The internal/activate handler surfaces an adoption failure as an ACTIONABLE 503.

Activation does the real champion-adoption work — the hot-swap that ``docker run``s
the solver runtime. When that throws (e.g. a missing docker network on the
follower's host), the handler used to let it become a bare HTTP 500 ("Internal
Server Error"): the follower silently fell back to 100% burn while the leader's
reattest log showed only an opaque error. Now an unexpected failure becomes a 503
carrying the cause — and since the leader logs the peer response body on rejection
(``Peer … rejected … (HTTP 503): <detail>``), the reason shows up FLEET-side without
needing the follower's own logs. The KeyError→404 / ValueError→409 mappings and a
deliberate downstream HTTPException must still pass through unchanged.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from minotaur_subnet.api.routes.submissions import routes as R
from minotaur_subnet.api.routes.submissions.models import ActivateRoundRequest


async def _noop_auth(request):  # bypass the shared-key / signature auth
    return None


def _body():
    return ActivateRoundRequest(round_id="round-1", activation_epoch=6, champion_changed=True)


async def _call_with(state_fn):
    with pytest.MonkeyPatch().context() as mp:
        mp.setattr(R, "_authorize_internal_round_sync", _noop_auth)
        mp.setattr(R, "_activate_solver_round_state", state_fn)
        return await R.internal_activate_solver_round(_body(), request=object())


@pytest.mark.asyncio
async def test_hotswap_failure_returns_503_with_cause():
    async def _boom(body):
        raise RuntimeError("docker: network benchmark-sandbox not found")

    with pytest.raises(HTTPException) as ei:
        await _call_with(_boom)
    assert ei.value.status_code == 503
    assert "champion activation failed" in ei.value.detail
    assert "benchmark-sandbox" in ei.value.detail  # the real cause is carried through


@pytest.mark.asyncio
async def test_keyerror_still_404():
    async def _missing(body):
        raise KeyError("Round not found: round-1")

    with pytest.raises(HTTPException) as ei:
        await _call_with(_missing)
    assert ei.value.status_code == 404


@pytest.mark.asyncio
async def test_valueerror_still_409():
    async def _badstate(body):
        raise ValueError("Round round-1 is open; expected certified")

    with pytest.raises(HTTPException) as ei:
        await _call_with(_badstate)
    assert ei.value.status_code == 409


@pytest.mark.asyncio
async def test_deliberate_httpexception_passes_through():
    # A downstream HTTPException (its own status) must NOT be masked into 503.
    async def _teapot(body):
        raise HTTPException(status_code=418, detail="i am a teapot")

    with pytest.raises(HTTPException) as ei:
        await _call_with(_teapot)
    assert ei.value.status_code == 418
    assert ei.value.detail == "i am a teapot"
