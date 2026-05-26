"""Regression tests for relayer ``/v1/submit-plan`` signature verification.

The bug this guards against: validators sign EIP-712 plan approvals with
``score_bps = scoreThreshold()`` of the App (the on-chain verifier reconstructs
the digest the same way). Before the fix, ``handle_submit_plan`` called
``verify_plan_approval`` passing only the float ``score=ap.score`` — so the
helper recomputed ``score_bps = int(score*10000)``, which is never the
threshold unless the JS score happens to truncate to it. Result: every order
that cleared consensus failed at the relayer with "invalid EIP-712 signature
from <leader>" and never reached the chain.

The fix passes ``score_bps`` explicitly using the same
``score_threshold_for(...)`` helper the signers use.
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

from minotaur_subnet.consensus.leader_wrapper import sign_wrapper
from minotaur_subnet.consensus.signatures import hash_plan, sign_plan_approval
from minotaur_subnet.shared.types import ExecutionPlan, Interaction


# An App-deployed-on-Base address. The contents don't matter — we mock the
# RPC call that would read ``scoreThreshold()`` from it.
APP_ADDR = "0x0aea6ab70b384adc6493d40e927ce53a7cefe035"
CHAIN_ID = 8453
# A score whose truncated bps (5824) is decidedly NOT the App's threshold
# (5000). The pre-fix code would compute 5824 at verify time and reject;
# the fix passes 5000 (the actual threshold) and accepts. Picking a score
# that does NOT coincide with the threshold is the whole point of the test.
JS_SCORE = 0.5824517492906979
THRESHOLD_BPS = 5000


def _build_plan() -> ExecutionPlan:
    """Minimal but realistic ExecutionPlan with a future deadline."""
    return ExecutionPlan(
        intent_id="app_test",
        interactions=[
            Interaction(
                target="0x" + "11" * 20,
                value="0",
                call_data="0xdeadbeef",
                chain_id=CHAIN_ID,
            ),
        ],
        deadline=int(time.time()) + 3600,
        nonce=0,
        metadata={},
    )


def _build_service():
    """Build a ``RelayerService`` with mocked downstream relayer + safeguards.

    Mirrors the pattern in ``test_relayer_deploy_wrapper.py`` — we bind the
    real ``handle_submit_plan`` bound method to a ``MagicMock`` instance so
    the verification path runs, with all I/O dependencies stubbed.
    """
    from minotaur_subnet.relayer import main as relayer_main
    from minotaur_subnet.relayer.evm_relayer import SubmitResult
    from minotaur_subnet.relayer.safeguards import Safeguards

    service = MagicMock(spec=relayer_main.RelayerService)
    service.chains = {
        CHAIN_ID: MagicMock(
            chain_id=CHAIN_ID,
            rpc_url="http://localhost:1",
            validator_registry_address="0xBaseRegistry",
        ),
    }
    service.safeguards = Safeguards()
    service.protocol_config = MagicMock(quorum_bps=6666)
    service.relayer = MagicMock()
    service.relayer.submit_plan = AsyncMock(
        return_value=SubmitResult(
            success=True,
            tx_hash="0x" + "ab" * 32,
            chain_id=CHAIN_ID,
            block_number=12345,
            gas_used=100_000,
            error=None,
        ),
    )
    service.handle_submit_plan = relayer_main.RelayerService.handle_submit_plan.__get__(service)
    return service


async def _post_submit_plan(service, body: dict):
    """Synthesize an aiohttp Request from a dict and call handle_submit_plan."""
    request = MagicMock()
    request.json = AsyncMock(return_value=body)
    return await service.handle_submit_plan(request)


def _read_json(resp: web.Response) -> dict:
    return json.loads(resp.body.decode())


def _build_bundle(*, signer_keys: list[tuple[str, str]], score_bps_for_signing: int):
    """Build a submit-plan request body signed by the given (priv, addr) keys.

    ``score_bps_for_signing`` controls the EIP-712 score_bps the signers commit
    to. Passing the App's actual ``scoreThreshold`` (5000) mirrors what real
    validators do; passing ``int(JS_SCORE * 10000)`` (5824) reproduces the
    pre-fix (broken) verification expectation — useful as a negative case.
    """
    plan = _build_plan()
    plan_hash = hash_plan(plan)
    order_id = "ord_regression_001"

    approvals = []
    for priv, addr in signer_keys:
        sig_hex = sign_plan_approval(
            priv,
            order_id,
            plan_hash,
            JS_SCORE,
            chain_id=CHAIN_ID,
            contract_address=APP_ADDR,
            score_bps=score_bps_for_signing,
        )
        approvals.append({
            "validator_id": addr,
            "order_id": order_id,
            "plan_hash": plan_hash,
            "score": JS_SCORE,
            "signature": sig_hex,
            "timestamp": time.time(),
        })

    # Leader wrapper — first signer plays the role of leader/submitter.
    leader_priv = signer_keys[0][0]
    wrapper, wrapper_sig = sign_wrapper(
        leader_priv,
        plan_hash=plan_hash,
        submission_nonce=1,
        chain_id=CHAIN_ID,
    )

    return {
        "order": {
            "order_id": order_id,
            "chain_id": CHAIN_ID,
            "user_signature": "0x" + "00" * 65,
            "params": {},
            "contract_address": APP_ADDR,
        },
        "plan": {
            "intent_id": plan.intent_id,
            "interactions": [
                {
                    "target": ix.target,
                    "value": ix.value,
                    "call_data": ix.call_data,
                    "chain_id": ix.chain_id,
                }
                for ix in plan.interactions
            ],
            "deadline": plan.deadline,
            "nonce": plan.nonce,
            "metadata": plan.metadata,
        },
        "score": JS_SCORE,
        "consensus_result": {
            "reached": True,
            "approvals": approvals,
            "quorum": len(approvals),
            "collected": len(approvals),
            "combined_score": JS_SCORE,
        },
        "contract_address": APP_ADDR,
        "wrapper": {
            "plan_hash": wrapper.plan_hash,
            "submission_nonce": wrapper.submission_nonce,
            "timestamp": wrapper.timestamp,
            "chain_id": wrapper.chain_id,
        },
        "wrapper_signature": wrapper_sig,
    }


# ── Tests ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_submit_plan_accepts_signatures_signed_with_threshold_bps():
    """Happy path matching production: validators sign with
    ``score_bps = scoreThreshold()`` (here 5000). The JS score is 0.5824...
    (truncates to 5824 bps), explicitly NOT equal to the threshold. The
    relayer must still accept these signatures.

    Pre-fix this returned 400 "invalid EIP-712 signature". The fix is to
    pass ``score_bps=threshold`` to ``verify_plan_approval`` instead of
    letting it default to ``int(score*10000)``.
    """
    sig1 = Account.create()
    sig2 = Account.create()
    signer_keys = [(sig1.key.hex(), sig1.address), (sig2.key.hex(), sig2.address)]
    body = _build_bundle(
        signer_keys=signer_keys,
        score_bps_for_signing=THRESHOLD_BPS,  # what real validators do
    )

    service = _build_service()
    with patch(
        "minotaur_subnet.relayer.main._read_authorized_validators",
        return_value=[sig1.address, sig2.address, "0x" + "cd" * 20],
    ), patch(
        "minotaur_subnet.relayer.main.score_threshold_for",
        return_value=THRESHOLD_BPS,
    ):
        resp = await _post_submit_plan(service, body)

    assert resp.status == 200, _read_json(resp)
    body_out = _read_json(resp)
    assert body_out["success"] is True
    assert body_out["tx_hash"].startswith("0x")
    service.relayer.submit_plan.assert_awaited_once()


@pytest.mark.asyncio
async def test_submit_plan_rejects_signatures_signed_with_wrong_score_bps():
    """A bad actor who signed with the WRONG score_bps (e.g. forgot to use the
    App's threshold) must still be rejected. This locks in that the fix
    didn't accidentally make verification permissive — it just made it use
    the right ``score_bps`` value."""
    sig1 = Account.create()
    signer_keys = [(sig1.key.hex(), sig1.address)]
    # Sign with int(JS_SCORE*10000) = 5824 — the broken pre-fix expectation.
    # The relayer will verify with score_bps=THRESHOLD_BPS=5000 → mismatch.
    body = _build_bundle(
        signer_keys=signer_keys,
        score_bps_for_signing=int(JS_SCORE * 10000),
    )

    service = _build_service()
    with patch(
        "minotaur_subnet.relayer.main._read_authorized_validators",
        return_value=[sig1.address],
    ), patch(
        "minotaur_subnet.relayer.main.score_threshold_for",
        return_value=THRESHOLD_BPS,
    ):
        resp = await _post_submit_plan(service, body)

    assert resp.status == 400
    assert "invalid EIP-712 signature" in _read_json(resp)["error"]


@pytest.mark.asyncio
async def test_submit_plan_uses_score_threshold_for_helper():
    """The relayer must consult ``score_threshold_for`` with the resolved
    contract + chain, so it reads the same on-chain ``scoreThreshold()`` the
    signers used. Lock that in so a future refactor can't silently drop the
    contract lookup and fall back to a global default."""
    sig1 = Account.create()
    signer_keys = [(sig1.key.hex(), sig1.address)]
    body = _build_bundle(
        signer_keys=signer_keys,
        score_bps_for_signing=THRESHOLD_BPS,
    )

    service = _build_service()
    with patch(
        "minotaur_subnet.relayer.main._read_authorized_validators",
        return_value=[sig1.address],
    ), patch(
        "minotaur_subnet.relayer.main.score_threshold_for",
        return_value=THRESHOLD_BPS,
    ) as mock_threshold:
        resp = await _post_submit_plan(service, body)

    assert resp.status == 200, _read_json(resp)
    # Called once for this submit-plan request, with the App + chain we sent.
    mock_threshold.assert_called_once()
    call_args = mock_threshold.call_args
    # First positional is the contract address, second is the chain_id.
    assert call_args.args[0].lower() == APP_ADDR.lower()
    assert int(call_args.args[1]) == CHAIN_ID
