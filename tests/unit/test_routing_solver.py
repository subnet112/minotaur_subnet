"""Tests for RoutingSolver."""

import pytest

from minotaur_subnet.sdk.routing_solver import RoutingSolver
from minotaur_subnet.sdk.strategy import Strategy
from minotaur_subnet.shared.types import (
    AppIntentConfig,
    AppIntentDefinition,
    ExecutionPlan,
    Interaction,
    IntentState,
    TriggerType,
)
from minotaur_subnet.sdk.intent_solver import MarketSnapshot
from minotaur_subnet.v3.contexts import BaseIntentContext


# ── Test strategies ──────────────────────────────────────────────────────────


class FakeSwapStrategy(Strategy):
    APP_ID = "app-swap-001"

    def generate_plan(self, intent, state, snapshot):
        return ExecutionPlan(
            intent_id=intent.app_id,
            interactions=[
                Interaction(
                    target="0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
                    value="0",
                    call_data="0xdeadbeef",
                    chain_id=1,
                ),
            ],
            deadline=snapshot.timestamp + 300,
            nonce=state.nonce,
            metadata={"plan_type": "swap_strategy"},
        )


class FakeVaultStrategy(Strategy):
    APP_ID = "app-vault-001"
    INTENT_FUNCTIONS = ["buyDip"]

    def generate_plan(self, intent, state, snapshot):
        return ExecutionPlan(
            intent_id=intent.app_id,
            interactions=[
                Interaction(
                    target="0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",
                    value="1000000000000000000",
                    call_data="0xd0e30db0",
                    chain_id=1,
                ),
            ],
            deadline=snapshot.timestamp + 600,
            nonce=state.nonce,
            metadata={"plan_type": "vault_strategy"},
        )

    def check_trigger(self, intent, state, snapshot):
        return snapshot.prices.get("ETH/USD", 9999) < 1800.0


class CrashingStrategy(Strategy):
    APP_ID = "app-crash-001"

    def generate_plan(self, intent, state, snapshot):
        raise RuntimeError("Strategy crashed!")


class EmptyAppIdStrategy(Strategy):
    # APP_ID deliberately not set

    def generate_plan(self, intent, state, snapshot):
        return ExecutionPlan(
            intent_id=intent.app_id,
            interactions=[],
            deadline=snapshot.timestamp + 300,
            nonce=state.nonce,
        )


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def solver():
    s = RoutingSolver()
    s.initialize({"chain_ids": [1]})
    return s


@pytest.fixture
def snapshot():
    return MarketSnapshot(
        chain_id=1,
        block_number=18500000,
        timestamp=1700000000,
        prices={"ETH/USD": 1850.0, "USDC/USD": 1.0},
    )


def _make_intent(app_id: str) -> AppIntentDefinition:
    return AppIntentDefinition(
        app_id=app_id,
        name="Test",
        version="1.0.0",
        intent_type="swap",
        js_code="// test",
        config=AppIntentConfig(
            supported_chains=[1],
            trigger_type=TriggerType.USER_TRIGGERED,
        ),
    )


def _make_state(intent_function: str = "execute") -> IntentState:
    return IntentState(
        contract_address="0x5aAdFB43eF8dAF45DD80F4676345b7676f1D70e3",
        chain_id=1,
        nonce=1,
        owner="0x0000000000000000000000000000000000000001",
        control={"_intent_function": intent_function},
    )


# ── Tests ───────────────────────────────────────────────────────────────────


class TestRegistration:
    def test_register_strategy(self, solver):
        solver.register_strategy(FakeSwapStrategy())
        assert solver.get_strategy("app-swap-001") is not None

    def test_register_replaces_existing(self, solver):
        s1 = FakeSwapStrategy()
        s2 = FakeSwapStrategy()
        solver.register_strategy(s1)
        solver.register_strategy(s2)
        assert solver.get_strategy("app-swap-001") is s2

    def test_register_empty_app_id_raises(self, solver):
        with pytest.raises(ValueError, match="APP_ID must be set"):
            solver.register_strategy(EmptyAppIdStrategy())

    def test_remove_strategy(self, solver):
        solver.register_strategy(FakeSwapStrategy())
        assert solver.remove_strategy("app-swap-001") is True
        assert solver.get_strategy("app-swap-001") is None

    def test_remove_nonexistent(self, solver):
        assert solver.remove_strategy("app-nonexistent") is False

    def test_get_strategy_none(self, solver):
        assert solver.get_strategy("app-nonexistent") is None


class TestDispatch:
    def test_dispatches_to_matching_strategy(self, solver, snapshot):
        solver.register_strategy(FakeSwapStrategy())
        intent = _make_intent("app-swap-001")
        state = _make_state()

        plan = solver.generate_plan(intent, state, snapshot)
        assert plan.metadata.get("plan_type") == "swap_strategy"
        assert plan.intent_id == "app-swap-001"

    def test_dispatches_to_correct_strategy(self, solver, snapshot):
        solver.register_strategy(FakeSwapStrategy())
        solver.register_strategy(FakeVaultStrategy())

        # Request vault
        intent = _make_intent("app-vault-001")
        state = _make_state("buyDip")
        plan = solver.generate_plan(intent, state, snapshot)
        assert plan.metadata.get("plan_type") == "vault_strategy"

    def test_dispatches_using_typed_context_intent_function(self, solver, snapshot):
        solver.register_strategy(FakeVaultStrategy())
        intent = _make_intent("app-vault-001")
        state = IntentState(
            contract_address="0x5aAdFB43eF8dAF45DD80F4676345b7676f1D70e3",
            chain_id=1,
            nonce=1,
            owner="0x0000000000000000000000000000000000000001",
            raw_params={},
            typed_context=BaseIntentContext(
                app_id="app-vault-001",
                intent_function="buyDip",
                chain_id=1,
                owner="0x0000000000000000000000000000000000000001",
                contract_address="0x5aAdFB43eF8dAF45DD80F4676345b7676f1D70e3",
                nonce=1,
                raw_params={},
            ),
        )

        plan = solver.generate_plan(intent, state, snapshot)
        assert plan.metadata.get("plan_type") == "vault_strategy"

    def test_fallback_when_no_strategy(self, solver, snapshot):
        intent = _make_intent("app-unknown-001")
        state = _make_state()
        plan = solver.generate_plan(intent, state, snapshot)
        assert plan.metadata.get("plan_type") == "fallback"
        assert plan.intent_id == "app-unknown-001"
        assert len(plan.interactions) >= 1

    def test_fallback_on_strategy_crash(self, solver, snapshot):
        solver.register_strategy(CrashingStrategy())
        intent = _make_intent("app-crash-001")
        state = _make_state()
        plan = solver.generate_plan(intent, state, snapshot)
        assert plan.metadata.get("plan_type") == "fallback"

    def test_fallback_has_valid_structure(self, solver, snapshot):
        intent = _make_intent("app-unknown-001")
        state = _make_state()
        plan = solver.generate_plan(intent, state, snapshot)
        assert plan.deadline > snapshot.timestamp
        assert plan.nonce == state.nonce
        for ix in plan.interactions:
            assert ix.target.startswith("0x")
            assert len(ix.target) == 42
            assert ix.call_data.startswith("0x")


class TestCheckTrigger:
    def test_delegates_to_strategy(self, solver, snapshot):
        solver.register_strategy(FakeVaultStrategy())
        intent = _make_intent("app-vault-001")
        state = _make_state("buyDip")
        # ETH is 1850, threshold 1800 => no trigger
        assert solver.check_trigger(intent, state, snapshot) is False

    def test_trigger_fires(self, solver):
        solver.register_strategy(FakeVaultStrategy())
        dip_snapshot = MarketSnapshot(
            chain_id=1, block_number=18500000, timestamp=1700000000,
            prices={"ETH/USD": 1700.0},
        )
        intent = _make_intent("app-vault-001")
        state = _make_state("buyDip")
        assert solver.check_trigger(intent, state, dip_snapshot) is True

    def test_no_strategy_returns_false(self, solver, snapshot):
        intent = _make_intent("app-unknown-001")
        state = _make_state()
        assert solver.check_trigger(intent, state, snapshot) is False


class TestMetadata:
    def test_basic_metadata(self, solver):
        meta = solver.metadata()
        assert meta.name == "routing-solver"
        assert meta.version == "1.0.0"

    def test_metadata_reflects_strategies(self, solver):
        solver.register_strategy(FakeSwapStrategy())
        solver.register_strategy(FakeVaultStrategy())
        meta = solver.metadata()
        assert "2" in meta.description


class TestInitialize:
    def test_initialize_stores_chain_ids(self):
        solver = RoutingSolver()
        solver.initialize({"chain_ids": [1, 8453]})
        meta = solver.metadata()
        assert meta.supported_chains == [1, 8453]

    def test_initialize_defaults(self):
        solver = RoutingSolver()
        solver.initialize({})
        meta = solver.metadata()
        assert meta.supported_chains == [1]


class TestSolverClass:
    def test_solver_class_export(self):
        from minotaur_subnet.sdk.routing_solver import SOLVER_CLASS
        assert SOLVER_CLASS is RoutingSolver
