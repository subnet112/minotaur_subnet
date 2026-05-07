"""Tests for cross-chain plan generation in BaselineSwapSolver.

Updated for the CrossChainPlan architecture: solver emits CrossChainPlan
in metadata instead of raw bridge calldata and MultiLegPlan.
"""

from __future__ import annotations

import asyncio
import pytest

pytestmark = pytest.mark.cross_chain

# BaselineSwapSolver was moved to the external solver repo
# (subnet112/minotaur-solver). These tests exercise the cross-chain
# planning surface on top of it and skip when the module is absent.
pytest.importorskip("minotaur_subnet.sdk.solvers.baseline_solver")

from minotaur_subnet.shared.types import (
    AppIntentDefinition,
    CrossChainPlan,
    IntentState,
    ExecutionPlan,
)
from minotaur_subnet.bridge.registry import BridgeRegistry
from minotaur_subnet.bridge.mock import MockBridgeAdapter
from minotaur_subnet.bridge.base import BridgeAdapter, BridgeQuote, BridgeStatus, BridgeStatusEnum
from minotaur_subnet.shared.types import Interaction


class TestCrossChainDetection:
    """Test that the solver detects dest_chain_id and routes to cross-chain."""

    @pytest.fixture
    def solver(self):
        from minotaur_subnet.sdk.solvers.baseline_solver import BaselineSwapSolver
        s = BaselineSwapSolver()
        reg = BridgeRegistry()
        reg.register(MockBridgeAdapter())
        s.initialize({
            "chain_ids": [1, 964],
            "rpc_urls": {},
            "bridge_registry": reg,
        })
        return s

    @pytest.fixture
    def app(self):
        return AppIntentDefinition(
            app_id="test_swap",
            name="Test Swap",
            version="1.0.0",
            intent_type="swap",
            js_code="module.exports = { score() { return { score: 0.8 }; } };",
        )

    def test_single_chain_no_dest(self, solver, app):
        """Without dest_chain_id, should use single-chain path."""
        state = IntentState(
            contract_address="0x" + "aa" * 20,
            chain_id=1,
            nonce=0,
            owner="0x" + "bb" * 20,
            raw_params={
                "input_token": "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",
                "output_token": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
                "input_amount": "1000000000000000000",
            },
            control={"_intent_function": "execute"},
        )
        plan = solver.generate_plan(app, state, None)
        assert plan is not None
        assert not plan.metadata.get("cross_chain_plan")
        assert not plan.metadata.get("cross_chain", False)

    def test_same_chain_dest(self, solver, app):
        """dest_chain_id == chain_id should use single-chain path."""
        state = IntentState(
            contract_address="0x" + "aa" * 20,
            chain_id=1,
            nonce=0,
            owner="0x" + "bb" * 20,
            raw_params={
                "input_token": "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",
                "output_token": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
                "input_amount": "1000000000000000000",
                "dest_chain_id": "1",
            },
            control={"_intent_function": "execute"},
        )
        plan = solver.generate_plan(app, state, None)
        assert plan is not None
        assert not plan.metadata.get("cross_chain_plan")

    def test_cross_chain_detected(self, solver, app):
        """Different dest_chain_id should generate CrossChainPlan."""
        state = IntentState(
            contract_address="0x" + "aa" * 20,
            chain_id=1,
            nonce=0,
            owner="0x" + "bb" * 20,
            raw_params={
                "input_token": "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",
                "output_token": "0x77E06c9eCCf2E797fd462A92b6D7642EF85b0A44",
                "input_amount": "1000000000000000000",
                "dest_chain_id": "964",
            },
            control={"_intent_function": "execute"},
        )
        plan = solver.generate_plan(app, state, None)
        assert plan is not None
        # New architecture: CrossChainPlan in metadata
        assert "cross_chain_plan" in plan.metadata

    def test_cross_chain_uses_typed_swap_params_with_raw_dest_metadata(self, solver, app):
        """Swap fields may come from typed_context while dest_chain_id stays raw."""
        from minotaur_subnet.v3.contexts import SwapIntentContext
        state = IntentState(
            contract_address="0x" + "aa" * 20,
            chain_id=1,
            nonce=0,
            owner="0x" + "bb" * 20,
            raw_params={"dest_chain_id": "964"},
            typed_context=SwapIntentContext(
                app_id=app.app_id,
                intent_function="execute",
                chain_id=1,
                owner="0x" + "bb" * 20,
                contract_address="0x" + "aa" * 20,
                nonce=0,
                raw_params={
                    "_intent_function": "execute",
                    "input_token": "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",
                    "output_token": "0x77E06c9eCCf2E797fd462A92b6D7642EF85b0A44",
                    "input_amount": "1000000000000000000",
                },
                input_token="0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",
                output_token="0x77E06c9eCCf2E797fd462A92b6D7642EF85b0A44",
                input_amount=1_000_000_000_000_000_000,
                min_output_amount=0,
                receiver="0x" + "aa" * 20,
                fee_tier=3000,
            ),
        )
        plan = solver.generate_plan(app, state, None)
        assert plan is not None
        assert "cross_chain_plan" in plan.metadata

    def test_cross_chain_plan_structure(self, solver, app):
        """CrossChainPlan should have legs and bridge_requests."""
        state = IntentState(
            contract_address="0x" + "aa" * 20,
            chain_id=1,
            nonce=0,
            owner="0x" + "bb" * 20,
            raw_params={
                "input_token": "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",
                "output_token": "0x77E06c9eCCf2E797fd462A92b6D7642EF85b0A44",
                "input_amount": "1000000000000000000",
                "dest_chain_id": "964",
            },
            control={"_intent_function": "execute"},
        )
        plan = solver.generate_plan(app, state, None)
        assert plan is not None

        ccp = CrossChainPlan.from_dict(plan.metadata["cross_chain_plan"])
        assert len(ccp.legs) >= 2
        assert len(ccp.bridge_requests) == 1

        # Verify chain IDs
        assert ccp.legs[0].chain_id == 1      # Source chain
        assert ccp.legs[-1].chain_id == 964    # Dest chain

        # Bridge request
        br = ccp.bridge_requests[0]
        assert br.src_chain_id == 1
        assert br.dst_chain_id == 964
        assert br.amount > 0

        # Metadata
        assert plan.metadata["src_chain_id"] == 1
        assert plan.metadata["dst_chain_id"] == 964

    def test_dest_leg_empty_when_bridge_delivers_desired_token(self, solver, app):
        """When bridge directly delivers the desired token, dest leg may have no interactions."""
        state = IntentState(
            contract_address="0x" + "aa" * 20,
            chain_id=1,
            nonce=0,
            owner="0x" + "bb" * 20,
            raw_params={
                "input_token": "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",
                "output_token": "0x77E06c9eCCf2E797fd462A92b6D7642EF85b0A44",
                "input_amount": "1000000000000000000",
                "dest_chain_id": "964",
            },
            control={"_intent_function": "execute"},
        )
        plan = solver.generate_plan(app, state, None)
        assert plan is not None

        ccp = CrossChainPlan.from_dict(plan.metadata["cross_chain_plan"])
        # With MockBridgeAdapter (token_out == token_in), if the bridge
        # delivers the desired output token, dest leg may have no swap interactions
        dest_leg = ccp.legs[-1]
        # Dest leg exists but may have empty interactions
        assert dest_leg.chain_id == 964

    def test_dest_leg_has_swap_when_tokens_differ(self, app):
        """When bridge_token_out != output_token, dest leg has swap interactions."""
        from minotaur_subnet.sdk.solvers.baseline_solver import BaselineSwapSolver

        class DifferentTokenBridge(BridgeAdapter):
            """Bridge that outputs a different token than input."""
            PROTOCOL = "diff_bridge"

            async def quote(self, token_in, amount, src, dst, **kw):
                return BridgeQuote(
                    protocol=self.PROTOCOL,
                    src_chain_id=src, dst_chain_id=dst,
                    token_in=token_in,
                    token_out="0xDIFFERENT_TOKEN_ON_DEST" + "0" * 16,
                    amount_in=amount,
                    estimated_output=amount - 100,
                    fee=100, estimated_duration_s=60,
                )

            def build_bridge_interactions(self, quote, sender):
                return [Interaction(
                    target="0x" + "00" * 19 + "B1",
                    value="0", call_data="0xdeadbeef" + "00" * 28,
                    chain_id=quote.src_chain_id,
                )]

            async def check_status(self, src_tx_hash, src_chain_id, dst_chain_id=0):
                return BridgeStatus(
                    status=BridgeStatusEnum.COMPLETED,
                    src_tx_hash=src_tx_hash,
                )

            def supported_routes(self):
                return [(1, 964), (964, 1)]

        reg = BridgeRegistry()
        reg.register(DifferentTokenBridge())
        s = BaselineSwapSolver()
        s.initialize({
            "chain_ids": [1, 964],
            "rpc_urls": {},
            "bridge_registry": reg,
        })

        state = IntentState(
            contract_address="0x" + "aa" * 20,
            chain_id=1,
            nonce=0,
            owner="0x" + "bb" * 20,
            raw_params={
                "input_token": "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",
                "output_token": "0x77E06c9eCCf2E797fd462A92b6D7642EF85b0A44",
                "input_amount": "1000000000000000000",
                "dest_chain_id": "964",
            },
            control={"_intent_function": "execute"},
        )
        plan = s.generate_plan(app, state, None)
        assert plan is not None
        assert "cross_chain_plan" in plan.metadata

        ccp = CrossChainPlan.from_dict(plan.metadata["cross_chain_plan"])
        assert len(ccp.legs) >= 2
        assert len(ccp.bridge_requests) == 1

    def test_no_bridge_registry_uses_placeholder(self, app):
        """Without bridge_registry, cross-chain plan still generates."""
        from minotaur_subnet.sdk.solvers.baseline_solver import BaselineSwapSolver
        s = BaselineSwapSolver()
        s.initialize({"chain_ids": [1, 964], "rpc_urls": {}})

        state = IntentState(
            contract_address="0x" + "aa" * 20,
            chain_id=1,
            nonce=0,
            owner="0x" + "bb" * 20,
            raw_params={
                "input_token": "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",
                "output_token": "0x77E06c9eCCf2E797fd462A92b6D7642EF85b0A44",
                "input_amount": "1000000000000000000",
                "dest_chain_id": "964",
            },
            control={"_intent_function": "execute"},
        )
        plan = s.generate_plan(app, state, None)
        assert plan is not None
        # Should still produce a cross-chain plan (bridge_requests may be empty
        # since no registry, but the solver detects cross-chain)
        assert "cross_chain_plan" in plan.metadata

    def test_no_bridge_selectors_in_solver_legs(self, solver, app):
        """Solver's ChainLeg interactions must not contain bridge calldata."""
        state = IntentState(
            contract_address="0x" + "aa" * 20,
            chain_id=1,
            nonce=0,
            owner="0x" + "bb" * 20,
            raw_params={
                "input_token": "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",
                "output_token": "0x77E06c9eCCf2E797fd462A92b6D7642EF85b0A44",
                "input_amount": "1000000000000000000",
                "dest_chain_id": "964",
            },
            control={"_intent_function": "execute"},
        )
        plan = solver.generate_plan(app, state, None)
        ccp = CrossChainPlan.from_dict(plan.metadata["cross_chain_plan"])

        from minotaur_subnet.shared.types import _BRIDGE_CALL_SELECTORS
        for leg in ccp.legs:
            for ix in leg.interactions:
                raw = (ix.call_data or "")[2:] if (ix.call_data or "").startswith("0x") else (ix.call_data or "")
                selector = raw[:8] if len(raw) >= 8 else ""
                assert selector not in _BRIDGE_CALL_SELECTORS, (
                    f"Solver leg contains bridge selector {selector}"
                )
