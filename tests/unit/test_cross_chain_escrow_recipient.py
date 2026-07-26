"""Tests for the destination-escrow recipient split and the plan-set gate.

Two fixes from docs/architecture/cross-chain-review-2026-07-26.md:

  §2  A bridge hop must deliver to the App on the DESTINATION chain (the
      only address escrowDeposit can gate and escrowRefund can return from),
      while the ORIGIN refund still goes to the user's own wallet.
  §3  A compiled multi-leg order waits for the user's plan-set signature
      before any leg executes, instead of compiling straight into execution
      with no moment at which a wallet could sign.

Mocking policy (matches test_cross_chain_primitive.py):
  - Real types, real MockBridgeAdapter/AcrossAdapter, real registry+compiler
  - No RPC / no Anvil; the Across quote is stubbed at the HTTP boundary only
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, patch

import pytest

pytestmark = pytest.mark.cross_chain

from minotaur_subnet.bridge.across import AcrossAdapter
from minotaur_subnet.bridge.base import BridgeQuote
from minotaur_subnet.bridge.compiler import CrossChainCompiler, CrossChainCompileError
from minotaur_subnet.bridge.mock import MockBridgeAdapter
from minotaur_subnet.bridge.registry import BridgeRegistry
from minotaur_subnet.orderbook.orderbook import IntentOrderBook, OrderStatus
from minotaur_subnet.shared.types import (
    BridgeRequest,
    ChainLeg,
    CrossChainPlan,
    Interaction,
    LegPlan,
    MultiLegPlan,
)

WETH_ETH = "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"
WETH_BASE = "0x4200000000000000000000000000000000000006"
USER = "0x" + "aa" * 20
APP_ETH = "0x" + "e1" * 20
APP_BASE = "0x" + "ba" * 20


def _run(coro):
    return asyncio.run(coro)


def _ix(chain_id: int = 1) -> Interaction:
    return Interaction(
        target="0x" + "11" * 20, value="0",
        call_data="0xa9059cbb" + "00" * 28, chain_id=chain_id,
    )


def _plan() -> CrossChainPlan:
    """Two-leg Ethereum→Base plan with one bridge request."""
    return CrossChainPlan(
        legs=[
            ChainLeg(chain_id=1, interactions=[_ix(1)]),
            ChainLeg(chain_id=8453, interactions=[_ix(8453)]),
        ],
        bridge_requests=[
            BridgeRequest(
                token=WETH_ETH, amount=10**18,
                src_chain_id=1, dst_chain_id=8453, recipient=USER,
            ),
        ],
    )


@pytest.fixture
def compiler():
    reg = BridgeRegistry()
    reg.register(MockBridgeAdapter())
    return CrossChainCompiler(reg)


async def _compile(compiler, app_addresses=None):
    return await compiler.compile(
        _plan(), order_id="o1", user_address=USER,
        contract_address=APP_ETH, deadline=0,
        app_addresses=app_addresses,
    )


# ═══════════════════════════════════════════════════════════════════════════
#  §2 Recipient split — destination App vs origin refund
# ═══════════════════════════════════════════════════════════════════════════


class TestCompilerRecipient:
    def test_bridges_to_destination_app_not_user(self, compiler):
        compiled = _run(_compile(compiler, {1: APP_ETH, 8453: APP_BASE}))
        bridge_leg = compiled.multi_leg_plan.forward_legs[1]
        # The fill must land where escrowDeposit's balanceOf(this) check can
        # see it — anywhere else and the destination leg has nothing to spend.
        assert bridge_leg.metadata["bridge_recipient"] == APP_BASE
        assert bridge_leg.metadata["bridge_recipient_is_app"] is True

    def test_origin_refund_still_goes_to_the_user(self, compiler):
        compiled = _run(_compile(compiler, {1: APP_ETH, 8453: APP_BASE}))
        bridge_leg = compiled.multi_leg_plan.forward_legs[1]
        # An origin-chain refund into the destination app would be stranded
        # on the wrong chain; it belongs in the user's own wallet.
        assert bridge_leg.metadata["bridge_refund_recipient"] == USER

    def test_falls_back_to_user_when_no_app_on_destination(self, compiler):
        compiled = _run(_compile(compiler, {1: APP_ETH}))
        bridge_leg = compiled.multi_leg_plan.forward_legs[1]
        assert bridge_leg.metadata["bridge_recipient"] == USER
        assert bridge_leg.metadata["bridge_recipient_is_app"] is False

    def test_legacy_call_without_app_addresses_unchanged(self, compiler):
        # Callers that never pass the map keep the pre-fix behaviour.
        compiled = _run(_compile(compiler, None))
        assert compiled.multi_leg_plan.forward_legs[1].metadata[
            "bridge_recipient"
        ] == USER

    def test_escrow_beneficiary_is_the_user_not_the_app(self, compiler):
        # escrowRefund is callable only by dep.user — that must stay the
        # human, even though the tokens sit in the app.
        compiled = _run(_compile(compiler, {1: APP_ETH, 8453: APP_BASE}))
        assert compiled.escrow_params[0]["user"] == USER

    def test_rollback_bridge_returns_to_the_user(self, compiler):
        # The reverse bridge is the terminal step of a revert: the user's own
        # wallet on the source chain, not an app balance.
        compiled = _run(_compile(compiler, {1: APP_ETH, 8453: APP_BASE}))
        rollbacks = compiled.multi_leg_plan.rollback_legs
        assert rollbacks, "expected a reverse-bridge rollback leg"
        assert rollbacks[0].metadata["type"] == "rollback_bridge"


class TestAcrossEncodesBothAddresses:
    def _quote(self) -> BridgeQuote:
        return BridgeQuote(
            protocol="across", src_chain_id=1, dst_chain_id=8453,
            token_in=WETH_ETH, token_out=WETH_BASE,
            amount_in=10**18, estimated_output=10**18 - 10**14,
            fee=10**14, estimated_duration_s=60,
            metadata={
                "spoke_pool": "0x" + "5c" * 20,
                "quote_timestamp": 1_700_000_000,
                "fill_deadline": 1_700_003_600,
                "token_symbol": "WETH",
            },
        )

    def test_depositor_and_recipient_differ(self):
        ixs = AcrossAdapter().build_bridge_interactions(
            self._quote(), APP_BASE, refund_recipient=USER,
        )
        deposit_calldata = ixs[1].call_data
        # depositV3(depositor, recipient, ...) — first two ABI words after
        # the selector, each a left-padded address.
        body = deposit_calldata[10:]
        depositor = "0x" + body[24:64]
        recipient = "0x" + body[88:128]
        assert depositor.lower() == USER.lower()
        assert recipient.lower() == APP_BASE.lower()

    def test_refund_recipient_defaults_to_recipient(self):
        ixs = AcrossAdapter().build_bridge_interactions(self._quote(), USER)
        body = ixs[1].call_data[10:]
        assert ("0x" + body[24:64]).lower() == USER.lower()
        assert ("0x" + body[88:128]).lower() == USER.lower()


class TestResolveAppAddressesFailsClosed:
    """OrderProcessor rejects a plan that would bridge into a chain where the
    App isn't deployed — the hop would move the user's funds somewhere the
    intent can't continue from and no escrow would back them."""

    def _processor(self, deployments: dict):
        from minotaur_subnet.blockloop.order_processor import OrderProcessor

        class FakeStatus:
            def __init__(self, ready): self._ready = ready
            def is_order_ready(self): return self._ready
            @property
            def value(self): return "solved" if self._ready else "failed"

        class FakeDeployment:
            def __init__(self, addr, ready=True):
                self.contract_address = addr
                self.status = FakeStatus(ready)

        class FakeStore:
            def get_deployment(self, app_id, chain_id=None):
                entry = deployments.get(chain_id)
                if entry is None:
                    return None
                addr, ready = entry
                return FakeDeployment(addr, ready)

        proc = OrderProcessor.__new__(OrderProcessor)
        proc.app_store = FakeStore()
        return proc

    def test_resolves_source_and_destination(self):
        proc = self._processor({8453: (APP_BASE, True)})
        out = proc._resolve_app_addresses("app-1", _plan(), APP_ETH, 1)
        assert out == {1: APP_ETH, 8453: APP_BASE}

    def test_raises_when_destination_undeployed(self):
        proc = self._processor({})
        with pytest.raises(CrossChainCompileError, match="no deployment"):
            proc._resolve_app_addresses("app-1", _plan(), APP_ETH, 1)

    def test_raises_when_destination_not_order_ready(self):
        proc = self._processor({8453: (APP_BASE, False)})
        with pytest.raises(CrossChainCompileError, match="not order-ready"):
            proc._resolve_app_addresses("app-1", _plan(), APP_ETH, 1)


# ═══════════════════════════════════════════════════════════════════════════
#  §3 Plan-set gate
# ═══════════════════════════════════════════════════════════════════════════


class FakeSubmitResult:
    success = True
    tx_hash = "0xabc"
    error = None


class FakeRelayer:
    def __init__(self):
        self.submitted: list = []

    async def submit_plan(self, order, plan, score, consensus, **kwargs):
        self.submitted.append((order, plan))
        return FakeSubmitResult()


class FakeAppStore:
    def get_app(self, app_id): return None
    def record_execution(self, *a, **kw): pass


class FakeScorer:
    async def score(self, *a, **kw):
        class _R:
            score = 0.7
        return _R()


def _multi_leg_plan() -> MultiLegPlan:
    return MultiLegPlan(
        forward_legs=[
            LegPlan(leg_index=0, chain_id=1, intent_selector="",
                    intent_params_hex="", interactions=[_ix(1)],
                    metadata={"type": "solver_leg"}),
            LegPlan(leg_index=1, chain_id=1, intent_selector="",
                    intent_params_hex="", interactions=[_ix(1)],
                    metadata={"type": "solver_leg"}),
        ],
        rollback_legs=[],
    )


PLAN_SET = {"hashes": ["0x" + "11" * 32], "plan_set_hash": "0x" + "22" * 32,
            "positions": {"0": 0}}


@pytest.fixture
def parked_env():
    """An order parked in AWAITING_PLAN_SET_SIGNATURE with a stored plan."""
    ob = IntentOrderBook()
    relayer = FakeRelayer()
    from minotaur_subnet.blockloop.multi_leg import MultiLegOrchestrator
    orch = MultiLegOrchestrator(
        orderbook=ob, relayer=relayer, app_store=FakeAppStore(),
        plan_scorer=FakeScorer(),
    )
    order = ob.submit(
        app_id="app-1", intent_function="swap",
        params={"input_token": "0x" + "22" * 20},
        submitted_by=USER, deadline=time.time() + 3600,
    )
    ob.update_order(
        order.order_id,
        status=OrderStatus.AWAITING_PLAN_SET_SIGNATURE,
        plan={
            "metadata": {
                "multi_leg_plan": _multi_leg_plan().to_dict(),
                "contract_address": APP_ETH,
                "plan_set": PLAN_SET,
                "cross_chain": True,
            },
        },
    )
    return ob, relayer, orch, order


class TestPlanSetGate:
    def test_resume_requires_a_signature(self, parked_env):
        ob, relayer, orch, order = parked_env
        with pytest.raises(ValueError, match="no plan-set signature"):
            _run(orch.resume_after_plan_set_signature(order.order_id))
        assert relayer.submitted == []

    def test_resume_rejects_wrong_state(self, parked_env):
        ob, relayer, orch, order = parked_env
        ob.update_order(order.order_id, status=OrderStatus.SOLVED)
        with pytest.raises(ValueError, match="not awaiting"):
            _run(orch.resume_after_plan_set_signature(order.order_id))

    def test_resume_executes_the_legs(self, parked_env):
        ob, relayer, orch, order = parked_env
        o = ob.get(order.order_id)
        o.params["plan_set_signature"] = "0x" + "ab" * 65
        ob.update_order(order.order_id, params=o.params)

        result = _run(orch.resume_after_plan_set_signature(order.order_id))
        assert result["resumed"] is True
        assert len(relayer.submitted) == 2  # both forward legs
        assert ob.get(order.order_id).status == OrderStatus.FILLED

    def test_resumed_legs_carry_the_plan_set(self, parked_env):
        ob, relayer, orch, order = parked_env
        o = ob.get(order.order_id)
        o.params["plan_set_signature"] = "0x" + "ab" * 65
        ob.update_order(order.order_id, params=o.params)
        _run(orch.resume_after_plan_set_signature(order.order_id))

        submitted_order, _ = relayer.submitted[0]
        # leg 0 has a position in the set → executeLegSigned inputs threaded
        assert submitted_order.params["_plan_set_signature"] == "0x" + "ab" * 65
        assert submitted_order.params["_plan_set_position"] == 0


# ═══════════════════════════════════════════════════════════════════════════
#  Bridge-hop failure: refresh-only recovery
# ═══════════════════════════════════════════════════════════════════════════


class TestBridgeHopFailure:
    def _orch(self):
        ob = IntentOrderBook()
        from minotaur_subnet.blockloop.multi_leg import MultiLegOrchestrator
        orch = MultiLegOrchestrator(orderbook=ob, relayer=FakeRelayer())
        order = ob.submit(
            app_id="app-1", intent_function="swap", params={},
            submitted_by=USER, deadline=time.time() + 3600,
        )
        return ob, orch, order

    def test_parks_with_refresh_only(self, monkeypatch):
        monkeypatch.setenv("CROSS_CHAIN_USER_DECISION", "1")
        ob, orch, order = self._orch()
        _run(orch.park_for_user_decision(
            order, [], [], APP_ETH, {}, -1, "Across deposit expired",
            options=["refresh"], expiry_status=OrderStatus.BRIDGE_FAILED,
        ))
        rec = ob.get(order.order_id).params["recovery"]
        # Nothing reached the destination, so the reverse-bridge revert legs
        # have no funds to move — offering "revert" would be a lie.
        assert rec["options"] == ["refresh"]
        assert rec["expiry_status"] == OrderStatus.BRIDGE_FAILED.value

    def test_revert_is_refused_when_not_offered(self, monkeypatch):
        monkeypatch.setenv("CROSS_CHAIN_USER_DECISION", "1")
        ob, orch, order = self._orch()
        _run(orch.park_for_user_decision(
            order, [], [], APP_ETH, {}, -1, "Across deposit expired",
            options=["refresh"], expiry_status=OrderStatus.BRIDGE_FAILED,
        ))
        with pytest.raises(ValueError, match="not available"):
            _run(orch.resolve_user_decision(order.order_id, "revert"))

    def test_refresh_still_resolves(self, monkeypatch):
        monkeypatch.setenv("CROSS_CHAIN_USER_DECISION", "1")
        ob, orch, order = self._orch()
        _run(orch.park_for_user_decision(
            order, [], [], APP_ETH, {}, -1, "Across deposit expired",
            options=["refresh"], expiry_status=OrderStatus.BRIDGE_FAILED,
        ))
        out = _run(orch.resolve_user_decision(order.order_id, "refresh"))
        assert out["action"] == "refresh"
        assert ob.get(order.order_id).status == OrderStatus.REFRESHING


class TestBridgeTrackerRoutesHopFailure:
    """Bridge expiry / poll timeout used to bypass the recovery flow
    entirely and dead-end in BRIDGE_FAILED."""

    def _tracker(self, decision_on: bool, monkeypatch):
        from minotaur_subnet.relayer.bridge_tracker import BridgeTracker, TrackedBridge
        from minotaur_subnet.shared.types import ExecutionPlan

        monkeypatch.setenv(
            "CROSS_CHAIN_USER_DECISION", "1" if decision_on else "0",
        )
        ob = IntentOrderBook()
        order = ob.submit(
            app_id="app-1", intent_function="swap", params={},
            submitted_by=USER, deadline=time.time() + 3600,
        )
        tracker = BridgeTracker.__new__(BridgeTracker)
        tracker.orderbook = ob
        tracker.multi_leg_orchestrator = None
        if decision_on:
            from minotaur_subnet.blockloop.multi_leg import MultiLegOrchestrator
            tracker.multi_leg_orchestrator = MultiLegOrchestrator(
                orderbook=ob, relayer=FakeRelayer(),
            )
        tracked = TrackedBridge(
            order_id=order.order_id, src_tx_hash="0xdead",
            src_chain_id=1, dst_chain_id=8453, bridge_protocol="across",
            plan=ExecutionPlan(
                intent_id="app-1", interactions=[], deadline=0, nonce=0,
                metadata={"contract_address": APP_ETH, "bridge_leg_index": 1},
            ),
        )
        return ob, tracker, tracked, order

    def test_parks_for_refresh_when_enabled(self, monkeypatch):
        ob, tracker, tracked, order = self._tracker(True, monkeypatch)
        _run(tracker._fail_bridge(tracked, "Across deposit expired unfilled"))
        parked = ob.get(order.order_id)
        assert parked.status == OrderStatus.AWAITING_USER_DECISION
        assert parked.params["recovery"]["options"] == ["refresh"]

    def test_legacy_dead_end_when_disabled(self, monkeypatch):
        ob, tracker, tracked, order = self._tracker(False, monkeypatch)
        _run(tracker._fail_bridge(tracked, "Bridge polling timeout"))
        assert ob.get(order.order_id).status == OrderStatus.BRIDGE_FAILED
