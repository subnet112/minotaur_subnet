"""Tests for the revert-or-refresh recovery flow (AWAITING_USER_DECISION).

Covers the MultiLegOrchestrator's park/resolve/timeout paths, the
BridgeTracker's destination-leg parking, and the decision signature check —
all with the real IntentOrderBook and fakes only at the relayer boundary.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, patch

import pytest

pytestmark = pytest.mark.cross_chain

from minotaur_subnet.blockloop.multi_leg import MultiLegOrchestrator
from minotaur_subnet.orderbook.orderbook import IntentOrderBook, Order, OrderStatus
from minotaur_subnet.shared.types import Interaction, LegPlan

USER = "0x" + "aa" * 20


def _run(coro):
    return asyncio.run(coro)


class FakeSubmitResult:
    def __init__(self, success=True, tx_hash="0xabc", error=None):
        self.success = success
        self.tx_hash = tx_hash
        self.error = error


class FakeRelayer:
    def __init__(self, succeed=True):
        self.succeed = succeed
        self.submitted: list = []

    async def submit_plan(self, order, plan, score, consensus, **kwargs):
        self.submitted.append((order, plan))
        return FakeSubmitResult(success=self.succeed, error=None if self.succeed else "boom")


def _leg(leg_index=0, chain_id=1, rollback_for=None, metadata=None) -> LegPlan:
    return LegPlan(
        leg_index=leg_index,
        chain_id=chain_id,
        intent_selector="",
        intent_params_hex="",
        interactions=[
            Interaction(target="0x" + "11" * 20, value="0",
                        call_data="0xa9059cbb" + "00" * 28, chain_id=chain_id),
        ],
        rollback_for=rollback_for,
        metadata=metadata or {"type": "solver_leg"},
    )


@pytest.fixture
def env():
    ob = IntentOrderBook()
    relayer = FakeRelayer()
    orch = MultiLegOrchestrator(orderbook=ob, relayer=relayer)
    order = ob.submit(
        app_id="app-1",
        intent_function="swap",
        params={"input_token": "0x" + "22" * 20},
        submitted_by=USER,
        deadline=time.time() + 3600,
    )
    return ob, relayer, orch, order


COMPLETED = [_leg(leg_index=0, chain_id=1, metadata={"type": "solver_leg"})]
# Rollback semantics: _execute_rollback recovers COMPLETED legs only (a
# failed leg moved nothing), so the revert leg targets completed leg 0.
ROLLBACKS = [_leg(leg_index=200, chain_id=1, rollback_for=0,
                  metadata={"type": "rollback_solver"})]


async def _fail(orch, order):
    await orch._fail_leg(
        order, COMPLETED, ROLLBACKS, "0x" + "cc" * 20,
        {"plan_set": {"hashes": [], "plan_set_hash": "0x00", "positions": {}},
         "escrow_params": [{"leg_index": 2, "deadline": 123}]},
        2, "Leg 2 submission failed: boom",
    )


class TestFlagOff:
    def test_auto_rollback_preserved(self, env, monkeypatch):
        monkeypatch.delenv("CROSS_CHAIN_USER_DECISION", raising=False)
        ob, relayer, orch, order = env
        _run(_fail(orch, order))
        assert ob.get(order.order_id).status == OrderStatus.ROLLED_BACK
        assert len(relayer.submitted) == 1  # the rollback leg executed
        assert "recovery" not in ob.get(order.order_id).params


class TestPark:
    def test_parks_with_context(self, env, monkeypatch):
        monkeypatch.setenv("CROSS_CHAIN_USER_DECISION", "1")
        ob, relayer, orch, order = env
        _run(_fail(orch, order))
        parked = ob.get(order.order_id)
        assert parked.status == OrderStatus.AWAITING_USER_DECISION
        assert relayer.submitted == []  # nothing auto-executed
        rec = parked.params["recovery"]
        assert rec["failed_leg_index"] == 2
        assert rec["options"] == ["revert", "refresh"]
        assert rec["decision_deadline"] > time.time()
        assert len(rec["rollback_legs"]) == 1
        assert rec["escrow_params"] == [{"leg_index": 2, "deadline": 123}]
        assert rec["resolved"] is None


class TestResolve:
    def _park(self, env, monkeypatch):
        monkeypatch.setenv("CROSS_CHAIN_USER_DECISION", "1")
        ob, relayer, orch, order = env
        # Park without spawning the watcher task noise
        with patch.object(asyncio, "create_task", lambda coro: coro.close()):
            _run(_fail(orch, order))
        return ob, relayer, orch, order

    def test_revert_executes_rollback(self, env, monkeypatch):
        ob, relayer, orch, order = self._park(env, monkeypatch)
        result = _run(orch.resolve_user_decision(order.order_id, "revert"))
        assert result["action"] == "revert"
        assert ob.get(order.order_id).status == OrderStatus.ROLLED_BACK
        assert len(relayer.submitted) == 1
        # The rollback plan was built through the canonical builder
        _, plan = relayer.submitted[0]
        assert plan.metadata["is_rollback"] is True
        assert plan.metadata["leg_index"] == 200

    def test_refresh_ends_orchestration(self, env, monkeypatch):
        ob, relayer, orch, order = self._park(env, monkeypatch)
        result = _run(orch.resolve_user_decision(order.order_id, "refresh"))
        assert result["action"] == "refresh"
        assert result["escrow_params"] == [{"leg_index": 2, "deadline": 123}]
        assert ob.get(order.order_id).status == OrderStatus.REFRESHING
        assert relayer.submitted == []  # nothing moves until a new signed order

    def test_double_resolve_rejected(self, env, monkeypatch):
        ob, relayer, orch, order = self._park(env, monkeypatch)
        _run(orch.resolve_user_decision(order.order_id, "refresh"))
        with pytest.raises(ValueError, match="not awaiting a decision"):
            _run(orch.resolve_user_decision(order.order_id, "revert"))

    def test_wrong_state_rejected(self, env, monkeypatch):
        ob, relayer, orch, order = env
        with pytest.raises(ValueError, match="not awaiting a decision"):
            _run(orch.resolve_user_decision(order.order_id, "revert"))

    def test_unknown_action_rejected(self, env, monkeypatch):
        ob, relayer, orch, order = self._park(env, monkeypatch)
        with pytest.raises(ValueError, match="Unknown recovery action"):
            _run(orch.resolve_user_decision(order.order_id, "yolo"))

    def test_failed_revert_leg_is_partial_rollback(self, env, monkeypatch):
        ob, relayer, orch, order = self._park(env, monkeypatch)
        relayer.succeed = False
        _run(orch.resolve_user_decision(order.order_id, "revert"))
        # Stale revert plan fails its leg → PARTIAL_ROLLBACK, escrowRefund
        # remains the user's backstop.
        assert ob.get(order.order_id).status == OrderStatus.PARTIAL_ROLLBACK


class TestTimeout:
    def test_expired_window_auto_reverts(self, env, monkeypatch):
        ob, relayer, orch, order = env
        monkeypatch.setenv("CROSS_CHAIN_USER_DECISION", "1")
        with patch.object(asyncio, "create_task", lambda coro: coro.close()):
            _run(_fail(orch, order))
        # Force the stored deadline into the past and run the watcher with
        # sleep stubbed out.
        order = ob.get(order.order_id)
        order.params["recovery"]["decision_deadline"] = int(time.time()) - 1

        with patch("asyncio.sleep", new=AsyncMock()):
            _run(orch._decision_timeout_watch(order.order_id, int(time.time()) - 1))
        assert ob.get(order.order_id).status == OrderStatus.ROLLED_BACK

    def test_watcher_exits_after_endpoint_resolution(self, env, monkeypatch):
        ob, relayer, orch, order = env
        monkeypatch.setenv("CROSS_CHAIN_USER_DECISION", "1")
        with patch.object(asyncio, "create_task", lambda coro: coro.close()):
            _run(_fail(orch, order))
        _run(orch.resolve_user_decision(order.order_id, "refresh"))

        with patch("asyncio.sleep", new=AsyncMock()):
            _run(orch._decision_timeout_watch(order.order_id, int(time.time()) - 1))
        # Watcher must not override the user's refresh with a revert.
        assert ob.get(order.order_id).status == OrderStatus.REFRESHING
        assert relayer.submitted == []


class TestBridgeTrackerParking:
    def _tracker(self, ob, orch):
        from minotaur_subnet.relayer.bridge_tracker import BridgeTracker
        tracker = BridgeTracker(bridge_registry=None, orderbook=ob, relayer=None)
        tracker.multi_leg_orchestrator = orch
        return tracker

    def _tracked(self, order):
        from minotaur_subnet.relayer.bridge_tracker import TrackedBridge
        from minotaur_subnet.shared.types import ExecutionPlan, MultiLegPlan
        mlp = MultiLegPlan(forward_legs=[COMPLETED[0], _leg(leg_index=2, chain_id=8453)],
                           rollback_legs=ROLLBACKS)
        plan = ExecutionPlan(
            intent_id=order.app_id, interactions=[], deadline=0, nonce=0,
            metadata={
                "multi_leg_plan": mlp.to_dict(),
                "rollback_legs": [l.to_dict() for l in ROLLBACKS],
                "completed_leg_indices": [0],
                "contract_address": "0x" + "cc" * 20,
                "plan_set": None,
                "escrow_params": [{"leg_index": 2}],
            },
        )
        return TrackedBridge(order_id=order.order_id, src_tx_hash="0x" + "ab" * 32,
                             plan=plan, src_chain_id=1, dst_chain_id=8453,
                             bridge_protocol="mock")

    def test_flag_off_dead_ends_bridge_failed(self, env, monkeypatch):
        monkeypatch.delenv("CROSS_CHAIN_USER_DECISION", raising=False)
        ob, relayer, orch, order = env
        tracker = self._tracker(ob, orch)
        _run(tracker._fail_dest_leg(self._tracked(order), order, 2, "dest sim failed"))
        assert ob.get(order.order_id).status == OrderStatus.BRIDGE_FAILED

    def test_flag_on_parks(self, env, monkeypatch):
        monkeypatch.setenv("CROSS_CHAIN_USER_DECISION", "1")
        ob, relayer, orch, order = env
        tracker = self._tracker(ob, orch)
        with patch.object(asyncio, "create_task", lambda coro: coro.close()):
            _run(tracker._fail_dest_leg(self._tracked(order), order, 2, "dest sim failed"))
        parked = ob.get(order.order_id)
        assert parked.status == OrderStatus.AWAITING_USER_DECISION
        rec = parked.params["recovery"]
        assert rec["failed_leg_index"] == 2
        assert [l["leg_index"] for l in rec["completed_legs"]] == [0]
        assert rec["escrow_params"] == [{"leg_index": 2}]

    def test_no_orchestrator_falls_back(self, env, monkeypatch):
        monkeypatch.setenv("CROSS_CHAIN_USER_DECISION", "1")
        ob, relayer, orch, order = env
        tracker = self._tracker(ob, None)
        tracker.multi_leg_orchestrator = None
        _run(tracker._fail_dest_leg(self._tracked(order), order, 2, "oops"))
        assert ob.get(order.order_id).status == OrderStatus.BRIDGE_FAILED


class TestDecisionSignature:
    def test_roundtrip_and_binding(self):
        from eth_account import Account
        from eth_account.messages import encode_defunct

        from minotaur_subnet.api.routes.orders import _verify_decision_sig

        key = "0x" + "11" * 32
        addr = Account.from_key(key).address
        msg = encode_defunct(text="order-decision:o-1:revert")
        sig = Account.sign_message(msg, private_key=key).signature.hex()

        assert _verify_decision_sig(addr, "o-1", "revert", sig)
        # Bound to action and order id
        assert not _verify_decision_sig(addr, "o-1", "refresh", sig)
        assert not _verify_decision_sig(addr, "o-2", "revert", sig)
        # Wrong signer
        assert not _verify_decision_sig(USER, "o-1", "revert", sig)
        # Garbage
        assert not _verify_decision_sig(addr, "o-1", "revert", "0xzz")
        assert not _verify_decision_sig(addr, "o-1", "revert", "")
