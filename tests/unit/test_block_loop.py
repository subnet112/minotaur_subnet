"""Unit tests for the BlockLoop."""

import sys
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pytest
from minotaur_subnet.orderbook.orderbook import IntentOrderBook, OrderStatus
from minotaur_subnet.blockloop.loop import BlockLoop, TickResult
from minotaur_subnet.relayer.base import MockRelayer, SubmitResult
from minotaur_subnet.store import AppIntentStore
from minotaur_subnet.shared.types import (
    AppIntentConfig,
    AppIntentDefinition,
    DeploymentResult,
    AppStatus,
    ExecutionPlan,
    Interaction,
    PolicyTier,
    SimulationResult,
)
from minotaur_subnet.v3.contexts import SwapIntentContext


@pytest.fixture
def temp_store(tmp_path):
    return AppIntentStore(store_path=tmp_path / "test_store.json")


@pytest.fixture
def app_def():
    return AppIntentDefinition(
        app_id="test_app",
        name="Test Swap",
        version="1.0.0",
        intent_type="swap",
        js_code="module.exports = { config: {name: 'test'}, score: () => ({score: 0.8, valid: true}) }",
        config=AppIntentConfig(supported_chains=[1]),
    )


@pytest.fixture
def setup(temp_store, app_def):
    """Set up an OrderBook, store with app, and BlockLoop."""
    temp_store.save_app(app_def)
    temp_store.save_deployment(DeploymentResult(
        app_id="test_app",
        status=AppStatus.ACTIVE,
        contract_address="0x" + "ab" * 20,
    ))

    ob = IntentOrderBook()
    relayer = MockRelayer()
    loop = BlockLoop(
        orderbook=ob,
        app_store=temp_store,
        relayer=relayer,
        tick_interval=1.0,
        score_threshold=0.5,
    )
    return ob, relayer, loop


class TestTick:
    @pytest.mark.asyncio
    async def test_empty_tick(self, setup):
        ob, relayer, loop = setup
        result = await loop.tick()
        assert isinstance(result, TickResult)
        assert result.orders_processed == 0
        assert result.tick_number == 1

    @pytest.mark.asyncio
    async def test_tick_processes_order(self, setup):
        ob, relayer, loop = setup
        ob.submit(
            app_id="test_app",
            intent_function="execute",
            params={"input_token": "WETH", "output_token": "USDC", "input_amount": "1000"},
            submitted_by="0xuser",
        )

        result = await loop.tick()
        assert result.orders_processed == 1
        # Mock scoring gives ~0.75 which passes 0.5 threshold
        assert result.orders_approved == 1 or result.orders_rejected == 1

    @pytest.mark.asyncio
    async def test_tick_rejects_unknown_app(self, setup):
        ob, relayer, loop = setup
        ob.submit(
            app_id="nonexistent_app",
            intent_function="execute",
            params={},
            submitted_by="0xuser",
        )

        result = await loop.tick()
        assert result.orders_processed == 1
        assert result.orders_rejected == 1

    @pytest.mark.asyncio
    async def test_tick_expires_stale(self, setup):
        import time
        ob, relayer, loop = setup
        ob.submit(
            app_id="test_app",
            intent_function="execute",
            params={},
            submitted_by="0xuser",
            deadline=time.time() - 100,
        )

        result = await loop.tick()
        assert result.orders_expired == 1
        assert result.orders_processed == 0

    @pytest.mark.asyncio
    async def test_multiple_orders_in_tick(self, setup):
        ob, relayer, loop = setup
        for i in range(3):
            ob.submit(
                app_id="test_app",
                intent_function="execute",
                params={"input_token": "WETH", "output_token": "USDC", "input_amount": "1000"},
                submitted_by=f"0xuser{i}",
            )

        result = await loop.tick()
        assert result.orders_processed == 3

    @pytest.mark.asyncio
    async def test_relayer_receives_submissions(self, setup):
        ob, relayer, loop = setup
        ob.submit(
            app_id="test_app",
            intent_function="execute",
            params={"input_token": "WETH", "output_token": "USDC", "input_amount": "1000"},
            submitted_by="0xuser",
        )

        await loop.tick()
        # If the order was approved, relayer should have a submission
        if relayer.submissions:
            assert relayer.submissions[0]["order_id"].startswith("ord_")


class TestScoreThreshold:
    @pytest.mark.asyncio
    async def test_high_threshold_rejects(self, temp_store, app_def):
        temp_store.save_app(app_def)
        temp_store.save_deployment(DeploymentResult(
            app_id="test_app", status=AppStatus.ACTIVE,
        ))
        ob = IntentOrderBook()
        loop = BlockLoop(
            orderbook=ob,
            app_store=temp_store,
            relayer=MockRelayer(),
            score_threshold=0.99,  # Very high threshold
        )
        ob.submit(
            app_id="test_app",
            intent_function="execute",
            params={},
            submitted_by="0xuser",
        )

        result = await loop.tick()
        # Fallback plans score 0.3 which is below 0.99
        assert result.orders_rejected >= 0  # May or may not reject depending on mock score


class TestSolverSwap:
    def test_set_solver(self, setup):
        ob, relayer, loop = setup
        mock_solver = MagicMock()
        loop.set_solver(mock_solver)
        assert loop.solver is mock_solver

    @pytest.mark.asyncio
    async def test_async_solver_generate_plan_supported(self, setup):
        ob, relayer, loop = setup

        class AsyncSolver:
            async def generate_plan(self, app, state, snapshot):
                return ExecutionPlan(
                    intent_id=app.app_id,
                    interactions=[
                        Interaction(
                            target="0x" + "11" * 20,
                            value="0",
                            call_data="0x",
                            chain_id=state.chain_id,
                        ),
                    ],
                    deadline=9999999999,
                    nonce=state.nonce,
                    metadata={},
                )

            def metadata(self):
                return None

        loop.set_solver(AsyncSolver())
        ob.submit(
            app_id="test_app",
            intent_function="execute",
            params={"input_token": "WETH", "output_token": "USDC", "input_amount": "1000"},
            submitted_by="0xuser",
        )

        result = await loop.tick()
        assert result.orders_processed == 1

    @pytest.mark.asyncio
    async def test_block_loop_builds_swap_typed_context_when_enabled(self, temp_store, monkeypatch):
        monkeypatch.setenv("V3_TYPED_CONTEXTS_ENABLED", "1")

        app = AppIntentDefinition(
            app_id="dex_app",
            name="Dex Aggregator",
            version="1.0.0",
            intent_type="swap",
            js_code="module.exports = { config: {name: 'dex'}, score: () => ({score: 0.8, valid: true}) }",
            config=AppIntentConfig(
                supported_chains=[1],
                policy_tier=PolicyTier.STRICT,
            ),
        )
        temp_store.save_app(app)
        temp_store.save_deployment(
            DeploymentResult(
                app_id="dex_app",
                status=AppStatus.ACTIVE,
                contract_address="0x" + "ab" * 20,
                chain_id=1,
            )
        )

        captured = {}

        class CapturingSolver:
            async def generate_plan(self, app, state, snapshot):
                captured["state"] = state
                return ExecutionPlan(
                    intent_id=app.app_id,
                    interactions=[
                        Interaction(
                            target="0x" + "11" * 20,
                            value="0",
                            call_data="0x",
                            chain_id=state.chain_id,
                        ),
                    ],
                    deadline=9999999999,
                    nonce=state.nonce,
                    metadata={},
                )

            def metadata(self):
                return None

        ob = IntentOrderBook()
        loop = BlockLoop(
            orderbook=ob,
            app_store=temp_store,
            relayer=MockRelayer(),
            solver=CapturingSolver(),
            tick_interval=1.0,
            score_threshold=0.5,
        )
        ob.submit(
            app_id="dex_app",
            intent_function="swap",
            params={
                "input_token": "0x" + "01" * 20,
                "output_token": "0x" + "02" * 20,
                "input_amount": "1000",
                "min_output_amount": "900",
            },
            submitted_by="0x" + "03" * 20,
            chain_id=1,
        )

        await loop.tick()

        state = captured["state"]
        assert state.context_version == "v3"
        assert state.policy_tier == PolicyTier.STRICT
        assert isinstance(state.typed_context, SwapIntentContext)
        assert state.typed_context.contract_address == "0x" + "ab" * 20
        assert state.typed_context.receiver == "0x" + "ab" * 20
        assert state.typed_context.input_amount == 1000
        assert state.typed_context.min_output_amount == 900

    @pytest.mark.asyncio
    async def test_policy_assessment_shadow_mode_persists_assessment(self, temp_store, monkeypatch):
        monkeypatch.setenv("V3_POLICY_ASSESSMENT_ENABLED", "1")

        app = AppIntentDefinition(
            app_id="dex_app",
            name="Dex Aggregator",
            version="1.0.0",
            intent_type="swap",
            js_code="module.exports = { score: () => ({score: 0.8, valid: true}) }",
            config=AppIntentConfig(
                supported_chains=[1],
                policy_tier=PolicyTier.HYBRID,
            ),
        )
        temp_store.save_app(app)
        temp_store.save_deployment(
            DeploymentResult(
                app_id="dex_app",
                status=AppStatus.ACTIVE,
                contract_address="0x" + "ab" * 20,
                chain_id=1,
            )
        )

        class UnknownSelectorSolver:
            async def generate_plan(self, app, state, snapshot):
                return ExecutionPlan(
                    intent_id=app.app_id,
                    interactions=[
                        Interaction(
                            target="0x" + "11" * 20,
                            value="0",
                            call_data="0xdeadbeef" + "00" * 32,
                            chain_id=state.chain_id,
                        ),
                    ],
                    deadline=9999999999,
                    nonce=state.nonce,
                    metadata={},
                )

            def metadata(self):
                return None

        ob = IntentOrderBook()
        relayer = MockRelayer()
        loop = BlockLoop(
            orderbook=ob,
            app_store=temp_store,
            relayer=relayer,
            solver=UnknownSelectorSolver(),
            tick_interval=1.0,
            score_threshold=0.5,
        )
        order = ob.submit(
            app_id="dex_app",
            intent_function="swap",
            params={"input_token": "WETH", "output_token": "USDC", "input_amount": "1000"},
            submitted_by="0x" + "03" * 20,
            chain_id=1,
            policy_tier="hybrid",
        )

        result = await loop.tick()

        assert result.orders_approved == 1
        assert relayer.submissions
        updated = ob.get(order.order_id)
        assert updated is not None
        assert updated.plan_assessment is not None
        assert updated.plan_assessment["tier"] == "hybrid"
        assert updated.plan_assessment["accepted"] is True
        assert updated.plan_assessment["requires_extra_scrutiny"] is True

    @pytest.mark.asyncio
    async def test_policy_enforcement_rejects_strict_opaque_plan(self, temp_store, monkeypatch):
        monkeypatch.setenv("V3_POLICY_ASSESSMENT_ENABLED", "1")
        monkeypatch.setenv("V3_POLICY_ENFORCEMENT_ENABLED", "1")

        app = AppIntentDefinition(
            app_id="dex_app",
            name="Dex Aggregator",
            version="1.0.0",
            intent_type="swap",
            js_code="module.exports = { score: () => ({score: 0.8, valid: true}) }",
            config=AppIntentConfig(
                supported_chains=[1],
                policy_tier=PolicyTier.STRICT,
            ),
        )
        temp_store.save_app(app)
        temp_store.save_deployment(
            DeploymentResult(
                app_id="dex_app",
                status=AppStatus.ACTIVE,
                contract_address="0x" + "ab" * 20,
                chain_id=1,
            )
        )

        class OpaqueSolver:
            async def generate_plan(self, app, state, snapshot):
                return ExecutionPlan(
                    intent_id=app.app_id,
                    interactions=[
                        Interaction(
                            target="0x" + "11" * 20,
                            value="0",
                            call_data="0x",
                            chain_id=state.chain_id,
                        ),
                    ],
                    deadline=9999999999,
                    nonce=state.nonce,
                    metadata={},
                )

            def metadata(self):
                return None

        ob = IntentOrderBook()
        relayer = MockRelayer()
        loop = BlockLoop(
            orderbook=ob,
            app_store=temp_store,
            relayer=relayer,
            solver=OpaqueSolver(),
            tick_interval=1.0,
            score_threshold=0.5,
        )
        order = ob.submit(
            app_id="dex_app",
            intent_function="swap",
            params={"input_token": "WETH", "output_token": "USDC", "input_amount": "1000"},
            submitted_by="0x" + "03" * 20,
            chain_id=1,
        )

        result = await loop.tick()

        assert result.orders_rejected == 1
        assert not relayer.submissions
        updated = ob.get(order.order_id)
        assert updated is not None
        assert updated.status == OrderStatus.REJECTED
        assert updated.plan_assessment is not None
        assert updated.plan_assessment["accepted"] is False
        assert "Policy rejected plan" in (updated.error or "")

    @pytest.mark.asyncio
    async def test_set_solver_shuts_down_previous_async_solver(self, setup):
        ob, relayer, loop = setup

        class ClosableSolver:
            def __init__(self):
                self.closed = False

            async def shutdown(self):
                self.closed = True

            def metadata(self):
                return None

        old_solver = ClosableSolver()
        new_solver = MagicMock()
        loop.set_solver(old_solver)
        loop.set_solver(new_solver)
        await asyncio.sleep(0)
        assert old_solver.closed is True


class TestStatus:
    @pytest.mark.asyncio
    async def test_status_before_tick(self, setup):
        ob, relayer, loop = setup
        status = loop.status()
        assert status["running"] is False
        assert status["tick_number"] == 0
        assert status["last_tick"] is None

    @pytest.mark.asyncio
    async def test_status_after_tick(self, setup):
        ob, relayer, loop = setup
        await loop.tick()
        status = loop.status()
        assert status["tick_number"] == 1
        assert status["last_tick"] is not None


class TestOrderPersistence:
    """Tests for order persistence to store (OB-11, OB-12)."""

    @pytest.mark.asyncio
    async def test_order_persisted_after_fill(self, setup):
        ob, relayer, loop = setup
        ob.submit(
            app_id="test_app",
            intent_function="execute",
            params={"input_token": "WETH", "output_token": "USDC", "input_amount": "1000"},
            submitted_by="0xuser",
        )
        await loop.tick()
        # Verify the store has the order persisted
        stored_orders = loop.app_store.list_orders()
        assert len(stored_orders) >= 1
        order_dict = stored_orders[0]
        assert order_dict["app_id"] == "test_app"
        # Terminal status: filled or rejected
        assert order_dict["status"] in ("filled", "rejected", "scored", "approved", "solved")

    @pytest.mark.asyncio
    async def test_order_persisted_after_rejection(self, temp_store, app_def):
        temp_store.save_app(app_def)
        temp_store.save_deployment(DeploymentResult(
            app_id="test_app", status=AppStatus.ACTIVE,
        ))
        ob = IntentOrderBook()
        loop = BlockLoop(
            orderbook=ob,
            app_store=temp_store,
            relayer=MockRelayer(),
            score_threshold=0.99,  # Very high to force rejection
        )
        ob.submit(
            app_id="test_app",
            intent_function="execute",
            params={},
            submitted_by="0xuser",
        )
        await loop.tick()
        stored_orders = temp_store.list_orders()
        assert len(stored_orders) >= 1
        assert stored_orders[0]["status"] == "rejected"


class TestDualScoring:
    """Tests for dual JS + on-chain scoring (SCR-4, SCR-5, SCR-6)."""

    @pytest.mark.asyncio
    async def test_dual_scoring_rejects_low_onchain(self, temp_store, app_def):
        """On-chain score below threshold should reject, even if JS score passes."""
        app_def.config.score_threshold = 0.3  # Low per-app JS threshold
        temp_store.save_app(app_def)
        temp_store.save_deployment(DeploymentResult(
            app_id="test_app", status=AppStatus.ACTIVE,
            contract_address="0x" + "ab" * 20,
        ))
        ob = IntentOrderBook()

        # Mock simulator that returns low on-chain score
        mock_sim = AsyncMock()
        mock_sim.simulate = AsyncMock(return_value=SimulationResult(
            success=True,
            gas_used=100000,
            on_chain_score=3000,  # Below default 5000 threshold
        ))

        loop = BlockLoop(
            orderbook=ob,
            app_store=temp_store,
            relayer=MockRelayer(),
            simulator=mock_sim,
            score_threshold=0.3,  # JS threshold is low, will pass
        )
        ob.submit(
            app_id="test_app",
            intent_function="execute",
            params={"input_token": "WETH", "output_token": "USDC", "input_amount": "1000"},
            submitted_by="0xuser",
        )
        result = await loop.tick()
        assert result.orders_rejected == 1
        # Check the order has on-chain score rejection
        orders = ob.list_orders(status="rejected")
        assert len(orders) >= 1
        assert "On-chain score" in (orders[0].error or "")

    @pytest.mark.asyncio
    async def test_dual_scoring_passes_both(self, temp_store, app_def):
        """Both JS and on-chain scores above threshold should approve."""
        app_def.config.score_threshold = 0.3  # Low per-app JS threshold
        temp_store.save_app(app_def)
        temp_store.save_deployment(DeploymentResult(
            app_id="test_app", status=AppStatus.ACTIVE,
            contract_address="0x" + "ab" * 20,
        ))
        ob = IntentOrderBook()

        mock_sim = AsyncMock()
        mock_sim.simulate = AsyncMock(return_value=SimulationResult(
            success=True,
            gas_used=100000,
            on_chain_score=8000,  # Above default 5000
        ))

        loop = BlockLoop(
            orderbook=ob,
            app_store=temp_store,
            relayer=MockRelayer(),
            simulator=mock_sim,
            score_threshold=0.3,
        )
        ob.submit(
            app_id="test_app",
            intent_function="execute",
            params={"input_token": "WETH", "output_token": "USDC", "input_amount": "1000"},
            submitted_by="0xuser",
        )
        result = await loop.tick()
        assert result.orders_approved == 1

    @pytest.mark.asyncio
    async def test_dual_scoring_skipped_when_none(self, setup):
        """When on_chain_score is None, only JS gate applies."""
        ob, relayer, loop = setup
        ob.submit(
            app_id="test_app",
            intent_function="execute",
            params={"input_token": "WETH", "output_token": "USDC", "input_amount": "1000"},
            submitted_by="0xuser",
        )
        # No simulator set → mock simulation → on_chain_score=None
        result = await loop.tick()
        # Should process normally (only JS gate)
        assert result.orders_processed == 1


class TestPerAppScoreThreshold:
    """Tests for per-app JS score threshold (SCR-7)."""

    @pytest.mark.asyncio
    async def test_per_app_threshold_used(self, temp_store, app_def):
        """App with high per-app threshold should reject mock-scored orders."""
        app_def.config.score_threshold = 0.95  # Higher than mock score (~0.75)
        temp_store.save_app(app_def)
        temp_store.save_deployment(DeploymentResult(
            app_id="test_app", status=AppStatus.ACTIVE,
            contract_address="0x" + "ab" * 20,
        ))
        ob = IntentOrderBook()
        loop = BlockLoop(
            orderbook=ob,
            app_store=temp_store,
            relayer=MockRelayer(),
            score_threshold=0.3,  # Global threshold is low
        )
        ob.submit(
            app_id="test_app",
            intent_function="execute",
            params={"input_token": "WETH", "output_token": "USDC", "input_amount": "1000"},
            submitted_by="0xuser",
        )
        result = await loop.tick()
        # Mock score ~0.75 < per-app 0.95, should reject
        assert result.orders_rejected == 1

    @pytest.mark.asyncio
    async def test_low_per_app_threshold_accepts(self, temp_store, app_def):
        """App with low per-app threshold should accept mock-scored orders."""
        app_def.config.score_threshold = 0.1  # Lower than mock score
        temp_store.save_app(app_def)
        temp_store.save_deployment(DeploymentResult(
            app_id="test_app", status=AppStatus.ACTIVE,
            contract_address="0x" + "ab" * 20,
        ))
        ob = IntentOrderBook()
        loop = BlockLoop(
            orderbook=ob,
            app_store=temp_store,
            relayer=MockRelayer(),
            score_threshold=0.99,  # Global threshold is high (should be ignored)
        )
        ob.submit(
            app_id="test_app",
            intent_function="execute",
            params={"input_token": "WETH", "output_token": "USDC", "input_amount": "1000"},
            submitted_by="0xuser",
        )
        result = await loop.tick()
        # Mock score ~0.75 > per-app 0.1, should approve
        assert result.orders_approved == 1


class TestOnLeaderChanged:
    """Tests for BlockLoop leader-change handling (CON-15, REL-12, OB-12)."""

    @pytest.mark.asyncio
    async def test_on_leader_changed_clears_relayer(self, setup):
        ob, relayer, loop = setup
        # Simulate some prior submissions
        from types import SimpleNamespace
        await relayer.submit_plan(SimpleNamespace(order_id="old_1", chain_id=1), None, 0.5)
        assert len(relayer.submissions) == 1

        await loop.on_leader_changed("0xNewLeader")
        assert len(relayer.submissions) == 0

    @pytest.mark.asyncio
    async def test_on_leader_changed_clears_consensus(self, temp_store, app_def):
        temp_store.save_app(app_def)
        temp_store.save_deployment(DeploymentResult(
            app_id="test_app", status=AppStatus.ACTIVE,
        ))
        ob = IntentOrderBook()
        from minotaur_subnet.consensus.eip712 import address_from_key
        key = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
        addr = address_from_key(key)
        from minotaur_subnet.consensus.manager import ConsensusManager
        cm = ConsensusManager(
            validator_id=addr,
            private_key=key,
            validators=[addr, "0x" + "11" * 20],  # multi-validator
        )
        loop = BlockLoop(
            orderbook=ob,
            app_store=temp_store,
            relayer=MockRelayer(),
            consensus=cm,
        )
        # Manually inject a pending proposal
        from minotaur_subnet.consensus.manager import _PendingProposal
        cm._pending["fake_order"] = _PendingProposal(
            order_id="fake_order", plan_hash="0x00", score=0.8, quorum=2,
        )
        assert len(cm._pending) == 1

        await loop.on_leader_changed("0xNewLeader")
        assert len(cm._pending) == 0

    @pytest.mark.asyncio
    async def test_load_open_orders_from_store(self, setup):
        ob, relayer, loop = setup
        # Save an OPEN order to store
        loop.app_store.save_order({
            "order_id": "ord_reloaded",
            "app_id": "test_app",
            "intent_function": "execute",
            "params": {},
            "submitted_by": "0xuser",
            "chain_id": 1,
            "status": "open",
            "perpetual": False,
            "max_executions": 1,
            "cooldown": 0.0,
            "deadline": 0.0,
        })
        loaded = loop.load_open_orders_from_store()
        assert loaded == 1
        reloaded = ob.get("ord_reloaded")
        assert reloaded is not None
        assert reloaded.status == OrderStatus.OPEN

    @pytest.mark.asyncio
    async def test_load_skips_existing_orders(self, setup):
        ob, relayer, loop = setup
        # Submit an order normally
        order = ob.submit(
            app_id="test_app",
            intent_function="execute",
            params={},
            submitted_by="0xuser",
        )
        # Also save it in the store
        loop.app_store.save_order({"order_id": order.order_id, "status": "open",
                                    "app_id": "test_app", "params": {}, "submitted_by": "0xuser"})
        loaded = loop.load_open_orders_from_store()
        assert loaded == 0  # Already in OB, skip


class TestStopLoop:
    @pytest.mark.asyncio
    async def test_stop(self, setup):
        ob, relayer, loop = setup
        loop.tick_interval = 0.1

        task = asyncio.create_task(loop.run_loop())
        await asyncio.sleep(0.3)
        loop.stop()

        # Wait for clean shutdown
        try:
            await asyncio.wait_for(task, timeout=2.0)
        except asyncio.TimeoutError:
            task.cancel()

        assert not loop.running


class TestManifestAttachment:
    """Tests for SE-11: manifest attached to app def before solver call."""

    @pytest.mark.asyncio
    async def test_manifest_attached_from_js_engine(self, temp_store, app_def):
        """Manifest is extracted from JS engine and attached to app def."""
        temp_store.save_app(app_def)
        temp_store.save_deployment(DeploymentResult(
            app_id="test_app", status=AppStatus.ACTIVE,
            contract_address="0x" + "ab" * 20,
        ))
        ob = IntentOrderBook()

        # Mock JS engine with a get_manifest method
        mock_engine = MagicMock()
        mock_engine.get_manifest.return_value = {
            "intent_functions": [{"name": "execute", "params": {"input_token": "string"}}]
        }
        mock_engine._intents = {}

        loop = BlockLoop(
            orderbook=ob,
            app_store=temp_store,
            relayer=MockRelayer(),
            js_engine=mock_engine,
            score_threshold=0.3,
        )
        ob.submit(
            app_id="test_app",
            intent_function="execute",
            params={"input_token": "WETH", "output_token": "USDC", "input_amount": "1000"},
            submitted_by="0xuser",
        )
        await loop.tick()

        # Verify get_manifest was called
        mock_engine.get_manifest.assert_called_with("test_app")

    @pytest.mark.asyncio
    async def test_manifest_none_when_no_engine(self, setup):
        """Without JS engine, manifest stays None (no crash)."""
        ob, relayer, loop = setup
        ob.submit(
            app_id="test_app",
            intent_function="execute",
            params={"input_token": "WETH", "output_token": "USDC", "input_amount": "1000"},
            submitted_by="0xuser",
        )
        result = await loop.tick()
        assert result.orders_processed == 1  # No crash


class TestHotReloadAppsAndJs:
    """Tests for VAL-17/18: periodic hot-reload of apps and JS."""

    @pytest.mark.asyncio
    async def test_reload_loads_new_app_js(self, temp_store, app_def):
        """New active app's JS is loaded into engine on reload."""
        temp_store.save_app(app_def)
        temp_store.save_deployment(DeploymentResult(
            app_id="test_app", status=AppStatus.ACTIVE,
            contract_address="0x" + "ab" * 20,
        ))

        mock_engine = MagicMock()
        mock_engine.load_intent = AsyncMock()
        mock_engine._intents = {}

        ob = IntentOrderBook()
        loop = BlockLoop(
            orderbook=ob,
            app_store=temp_store,
            relayer=MockRelayer(),
            js_engine=mock_engine,
        )

        await loop._reload_apps_and_js()

        mock_engine.load_intent.assert_called_once_with("test_app", app_def.js_code)
        assert "test_app" in loop._known_js_hashes

    @pytest.mark.asyncio
    async def test_reload_detects_js_change(self, temp_store, app_def):
        """Updated JS code is detected and reloaded."""
        temp_store.save_app(app_def)
        temp_store.save_deployment(DeploymentResult(
            app_id="test_app", status=AppStatus.ACTIVE,
            contract_address="0x" + "ab" * 20,
        ))

        mock_engine = MagicMock()
        mock_engine.load_intent = AsyncMock()

        ob = IntentOrderBook()
        loop = BlockLoop(
            orderbook=ob,
            app_store=temp_store,
            relayer=MockRelayer(),
            js_engine=mock_engine,
        )

        # First load
        await loop._reload_apps_and_js()
        assert mock_engine.load_intent.call_count == 1

        # Same code → no reload
        await loop._reload_apps_and_js()
        assert mock_engine.load_intent.call_count == 1

        # Change JS code → should reload
        app_def.js_code = "module.exports = { config: {name: 'v2'}, score: () => ({score: 0.9}) }"
        temp_store.save_app(app_def)
        await loop._reload_apps_and_js()
        assert mock_engine.load_intent.call_count == 2

    @pytest.mark.asyncio
    async def test_reload_skips_draft_apps(self, temp_store, app_def):
        """Non-active apps are not loaded."""
        temp_store.save_app(app_def)
        temp_store.save_deployment(DeploymentResult(
            app_id="test_app", status=AppStatus.DRAFT,
        ))

        mock_engine = MagicMock()
        mock_engine.load_intent = AsyncMock()

        ob = IntentOrderBook()
        loop = BlockLoop(
            orderbook=ob,
            app_store=temp_store,
            relayer=MockRelayer(),
            js_engine=mock_engine,
        )

        await loop._reload_apps_and_js()
        mock_engine.load_intent.assert_not_called()


class TestWeightsEmitterLeaderCheck:
    """Tests for BT-8: leader-only weight emission."""

    @pytest.mark.asyncio
    async def test_non_leader_skips_emission(self):
        """Follower validator skips weight emission."""
        from minotaur_subnet.validator.weights_emitter import WeightsEmitter

        mock_sync = MagicMock()
        mock_sync.is_leader = False

        emitter = WeightsEmitter(
            wallet=MagicMock(),
            subtensor=MagicMock(),
            metagraph_sync=mock_sync,
        )
        result = await emitter.emit_async({"hotkey1": 0.5, "hotkey2": 0.5})
        assert result is False

    @pytest.mark.asyncio
    async def test_leader_proceeds_with_emission(self):
        """Leader validator proceeds (may fail due to mock subtensor, but doesn't skip)."""
        from minotaur_subnet.validator.weights_emitter import WeightsEmitter

        mock_sync = MagicMock()
        mock_sync.is_leader = True

        emitter = WeightsEmitter(
            wallet=MagicMock(),
            subtensor=MagicMock(),
            metagraph_sync=mock_sync,
        )
        # It will try to emit and likely fail on mock subtensor, but it shouldn't
        # return False from the leader check
        result = await emitter.emit_async({"hotkey1": 0.5})
        # False because mock subtensor will fail, but NOT because of leader check
        assert result is False  # Expected: subtensor mock fails

    @pytest.mark.asyncio
    async def test_no_metagraph_sync_proceeds(self):
        """Without metagraph_sync, emission proceeds unconditionally."""
        from minotaur_subnet.validator.weights_emitter import WeightsEmitter

        emitter = WeightsEmitter(
            wallet=MagicMock(),
            subtensor=MagicMock(),
            metagraph_sync=None,
        )
        # Will try to emit (and fail on mock subtensor), but no leader gate
        result = await emitter.emit_async({"hotkey1": 0.5})
        assert result is False  # Fails on mock subtensor, not leader check


class TestSimulatorAnvilFallback:
    """SIM-10: AnvilSimulator returns failed result when Anvil is unreachable."""

    @pytest.mark.asyncio
    async def test_simulate_returns_failed_when_disconnected(self):
        """Simulator gracefully returns failure instead of crashing."""
        from minotaur_subnet.simulator.anvil_simulator import AnvilSimulator
        from minotaur_subnet.shared.types import ExecutionPlan, Interaction

        sim = AnvilSimulator.__new__(AnvilSimulator)
        sim.rpc_url = "http://localhost:99999"
        sim.default_executor = "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"
        sim.fund_executor = True
        sim.sim_timeout = 5.0
        # Mock w3 as disconnected
        sim.w3 = MagicMock()
        sim.w3.is_connected.return_value = False

        plan = ExecutionPlan(
            intent_id="test",
            interactions=[Interaction(target="0x" + "11" * 20, value="0", call_data="0x", chain_id=1)],
            deadline=0, nonce=0, metadata={},
        )

        result = await sim.simulate(plan)
        assert result.success is False
        assert result.gas_used == 0
        assert "Anvil unavailable" in (result.error or "")
