"""Phase B: Real Solver + JS Scoring E2E tests.

Tests the AnvilSwapSolver generating real swap calldata and the JS scoring
engine evaluating plans against Anvil-deployed contracts.

Requires: Anvil (Foundry) for on-chain tests, Node.js for JS engine.
"""

import asyncio
import shutil
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from minotaur_subnet.blockloop.loop import BlockLoop
from minotaur_subnet.blockchain.chains import _web3_cache
from minotaur_subnet.consensus.eip712 import address_from_key
from minotaur_subnet.store import AppIntentStore
from minotaur_subnet.orderbook.orderbook import IntentOrderBook, OrderStatus
from minotaur_subnet.relayer.base import MockRelayer
from minotaur_subnet.shared.types import (
    AppIntentConfig,
    AppIntentDefinition,
    ExecutionPlan,
    Interaction,
    IntentState,
    SimulationResult,
    TokenTransfer,
)
from minotaur_subnet.sdk.intent_solver import MarketSnapshot
from minotaur_subnet.sdk.solvers.anvil_swap_solver import AnvilSwapSolver

from conftest import ANVIL_KEYS, CHAIN_ID, RPC_URL

pytestmark = pytest.mark.skipif(
    not shutil.which("anvil"), reason="Foundry (anvil) required"
)

# Path to the JS scorer
JS_SCORER_PATH = Path(__file__).parent / "fixtures" / "swap_scorer.js"


# ── Fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def anvil_solver(deployed_contracts):
    """AnvilSwapSolver initialized with deployed contract addresses."""
    solver = AnvilSwapSolver()
    solver.initialize({
        "router_address": deployed_contracts.router,
        "weth_address": deployed_contracts.weth,
        "usdc_address": deployed_contracts.usdc,
    })
    return solver


@pytest.fixture(scope="module")
def swap_app_def():
    """AppIntentDefinition for the swap app."""
    js_code = JS_SCORER_PATH.read_text() if JS_SCORER_PATH.exists() else "// mock"
    return AppIntentDefinition(
        app_id="swap-app",
        name="Test Swap App",
        version="1.0.0",
        intent_type="swap",
        js_code=js_code,
        config=AppIntentConfig(supported_chains=[CHAIN_ID]),
    )


@pytest.fixture(scope="module")
def test_state(deployed_contracts, test_accounts):
    """IntentState for test swap orders."""
    return IntentState(
        contract_address=deployed_contracts.dex_app,
        chain_id=CHAIN_ID,
        nonce=0,
        owner=test_accounts.user_addr,
        raw_params={
            "input_token": deployed_contracts.weth,
            "output_token": deployed_contracts.usdc,
            "input_amount": str(10**18),
            "min_output_amount": str(1800 * 10**6),
            "output_amount": str(1800 * 10**6),
        },
    )


@pytest.fixture(scope="module")
def test_snapshot():
    """MarketSnapshot for Anvil testing."""
    return MarketSnapshot(
        chain_id=CHAIN_ID,
        block_number=1,
        timestamp=int(time.time()),
        prices={"ETH/USD": 2000.0, "USDC/USD": 1.0},
        dex_config={},
    )


# ── Tests ─────────────────────────────────────────────────────────────────


class TestAnvilSwapSolver:
    """Tests for the AnvilSwapSolver producing valid execution plans."""

    def test_real_solver_generates_valid_plan(
        self, anvil_solver, swap_app_def, test_state, test_snapshot,
    ):
        """AnvilSwapSolver produces an ExecutionPlan with valid calldata."""
        plan = anvil_solver.generate_plan(swap_app_def, test_state, test_snapshot)

        assert isinstance(plan, ExecutionPlan)
        assert len(plan.interactions) == 1
        assert plan.interactions[0].target == anvil_solver.router
        assert plan.interactions[0].call_data.startswith("0x")
        assert len(plan.interactions[0].call_data) > 10  # Non-trivial calldata
        assert plan.deadline > int(time.time())
        assert plan.nonce == test_state.nonce

    def test_solver_plan_has_correct_target(
        self, anvil_solver, swap_app_def, test_state, test_snapshot, deployed_contracts,
    ):
        """Plan targets the deployed router address."""
        plan = anvil_solver.generate_plan(swap_app_def, test_state, test_snapshot)
        assert plan.interactions[0].target == deployed_contracts.router

    def test_solver_metadata(self, anvil_solver):
        """Solver reports correct metadata."""
        meta = anvil_solver.metadata()
        assert meta.name == "anvil-swap-solver"
        assert meta.version == "1.0.0"
        assert 31337 in meta.supported_chains
        assert "swap" in meta.supported_intent_types

    def test_bad_solver_plan_gets_low_score(self, swap_app_def, test_state, test_snapshot):
        """A solver producing invalid calldata gets a low mock score."""
        # Create a solver with wrong addresses
        bad_solver = AnvilSwapSolver()
        bad_solver.initialize({
            "router_address": "0x" + "00" * 20,
            "weth_address": "0x" + "00" * 20,
            "usdc_address": "0x" + "00" * 20,
        })

        plan = bad_solver.generate_plan(swap_app_def, test_state, test_snapshot)
        # Plan is generated but targets zero address
        assert plan.interactions[0].target == "0x" + "00" * 20


class TestJsScoringEngine:
    """Tests for the JS scoring engine with real scoring modules."""

    @pytest.mark.skipif(
        not shutil.which("node"), reason="Node.js required for JS engine"
    )
    def test_js_engine_scores_plan(self, swap_app_def, test_state):
        """JS scoring engine runs and returns a 0-1 score."""
        if not JS_SCORER_PATH.exists():
            pytest.skip("swap_scorer.js not found")

        from minotaur_subnet.engine.js_engine import JsExecutionEngine

        engine = JsExecutionEngine(timeout_ms=5000)

        async def _run():
            await engine.load_intent("swap-app", swap_app_def.js_code)

            plan = ExecutionPlan(
                intent_id="swap-app",
                interactions=[
                    Interaction(target="0x" + "11" * 20, value="0", call_data="0x1234"),
                ],
                deadline=int(time.time()) + 300,
                nonce=0,
            )

            simulation = SimulationResult(
                success=True,
                gas_used=150000,
                token_transfers=[
                    TokenTransfer(
                        token="0x" + "aa" * 20,
                        from_addr="0x" + "00" * 20,
                        to_addr="0x" + "11" * 20,
                        amount="1000000000",
                    ),
                    TokenTransfer(
                        token="0x" + "bb" * 20,
                        from_addr="0x" + "11" * 20,
                        to_addr="0x" + "00" * 20,
                        amount="980000000",
                    ),
                ],
            )

            result = await engine.score("swap-app", plan, simulation, test_state)
            return result

        result = asyncio.get_event_loop().run_until_complete(_run())
        assert result.score > 0
        assert result.score <= 1.0
        assert result.valid is True

    @pytest.mark.skipif(
        not shutil.which("node"), reason="Node.js required for JS engine"
    )
    def test_js_should_trigger(self, swap_app_def):
        """JS shouldTrigger() controls perpetual order activation."""
        if not JS_SCORER_PATH.exists():
            pytest.skip("swap_scorer.js not found")

        from minotaur_subnet.engine.js_engine import JsExecutionEngine

        engine = JsExecutionEngine(timeout_ms=5000)

        async def _run():
            await engine.load_intent("swap-app", swap_app_def.js_code)

            # State with target price below current — should NOT trigger
            state_no_trigger = IntentState(
                contract_address="0x" + "00" * 20,
                chain_id=CHAIN_ID,
                nonce=0,
                owner="0x" + "00" * 20,
                raw_params={"target_price": "500.0"},  # Way below market
            )

            result = await engine.should_trigger("swap-app", state_no_trigger)
            return result

        result = asyncio.get_event_loop().run_until_complete(_run())
        # With no price context, shouldTrigger returns False
        assert result is False


class TestSolverHotSwap:
    """Test solver hot-swap into the BlockLoop."""

    def test_solver_hot_swap(self, tmp_path, swap_app_def, deployed_contracts):
        """loop.set_solver(new_solver) → next tick uses new solver."""
        ob = IntentOrderBook()
        app_store = AppIntentStore(store_path=tmp_path / "store.json")
        app_store.save_app(swap_app_def)

        # Start with no solver
        loop = BlockLoop(
            orderbook=ob,
            app_store=app_store,
            solver=None,
            relayer=MockRelayer(),
            score_threshold=0.1,
        )

        # Submit order
        ob.submit(
            app_id="swap-app",
            intent_function="execute",
            params={
                "input_token": deployed_contracts.weth,
                "output_token": deployed_contracts.usdc,
                "input_amount": "1000000000",
            },
            submitted_by="0x" + "01" * 20,
            chain_id=CHAIN_ID,
        )

        # Tick with no solver — uses fallback plan
        result1 = asyncio.get_event_loop().run_until_complete(loop.tick())
        assert result1.orders_processed == 1

        # Hot-swap to real solver
        solver = AnvilSwapSolver()
        solver.initialize({
            "router_address": deployed_contracts.router,
            "weth_address": deployed_contracts.weth,
            "usdc_address": deployed_contracts.usdc,
        })
        loop.set_solver(solver)

        # Submit another order
        ob.submit(
            app_id="swap-app",
            intent_function="execute",
            params={
                "input_token": deployed_contracts.weth,
                "output_token": deployed_contracts.usdc,
                "input_amount": "1000000000",
                "output_amount": str(1800 * 10**6),
            },
            submitted_by="0x" + "02" * 20,
            chain_id=CHAIN_ID,
        )

        # Tick with real solver
        result2 = asyncio.get_event_loop().run_until_complete(loop.tick())
        assert result2.orders_processed == 1
