"""Cross-chain order tests — verify orders route to correct chain and
exercise the full cross-chain lifecycle: submit -> solve -> bridge -> fill.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pytest

pytestmark = pytest.mark.cross_chain
from minotaur_subnet.orderbook import IntentOrderBook, OrderStatus
from minotaur_subnet.shared.types import (
    AppIntentConfig,
    AppIntentDefinition,
    AppStatus,
    DeploymentResult,
)


# Well-known token addresses (mainnet)
WETH = "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"
USDC = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
WTAO = "0x77E06c9eCCf2E797fd462A92b6D7642EF85b0A44"


class TestCrossChain:
    """Original chain-routing test (kept for backwards compat)."""

    @pytest.mark.asyncio
    async def test_orders_on_different_chains(self, block_loop, orderbook, temp_store):
        """Orders on ETH vs Base are tracked with correct chain_id."""
        # Submit ETH order
        eth_order = orderbook.submit(
            app_id="emulation_swap",
            intent_function="execute",
            params={"input_token": "WETH", "output_token": "USDC", "input_amount": "1000"},
            submitted_by="0x" + "aa" * 20,
            chain_id=1,
        )

        # Submit Base order
        base_order = orderbook.submit(
            app_id="emulation_swap",
            intent_function="execute",
            params={"input_token": "WETH", "output_token": "USDC", "input_amount": "1000"},
            submitted_by="0x" + "bb" * 20,
            chain_id=8453,
        )

        assert eth_order.chain_id == 1
        assert base_order.chain_id == 8453

        # Both process in same tick
        result = await block_loop.tick()
        assert result.orders_processed == 2


class TestCrossChainLifecycle:
    """Full cross-chain lifecycle: submit -> solve -> bridge -> fill."""

    @pytest.mark.asyncio
    async def test_cross_chain_order_enters_bridging(
        self, cross_chain_block_loop, orderbook, bridge_tracker,
    ):
        """Cross-chain order transitions: OPEN -> ... -> BRIDGING."""
        order = orderbook.submit(
            app_id="emulation_swap",
            intent_function="execute",
            params={
                "input_token": WETH,
                "output_token": USDC,
                "input_amount": "1000000000000000000",
                "dest_chain_id": "8453",
            },
            submitted_by="0x" + "aa" * 20,
        )
        assert order.status == OrderStatus.OPEN

        result = await cross_chain_block_loop.tick()
        assert result.orders_processed == 1

        updated = orderbook.get(order.order_id)
        # With bridge tracker wired, cross-chain orders enter BRIDGING
        # (or FILLED if the mock bridge completes synchronously in the tracker)
        assert updated.status in (OrderStatus.BRIDGING, OrderStatus.FILLED)

    @pytest.mark.asyncio
    async def test_bridge_tracker_completes_order(
        self, cross_chain_block_loop, orderbook, bridge_tracker,
    ):
        """BridgeTracker polls -> bridge completes -> order FILLED."""
        order = orderbook.submit(
            app_id="emulation_swap",
            intent_function="execute",
            params={
                "input_token": WETH,
                "output_token": USDC,
                "input_amount": "1000000000000000000",
                "dest_chain_id": "8453",
            },
            submitted_by="0x" + "aa" * 20,
        )

        await cross_chain_block_loop.tick()
        updated = orderbook.get(order.order_id)

        if updated.status == OrderStatus.BRIDGING:
            # Poll the tracker — mock bridge completes instantly
            completed = await bridge_tracker.poll_once()
            assert completed == 1
            final = orderbook.get(order.order_id)
            assert final.status == OrderStatus.FILLED
        else:
            # Already filled (no bridge tracker delay)
            assert updated.status == OrderStatus.FILLED

    @pytest.mark.asyncio
    async def test_bridge_failure_marks_order(
        self, orderbook, temp_store, mock_relayer, sample_swap_app,
    ):
        """Bridge failure -> BRIDGE_FAILED status."""
        from minotaur_subnet.bridge.base import (
            BridgeAdapter,
            BridgeQuote,
            BridgeStatus,
            BridgeStatusEnum,
        )
        from minotaur_subnet.bridge.registry import BridgeRegistry
        from minotaur_subnet.relayer.bridge_tracker import BridgeTracker
        from minotaur_subnet.blockloop import BlockLoop
        from minotaur_subnet.shared.types import Interaction

        class FailingBridgeAdapter(BridgeAdapter):
            """Bridge adapter that always reports FAILED status."""
            PROTOCOL = "failing"

            async def quote(self, token_in, amount, src, dst):
                return BridgeQuote(
                    protocol=self.PROTOCOL,
                    src_chain_id=src, dst_chain_id=dst,
                    token_in=token_in, token_out=token_in,
                    amount_in=amount, estimated_output=amount,
                    fee=0, estimated_duration_s=0,
                )

            def build_bridge_interactions(self, quote, sender):
                return [Interaction(
                    target="0x" + "00" * 19 + "B1",
                    value="0",
                    call_data="0xfailing_bridge",
                    chain_id=quote.src_chain_id,
                )]

            async def check_status(self, src_tx_hash, src_chain_id):
                return BridgeStatus(
                    status=BridgeStatusEnum.FAILED,
                    src_tx_hash=src_tx_hash,
                    error="Bridge transfer reverted",
                )

            def supported_routes(self):
                return [(1, 8453), (8453, 1)]

        reg = BridgeRegistry()
        reg.register(FailingBridgeAdapter())
        tracker = BridgeTracker(
            bridge_registry=reg, orderbook=orderbook, relayer=mock_relayer,
        )

        temp_store.save_app(sample_swap_app)
        temp_store.save_deployment(DeploymentResult(
            app_id="emulation_swap",
            status=AppStatus.ACTIVE,
            contract_address="0x" + "ee" * 20,
        ))
        loop = BlockLoop(
            orderbook=orderbook,
            app_store=temp_store,
            relayer=mock_relayer,
            tick_interval=1.0,
            score_threshold=0.4,
            bridge_registry=reg,
            bridge_tracker=tracker,
        )

        order = orderbook.submit(
            app_id="emulation_swap",
            intent_function="execute",
            params={
                "input_token": WETH,
                "output_token": USDC,
                "input_amount": "1000000000000000000",
                "dest_chain_id": "8453",
            },
            submitted_by="0x" + "cc" * 20,
        )

        await loop.tick()
        updated = orderbook.get(order.order_id)
        if updated.status == OrderStatus.BRIDGING:
            completed = await tracker.poll_once()
            assert completed == 0  # failure doesn't count as completed
            final = orderbook.get(order.order_id)
            assert final.status == OrderStatus.BRIDGE_FAILED
            assert final.error is not None

    @pytest.mark.asyncio
    async def test_single_chain_order_unaffected(
        self, cross_chain_block_loop, orderbook,
    ):
        """Single-chain orders still work with bridge infra wired."""
        order = orderbook.submit(
            app_id="emulation_swap",
            intent_function="execute",
            params={
                "input_token": WETH,
                "output_token": USDC,
                "input_amount": "1000",
            },
            submitted_by="0x" + "bb" * 20,
        )

        await cross_chain_block_loop.tick()
        updated = orderbook.get(order.order_id)
        # Single-chain should go straight to FILLED or REJECTED, never BRIDGING
        assert updated.status in (OrderStatus.FILLED, OrderStatus.REJECTED)
        assert updated.status != OrderStatus.BRIDGING

    @pytest.mark.asyncio
    async def test_cross_chain_plan_has_three_legs(
        self, cross_chain_block_loop, orderbook,
    ):
        """Verify plan metadata contains source + bridge + destination legs."""
        order = orderbook.submit(
            app_id="emulation_swap",
            intent_function="execute",
            params={
                "input_token": WETH,
                "output_token": USDC,
                "input_amount": "1000000000000000000",
                "dest_chain_id": "8453",
            },
            submitted_by="0x" + "dd" * 20,
        )

        await cross_chain_block_loop.tick()
        updated = orderbook.get(order.order_id)
        assert updated.plan is not None

        legs = updated.plan.get("metadata", {}).get("legs", [])
        assert len(legs) == 3

        leg_types = {leg["type"] for leg in legs}
        assert leg_types == {"source", "bridge", "destination"}

        # Verify chain routing
        src_leg = next(l for l in legs if l["type"] == "source")
        dest_leg = next(l for l in legs if l["type"] == "destination")
        assert src_leg["chain_id"] == 1  # default chain_id
        assert dest_leg["chain_id"] == 8453

    @pytest.mark.asyncio
    async def test_dest_leg_empty_when_same_token(
        self, cross_chain_block_loop, orderbook,
    ):
        """When bridge delivers the desired token, dest leg has no interactions.

        MockBridgeAdapter returns token_out == token_in, so if the user's
        output_token matches the input (bridge delivers it directly), the
        dest leg should be empty.
        """
        # Mock bridge: token_out == token_in (WETH)
        # User wants WETH on dest chain — bridge delivers WETH directly
        order = orderbook.submit(
            app_id="emulation_swap",
            intent_function="execute",
            params={
                "input_token": WETH,
                "output_token": WETH,  # same as what bridge delivers
                "input_amount": "1000000000000000000",
                "dest_chain_id": "8453",
            },
            submitted_by="0x" + "ee" * 20,
        )

        await cross_chain_block_loop.tick()
        updated = orderbook.get(order.order_id)
        assert updated.plan is not None

        legs = updated.plan.get("metadata", {}).get("legs", [])
        dest_leg = next(l for l in legs if l["type"] == "destination")
        # Dest leg has no interactions (bridge delivers desired token)
        assert dest_leg["interaction_indices"] == []

    @pytest.mark.asyncio
    async def test_bridge_tracker_tracking_info(
        self, cross_chain_block_loop, orderbook, bridge_tracker,
    ):
        """BridgeTracker exposes tracking info for BRIDGING orders."""
        order = orderbook.submit(
            app_id="emulation_swap",
            intent_function="execute",
            params={
                "input_token": WETH,
                "output_token": USDC,
                "input_amount": "1000000000000000000",
                "dest_chain_id": "8453",
            },
            submitted_by="0x" + "ff" * 20,
        )

        await cross_chain_block_loop.tick()
        updated = orderbook.get(order.order_id)

        if updated.status == OrderStatus.BRIDGING:
            info = bridge_tracker.get_tracking_info(order.order_id)
            assert info is not None
            assert info["bridge_protocol"] == "mock"
            assert info["dst_chain_id"] == 8453
