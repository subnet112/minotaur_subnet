"""Anvil swap solver — generates real swap calldata for E2E testing.

Produces ExecutionPlans targeting a TestSwapRouter deployed on Anvil.
The router.swapExact(outputToken, outputAmount, recipient) call mints
output tokens to the recipient, simulating a real swap.

Usage:
    solver = AnvilSwapSolver()
    solver.initialize({
        "router_address": "0x...",
        "weth_address": "0x...",
        "usdc_address": "0x...",
    })
    plan = solver.generate_plan(app, state, snapshot)
"""

from __future__ import annotations

import time
from typing import Any

from eth_abi import encode as abi_encode
from eth_hash.auto import keccak

from minotaur_subnet.shared.types import (
    AppIntentDefinition,
    ExecutionPlan,
    Interaction,
    IntentState,
)
from minotaur_subnet.sdk.intent_solver import IntentSolver, MarketSnapshot, SolverMetadata
from minotaur_subnet.v3.manifest import normalize_swap_intent_params


def _state_params(state: IntentState) -> dict[str, Any]:
    typed = getattr(state, "typed_context", None)
    if typed is not None:
        raw = getattr(typed, "raw_params", None)
        if isinstance(raw, dict):
            return raw
    return state.raw_params_view()


class AnvilSwapSolver(IntentSolver):
    """Solver for testing against Anvil-deployed contracts.

    Generates real swapExact() calldata targeting the TestSwapRouter.
    """

    def __init__(self) -> None:
        self.router = ""
        self.weth = ""
        self.usdc = ""

    def initialize(self, config: dict[str, Any]) -> None:
        self.router = config.get("router_address", "")
        self.weth = config.get("weth_address", "")
        self.usdc = config.get("usdc_address", "")

    def generate_plan(
        self,
        intent: AppIntentDefinition,
        state: IntentState,
        snapshot: MarketSnapshot,
    ) -> ExecutionPlan:
        """Build a real swapExact() call targeting the test router."""
        params = normalize_swap_intent_params(
            _state_params(state),
            receiver_default=state.contract_address or state.owner,
        )
        output_token = params.get("output_token", self.usdc) or self.usdc
        output_amount = params.get("min_output_amount", 0) or 1_800_000_000
        # DexAggregatorApp measures gained output on the app contract, then
        # forwards tokens to the final receiver itself after fee accounting.
        recipient = state.contract_address or state.owner

        # Encode swapExact(address outputToken, uint256 outputAmount, address recipient)
        selector = keccak(b"swapExact(address,uint256,address)")[:4]
        args = abi_encode(
            ["address", "uint256", "address"],
            [output_token, output_amount, recipient],
        )
        calldata = "0x" + (selector + args).hex()

        return ExecutionPlan(
            intent_id=intent.app_id,
            interactions=[
                Interaction(
                    target=self.router,
                    value="0",
                    call_data=calldata,
                    chain_id=state.chain_id,
                ),
            ],
            deadline=int(time.time()) + 300,
            nonce=state.nonce,
            metadata={"solver": "anvil-swap-solver"},
        )

    def metadata(self) -> SolverMetadata:
        return SolverMetadata(
            name="anvil-swap-solver",
            version="1.0.0",
            author="test",
            description="Test solver for Anvil E2E testing",
            supported_chains=[31337],
            supported_intent_types=["swap"],
        )
