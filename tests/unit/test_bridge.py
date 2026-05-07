"""Tests for the bridge package: types, registry, mock adapter, tracker."""

from __future__ import annotations

import asyncio
import pytest

from minotaur_subnet.bridge.base import (
    BridgeAdapter,
    BridgeQuote,
    BridgeStatus,
    BridgeStatusEnum,
)
from minotaur_subnet.bridge.registry import BridgeRegistry
from minotaur_subnet.bridge.mock import MockBridgeAdapter
from minotaur_subnet.bridge.tensorplex import TensorplexAdapter, BITTENSOR_SUBSTRATE_CHAIN_ID


# ── BridgeQuote / BridgeStatus ───────────────────────────────────────────────


class TestBridgeTypes:
    def test_bridge_quote_fields(self):
        q = BridgeQuote(
            protocol="mock",
            src_chain_id=1,
            dst_chain_id=964,
            token_in="0xabc",
            token_out="0xabc",
            amount_in=1_000_000,
            estimated_output=999_000,
            fee=1_000,
            estimated_duration_s=1800,
        )
        assert q.protocol == "mock"
        assert q.estimated_output == 999_000
        assert q.fee == 1_000

    def test_bridge_status_enum(self):
        assert BridgeStatusEnum.COMPLETED.value == "completed"
        assert BridgeStatusEnum.FAILED.value == "failed"

    def test_bridge_status_fields(self):
        s = BridgeStatus(
            status=BridgeStatusEnum.COMPLETED,
            src_tx_hash="0xabc",
            dst_tx_hash="0xdef",
        )
        assert s.status == BridgeStatusEnum.COMPLETED
        assert s.dst_tx_hash == "0xdef"


# ── MockBridgeAdapter ────────────────────────────────────────────────────────


class TestMockBridgeAdapter:
    @pytest.fixture
    def adapter(self):
        return MockBridgeAdapter(fee_bps=10)

    def test_protocol_name(self, adapter):
        assert adapter.PROTOCOL == "mock"

    def test_quote_deducts_fee(self, adapter):
        quote = asyncio.run(adapter.quote("0xtoken", 1_000_000, 1, 964))
        assert quote.amount_in == 1_000_000
        assert quote.fee == 1000  # 10 bps of 1M = 1000
        assert quote.estimated_output == 999_000
        assert quote.estimated_duration_s == 0  # instant

    def test_build_bridge_interactions(self, adapter):
        quote = BridgeQuote(
            protocol="mock",
            src_chain_id=1,
            dst_chain_id=964,
            token_in="0xtoken",
            token_out="0xtoken",
            amount_in=1_000_000,
            estimated_output=999_000,
            fee=1_000,
            estimated_duration_s=0,
        )
        ixs = adapter.build_bridge_interactions(quote, "0xsender")
        assert len(ixs) == 1
        assert ixs[0].chain_id == 1

    def test_check_status_instant(self, adapter):
        status = asyncio.run(adapter.check_status("0xtx123", 1))
        assert status.status == BridgeStatusEnum.COMPLETED

    def test_supported_routes(self, adapter):
        routes = adapter.supported_routes()
        assert (1, 964) in routes
        assert (1, 8453) in routes
        # No self-routes
        assert all(src != dst for src, dst in routes)


# ── TensorplexAdapter ────────────────────────────────────────────────────────


class TestTensorplexAdapter:
    @pytest.fixture
    def adapter(self):
        return TensorplexAdapter()

    def test_protocol(self, adapter):
        assert adapter.PROTOCOL == "tensorplex"

    def test_quote_fee(self, adapter):
        quote = asyncio.run(adapter.quote("0xwtao", 10_000_000, 1, 964))
        assert quote.fee == 10_000  # 0.1%
        assert quote.estimated_output == 9_990_000
        assert quote.estimated_duration_s == 1800

    def test_build_interactions_not_implemented(self, adapter):
        quote = BridgeQuote(
            protocol="tensorplex",
            src_chain_id=1,
            dst_chain_id=964,
            token_in="0x",
            token_out="0x",
            amount_in=1000,
            estimated_output=999,
            fee=1,
            estimated_duration_s=1800,
        )
        with pytest.raises(NotImplementedError):
            adapter.build_bridge_interactions(quote, "0x")

    def test_check_status_returns_pending_on_network_error(self, adapter):
        """check_status calls the Tensorplex API; network errors return PENDING."""
        status = asyncio.run(adapter.check_status("0x", 1, dst_chain_id=0))
        assert status.status == BridgeStatusEnum.PENDING

    def test_supported_routes(self, adapter):
        routes = adapter.supported_routes()
        assert (1, 964) in routes
        assert (964, 1) in routes
        assert (BITTENSOR_SUBSTRATE_CHAIN_ID, 1) in routes
        assert (1, BITTENSOR_SUBSTRATE_CHAIN_ID) in routes
        assert len(routes) == 4


# ── BridgeRegistry ───────────────────────────────────────────────────────────


class TestBridgeRegistry:
    def test_register_and_get(self):
        reg = BridgeRegistry()
        mock = MockBridgeAdapter()
        reg.register(mock)
        assert reg.get("mock") is mock
        assert reg.get("nonexistent") is None

    def test_protocols_property(self):
        reg = BridgeRegistry()
        reg.register(MockBridgeAdapter())
        reg.register(TensorplexAdapter())
        assert sorted(reg.protocols) == ["mock", "tensorplex"]

    def test_find_bridge_by_route(self):
        reg = BridgeRegistry()
        mock = MockBridgeAdapter()
        tp = TensorplexAdapter()
        reg.register(mock)
        reg.register(tp)

        # Both support ETH → BT EVM
        adapters = reg.find_bridge(1, 964)
        assert len(adapters) == 2

        # Only mock supports ETH → Base
        adapters = reg.find_bridge(1, 8453)
        assert len(adapters) == 1
        assert adapters[0].PROTOCOL == "mock"

    def test_best_quote(self):
        reg = BridgeRegistry()
        # Both support ETH→BT EVM route; mock + tensorplex
        reg.register(MockBridgeAdapter(fee_bps=10))  # 0.1%
        reg.register(TensorplexAdapter())  # also 0.1%, same fee

        quote = asyncio.run(reg.best_quote("0xwtao", 1_000_000, 1, 964))
        assert quote is not None
        # Both have 10bps fee = 1000 on 1M
        assert quote.fee == 1000
        assert quote.estimated_output == 999_000

    def test_best_quote_no_adapters(self):
        reg = BridgeRegistry()
        quote = asyncio.run(reg.best_quote("0xtoken", 1000, 1, 8453))
        assert quote is None


# ── BridgeTracker ────────────────────────────────────────────────────────────


class TestBridgeTracker:
    @pytest.fixture
    def setup(self):
        from minotaur_subnet.relayer.bridge_tracker import BridgeTracker
        from minotaur_subnet.orderbook.orderbook import IntentOrderBook, OrderStatus
        from minotaur_subnet.shared.types import ExecutionPlan, Interaction
        from minotaur_subnet.relayer.base import MockRelayer

        reg = BridgeRegistry()
        reg.register(MockBridgeAdapter())
        ob = IntentOrderBook()
        relayer = MockRelayer()

        tracker = BridgeTracker(
            bridge_registry=reg,
            orderbook=ob,
            relayer=relayer,
            poll_interval=1.0,
        )

        # Submit a test order and set it to BRIDGING
        order = ob.submit(
            app_id="test_app",
            intent_function="execute",
            params={"input_token": "0xA", "output_token": "0xB"},
            submitted_by="0x" + "11" * 20,
        )
        ob.update_order(order.order_id, status=OrderStatus.BRIDGING)

        plan = ExecutionPlan(
            intent_id="execute",
            interactions=[
                Interaction(target="0x" + "aa" * 20, value="0", call_data="0x", chain_id=1),
                Interaction(target="0x" + "bb" * 20, value="0", call_data="0x", chain_id=1),
                Interaction(target="0x" + "cc" * 20, value="0", call_data="0x", chain_id=964),
            ],
            deadline=9999999999,
            nonce=0,
            metadata={
                "cross_chain": True,
                "src_chain_id": 1,
                "dst_chain_id": 964,
                "bridge_protocol": "mock",
                "legs": [
                    {"leg_id": 0, "chain_id": 1, "type": "source", "interaction_indices": [0]},
                    {"leg_id": 1, "chain_id": 1, "type": "bridge", "bridge_protocol": "mock", "interaction_indices": [1]},
                    {"leg_id": 2, "chain_id": 964, "type": "destination", "interaction_indices": [2]},
                ],
            },
        )

        return tracker, ob, order, plan

    def test_track_registers_bridge(self, setup):
        tracker, ob, order, plan = setup
        tracker.track(order.order_id, "0xtx_source", plan)
        assert tracker.tracked_count == 1

    def test_poll_once_completes_mock_bridge(self, setup):
        tracker, ob, order, plan = setup
        tracker.track(order.order_id, "0xtx_source", plan)

        # Mock bridge completes instantly
        completed = asyncio.run(tracker.poll_once())
        assert completed == 1
        assert tracker.tracked_count == 0

        # Order should be FILLED
        from minotaur_subnet.orderbook.orderbook import OrderStatus
        updated = ob.get(order.order_id)
        assert updated.status == OrderStatus.FILLED

    def test_poll_timeout(self, setup):
        tracker, ob, order, plan = setup
        tracker.track(order.order_id, "0xtx_source", plan)

        # Set max_polls to 0 to trigger immediate timeout
        tracked = tracker._tracked[order.order_id]
        tracked.max_polls = 0

        completed = asyncio.run(tracker.poll_once())
        assert completed == 0

        from minotaur_subnet.orderbook.orderbook import OrderStatus
        updated = ob.get(order.order_id)
        assert updated.status == OrderStatus.BRIDGE_FAILED

    def test_empty_poll(self, setup):
        tracker, _, _, _ = setup
        completed = asyncio.run(tracker.poll_once())
        assert completed == 0


# ── Cross-chain plan helpers ─────────────────────────────────────────────────


class TestPlanHelpers:
    def test_partition_plan_by_leg(self):
        from minotaur_subnet.shared.types import (
            ExecutionPlan,
            Interaction,
            partition_plan_by_leg,
        )

        plan = ExecutionPlan(
            intent_id="test",
            interactions=[
                Interaction(target="0xa", value="0", call_data="0x", chain_id=1),
                Interaction(target="0xb", value="0", call_data="0x", chain_id=1),
                Interaction(target="0xc", value="0", call_data="0x", chain_id=964),
            ],
            deadline=0,
            nonce=0,
            metadata={
                "legs": [
                    {"leg_id": 0, "chain_id": 1, "interaction_indices": [0, 1]},
                    {"leg_id": 1, "chain_id": 964, "interaction_indices": [2]},
                ],
            },
        )

        parts = partition_plan_by_leg(plan)
        assert len(parts) == 2
        assert len(parts[0]) == 2
        assert len(parts[1]) == 1
        assert parts[1][0].target == "0xc"

    def test_partition_no_legs(self):
        from minotaur_subnet.shared.types import (
            ExecutionPlan,
            Interaction,
            partition_plan_by_leg,
        )

        plan = ExecutionPlan(
            intent_id="test",
            interactions=[Interaction(target="0xa", value="0", call_data="0x")],
            deadline=0,
            nonce=0,
            metadata={},
        )
        parts = partition_plan_by_leg(plan)
        assert 0 in parts
        assert len(parts[0]) == 1

    def test_extract_leg_plan(self):
        from minotaur_subnet.shared.types import (
            ExecutionPlan,
            Interaction,
            extract_leg_plan,
        )

        plan = ExecutionPlan(
            intent_id="test",
            interactions=[
                Interaction(target="0xa", value="0", call_data="0x", chain_id=1),
                Interaction(target="0xb", value="0", call_data="0x", chain_id=964),
            ],
            deadline=100,
            nonce=5,
            metadata={
                "legs": [
                    {"leg_id": 0, "chain_id": 1, "interaction_indices": [0]},
                    {"leg_id": 1, "chain_id": 964, "interaction_indices": [1]},
                ],
            },
        )

        leg1 = extract_leg_plan(plan, 1)
        assert len(leg1.interactions) == 1
        assert leg1.interactions[0].target == "0xb"
        assert leg1.metadata["chain_id"] == 964
        assert leg1.deadline == 100
        assert leg1.nonce == 5

    def test_extract_leg_plan_no_legs(self):
        from minotaur_subnet.shared.types import (
            ExecutionPlan,
            Interaction,
            extract_leg_plan,
        )

        plan = ExecutionPlan(
            intent_id="test",
            interactions=[Interaction(target="0xa", value="0", call_data="0x")],
            deadline=0,
            nonce=0,
            metadata={},
        )
        result = extract_leg_plan(plan, 99)
        # Should return original plan when no legs metadata
        assert result is plan
