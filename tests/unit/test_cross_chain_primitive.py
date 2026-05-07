"""Tests for the cross-chain platform primitive architecture.

Tests the full trust boundary: solver declares CrossChainPlan,
platform compiles it via CrossChainCompiler, validators verify
via bridge verifier. No bridge calldata in solver code.

Test levels:
  1. Type serialization (BridgeRequest, ChainLeg, CrossChainPlan)
  2. CrossChainCompiler validation (rejects bad plans, bridge selectors)
  3. CrossChainCompiler compilation (real quotes, real adapter calldata)
  4. mock_config on adapters (Hyperlane, Mock)
  5. mock_bridge_interactions_from_config (adapter-driven mocking)
  6. Bridge verifier (plan verification, escrow detection)
  7. Blockloop integration (compiler call in _process_order)
  8. Solver migration (CrossChainPlan output from baseline solver)

Mocking policy:
  - Real types, real bridge adapters (Hyperlane, Mock) — no mocking
  - Real BridgeRegistry — no mocking
  - Real CrossChainCompiler — no mocking
  - Mock: RPC calls (bridge IGP fee estimation), external APIs
  - Mock: Anvil simulator (unit tests don't need real forks)
  - Mock: Relayer (no real TX submission)
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import asdict
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytestmark = pytest.mark.cross_chain

from minotaur_subnet.shared.types import (
    BridgeRequest,
    ChainLeg,
    CrossChainPlan,
    ExecutionPlan,
    Interaction,
    LegPlan,
    MultiLegPlan,
    mock_bridge_interactions_from_config,
    _BRIDGE_CALL_SELECTORS,
    _MOCK_BRIDGE_TARGET,
)
from minotaur_subnet.bridge.base import BridgeQuote, BridgeStatusEnum
from minotaur_subnet.bridge.registry import BridgeRegistry
from minotaur_subnet.bridge.mock import MockBridgeAdapter
from minotaur_subnet.bridge.compiler import (
    CrossChainCompiler,
    CompiledCrossChainPlan,
    CrossChainCompileError,
)
from minotaur_subnet.bridge.verifier import (
    verify_platform_compiled,
    verify_escrow_on_chain,
)


# ── Helpers ──────────────────────────────────────────────────────────────────

USDC_BASE = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
USDC_BTEVM = "0xB833E8137FEDf80de7E908dc6fea43a029142F20"
WTAO = "0x9Dc08C6e2BF0F1eeD1E00670f80Df39145529F81"
USER = "0x" + "aa" * 20
CONTRACT = "0x" + "cc" * 20
TRANSFER_REMOTE_SELECTOR = "81b4e8b4"


def _make_bridge_request(**overrides) -> BridgeRequest:
    defaults = dict(
        token=USDC_BASE,
        amount=1_000_000,
        src_chain_id=8453,
        dst_chain_id=964,
        recipient=USER,
    )
    defaults.update(overrides)
    return BridgeRequest(**defaults)


def _make_chain_leg(chain_id: int, interactions=None, **kw) -> ChainLeg:
    return ChainLeg(
        chain_id=chain_id,
        interactions=interactions or [],
        **kw,
    )


def _make_interaction(selector: str = "a9059cbb", chain_id: int = 8453) -> Interaction:
    return Interaction(
        target="0x" + "11" * 20,
        value="0",
        call_data=f"0x{selector}" + "00" * 28,
        chain_id=chain_id,
    )


def _make_bridge_interaction() -> Interaction:
    """Create an interaction with a bridge protocol selector."""
    return _make_interaction(selector=TRANSFER_REMOTE_SELECTOR, chain_id=8453)


def _run(coro):
    """Run async in sync test context."""
    return asyncio.get_event_loop().run_until_complete(coro)


@pytest.fixture
def bridge_registry():
    """Real BridgeRegistry with MockBridgeAdapter."""
    reg = BridgeRegistry()
    reg.register(MockBridgeAdapter())
    return reg


@pytest.fixture
def compiler(bridge_registry):
    """Real CrossChainCompiler with MockBridgeAdapter."""
    return CrossChainCompiler(bridge_registry)


# ═══════════════════════════════════════════════════════════════════════════════
#  1. TYPE SERIALIZATION
# ═══════════════════════════════════════════════════════════════════════════════


class TestBridgeRequest:
    def test_construction(self):
        br = _make_bridge_request()
        assert br.token == USDC_BASE
        assert br.amount == 1_000_000
        assert br.src_chain_id == 8453
        assert br.dst_chain_id == 964

    def test_serialization_roundtrip(self):
        br = _make_bridge_request(min_output=900_000, purpose="test bridge")
        d = br.to_dict()
        br2 = BridgeRequest.from_dict(d)
        assert br2.token == br.token
        assert br2.amount == br.amount
        assert br2.min_output == 900_000
        assert br2.purpose == "test bridge"

    def test_defaults(self):
        br = _make_bridge_request()
        assert br.min_output == 0
        assert br.purpose == ""


class TestChainLeg:
    def test_construction_empty(self):
        leg = _make_chain_leg(8453)
        assert leg.chain_id == 8453
        assert leg.interactions == []

    def test_construction_with_interactions(self):
        ix = _make_interaction()
        leg = _make_chain_leg(8453, interactions=[ix])
        assert len(leg.interactions) == 1
        assert leg.interactions[0].target == ix.target

    def test_serialization_roundtrip(self):
        ix = _make_interaction()
        leg = _make_chain_leg(
            8453,
            interactions=[ix],
            intent_selector="abcd1234",
            metadata={"type": "source_swap"},
        )
        d = leg.to_dict()
        leg2 = ChainLeg.from_dict(d)
        assert leg2.chain_id == 8453
        assert len(leg2.interactions) == 1
        assert leg2.intent_selector == "abcd1234"
        assert leg2.metadata["type"] == "source_swap"


class TestCrossChainPlan:
    def test_two_legs_one_bridge(self):
        plan = CrossChainPlan(
            legs=[_make_chain_leg(8453), _make_chain_leg(964)],
            bridge_requests=[_make_bridge_request()],
        )
        assert plan.is_cross_chain
        assert len(plan.legs) == 2
        assert len(plan.bridge_requests) == 1

    def test_single_leg_no_bridge(self):
        plan = CrossChainPlan(
            legs=[_make_chain_leg(8453)],
            bridge_requests=[],
        )
        assert not plan.is_cross_chain

    def test_serialization_roundtrip(self):
        plan = CrossChainPlan(
            legs=[
                _make_chain_leg(8453, interactions=[_make_interaction()]),
                _make_chain_leg(964),
            ],
            bridge_requests=[_make_bridge_request()],
        )
        d = plan.to_dict()
        plan2 = CrossChainPlan.from_dict(d)
        assert len(plan2.legs) == 2
        assert len(plan2.bridge_requests) == 1
        assert plan2.legs[0].chain_id == 8453
        assert plan2.bridge_requests[0].amount == 1_000_000


# ═══════════════════════════════════════════════════════════════════════════════
#  2. COMPILER VALIDATION
# ═══════════════════════════════════════════════════════════════════════════════


class TestCompilerValidation:
    def test_valid_plan(self, compiler):
        plan = CrossChainPlan(
            legs=[_make_chain_leg(8453), _make_chain_leg(964)],
            bridge_requests=[_make_bridge_request()],
        )
        errors = compiler._validate(plan)
        assert errors == []

    def test_empty_legs(self, compiler):
        plan = CrossChainPlan(legs=[], bridge_requests=[])
        errors = compiler._validate(plan)
        assert any("No legs" in e for e in errors)

    def test_wrong_bridge_count(self, compiler):
        plan = CrossChainPlan(
            legs=[_make_chain_leg(8453), _make_chain_leg(964)],
            bridge_requests=[],  # Should be 1
        )
        errors = compiler._validate(plan)
        assert any("bridge_requests count" in e for e in errors)

    def test_chain_continuity_src_mismatch(self, compiler):
        plan = CrossChainPlan(
            legs=[_make_chain_leg(8453), _make_chain_leg(964)],
            bridge_requests=[_make_bridge_request(src_chain_id=1)],  # Mismatch
        )
        errors = compiler._validate(plan)
        assert any("src_chain" in e and "doesn't match" in e for e in errors)

    def test_chain_continuity_dst_mismatch(self, compiler):
        plan = CrossChainPlan(
            legs=[_make_chain_leg(8453), _make_chain_leg(964)],
            bridge_requests=[_make_bridge_request(dst_chain_id=1)],  # Mismatch
        )
        errors = compiler._validate(plan)
        assert any("dst_chain" in e and "doesn't match" in e for e in errors)

    def test_zero_amount(self, compiler):
        plan = CrossChainPlan(
            legs=[_make_chain_leg(8453), _make_chain_leg(964)],
            bridge_requests=[_make_bridge_request(amount=0)],
        )
        errors = compiler._validate(plan)
        assert any("amount must be positive" in e for e in errors)

    def test_same_chain_bridge(self, compiler):
        plan = CrossChainPlan(
            legs=[_make_chain_leg(8453), _make_chain_leg(8453)],
            bridge_requests=[_make_bridge_request(src_chain_id=8453, dst_chain_id=8453)],
        )
        errors = compiler._validate(plan)
        assert any("src == dst" in e for e in errors)

    def test_rejects_bridge_selectors_in_solver_interactions(self, compiler):
        """Solver MUST NOT include bridge protocol calldata in its interactions."""
        bridge_ix = _make_bridge_interaction()
        plan = CrossChainPlan(
            legs=[
                _make_chain_leg(8453, interactions=[bridge_ix]),
                _make_chain_leg(964),
            ],
            bridge_requests=[_make_bridge_request()],
        )
        errors = compiler._validate(plan)
        assert any("bridge selector" in e.lower() for e in errors)

    def test_allows_non_bridge_selectors(self, compiler):
        """Normal ERC-20 approve/transfer selectors should pass."""
        normal_ix = _make_interaction(selector="095ea7b3")  # approve
        plan = CrossChainPlan(
            legs=[
                _make_chain_leg(8453, interactions=[normal_ix]),
                _make_chain_leg(964),
            ],
            bridge_requests=[_make_bridge_request()],
        )
        errors = compiler._validate(plan)
        assert errors == []

    def test_three_legs_two_bridges(self, compiler):
        """Multi-hop: chain A → bridge → chain B → bridge → chain C."""
        plan = CrossChainPlan(
            legs=[
                _make_chain_leg(8453),
                _make_chain_leg(964),
                _make_chain_leg(1),
            ],
            bridge_requests=[
                _make_bridge_request(src_chain_id=8453, dst_chain_id=964),
                _make_bridge_request(src_chain_id=964, dst_chain_id=1),
            ],
        )
        errors = compiler._validate(plan)
        assert errors == []


# ═══════════════════════════════════════════════════════════════════════════════
#  3. COMPILER COMPILATION (Real BridgeRegistry + MockAdapter)
# ═══════════════════════════════════════════════════════════════════════════════


class TestCompilerCompilation:
    @pytest.mark.asyncio
    async def test_compiles_two_leg_plan(self, compiler):
        plan = CrossChainPlan(
            legs=[_make_chain_leg(8453), _make_chain_leg(964)],
            bridge_requests=[_make_bridge_request()],
        )
        result = await compiler.compile(
            plan, order_id="0x" + "01" * 32,
            user_address=USER, contract_address=CONTRACT,
            deadline=int(time.time()) + 3600,
        )
        assert isinstance(result, CompiledCrossChainPlan)
        assert isinstance(result.multi_leg_plan, MultiLegPlan)
        assert len(result.multi_leg_plan.forward_legs) >= 3  # solver_leg + bridge + solver_leg
        assert len(result.bridge_quotes) == 1
        assert len(result.escrow_params) == 1
        assert result.solver_plan is plan

    @pytest.mark.asyncio
    async def test_bridge_leg_has_interactions(self, compiler):
        plan = CrossChainPlan(
            legs=[_make_chain_leg(8453), _make_chain_leg(964)],
            bridge_requests=[_make_bridge_request()],
        )
        result = await compiler.compile(
            plan, order_id="0x" + "02" * 32,
            user_address=USER, contract_address=CONTRACT,
            deadline=int(time.time()) + 3600,
        )
        # Find the bridge leg
        bridge_legs = [
            l for l in result.multi_leg_plan.forward_legs
            if l.metadata.get("type") == "bridge"
        ]
        assert len(bridge_legs) == 1
        assert len(bridge_legs[0].interactions) > 0  # Real bridge calldata

    @pytest.mark.asyncio
    async def test_escrow_params_match_bridge_quote(self, compiler):
        plan = CrossChainPlan(
            legs=[_make_chain_leg(8453), _make_chain_leg(964)],
            bridge_requests=[_make_bridge_request(amount=5_000_000)],
        )
        result = await compiler.compile(
            plan, order_id="0x" + "03" * 32,
            user_address=USER, contract_address=CONTRACT,
            deadline=int(time.time()) + 3600,
        )
        ep = result.escrow_params[0]
        assert ep["user"] == USER
        assert ep["amount"] > 0  # Bridge output (after fees)
        assert ep["token"]  # Destination token address

    @pytest.mark.asyncio
    async def test_recipient_overridden_to_user(self, compiler):
        """Compiler overrides solver's recipient with user_address."""
        br = _make_bridge_request(recipient="0x" + "EE" * 20)  # Solver tries wrong recipient
        plan = CrossChainPlan(
            legs=[_make_chain_leg(8453), _make_chain_leg(964)],
            bridge_requests=[br],
        )
        result = await compiler.compile(
            plan, order_id="0x" + "04" * 32,
            user_address=USER, contract_address=CONTRACT,
            deadline=int(time.time()) + 3600,
        )
        bridge_legs = [
            l for l in result.multi_leg_plan.forward_legs
            if l.metadata.get("type") == "bridge"
        ]
        # Bridge recipient should be USER, not the solver's value
        assert bridge_legs[0].metadata["bridge_recipient"] == USER

    @pytest.mark.asyncio
    async def test_platform_compiled_flag_set(self, compiler):
        plan = CrossChainPlan(
            legs=[_make_chain_leg(8453), _make_chain_leg(964)],
            bridge_requests=[_make_bridge_request()],
        )
        result = await compiler.compile(
            plan, order_id="0x" + "05" * 32,
            user_address=USER, contract_address=CONTRACT,
            deadline=int(time.time()) + 3600,
        )
        for leg in result.multi_leg_plan.forward_legs:
            assert leg.metadata.get("_platform_compiled") is True

    @pytest.mark.asyncio
    async def test_simulation_mocks_for_bridge_legs(self, compiler):
        plan = CrossChainPlan(
            legs=[_make_chain_leg(8453), _make_chain_leg(964)],
            bridge_requests=[_make_bridge_request()],
        )
        result = await compiler.compile(
            plan, order_id="0x" + "06" * 32,
            user_address=USER, contract_address=CONTRACT,
            deadline=int(time.time()) + 3600,
        )
        assert len(result.simulation_mocks) > 0
        # Bridge leg index should have a mock config
        bridge_idx = next(
            l.leg_index for l in result.multi_leg_plan.forward_legs
            if l.metadata.get("type") == "bridge"
        )
        assert bridge_idx in result.simulation_mocks

    @pytest.mark.asyncio
    async def test_rollback_legs_generated(self, compiler):
        plan = CrossChainPlan(
            legs=[_make_chain_leg(8453), _make_chain_leg(964)],
            bridge_requests=[_make_bridge_request()],
        )
        result = await compiler.compile(
            plan, order_id="0x" + "07" * 32,
            user_address=USER, contract_address=CONTRACT,
            deadline=int(time.time()) + 3600,
        )
        # Rollback legs should be generated by compiler (not empty)
        assert len(result.multi_leg_plan.rollback_legs) > 0

    @pytest.mark.asyncio
    async def test_rejects_plan_with_bridge_selectors(self, compiler):
        """Compilation fails if solver includes bridge calldata."""
        plan = CrossChainPlan(
            legs=[
                _make_chain_leg(8453, interactions=[_make_bridge_interaction()]),
                _make_chain_leg(964),
            ],
            bridge_requests=[_make_bridge_request()],
        )
        with pytest.raises(CrossChainCompileError, match="bridge selector"):
            await compiler.compile(
                plan, order_id="0x" + "08" * 32,
                user_address=USER, contract_address=CONTRACT,
                deadline=int(time.time()) + 3600,
            )


# ═══════════════════════════════════════════════════════════════════════════════
#  4. ADAPTER mock_config
# ═══════════════════════════════════════════════════════════════════════════════


class TestAdapterMockConfig:
    def test_mock_adapter_noop(self):
        adapter = MockBridgeAdapter()
        quote = BridgeQuote(
            protocol="mock", src_chain_id=8453, dst_chain_id=964,
            token_in=USDC_BASE, token_out=USDC_BTEVM,
            amount_in=1_000_000, estimated_output=999_000,
            fee=1_000, estimated_duration_s=0,
        )
        cfg = adapter.mock_config(quote)
        assert cfg["mock_type"] == "noop"
        assert cfg["selectors"] == []

    def test_hyperlane_adapter_config(self):
        from minotaur_subnet.bridge.hyperlane import HyperlaneAdapter
        adapter = HyperlaneAdapter()
        quote = BridgeQuote(
            protocol="hyperlane", src_chain_id=8453, dst_chain_id=964,
            token_in=USDC_BASE, token_out=USDC_BTEVM,
            amount_in=1_000_000, estimated_output=1_000_000,
            fee=500_000_000_000_000, estimated_duration_s=120,
        )
        cfg = adapter.mock_config(quote)
        assert TRANSFER_REMOTE_SELECTOR in cfg["selectors"]
        assert cfg["mock_type"] == "erc20_transfer"
        assert cfg["mock_token"] == USDC_BASE
        assert cfg["mock_amount"] == 1_000_000


# ═══════════════════════════════════════════════════════════════════════════════
#  5. mock_bridge_interactions_from_config
# ═══════════════════════════════════════════════════════════════════════════════


class TestMockBridgeFromConfig:
    def test_replaces_bridge_selector(self):
        ix = _make_bridge_interaction()
        cfg = {
            "selectors": [TRANSFER_REMOTE_SELECTOR],
            "mock_type": "erc20_transfer",
            "mock_token": USDC_BASE,
            "mock_amount": 1_000_000,
        }
        result = mock_bridge_interactions_from_config([ix], cfg)
        assert len(result) == 1
        # Should be an ERC-20 transfer, not transferRemote
        assert result[0].call_data.startswith("0xa9059cbb")
        assert result[0].target == USDC_BASE

    def test_preserves_non_bridge_interactions(self):
        normal_ix = _make_interaction(selector="095ea7b3")  # approve
        bridge_ix = _make_bridge_interaction()
        cfg = {
            "selectors": [TRANSFER_REMOTE_SELECTOR],
            "mock_type": "erc20_transfer",
            "mock_token": USDC_BASE,
            "mock_amount": 500_000,
        }
        result = mock_bridge_interactions_from_config([normal_ix, bridge_ix], cfg)
        assert len(result) == 2
        # First interaction unchanged
        assert result[0].call_data == normal_ix.call_data
        # Second replaced
        assert result[1].call_data.startswith("0xa9059cbb")

    def test_empty_config_returns_copy(self):
        ix = _make_bridge_interaction()
        result = mock_bridge_interactions_from_config([ix], {})
        assert len(result) == 1
        assert result[0].call_data == ix.call_data  # Not replaced

    def test_noop_mock_type_preserves(self):
        ix = _make_bridge_interaction()
        cfg = {
            "selectors": [TRANSFER_REMOTE_SELECTOR],
            "mock_type": "noop",
        }
        result = mock_bridge_interactions_from_config([ix], cfg)
        assert result[0].call_data == ix.call_data  # Not replaced


# ═══════════════════════════════════════════════════════════════════════════════
#  6. BRIDGE VERIFIER
# ═══════════════════════════════════════════════════════════════════════════════


class TestBridgeVerifier:
    def test_non_cross_chain_passes(self):
        ok, reason = verify_platform_compiled({}, {})
        assert ok
        assert "not cross-chain" in reason

    def test_platform_compiled_passes(self):
        plan_data = {
            "metadata": {"_platform_compiled": True, "cross_chain": True},
            "interactions": [{"call_data": "0x095ea7b3" + "00" * 28}],
        }
        ok, reason = verify_platform_compiled(plan_data, {})
        assert ok

    def test_rejects_bridge_selectors_in_interactions(self):
        plan_data = {
            "metadata": {"_platform_compiled": True, "cross_chain": True},
            "interactions": [{"call_data": "0x" + TRANSFER_REMOTE_SELECTOR + "00" * 28}],
        }
        ok, reason = verify_platform_compiled(plan_data, {})
        assert not ok
        assert "bridge selector" in reason.lower()

    def test_legacy_cross_chain_warns(self):
        plan_data = {
            "metadata": {"cross_chain": True},  # No _platform_compiled
            "interactions": [],
        }
        ok, reason = verify_platform_compiled(plan_data, {})
        assert ok  # Allowed but with warning
        assert "legacy" in reason.lower()

    def test_escrow_on_chain_mock(self):
        """verify_escrow_on_chain gracefully handles missing RPC."""
        ok, reason = verify_escrow_on_chain(
            "0x" + "cc" * 20, 964, "0x" + "01" * 32, 1,
        )
        # Should fail gracefully (no RPC for chain 964 in test env)
        assert not ok
        assert "failed" in reason.lower() or "0" in reason


# ═══════════════════════════════════════════════════════════════════════════════
#  7. COMPILER → MULTI-LEG PLAN STRUCTURE
# ═══════════════════════════════════════════════════════════════════════════════


class TestCompiledPlanStructure:
    @pytest.mark.asyncio
    async def test_leg_ordering(self, compiler):
        """Forward legs should be: solver_leg_0, bridge_0, solver_leg_1."""
        swap_ix = _make_interaction(selector="095ea7b3")
        plan = CrossChainPlan(
            legs=[
                _make_chain_leg(8453, interactions=[swap_ix]),
                _make_chain_leg(964, interactions=[swap_ix]),
            ],
            bridge_requests=[_make_bridge_request()],
        )
        result = await compiler.compile(
            plan, order_id="0x" + "10" * 32,
            user_address=USER, contract_address=CONTRACT,
            deadline=int(time.time()) + 3600,
        )
        legs = result.multi_leg_plan.forward_legs
        assert legs[0].metadata["type"] == "solver_leg"
        assert legs[0].chain_id == 8453
        assert legs[1].metadata["type"] == "bridge"
        assert legs[1].chain_id == 8453  # Bridge executes on source
        assert legs[2].metadata["type"] == "solver_leg"
        assert legs[2].chain_id == 964

    @pytest.mark.asyncio
    async def test_dependencies_set(self, compiler):
        plan = CrossChainPlan(
            legs=[_make_chain_leg(8453), _make_chain_leg(964)],
            bridge_requests=[_make_bridge_request()],
        )
        result = await compiler.compile(
            plan, order_id="0x" + "11" * 32,
            user_address=USER, contract_address=CONTRACT,
            deadline=int(time.time()) + 3600,
        )
        legs = result.multi_leg_plan.forward_legs
        # First leg has no dependencies
        assert legs[0].depends_on == []
        # Bridge depends on first leg
        assert legs[1].depends_on == [0]
        # Dest leg depends on bridge
        assert legs[2].depends_on == [1]

    @pytest.mark.asyncio
    async def test_leg_indices_sequential(self, compiler):
        plan = CrossChainPlan(
            legs=[_make_chain_leg(8453), _make_chain_leg(964)],
            bridge_requests=[_make_bridge_request()],
        )
        result = await compiler.compile(
            plan, order_id="0x" + "12" * 32,
            user_address=USER, contract_address=CONTRACT,
            deadline=int(time.time()) + 3600,
        )
        indices = [l.leg_index for l in result.multi_leg_plan.forward_legs]
        assert indices == list(range(len(indices)))

    @pytest.mark.asyncio
    async def test_compiled_plan_serializable(self, compiler):
        """CompiledCrossChainPlan.to_dict() should work without errors."""
        plan = CrossChainPlan(
            legs=[_make_chain_leg(8453), _make_chain_leg(964)],
            bridge_requests=[_make_bridge_request()],
        )
        result = await compiler.compile(
            plan, order_id="0x" + "13" * 32,
            user_address=USER, contract_address=CONTRACT,
            deadline=int(time.time()) + 3600,
        )
        d = result.to_dict()
        assert "multi_leg_plan" in d
        assert "escrow_params" in d
        assert "simulation_mocks" in d

    @pytest.mark.asyncio
    async def test_escrow_deadline_from_order(self, compiler):
        deadline = int(time.time()) + 7200
        plan = CrossChainPlan(
            legs=[_make_chain_leg(8453), _make_chain_leg(964)],
            bridge_requests=[_make_bridge_request()],
        )
        result = await compiler.compile(
            plan, order_id="0x" + "14" * 32,
            user_address=USER, contract_address=CONTRACT,
            deadline=deadline,
        )
        assert result.escrow_params[0]["deadline"] == deadline


# ═══════════════════════════════════════════════════════════════════════════════
#  8. SOLVER CrossChainPlan OUTPUT
# ═══════════════════════════════════════════════════════════════════════════════


class TestSolverCrossChainPlan:
    """Test that BaselineSwapSolver emits CrossChainPlan in metadata."""

    def _make_solver(self, bridge_registry=None):
        # BaselineSwapSolver lives in the external solver repo
        # (subnet112/minotaur-solver); skip if not importable here.
        baseline_mod = pytest.importorskip("minotaur_subnet.sdk.solvers.baseline_solver")
        solver = baseline_mod.BaselineSwapSolver()
        solver._bridge_registry = bridge_registry
        return solver

    def _make_state(self, chain_id=8453, input_token=USDC_BASE,
                    output_token=WTAO, input_amount="1000000"):
        from minotaur_subnet.shared.types import IntentState
        return IntentState(
            contract_address=CONTRACT,
            chain_id=chain_id,
            nonce=0,
            owner=USER,
            raw_params={
                "input_token": input_token,
                "output_token": output_token,
                "input_amount": input_amount,
                "dest_chain_id": "964",
                "input_chain_id": 8453,
                "output_chain_id": 964,
            },
        )

    def _make_intent(self):
        from minotaur_subnet.shared.types import AppIntentDefinition, AppIntentConfig
        return AppIntentDefinition(
            app_id="test_app",
            name="DexAggregatorApp",
            version="1.0.0",
            intent_type="",
            js_code="",
            config=AppIntentConfig(),
        )

    def test_cross_chain_plan_in_metadata(self):
        """Solver should emit cross_chain_plan, not multi_leg_plan."""
        reg = BridgeRegistry()
        reg.register(MockBridgeAdapter())
        solver = self._make_solver(bridge_registry=reg)

        plan = solver._generate_cross_chain_plan(
            self._make_intent(),
            self._make_state(),
            None, 8453, 964,
        )

        # New architecture: cross_chain_plan in metadata
        assert "cross_chain_plan" in plan.metadata
        # Should NOT have multi_leg_plan (that's compiled by platform)
        assert "multi_leg_plan" not in plan.metadata

    def test_cross_chain_plan_structure(self):
        reg = BridgeRegistry()
        reg.register(MockBridgeAdapter())
        solver = self._make_solver(bridge_registry=reg)

        plan = solver._generate_cross_chain_plan(
            self._make_intent(),
            self._make_state(),
            None, 8453, 964,
        )

        ccp = CrossChainPlan.from_dict(plan.metadata["cross_chain_plan"])
        assert len(ccp.legs) >= 2
        assert len(ccp.bridge_requests) == 1
        assert ccp.bridge_requests[0].src_chain_id == 8453
        assert ccp.bridge_requests[0].dst_chain_id == 964

    def test_no_bridge_selectors_in_solver_legs(self):
        """Solver's ChainLeg interactions must not contain bridge calldata."""
        reg = BridgeRegistry()
        reg.register(MockBridgeAdapter())
        solver = self._make_solver(bridge_registry=reg)

        plan = solver._generate_cross_chain_plan(
            self._make_intent(),
            self._make_state(),
            None, 8453, 964,
        )

        ccp = CrossChainPlan.from_dict(plan.metadata["cross_chain_plan"])
        for leg in ccp.legs:
            for ix in leg.interactions:
                raw = (ix.call_data or "")[2:] if (ix.call_data or "").startswith("0x") else (ix.call_data or "")
                selector = raw[:8] if len(raw) >= 8 else ""
                assert selector not in _BRIDGE_CALL_SELECTORS, (
                    f"Solver leg contains bridge selector {selector}"
                )
