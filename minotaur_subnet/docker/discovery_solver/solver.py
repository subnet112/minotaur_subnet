"""Discovery-aware IntentSolver reference implementation.

A reference solver that demonstrates the full miner workflow:
  1. Read app manifests to discover intent functions and parameters
  2. Use manifest data to build intelligent, schema-aware plans
  3. Leverage scoring hints for optimization

Miners can extend this class and override _build_manifest_aware_plan()
to implement their own strategies while benefiting from manifest data.

To test locally:
    python -m minotaur_subnet.harness.runner solver.py

To submit:
    1. Push this repo to a public git repository
    2. POST /v1/submissions with your repo_url, commit_hash, epoch, and hotkey
"""

import time
from typing import Any

from minotaur_subnet.sdk.intent_solver import IntentSolver, MarketSnapshot, SolverMetadata
from minotaur_subnet.shared.types import (
    AppIntentDefinition,
    ExecutionPlan,
    Interaction,
    IntentState,
)


class DiscoverySolver(IntentSolver):
    """Reference solver that uses manifest data for plan generation.

    When initialized with manifests via config["manifests"], this solver
    reads intent function definitions, parameter schemas, and example params
    to build better plans than a blind baseline solver.

    Config keys:
        manifests: dict[str, dict]  — {app_id: manifest_dict}
    """

    def initialize(self, config: dict[str, Any]) -> None:
        self._manifests: dict[str, dict] = config.get("manifests", {})
        self._config = config

    def generate_plan(
        self,
        intent: AppIntentDefinition,
        state: IntentState,
        snapshot: MarketSnapshot,
    ) -> ExecutionPlan:
        manifest = self._manifests.get(intent.app_id, {})
        functions = manifest.get("intent_functions", [])
        params = _state_params(state)

        # Find the matching intent function from manifest
        intent_fn_name = (
            getattr(getattr(state, "typed_context", None), "intent_function", "")
            or params.get("_intent_function")
            or params.get("intent_function")
            or "swap"
        )
        fn = next((f for f in functions if f["name"] == intent_fn_name), None)

        if fn and "example_params" in fn:
            return self._build_manifest_aware_plan(intent, state, snapshot, fn)

        return self._build_generic_plan(intent, state, snapshot)

    def _build_manifest_aware_plan(
        self,
        intent: AppIntentDefinition,
        state: IntentState,
        snapshot: MarketSnapshot,
        fn: dict[str, Any],
    ) -> ExecutionPlan:
        """Build a plan using manifest function metadata.

        Uses the function's parameter schema and example_params to construct
        meaningful interactions. Real solvers would use snapshot market data
        to optimize routes, but this reference implementation demonstrates
        the manifest-aware pattern.
        """
        params = _state_params(state)
        example = fn.get("example_params", {})

        # Use real params if available, fall back to manifest examples
        input_token = params.get("input_token", example.get("input_token", "0x" + "00" * 20))
        output_token = params.get("output_token", example.get("output_token", "0x" + "00" * 20))

        interactions = [
            # Step 1: Approve input token
            Interaction(
                target=input_token,
                value="0",
                call_data="0x095ea7b3",  # approve(address,uint256)
                chain_id=state.chain_id,
            ),
            # Step 2: Execute swap
            Interaction(
                target=output_token,
                value="0",
                call_data="0x38ed1739",  # swapExactTokensForTokens
                chain_id=state.chain_id,
            ),
        ]

        return ExecutionPlan(
            intent_id=intent.app_id,
            interactions=interactions,
            deadline=int(time.time()) + 300,
            nonce=state.nonce,
            metadata={
                "manifest_aware": True,
                "intent_function": fn["name"],
                "solver": "discovery-solver",
            },
        )

    def _build_generic_plan(
        self,
        intent: AppIntentDefinition,
        state: IntentState,
        snapshot: MarketSnapshot,
    ) -> ExecutionPlan:
        """Fallback plan when no manifest is available."""
        return ExecutionPlan(
            intent_id=intent.app_id,
            interactions=[
                Interaction(
                    target="0x" + "00" * 20,
                    value="0",
                    call_data="0x",
                    chain_id=state.chain_id,
                ),
            ],
            deadline=int(time.time()) + 300,
            nonce=state.nonce,
            metadata={"fallback": True, "solver": "discovery-solver"},
        )

    def metadata(self) -> SolverMetadata:
        return SolverMetadata(
            name="discovery-solver",
            version="1.0.0",
            author="reference-miner",
            description="Uses app manifests for intelligent plan generation",
            supported_chains=[1, 8453],
            supported_intent_types=["swap"],
        )


# REQUIRED: The harness runner looks for this variable to instantiate your solver
SOLVER_CLASS = DiscoverySolver


def _state_params(state: IntentState) -> dict[str, Any]:
    typed = getattr(state, "typed_context", None)
    if typed is not None:
        raw = getattr(typed, "raw_params", None)
        if isinstance(raw, dict):
            return raw
    return state.raw_params_view()
