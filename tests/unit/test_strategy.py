"""Tests for Strategy ABC."""

import pytest

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


# ── Test strategy implementations ────────────────────────────────────────────


class SwapStrategy(Strategy):
    APP_ID = "app-swap-001"
    INTENT_FUNCTIONS = ["execute"]

    def generate_plan(self, intent, state, snapshot):
        return ExecutionPlan(
            intent_id=intent.app_id,
            interactions=[
                Interaction(
                    target="0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",
                    value="0",
                    call_data="0xd0e30db0",
                    chain_id=1,
                ),
            ],
            deadline=snapshot.timestamp + 300,
            nonce=state.nonce,
        )


class VaultStrategy(Strategy):
    APP_ID = "app-vault-001"
    INTENT_FUNCTIONS = ["buyDip", "withdraw"]

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
        )

    def check_trigger(self, intent, state, snapshot):
        eth_price = snapshot.prices.get("ETH/USD", 0.0)
        return eth_price < 1800.0


class AllFunctionsStrategy(Strategy):
    """Strategy that handles all intent functions (empty INTENT_FUNCTIONS)."""
    APP_ID = "app-generic-001"

    def generate_plan(self, intent, state, snapshot):
        return ExecutionPlan(
            intent_id=intent.app_id,
            interactions=[
                Interaction(
                    target="0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",
                    value="0",
                    call_data="0xd0e30db0",
                    chain_id=1,
                ),
            ],
            deadline=snapshot.timestamp + 300,
            nonce=state.nonce,
        )


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def snapshot():
    return MarketSnapshot(
        chain_id=1,
        block_number=18500000,
        timestamp=1700000000,
        prices={"ETH/USD": 1850.0, "USDC/USD": 1.0},
    )


@pytest.fixture
def intent():
    return AppIntentDefinition(
        app_id="app-swap-001",
        name="Test Swap",
        version="1.0.0",
        intent_type="swap",
        js_code="// test",
        config=AppIntentConfig(
            supported_chains=[1],
            trigger_type=TriggerType.USER_TRIGGERED,
        ),
    )


@pytest.fixture
def state():
    return IntentState(
        contract_address="0x5aAdFB43eF8dAF45DD80F4676345b7676f1D70e3",
        chain_id=1,
        nonce=1,
        owner="0x0000000000000000000000000000000000000001",
        control={"_intent_function": "execute"},
    )


# ── Tests ───────────────────────────────────────────────────────────────────


class TestStrategyABC:
    def test_cannot_instantiate_abstract(self):
        with pytest.raises(TypeError):
            Strategy()

    def test_concrete_strategy_has_app_id(self):
        s = SwapStrategy()
        assert s.APP_ID == "app-swap-001"

    def test_concrete_strategy_has_intent_functions(self):
        s = VaultStrategy()
        assert s.INTENT_FUNCTIONS == ["buyDip", "withdraw"]


class TestAccepts:
    def test_accepts_matching_app_id(self):
        s = SwapStrategy()
        assert s.accepts("app-swap-001") is True

    def test_rejects_wrong_app_id(self):
        s = SwapStrategy()
        assert s.accepts("app-other-001") is False

    def test_accepts_matching_intent_function(self):
        s = VaultStrategy()
        assert s.accepts("app-vault-001", "buyDip") is True
        assert s.accepts("app-vault-001", "withdraw") is True

    def test_rejects_wrong_intent_function(self):
        s = VaultStrategy()
        assert s.accepts("app-vault-001", "unknown") is False

    def test_accepts_any_function_when_list_empty(self):
        s = AllFunctionsStrategy()
        assert s.accepts("app-generic-001", "execute") is True
        assert s.accepts("app-generic-001", "anything") is True
        assert s.accepts("app-generic-001", "") is True

    def test_accepts_no_function_specified(self):
        s = VaultStrategy()
        assert s.accepts("app-vault-001") is True
        assert s.accepts("app-vault-001", "") is True


class TestGeneratePlan:
    def test_generates_valid_plan(self, intent, state, snapshot):
        s = SwapStrategy()
        plan = s.generate_plan(intent, state, snapshot)
        assert plan.intent_id == "app-swap-001"
        assert len(plan.interactions) == 1
        assert plan.deadline > snapshot.timestamp

    def test_plan_uses_snapshot_timestamp(self, intent, state, snapshot):
        s = SwapStrategy()
        plan = s.generate_plan(intent, state, snapshot)
        assert plan.deadline == snapshot.timestamp + 300


class TestCheckTrigger:
    def test_default_returns_false(self, intent, state, snapshot):
        s = SwapStrategy()
        assert s.check_trigger(intent, state, snapshot) is False

    def test_custom_trigger_logic(self, intent, state, snapshot):
        s = VaultStrategy()
        # ETH/USD is 1850.0 in snapshot, threshold is 1800.0
        assert s.check_trigger(intent, state, snapshot) is False

    def test_custom_trigger_fires_on_dip(self, intent, state):
        dip_snapshot = MarketSnapshot(
            chain_id=1,
            block_number=18500000,
            timestamp=1700000000,
            prices={"ETH/USD": 1700.0},
        )
        s = VaultStrategy()
        assert s.check_trigger(intent, state, dip_snapshot) is True
