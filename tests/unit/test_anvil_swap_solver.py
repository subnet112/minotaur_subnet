"""Unit tests for AnvilSwapSolver."""

from minotaur_subnet.sdk.intent_solver import MarketSnapshot
from minotaur_subnet.sdk.solvers.anvil_swap_solver import AnvilSwapSolver
from minotaur_subnet.shared.types import AppIntentDefinition, IntentState
from minotaur_subnet.v3.contexts import SwapIntentContext


def _app() -> AppIntentDefinition:
    return AppIntentDefinition(
        app_id="swap-app",
        name="Swap App",
        version="1.0.0",
        intent_type="swap",
        js_code="module.exports = { score() { return { score: 1.0 }; } };",
    )


def _snapshot() -> MarketSnapshot:
    return MarketSnapshot(chain_id=31337, block_number=1, timestamp=1)


def test_anvil_solver_prefers_typed_context_params():
    solver = AnvilSwapSolver()
    solver.initialize({
        "router_address": "0x" + "11" * 20,
        "weth_address": "0x" + "22" * 20,
        "usdc_address": "0x" + "33" * 20,
    })

    state = IntentState(
        contract_address="0x" + "44" * 20,
        chain_id=31337,
        nonce=7,
        owner="0x" + "55" * 20,
        raw_params={},
        typed_context=SwapIntentContext(
            app_id="swap-app",
            intent_function="swap",
            chain_id=31337,
            owner="0x" + "55" * 20,
            contract_address="0x" + "44" * 20,
            nonce=7,
            raw_params={
                "output_token": "0x" + "66" * 20,
                "min_output_amount": "123",
                "receiver": "0x" + "77" * 20,
            },
            input_token="0x" + "22" * 20,
            output_token="0x" + "66" * 20,
            input_amount=1000,
            min_output_amount=123,
            receiver="0x" + "77" * 20,
            fee_tier=3000,
        ),
    )

    plan = solver.generate_plan(_app(), state, _snapshot())

    assert plan.intent_id == "swap-app"
    assert plan.interactions[0].target == "0x" + "11" * 20
    assert plan.nonce == 7
    assert "0000000000000000000000006666666666666666666666666666666666666666" in plan.interactions[0].call_data
    assert "0000000000000000000000004444444444444444444444444444444444444444" in plan.interactions[0].call_data
