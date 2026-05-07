"""Unit tests for VaultDipSolver."""

import time
from types import SimpleNamespace

import pytest

from minotaur_subnet.sdk.solvers.vault_dip_solver import (
    SOLVER_CLASS,
    UNISWAP_V3_ROUTER,
    WETH,
    USDC,
    VaultDipSolver,
    _DEPOSIT_SELECTOR,
    _APPROVE_SELECTOR,
    _EXACT_INPUT_SINGLE,
)
from minotaur_subnet.sdk.intent_solver import IntentSolver, MarketSnapshot, SolverMetadata
from minotaur_subnet.shared.types import (
    AppIntentConfig,
    AppIntentDefinition,
    ExecutionPlan,
    IntentState,
    TriggerType,
)


# ── fixtures ────────────────────────────────────────────────────────────


@pytest.fixture
def solver():
    s = VaultDipSolver()
    s.initialize({"chain_ids": [1]})
    return s


@pytest.fixture
def sample_intent():
    return AppIntentDefinition(
        app_id="test-vault-001",
        name="Test Vault",
        version="1.0.0",
        intent_type="vault",
        js_code="// test",
        config=AppIntentConfig(
            supported_chains=[1],
            trigger_type=TriggerType.USER_TRIGGERED,
        ),
    )


@pytest.fixture
def sample_state():
    return IntentState(
        contract_address="0x5aAdFB43eF8dAF45DD80F4676345b7676f1D70e3",
        chain_id=1,
        nonce=1,
        owner="0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266",
        raw_params={
            "input_token": WETH,
            "output_token": USDC,
            "input_amount": "1000000000000000000",
        },
    )


@pytest.fixture
def sample_snapshot():
    return MarketSnapshot(
        chain_id=1,
        block_number=18500000,
        timestamp=1700000000,
        prices={"ETH/USD": 1850.0, "USDC/USD": 1.0},
    )


# ── SOLVER_CLASS export ─────────────────────────────────────────────────


class TestSolverClassExport:
    def test_solver_class_is_exported(self):
        assert SOLVER_CLASS is VaultDipSolver

    def test_solver_class_is_intent_solver_subclass(self):
        assert issubclass(SOLVER_CLASS, IntentSolver)


# ── initialize ──────────────────────────────────────────────────────────


class TestInitialize:
    def test_initialize_does_not_throw(self):
        solver = VaultDipSolver()
        solver.initialize({"chain_ids": [1]})

    def test_initialize_with_empty_config(self):
        solver = VaultDipSolver()
        solver.initialize({})

    def test_initialize_stores_chain_ids(self):
        solver = VaultDipSolver()
        solver.initialize({"chain_ids": [1, 8453]})
        meta = solver.metadata()
        assert meta.supported_chains == [1, 8453]


# ── metadata ────────────────────────────────────────────────────────────


class TestMetadata:
    def test_metadata_returns_solver_metadata(self, solver):
        meta = solver.metadata()
        assert isinstance(meta, SolverMetadata)

    def test_metadata_has_correct_name(self, solver):
        meta = solver.metadata()
        assert meta.name == "vault-dip-solver"

    def test_metadata_has_version(self, solver):
        meta = solver.metadata()
        assert meta.version == "0.1.0"

    def test_metadata_has_author(self, solver):
        meta = solver.metadata()
        assert meta.author  # non-empty

    def test_metadata_has_supported_chains(self, solver):
        meta = solver.metadata()
        assert 1 in meta.supported_chains


# ── generate_plan ───────────────────────────────────────────────────────


class TestGeneratePlan:
    def test_returns_execution_plan(self, solver, sample_intent, sample_state, sample_snapshot):
        plan = solver.generate_plan(sample_intent, sample_state, sample_snapshot)
        assert isinstance(plan, ExecutionPlan)

    def test_plan_has_3_interactions(self, solver, sample_intent, sample_state, sample_snapshot):
        plan = solver.generate_plan(sample_intent, sample_state, sample_snapshot)
        assert len(plan.interactions) == 3

    def test_plan_intent_id_matches(self, solver, sample_intent, sample_state, sample_snapshot):
        plan = solver.generate_plan(sample_intent, sample_state, sample_snapshot)
        assert plan.intent_id == sample_intent.app_id

    def test_interaction_targets(self, solver, sample_intent, sample_state, sample_snapshot):
        plan = solver.generate_plan(sample_intent, sample_state, sample_snapshot)
        # 1: WETH deposit, 2: WETH approve, 3: Uniswap Router swap
        assert plan.interactions[0].target == WETH
        assert plan.interactions[1].target == WETH
        assert plan.interactions[2].target == UNISWAP_V3_ROUTER

    def test_deposit_calldata(self, solver, sample_intent, sample_state, sample_snapshot):
        plan = solver.generate_plan(sample_intent, sample_state, sample_snapshot)
        assert plan.interactions[0].call_data == "0x" + _DEPOSIT_SELECTOR

    def test_approve_calldata_starts_with_selector(self, solver, sample_intent, sample_state, sample_snapshot):
        plan = solver.generate_plan(sample_intent, sample_state, sample_snapshot)
        assert plan.interactions[1].call_data.startswith("0x" + _APPROVE_SELECTOR)

    def test_swap_calldata_starts_with_selector(self, solver, sample_intent, sample_state, sample_snapshot):
        plan = solver.generate_plan(sample_intent, sample_state, sample_snapshot)
        assert plan.interactions[2].call_data.startswith("0x" + _EXACT_INPUT_SINGLE)

    def test_deposit_has_value(self, solver, sample_intent, sample_state, sample_snapshot):
        plan = solver.generate_plan(sample_intent, sample_state, sample_snapshot)
        assert int(plan.interactions[0].value) > 0

    def test_approve_and_swap_have_zero_value(self, solver, sample_intent, sample_state, sample_snapshot):
        plan = solver.generate_plan(sample_intent, sample_state, sample_snapshot)
        assert plan.interactions[1].value == "0"
        assert plan.interactions[2].value == "0"

    def test_deadline_is_future(self, solver, sample_intent, sample_state, sample_snapshot):
        plan = solver.generate_plan(sample_intent, sample_state, sample_snapshot)
        assert plan.deadline > int(time.time()) - 10

    def test_nonce_matches_state(self, solver, sample_intent, sample_state, sample_snapshot):
        plan = solver.generate_plan(sample_intent, sample_state, sample_snapshot)
        assert plan.nonce == sample_state.nonce

    def test_metadata_has_plan_type(self, solver, sample_intent, sample_state, sample_snapshot):
        plan = solver.generate_plan(sample_intent, sample_state, sample_snapshot)
        assert plan.metadata.get("plan_type") == "vault_buydip"

    def test_uses_input_amount_from_params(self, solver, sample_intent, sample_snapshot):
        state = IntentState(
            contract_address="0x" + "00" * 20,
            chain_id=1,
            nonce=0,
            owner="0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266",
            raw_params={"input_amount": "500000000000000000"},  # 0.5 ETH
        )
        plan = solver.generate_plan(sample_intent, state, sample_snapshot)
        assert plan.interactions[0].value == "500000000000000000"

    def test_prefers_typed_context_input_amount(self, solver, sample_intent, sample_snapshot):
        state = IntentState(
            contract_address="0x" + "00" * 20,
            chain_id=1,
            nonce=0,
            owner="0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266",
            raw_params={"input_amount": "500000000000000000"},
            typed_context=SimpleNamespace(
                input_amount=250000000000000000,
                raw_params={"input_amount": "250000000000000000"},
            ),
        )
        plan = solver.generate_plan(sample_intent, state, sample_snapshot)
        assert plan.interactions[0].value == "250000000000000000"

    def test_all_interactions_have_correct_chain_id(self, solver, sample_intent, sample_state, sample_snapshot):
        plan = solver.generate_plan(sample_intent, sample_state, sample_snapshot)
        for ix in plan.interactions:
            assert ix.chain_id == 1

    def test_all_targets_are_valid_addresses(self, solver, sample_intent, sample_state, sample_snapshot):
        plan = solver.generate_plan(sample_intent, sample_state, sample_snapshot)
        for ix in plan.interactions:
            assert ix.target.startswith("0x")
            assert len(ix.target) == 42

    def test_all_calldata_starts_with_0x(self, solver, sample_intent, sample_state, sample_snapshot):
        plan = solver.generate_plan(sample_intent, sample_state, sample_snapshot)
        for ix in plan.interactions:
            assert ix.call_data.startswith("0x")


# ── check_trigger ───────────────────────────────────────────────────────


class TestCheckTrigger:
    def test_returns_bool(self, solver, sample_intent, sample_state, sample_snapshot):
        result = solver.check_trigger(sample_intent, sample_state, sample_snapshot)
        assert isinstance(result, bool)

    def test_triggers_on_price_dip(self, solver, sample_intent, sample_state):
        # ETH at $1850 is a 7.5% dip from $2000 reference -> should trigger
        snapshot = MarketSnapshot(
            chain_id=1, block_number=18500000, timestamp=1700000000,
            prices={"ETH/USD": 1850.0},
        )
        assert solver.check_trigger(sample_intent, sample_state, snapshot) is True

    def test_no_trigger_at_high_price(self, solver, sample_intent, sample_state):
        # ETH at $2000 is no dip -> should not trigger
        snapshot = MarketSnapshot(
            chain_id=1, block_number=18500000, timestamp=1700000000,
            prices={"ETH/USD": 2000.0},
        )
        assert solver.check_trigger(sample_intent, sample_state, snapshot) is False

    def test_no_trigger_without_price(self, solver, sample_intent, sample_state):
        snapshot = MarketSnapshot(
            chain_id=1, block_number=18500000, timestamp=1700000000,
            prices={},
        )
        assert solver.check_trigger(sample_intent, sample_state, snapshot) is False


# ── synthetic intents integration ───────────────────────────────────────


class TestSyntheticIntents:
    """Test that the solver produces valid plans for all synthetic intents."""

    def test_all_synthetic_intents_produce_valid_plans(self, solver):
        from minotaur_subnet.harness.snapshot import build_synthetic_intents
        from minotaur_subnet.harness.screening import _validate_plan_structure

        for intent, state, snapshot in build_synthetic_intents():
            plan = solver.generate_plan(intent, state, snapshot)
            error = _validate_plan_structure(plan, intent, snapshot)
            assert error is None, f"Invalid plan for {intent.app_id}: {error}"
