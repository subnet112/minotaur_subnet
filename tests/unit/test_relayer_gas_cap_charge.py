"""The daily gas cap is charged for ON-CHAIN reverts, not only successes.

The cap is the relayer's worst-case griefing backstop. It used to be charged
only when ``submit_result.success`` was True — but a tx that passes the
pre-broadcast dry-run yet reverts when mined still burns real gas (the EVM
relayer returns ``success=False`` with ``gas_used=receipt["gasUsed"]``). So the
backstop under-counted exactly the spend it exists to bound. The fix charges
whenever ``gas_used`` is truthy (success OR mined-revert); pre-broadcast /
dry-run failures leave ``gas_used=0`` and are still correctly not charged.
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

APP_ADDR = "0x0aea6ab70b384adc6493d40e927ce53a7cefe035"
CHAIN_ID = 8453
JS_SCORE = 0.5824517492906979
THRESHOLD_BPS = 5000
APPROX_GAS_PRICE_WEI = 50_000_000  # 0.05 gwei — must match relayer.main


def _build_plan() -> ExecutionPlan:
    return ExecutionPlan(
        intent_id="app_test",
        interactions=[
            Interaction(target="0x" + "11" * 20, value="0",
                        call_data="0xdeadbeef", chain_id=CHAIN_ID),
        ],
        deadline=int(time.time()) + 3600,
        nonce=0,
        metadata={},
    )


def _build_service(submit_result):
    """RelayerService MagicMock with a real Safeguards and a bound handler,
    returning the given SubmitResult from the downstream relayer."""
    from minotaur_subnet.relayer import main as relayer_main
    from minotaur_subnet.relayer.safeguards import Safeguards

    service = MagicMock(spec=relayer_main.RelayerService)
    service.chains = {
        CHAIN_ID: MagicMock(chain_id=CHAIN_ID, rpc_url="http://localhost:1",
                            validator_registry_address="0xBaseRegistry"),
    }
    service.safeguards = Safeguards()
    service.protocol_config = MagicMock(quorum_bps=6666)
    service.relayer = MagicMock()
    service.relayer.submit_plan = AsyncMock(return_value=submit_result)
    service.handle_submit_plan = relayer_main.RelayerService.handle_submit_plan.__get__(service)
    return service


def _build_bundle(signer_keys):
    plan = _build_plan()
    plan_hash = hash_plan(plan)
    order_id = "ord_gas_cap_001"
    approvals = []
    for priv, addr in signer_keys:
        sig_hex = sign_plan_approval(
            priv, order_id, plan_hash, JS_SCORE, chain_id=CHAIN_ID,
            contract_address=APP_ADDR, score_bps=THRESHOLD_BPS,
        )
        approvals.append({
            "validator_id": addr, "order_id": order_id, "plan_hash": plan_hash,
            "score": JS_SCORE, "signature": sig_hex, "timestamp": time.time(),
        })
    wrapper, wrapper_sig = sign_wrapper(
        signer_keys[0][0], plan_hash=plan_hash, submission_nonce=1, chain_id=CHAIN_ID,
    )
    return {
        "order": {"order_id": order_id, "chain_id": CHAIN_ID,
                  "user_signature": "0x" + "00" * 65, "params": {},
                  "contract_address": APP_ADDR},
        "plan": {
            "intent_id": plan.intent_id,
            "interactions": [{"target": ix.target, "value": ix.value,
                              "call_data": ix.call_data, "chain_id": ix.chain_id}
                             for ix in plan.interactions],
            "deadline": plan.deadline, "nonce": plan.nonce, "metadata": plan.metadata,
        },
        "score": JS_SCORE,
        "consensus_result": {"reached": True, "approvals": approvals,
                             "quorum": len(approvals), "collected": len(approvals),
                             "combined_score": JS_SCORE},
        "contract_address": APP_ADDR,
        "wrapper": {"plan_hash": wrapper.plan_hash,
                    "submission_nonce": wrapper.submission_nonce,
                    "timestamp": wrapper.timestamp, "chain_id": wrapper.chain_id},
        "wrapper_signature": wrapper_sig,
    }


async def _drive(submit_result):
    """Run handle_submit_plan to completion; return (response, safeguards)."""
    sig1 = Account.create()
    body = _build_bundle([(sig1.key.hex(), sig1.address)])
    service = _build_service(submit_result)
    request = MagicMock()
    request.json = AsyncMock(return_value=body)
    with patch("minotaur_subnet.relayer.main._read_authorized_validators",
               return_value=[sig1.address]), \
         patch("minotaur_subnet.relayer.main.score_threshold_for",
               return_value=THRESHOLD_BPS):
        resp = await service.handle_submit_plan(request)
    return resp, service.safeguards


def _charged_wei(safeguards) -> int:
    return safeguards._daily_gas.gas_used_wei


@pytest.mark.asyncio
async def test_onchain_revert_is_charged_against_daily_cap():
    """A mined-then-reverted tx (success=False, gas_used>0) MUST be charged —
    this is the under-counting the fix closes."""
    from minotaur_subnet.relayer.base import SubmitResult
    resp, safeguards = await _drive(SubmitResult(
        success=False, tx_hash="0x" + "ab" * 32, chain_id=CHAIN_ID,
        block_number=123, gas_used=120_000, error="Transaction reverted on-chain",
    ))
    assert resp.status == 200
    assert json.loads(resp.body.decode())["success"] is False
    assert _charged_wei(safeguards) == 120_000 * APPROX_GAS_PRICE_WEI


@pytest.mark.asyncio
async def test_pre_broadcast_failure_is_not_charged():
    """A pre-broadcast / dry-run rejection burns no gas (gas_used=0) → no charge."""
    from minotaur_subnet.relayer.base import SubmitResult
    _resp, safeguards = await _drive(SubmitResult(
        success=False, chain_id=CHAIN_ID,
        error="pre-broadcast dry-run reverted: insufficient balance",
    ))
    assert _charged_wei(safeguards) == 0


@pytest.mark.asyncio
async def test_successful_submit_still_charged():
    """Regression: the fix must not drop the existing success-path charge."""
    from minotaur_subnet.relayer.base import SubmitResult
    _resp, safeguards = await _drive(SubmitResult(
        success=True, tx_hash="0x" + "cd" * 32, chain_id=CHAIN_ID,
        block_number=456, gas_used=100_000, error=None,
    ))
    assert _charged_wei(safeguards) == 100_000 * APPROX_GAS_PRICE_WEI
