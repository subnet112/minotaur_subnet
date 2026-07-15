"""Tests for the relayer ``POST /v1/finalize-champion`` endpoint.

Champion FINALIZATION (the on-chain ``ChampionRegistry.certify()`` tx + the
GitHub PR squash-merge) lives on the TRUSTED relayer, not the leader. The leader
asks the relayer to finalize and gates its local adoption on the boolean reply.

The relayer must NOT trust the leader: it independently re-verifies the validator
quorum on the certificate before attesting/merging. These tests lock in:

  - quorum reached  → ``on_champion_adopted_pr`` is called → ``{"merge_ok": true}``
  - sub-quorum      → ``{"merge_ok": false}`` and NO attest/merge

We monkeypatch ``verify_champion_approval``, ``_read_authorized_validators``, and
``on_champion_adopted_pr`` so no real chain / network is touched, and drive the
bound ``handle_finalize_champion`` handler directly (same harness as
``test_relayer_submit_plan_score_bps.py``).
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from eth_account import Account

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from minotaur_subnet.consensus.leader_wrapper import (
    compute_champion_finalize_hash,
    sign_wrapper,
)
from minotaur_subnet.harness.round_store import ChampionApproval, ChampionCertificate

CHAMPION_CHAIN_ID = 964
CHAMPION_REGISTRY = "0x" + "ce" * 20
ROUND_ID = "round-finalize-001"
SUBMISSION_ID = "sub_abc123"
COMMIT_HASH = "0" * 40
PR_NUMBER = 42


def _build_service():
    """Build a ``RelayerService`` mock with the champion chain configured.

    Mirrors ``test_relayer_submit_plan_score_bps._build_service`` — bind the real
    ``handle_finalize_champion`` to a spec'd MagicMock so the verification path
    runs with all I/O stubbed.
    """
    from minotaur_subnet.relayer import main as relayer_main

    service = MagicMock(spec=relayer_main.RelayerService)
    service.chains = {
        CHAMPION_CHAIN_ID: MagicMock(
            chain_id=CHAMPION_CHAIN_ID,
            rpc_url="http://localhost:1",
            validator_registry_address="0xChampionVR",
        ),
    }
    # Bind the real methods: the v1/v2 handlers are thin serializers over the
    # shared _finalize_core, so all three must run the real code.
    service._finalize_core = (
        relayer_main.RelayerService._finalize_core.__get__(service)
    )
    service.handle_finalize_champion = (
        relayer_main.RelayerService.handle_finalize_champion.__get__(service)
    )
    service.handle_finalize_champion_v2 = (
        relayer_main.RelayerService.handle_finalize_champion_v2.__get__(service)
    )
    return service


def _make_certificate(*, signer_addrs: list[str], quorum_required: int) -> ChampionCertificate:
    """Build a certificate with one approval per signer address.

    The signatures are dummy strings — we monkeypatch ``verify_champion_approval``
    to decide which signers verify, so the actual signature bytes don't matter
    here. Each approval carries the candidate_submission_id so the wrapper hash
    binds correctly.
    """
    approvals = [
        ChampionApproval(
            validator_id=addr,
            round_id=ROUND_ID,
            candidate_submission_id=SUBMISSION_ID,
            commit_hash=COMMIT_HASH,
            signature="0x" + "ab" * 65,
        )
        for addr in signer_addrs
    ]
    return ChampionCertificate(
        round_id=ROUND_ID,
        candidate_submission_id=SUBMISSION_ID,
        quorum_required=quorum_required,
        approvals=approvals,
    )


def _build_body(cert: ChampionCertificate, *, leader_priv: str) -> dict:
    """Build the finalize-champion POST body with a valid leader wrapper sig."""
    finalize_hash = compute_champion_finalize_hash(
        ROUND_ID, cert.candidate_submission_id or "",
    )
    wrapper, wrapper_sig = sign_wrapper(
        leader_priv,
        plan_hash=finalize_hash,
        submission_nonce=int(time.time()),
        chain_id=CHAMPION_CHAIN_ID,
    )
    return {
        "certificate": cert.to_dict(),
        "submission": {
            "submission_id": SUBMISSION_ID,
            "commit_hash": COMMIT_HASH,
            "pr_number": PR_NUMBER,
        },
        "round_id": ROUND_ID,
        "wrapper": {
            "plan_hash": wrapper.plan_hash,
            "submission_nonce": wrapper.submission_nonce,
            "timestamp": wrapper.timestamp,
            "chain_id": wrapper.chain_id,
        },
        "wrapper_signature": wrapper_sig,
    }


async def _post(service, body: dict) -> web.Response:
    request = MagicMock()
    request.json = AsyncMock(return_value=body)
    return await service.handle_finalize_champion(request)


async def _post_v2(service, body: dict) -> web.Response:
    request = MagicMock()
    request.json = AsyncMock(return_value=body)
    return await service.handle_finalize_champion_v2(request)


def _read_json(resp: web.Response) -> dict:
    return json.loads(resp.body.decode())


@pytest.mark.asyncio
async def test_finalize_quorum_reached_attests_and_merges():
    """All signers verify + are authorized + meet quorum → attest+merge called,
    ``{"merge_ok": true}`` returned."""
    leader = Account.create()
    v1 = Account.create()
    v2 = Account.create()
    # Quorum of 2 with the leader + v1 + v2 all authorized; both approvals verify.
    cert = _make_certificate(signer_addrs=[v1.address, v2.address], quorum_required=2)
    body = _build_body(cert, leader_priv=leader.key.hex())

    adopt_mock = MagicMock(return_value=True)
    service = _build_service()
    with patch.dict(
        "os.environ",
        {f"CHAMPION_REGISTRY_{CHAMPION_CHAIN_ID}": CHAMPION_REGISTRY,
         "CHAMPION_CONSENSUS_CHAIN_ID": str(CHAMPION_CHAIN_ID)},
    ), patch(
        "minotaur_subnet.consensus.champion_manager.verify_champion_approval",
        return_value=True,
    ), patch(
        "minotaur_subnet.relayer.main._read_authorized_validators",
        return_value=[leader.address, v1.address, v2.address],
    ), patch(
        "minotaur_subnet.relayer.solver_repo.on_champion_adopted_pr",
        adopt_mock,
    ):
        resp = await _post(service, body)

    assert resp.status == 200, _read_json(resp)
    out = _read_json(resp)
    assert out["merge_ok"] is True, out
    assert out["round_id"] == ROUND_ID
    assert out["submission_id"] == SUBMISSION_ID
    # The relayer actually drove the finalization with the rebuilt cert.
    adopt_mock.assert_called_once()
    call = adopt_mock.call_args
    # positional: (submission_ns, round_id); kw: certificate=cert
    assert call.args[1] == ROUND_ID
    ns = call.args[0]
    assert ns.submission_id == SUBMISSION_ID
    assert ns.commit_hash == COMMIT_HASH
    assert ns.pr_number == PR_NUMBER
    assert call.kwargs["certificate"].round_id == ROUND_ID


@pytest.mark.asyncio
async def test_finalize_sub_quorum_refuses_without_merging():
    """Fewer authorized verified signers than ``quorum_required`` →
    ``{"merge_ok": false}`` and ``on_champion_adopted_pr`` is NOT called."""
    leader = Account.create()
    v1 = Account.create()
    v2 = Account.create()
    # quorum_required=2 but only ONE signer is authorized → sub-quorum.
    cert = _make_certificate(signer_addrs=[v1.address, v2.address], quorum_required=2)
    body = _build_body(cert, leader_priv=leader.key.hex())

    adopt_mock = MagicMock(return_value=True)
    service = _build_service()
    with patch.dict(
        "os.environ",
        {f"CHAMPION_REGISTRY_{CHAMPION_CHAIN_ID}": CHAMPION_REGISTRY,
         "CHAMPION_CONSENSUS_CHAIN_ID": str(CHAMPION_CHAIN_ID)},
    ), patch(
        "minotaur_subnet.consensus.champion_manager.verify_champion_approval",
        return_value=True,
    ), patch(
        # Only v1 (and the leader) are authorized; v2 is NOT → 1 authorized
        # verified signer < quorum_required 2.
        "minotaur_subnet.relayer.main._read_authorized_validators",
        return_value=[leader.address, v1.address],
    ), patch(
        "minotaur_subnet.relayer.solver_repo.on_champion_adopted_pr",
        adopt_mock,
    ):
        resp = await _post_v2(service, body)

    assert resp.status == 200, _read_json(resp)
    out = _read_json(resp)
    assert out["ok"] is False, out
    assert out["outcome"] == "refused"
    assert out["reason"]["code"] == "quorum_not_reached"
    assert "quorum not reached" in out["reason"]["detail"]
    adopt_mock.assert_not_called()


@pytest.mark.asyncio
async def test_finalize_unverified_signatures_refuses():
    """Signatures that DON'T verify against the champion domain → not counted →
    sub-quorum → ``{"merge_ok": false}``, no merge. The relayer never trusts the
    leader-supplied cert blindly."""
    leader = Account.create()
    v1 = Account.create()
    v2 = Account.create()
    cert = _make_certificate(signer_addrs=[v1.address, v2.address], quorum_required=2)
    body = _build_body(cert, leader_priv=leader.key.hex())

    adopt_mock = MagicMock(return_value=True)
    service = _build_service()
    with patch.dict(
        "os.environ",
        {f"CHAMPION_REGISTRY_{CHAMPION_CHAIN_ID}": CHAMPION_REGISTRY,
         "CHAMPION_CONSENSUS_CHAIN_ID": str(CHAMPION_CHAIN_ID)},
    ), patch(
        # No signature verifies → zero verified signers.
        "minotaur_subnet.consensus.champion_manager.verify_champion_approval",
        return_value=False,
    ), patch(
        "minotaur_subnet.relayer.main._read_authorized_validators",
        return_value=[leader.address, v1.address, v2.address],
    ), patch(
        "minotaur_subnet.relayer.solver_repo.on_champion_adopted_pr",
        adopt_mock,
    ):
        resp = await _post_v2(service, body)

    assert resp.status == 200, _read_json(resp)
    out = _read_json(resp)
    assert out["ok"] is False, out
    assert out["outcome"] == "refused"
    assert out["reason"]["code"] == "quorum_not_reached"
    assert "quorum not reached" in out["reason"]["detail"]
    adopt_mock.assert_not_called()


@pytest.mark.asyncio
async def test_finalize_bad_wrapper_signer_refuses():
    """A wrapper signed by a non-validator caller → refused (anti-spam gate),
    even though the certificate quorum would otherwise pass."""
    outsider = Account.create()  # NOT in the authorized set
    v1 = Account.create()
    v2 = Account.create()
    cert = _make_certificate(signer_addrs=[v1.address, v2.address], quorum_required=2)
    body = _build_body(cert, leader_priv=outsider.key.hex())

    adopt_mock = MagicMock(return_value=True)
    service = _build_service()
    with patch.dict(
        "os.environ",
        {f"CHAMPION_REGISTRY_{CHAMPION_CHAIN_ID}": CHAMPION_REGISTRY,
         "CHAMPION_CONSENSUS_CHAIN_ID": str(CHAMPION_CHAIN_ID)},
    ), patch(
        "minotaur_subnet.consensus.champion_manager.verify_champion_approval",
        return_value=True,
    ), patch(
        "minotaur_subnet.relayer.main._read_authorized_validators",
        return_value=[v1.address, v2.address],  # outsider absent
    ), patch(
        "minotaur_subnet.relayer.solver_repo.on_champion_adopted_pr",
        adopt_mock,
    ):
        resp = await _post_v2(service, body)

    assert resp.status == 200, _read_json(resp)
    out = _read_json(resp)
    assert out["ok"] is False, out
    assert out["reason"]["code"] == "wrapper_signer_unauthorized"
    assert "not in ValidatorRegistry" in out["reason"]["detail"]
    adopt_mock.assert_not_called()
