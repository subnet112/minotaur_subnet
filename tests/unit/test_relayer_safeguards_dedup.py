"""Tests for the relayer submission-dedup safeguard (fill-round scoped).

Locks in the 2026-07-15 incident fixes — the original ``plan_hash``-only
dedup had three production failure modes:

1. A submission that failed BEFORE broadcast (gas-balance floor) permanently
   reserved its hash — every retry 409'd until a relayer restart.
2. The reservation TTL trusted the miner-authored plan deadline; the champion
   emits sentinel deadlines (~10^10 s), making every entry effectively
   permanent.
3. The key was plan-bytes only, so a DIFFERENT order whose deterministic
   champion plan was byte-identical collided — blocking recurring/DCA orders
   and every perpetual fill after the first.

The fix: key = ``order_id:execution_count:plan_hash``, TTL clamped by
``dedup_max_ttl_seconds``, reservation released on definitely-pre-broadcast
failure (``SubmitResult.broadcast_attempted=False``). Cross-order replay
safety does NOT rest on the dedup — PlanApproval signs (orderId, planHash),
so an approved plan can't be submitted under another order at all.
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
from minotaur_subnet.relayer.base import SubmitResult
from minotaur_subnet.relayer.safeguards import Safeguards
from minotaur_subnet.shared.types import ExecutionPlan, Interaction

APP_ADDR = "0x0aea6ab70b384adc6493d40e927ce53a7cefe035"
CHAIN_ID = 8453
JS_SCORE = 0.5824517492906979
THRESHOLD_BPS = 5000

# The champion's plans carry sentinel deadlines far in the future — the
# exact production value class that made dedup entries permanent.
SENTINEL_DEADLINE = 9_999_999_980


# ── Safeguards unit tests ────────────────────────────────────────────────


def test_same_key_second_rejected():
    sg = Safeguards()
    ok, err = sg.check_plan_hash_unseen("ord_a:0:0xabc", int(time.time()) + 300)
    assert ok, err
    ok, err = sg.check_plan_hash_unseen("ord_a:0:0xabc", int(time.time()) + 300)
    assert not ok
    assert "already submitted" in err


def test_distinct_keys_do_not_collide():
    """Different orders / fill rounds with the same plan hash each get a slot."""
    sg = Safeguards()
    deadline = int(time.time()) + 300
    assert sg.check_plan_hash_unseen("ord_a:0:0xabc", deadline)[0]
    assert sg.check_plan_hash_unseen("ord_b:0:0xabc", deadline)[0]  # other order
    assert sg.check_plan_hash_unseen("ord_a:1:0xabc", deadline)[0]  # next fill round


def test_ttl_clamped_for_sentinel_deadline():
    """A sentinel plan deadline must not produce a (near-)permanent entry."""
    sg = Safeguards(dedup_max_ttl_seconds=900, dedup_grace_seconds=600)
    now = int(time.time())
    ok, _ = sg.check_plan_hash_unseen("ord_a:0:0xabc", SENTINEL_DEADLINE)
    assert ok
    evict_at = sg._seen_plan_hashes["ord_a:0:0xabc"]
    assert evict_at <= now + 900 + 600 + 5  # clamp + grace (+ scheduling slack)
    # Pre-fix behavior would have been SENTINEL_DEADLINE + grace:
    assert evict_at < SENTINEL_DEADLINE


def test_near_deadline_still_wins_when_shorter_than_clamp():
    """A plan expiring sooner than the clamp keeps its shorter TTL."""
    sg = Safeguards(dedup_max_ttl_seconds=900, dedup_grace_seconds=600)
    now = int(time.time())
    deadline = now + 60
    assert sg.check_plan_hash_unseen("ord_a:0:0xabc", deadline)[0]
    assert sg._seen_plan_hashes["ord_a:0:0xabc"] <= deadline + 600


def test_release_allows_resubmit():
    sg = Safeguards()
    deadline = int(time.time()) + 300
    assert sg.check_plan_hash_unseen("ord_a:0:0xabc", deadline)[0]
    sg.release_plan_hash("ord_a:0:0xabc")
    assert sg.check_plan_hash_unseen("ord_a:0:0xabc", deadline)[0]


def test_release_unknown_key_is_noop():
    sg = Safeguards()
    sg.release_plan_hash("never-reserved")  # must not raise


def test_expired_reservation_evicted():
    """Once the clamped TTL + grace elapses, the slot frees itself."""
    sg = Safeguards(dedup_max_ttl_seconds=10, dedup_grace_seconds=5)
    real_time = time.time
    assert sg.check_plan_hash_unseen("ord_a:0:0xabc", SENTINEL_DEADLINE)[0]
    with patch("minotaur_subnet.relayer.safeguards.time") as mock_time:
        mock_time.time = lambda: real_time() + 20  # past 10 + 5
        assert sg.check_plan_hash_unseen("ord_a:0:0xabc", SENTINEL_DEADLINE)[0]


# ── Handler-level tests (full /v1/submit-plan flow) ──────────────────────
# Scaffolding mirrors test_relayer_submit_plan_score_bps.py.


def _build_plan() -> ExecutionPlan:
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
        deadline=SENTINEL_DEADLINE,
        nonce=0,
        metadata={},
    )


def _build_service(submit_result: SubmitResult | None = None):
    from minotaur_subnet.relayer import main as relayer_main

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
        return_value=submit_result
        if submit_result is not None
        else SubmitResult(
            success=True,
            tx_hash="0x" + "ab" * 32,
            chain_id=CHAIN_ID,
            block_number=12345,
            gas_used=100_000,
            broadcast_attempted=True,
        ),
    )
    service.handle_submit_plan = relayer_main.RelayerService.handle_submit_plan.__get__(service)
    return service


def _build_bundle(
    signer_key,
    *,
    order_id: str,
    submission_nonce: int,
    execution_count: int = 0,
):
    """A quorum-of-1 submit-plan body for the given order, all sharing one
    identical plan (deterministic champion behavior)."""
    plan = _build_plan()
    plan_hash = hash_plan(plan)
    sig_hex = sign_plan_approval(
        signer_key.key.hex(),
        order_id,
        plan_hash,
        JS_SCORE,
        chain_id=CHAIN_ID,
        contract_address=APP_ADDR,
        score_bps=THRESHOLD_BPS,
    )
    wrapper, wrapper_sig = sign_wrapper(
        signer_key.key.hex(),
        plan_hash=plan_hash,
        submission_nonce=submission_nonce,
        chain_id=CHAIN_ID,
    )
    return {
        "order": {
            "order_id": order_id,
            "chain_id": CHAIN_ID,
            "user_signature": "0x" + "00" * 65,
            "params": {},
            "contract_address": APP_ADDR,
            "execution_count": execution_count,
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
            "approvals": [{
                "validator_id": signer_key.address,
                "order_id": order_id,
                "plan_hash": plan_hash,
                "score": JS_SCORE,
                "signature": sig_hex,
                "timestamp": time.time(),
            }],
            "quorum": 1,
            "collected": 1,
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


async def _post(service, body: dict) -> web.Response:
    request = MagicMock()
    request.json = AsyncMock(return_value=body)
    return await service.handle_submit_plan(request)


def _read_json(resp: web.Response) -> dict:
    return json.loads(resp.body.decode())


def _patches(signer_address: str):
    return (
        patch(
            "minotaur_subnet.relayer.main._read_authorized_validators",
            return_value=[signer_address],
        ),
        patch(
            "minotaur_subnet.relayer.main.score_threshold_for",
            return_value=THRESHOLD_BPS,
        ),
    )


@pytest.mark.asyncio
async def test_two_orders_identical_plan_both_accepted():
    """Deterministic champion: two DIFFERENT orders carry byte-identical
    plans. Both must reach the chain — each arrives with its own
    PlanApproval quorum. Pre-fix, the second 409'd."""
    signer = Account.create()
    service = _build_service()
    p1, p2 = _patches(signer.address)
    with p1, p2:
        r1 = await _post(service, _build_bundle(signer, order_id="ord_aaa", submission_nonce=1))
        r2 = await _post(service, _build_bundle(signer, order_id="ord_bbb", submission_nonce=2))
    assert r1.status == 200, _read_json(r1)
    assert r2.status == 200, _read_json(r2)
    assert service.relayer.submit_plan.await_count == 2


@pytest.mark.asyncio
async def test_same_fill_round_duplicate_rejected():
    """The race the dedup exists for: the SAME order + fill round submitted
    twice must 409 the second time (first one succeeded → slot kept)."""
    signer = Account.create()
    service = _build_service()
    p1, p2 = _patches(signer.address)
    with p1, p2:
        r1 = await _post(service, _build_bundle(signer, order_id="ord_aaa", submission_nonce=1))
        r2 = await _post(service, _build_bundle(signer, order_id="ord_aaa", submission_nonce=2))
    assert r1.status == 200, _read_json(r1)
    assert r2.status == 409
    assert "already submitted" in _read_json(r2)["error"]
    assert service.relayer.submit_plan.await_count == 1


@pytest.mark.asyncio
async def test_next_perpetual_fill_round_not_blocked():
    """Successive fills of a perpetual order re-submit identical plan bytes
    with an incremented execution_count — each round gets its own slot."""
    signer = Account.create()
    service = _build_service()
    p1, p2 = _patches(signer.address)
    with p1, p2:
        r1 = await _post(service, _build_bundle(
            signer, order_id="ord_perp", submission_nonce=1, execution_count=0))
        r2 = await _post(service, _build_bundle(
            signer, order_id="ord_perp", submission_nonce=2, execution_count=1))
    assert r1.status == 200, _read_json(r1)
    assert r2.status == 200, _read_json(r2)
    assert service.relayer.submit_plan.await_count == 2


@pytest.mark.asyncio
async def test_prebroadcast_failure_releases_slot():
    """The live incident: submission fails BEFORE broadcast (balance floor)
    → the reservation must be released so the retry isn't 409'd."""
    signer = Account.create()
    service = _build_service(SubmitResult(
        success=False,
        error="Relayer balance too low on chain 8453: 0.0095 ETH < 0.0100 ETH minimum",
        chain_id=CHAIN_ID,
        broadcast_attempted=False,
    ))
    p1, p2 = _patches(signer.address)
    with p1, p2:
        r1 = await _post(service, _build_bundle(signer, order_id="ord_aaa", submission_nonce=1))
        assert r1.status == 200
        assert _read_json(r1)["success"] is False
        # Retry of the same fill round must get through the dedup again.
        r2 = await _post(service, _build_bundle(signer, order_id="ord_aaa", submission_nonce=2))
    assert r2.status == 200
    assert service.relayer.submit_plan.await_count == 2


@pytest.mark.asyncio
async def test_ambiguous_broadcast_failure_keeps_slot():
    """success=False with NO tx_hash but broadcast_attempted=True (e.g. a
    receipt-wait timeout after send_raw_transaction) — the tx may be in the
    mempool, so the slot must stay reserved."""
    signer = Account.create()
    service = _build_service(SubmitResult(
        success=False,
        error="Failed after 3 attempts: TimeExhausted",
        chain_id=CHAIN_ID,
        broadcast_attempted=True,
    ))
    p1, p2 = _patches(signer.address)
    with p1, p2:
        r1 = await _post(service, _build_bundle(signer, order_id="ord_aaa", submission_nonce=1))
        assert r1.status == 200
        r2 = await _post(service, _build_bundle(signer, order_id="ord_aaa", submission_nonce=2))
    assert r2.status == 409
    assert service.relayer.submit_plan.await_count == 1


# ── broadcast_attempted stickiness through the EvmRelayer retry loop ─────
# Review-confirmed interleaving: attempt 1 broadcasts, the receipt wait
# times out ("is not in the chain" → retry branch), attempt 2's dry-run
# reverts (typically BECAUSE attempt 1's tx just mined). The dry-run-revert
# return must carry broadcast_attempted=True or the handler releases the
# dedup slot while attempt 1's tx is live.


def _submit_plan_sync_with(estimate_gas_effects, receipt_effect):
    from types import SimpleNamespace
    from unittest.mock import patch as _patch

    from minotaur_subnet.relayer.evm_relayer import EvmRelayer

    relayer = EvmRelayer.__new__(EvmRelayer)
    relayer.chains = {
        CHAIN_ID: SimpleNamespace(
            chain_id=CHAIN_ID,
            rpc_url="http://anvil:8546",  # 'anvil' skips the REAL CHAIN warning
            relayer_wallet="0x" + "22" * 20,
        ),
    }
    relayer.wallet_manager = None
    relayer.private_key = "0x" + "11" * 32
    relayer._nonce_manager = MagicMock()
    relayer._nonce_manager.get_and_increment.return_value = 1
    relayer._submissions = []

    w3 = MagicMock()
    w3.to_checksum_address.side_effect = lambda a: a
    w3.eth.estimate_gas.side_effect = list(estimate_gas_effects)
    w3.eth.account.sign_transaction.return_value = MagicMock(raw_transaction=b"\x01")
    w3.eth.send_raw_transaction.return_value = MagicMock(hex=lambda: "0x" + "aa" * 32)
    w3.eth.wait_for_transaction_receipt.side_effect = receipt_effect

    order = SimpleNamespace(
        chain_id=CHAIN_ID,
        order_id="ord_sticky",
        user_signature="",
        params={},
    )
    plan = SimpleNamespace(interactions=[], metadata={}, deadline=SENTINEL_DEADLINE)

    with _patch("minotaur_subnet.consensus.app_registry_cache.is_registered_app", return_value=True), \
         _patch("minotaur_subnet.blockchain.chains.get_web3", return_value=w3), \
         _patch("minotaur_subnet.relayer.evm_relayer.encode_intent_order", return_value=()), \
         _patch("minotaur_subnet.relayer.evm_relayer.encode_execution_plan", return_value=()), \
         _patch.object(EvmRelayer, "_check_balance", return_value=None), \
         _patch.object(EvmRelayer, "_get_gas_price", return_value=10**9):
        return relayer._submit_plan_sync(order, plan, 1.0, None, contract_address=APP_ADDR), w3


def test_dryrun_revert_after_broadcast_keeps_broadcast_attempted():
    """attempt 1: dry-run OK → broadcast → receipt timeout → retry;
    attempt 2: dry-run reverts. The result must report broadcast_attempted=True
    (a tx from attempt 1 may be live) even though tx_hash is None."""
    result, w3 = _submit_plan_sync_with(
        estimate_gas_effects=[100_000, Exception("execution reverted: order nonce burned")],
        receipt_effect=Exception("tx 0xaa is not in the chain after 15 seconds"),
    )
    assert result.success is False
    assert result.tx_hash is None
    assert "dry-run reverted" in (result.error or "")
    assert result.broadcast_attempted is True
    w3.eth.send_raw_transaction.assert_called_once()


def test_dryrun_revert_before_any_broadcast_reports_false():
    """Dry-run revert on the FIRST attempt: nothing was ever broadcast —
    the handler is allowed to release the dedup slot."""
    result, w3 = _submit_plan_sync_with(
        estimate_gas_effects=[Exception("execution reverted: no funds")],
        receipt_effect=None,
    )
    assert result.success is False
    assert result.tx_hash is None
    assert result.broadcast_attempted is False
    w3.eth.send_raw_transaction.assert_not_called()


@pytest.mark.asyncio
async def test_mined_revert_keeps_slot():
    """A broadcast-then-reverted tx burned real gas; replaying the same
    fill round would burn more. Slot stays reserved."""
    signer = Account.create()
    service = _build_service(SubmitResult(
        success=False,
        tx_hash="0x" + "cd" * 32,
        error="Transaction reverted on-chain",
        chain_id=CHAIN_ID,
        block_number=1,
        gas_used=50_000,
        broadcast_attempted=True,
    ))
    p1, p2 = _patches(signer.address)
    with p1, p2:
        r1 = await _post(service, _build_bundle(signer, order_id="ord_aaa", submission_nonce=1))
        assert r1.status == 200
        r2 = await _post(service, _build_bundle(signer, order_id="ord_aaa", submission_nonce=2))
    assert r2.status == 409
    assert service.relayer.submit_plan.await_count == 1
