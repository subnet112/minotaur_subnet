"""Unit tests for the champion re-attest endpoint + the canonical certify payload.

The cert-broadcast payload (``_certify_round_sync_payload``) must carry the v2 EIP-712
digest fields (commit_hash/nonce/deadline) at the top level so a follower rebuilds the
leader's signed proposal exactly — the residual operator/re-attest leg of the #414/#417
regression (the automated path was fixed in #417; this builder fed the operator certify
endpoint and was still gappy). ``reattest_current_champion`` re-broadcasts the CURRENT
champion's existing certificate on demand (a "force-sync the fleet" lever), so a
follower that missed the original election can re-verify and switch from burn to
champion-weight.

Pure Python — no Anvil, Docker, network, or chain.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from minotaur_subnet.api.routes.submissions.models import CertifyRoundRequest
from minotaur_subnet.api.routes.submissions.round_manager import _certify_round_sync_payload
from minotaur_subnet.consensus.champion_manager import ChampionApproval


def _approval(nonce=1_726_000_000_123, deadline=1_726_003_600, commit_hash="0x" + "aa" * 32):
    """A leader approval with the v2 EIP-712 fields populated (wall-clock nonce)."""
    return ChampionApproval(
        validator_id="0xLEAD",
        round_id="round-x",
        committee_hash="0x" + "ab" * 32,
        incumbent_image_id="sha256:" + "1" * 64,
        candidate_submission_id="sub-final",
        candidate_image_id="sha256:" + "2" * 64,
        benchmark_pack_hash="0x" + "cd" * 32,
        shadow_case_log_hash="0x" + "ef" * 32,
        effective_epoch=43,
        commit_hash=commit_hash,
        nonce=nonce,
        deadline=deadline,
        timestamp=1.0,
        signature="0x" + "5" * 130,
    )


def _state(approval):
    """A certified RoundState stand-in carrying the leader's certificate."""
    return SimpleNamespace(
        round_id="round-x",
        finalist_submission_id="sub-final",
        finalist_image_id="sha256:" + "2" * 64,
        committee_hash="0x" + "ab" * 32,
        benchmark_pack_hash="0x" + "cd" * 32,
        shadow_case_log_hash="0x" + "ef" * 32,
        effective_epoch=43,
        quorum_required=1,
        certificate=SimpleNamespace(approvals=[approval]),
    )


class TestCanonicalCertifyPayload:
    def test_payload_surfaces_v2_fields_top_level(self):
        ap = _approval()
        payload = _certify_round_sync_payload(_state(ap))
        assert payload["commit_hash"] == ap.commit_hash
        assert payload["nonce"] == ap.nonce != 0
        assert payload["deadline"] == ap.deadline != 0

    def test_payload_round_trips_through_the_real_model(self):
        # The #417 model fix: CertifyRoundRequest must accept the v2 fields so the
        # follower's override logic (body.nonce or None) gets the leader's nonce —
        # not its own wall-clock (which would 409). Pre-fix this builder omitted them
        # so body.nonce parsed to the 0 default.
        ap = _approval()
        body = CertifyRoundRequest(**_certify_round_sync_payload(_state(ap)))
        assert body.nonce == ap.nonce
        assert body.commit_hash == ap.commit_hash
        assert body.deadline == ap.deadline

    def test_per_approval_v2_fields_ride_along(self):
        ap = _approval()
        payload = _certify_round_sync_payload(_state(ap))
        assert payload["approvals"][0]["nonce"] == ap.nonce
        assert payload["approvals"][0]["commit_hash"] == ap.commit_hash
        assert payload["approvals"][0]["deadline"] == ap.deadline


@pytest.mark.asyncio
async def test_reattest_broadcasts_current_champion(monkeypatch):
    from minotaur_subnet.api.routes.submissions import routes as R

    captured = {}

    async def _auth(_req):
        return None

    async def _bcast(path, payload):
        captured["path"] = path
        captured["payload"] = payload

    ap = _approval()
    state = _state(ap)
    champ = SimpleNamespace(submission_id="sub-final", activated_round_id="round-x")
    store = SimpleNamespace(
        get_active_champion=lambda: champ,
        get_round=lambda rid: state if rid == "round-x" else None,
    )
    monkeypatch.setattr(R, "_authorize_internal_round", _auth)
    monkeypatch.setattr(R, "get_round_store", lambda: store)
    monkeypatch.setattr(R, "_broadcast_internal_round_sync", _bcast)

    resp = await R.reattest_current_champion(SimpleNamespace())

    # re-broadcast lands on the internal certify path, carrying the v2 fields (the fix)
    assert captured["path"] == "/v1/solver/round/internal/certify"
    assert captured["payload"]["nonce"] == ap.nonce != 0
    assert captured["payload"]["candidate_submission_id"] == "sub-final"
    assert resp == {
        "reattested_submission_id": "sub-final",
        "round_id": "round-x",
        "approvals": 1,
    }


@pytest.mark.asyncio
async def test_reattest_404_when_no_active_champion(monkeypatch):
    from fastapi import HTTPException
    from minotaur_subnet.api.routes.submissions import routes as R

    async def _auth(_req):
        return None

    champ = SimpleNamespace(submission_id=None, activated_round_id=None)
    store = SimpleNamespace(get_active_champion=lambda: champ, get_round=lambda rid: None)
    monkeypatch.setattr(R, "_authorize_internal_round", _auth)
    monkeypatch.setattr(R, "get_round_store", lambda: store)

    with pytest.raises(HTTPException) as ei:
        await R.reattest_current_champion(SimpleNamespace())
    assert ei.value.status_code == 404


@pytest.mark.asyncio
async def test_reattest_404_when_round_has_no_certificate(monkeypatch):
    from fastapi import HTTPException
    from minotaur_subnet.api.routes.submissions import routes as R

    async def _auth(_req):
        return None

    champ = SimpleNamespace(submission_id="sub-final", activated_round_id="round-x")
    state = SimpleNamespace(round_id="round-x", certificate=None)
    store = SimpleNamespace(
        get_active_champion=lambda: champ,
        get_round=lambda rid: state if rid == "round-x" else None,
    )
    monkeypatch.setattr(R, "_authorize_internal_round", _auth)
    monkeypatch.setattr(R, "get_round_store", lambda: store)

    with pytest.raises(HTTPException) as ei:
        await R.reattest_current_champion(SimpleNamespace())
    assert ei.value.status_code == 404
