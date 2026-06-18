"""Public certify endpoint must not bypass the adoption rule.

The operator-facing `POST /v1/solver/round/certify` may only certify the
round's rule-selected finalist, a genesis/builtin bootstrap candidate, or an
explicit audited `force=true` override — it must reject an arbitrary candidate
that never won the round (the explicit-certify bypass). The automated
coordinator, genesis bootstrap, and peer-sync call the internal functions
directly and are unaffected.
"""

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from minotaur_subnet.api.routes.submissions import routes as R
from minotaur_subnet.api.routes.submissions.models import CertifyRoundRequest


class _Req:  # minimal stand-in for fastapi Request (auth is monkeypatched off)
    pass


async def _call(monkeypatch, *, candidate, finalist, subs, force=False):
    monkeypatch.setattr(R, "_require_internal_round_api_key", lambda req: None)
    monkeypatch.setattr(
        R, "get_round_store",
        lambda: SimpleNamespace(
            get_round=lambda rid: SimpleNamespace(finalist_submission_id=finalist)
        ),
    )
    monkeypatch.setattr(R, "get_store", lambda: SimpleNamespace(get=lambda sid: subs.get(sid)))

    async def _fake_certify(body):
        return "CERTIFIED"

    async def _fake_bcast(*a, **k):
        return None

    monkeypatch.setattr(R, "_certify_solver_round_state", _fake_certify)
    monkeypatch.setattr(R, "_broadcast_internal_round_sync", _fake_bcast)
    monkeypatch.setattr(R, "_certify_round_sync_payload", lambda s: {})
    monkeypatch.setattr(R, "_round_state_to_response", lambda s: s)

    body = CertifyRoundRequest(
        round_id="round-1", candidate_submission_id=candidate,
        effective_epoch=1, force=force,
    )
    return await R.certify_solver_round(body, _Req())


def _miner_sub():
    return SimpleNamespace(hotkey="5Gminer", repo_url="https://github.com/x/y")


def _genesis_sub():
    return SimpleNamespace(hotkey="__genesis__", repo_url="builtin://genesis")


@pytest.mark.asyncio
async def test_rejects_arbitrary_non_finalist_candidate(monkeypatch):
    with pytest.raises(HTTPException) as ei:
        await _call(
            monkeypatch, candidate="sub_evil", finalist="sub_winner",
            subs={"sub_evil": _miner_sub(), "sub_winner": _miner_sub()},
        )
    assert ei.value.status_code == 409
    assert "never passed the adoption rule" in str(ei.value.detail)


@pytest.mark.asyncio
async def test_allows_the_rounds_finalist(monkeypatch):
    out = await _call(
        monkeypatch, candidate="sub_winner", finalist="sub_winner",
        subs={"sub_winner": _miner_sub()},
    )
    assert out == "CERTIFIED"


@pytest.mark.asyncio
async def test_allows_genesis_builtin_candidate(monkeypatch):
    # Genesis bootstrap may not be the finalist yet — builtin is allowed.
    out = await _call(
        monkeypatch, candidate="sub_genesis", finalist=None,
        subs={"sub_genesis": _genesis_sub()},
    )
    assert out == "CERTIFIED"


@pytest.mark.asyncio
async def test_allows_non_finalist_with_force_override(monkeypatch):
    out = await _call(
        monkeypatch, candidate="sub_evil", finalist="sub_winner",
        subs={"sub_evil": _miner_sub(), "sub_winner": _miner_sub()},
        force=True,
    )
    assert out == "CERTIFIED"
