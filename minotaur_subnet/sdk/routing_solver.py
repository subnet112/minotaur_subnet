"""RoutingSolver — dispatches plan generation to per-app Strategy instances.

A single IntentSolver that routes generate_plan() calls to registered
Strategy objects based on app_id. This is the solver submitted to the
validator via the git-based submission pipeline.

When no strategy matches, the fallback generates a minimal valid plan
(approve + generic swap) that will score low but not crash.

Usage::

    solver = RoutingSolver()
    solver.register_strategy(MyVaultStrategy())
    solver.initialize({"chain_ids": [1]})
    plan = solver.generate_plan(intent, state, snapshot)
"""

from __future__ import annotations

import logging
import time
from typing import Any

from minotaur_subnet.shared.types import (
    AppIntentDefinition,
    ExecutionPlan,
    Interaction,
    IntentState,
    QuoteResult,
)
from minotaur_subnet.sdk.intent_solver import IntentSolver, MarketSnapshot, SolverMetadata
from minotaur_subnet.sdk.strategy import Strategy

logger = logging.getLogger(__name__)

# Well-known addresses for fallback plan
_WETH = "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"
_DEPOSIT_SELECTOR = "0xd0e30db0"  # WETH.deposit()


class RoutingSolver(IntentSolver):
    """IntentSolver that dispatches to per-app Strategy instances.

    Register strategies via register_strategy(). For intents without a
    matching strategy, a minimal fallback plan is generated. The routing
    decision prefers ``state.typed_context.intent_function`` when available
    and falls back to ``state.control["_intent_function"]`` for compatibility.
    """

    def __init__(self) -> None:
        self._strategies: dict[str, Strategy] = {}  # app_id -> Strategy
        self._chain_ids: list[int] = []

    def register_strategy(self, strategy: Strategy) -> None:
        """Register a strategy for its APP_ID.

        Replaces any previously registered strategy for the same app_id.

        Args:
            strategy: Strategy instance with APP_ID set.

        Raises:
            ValueError: If strategy.APP_ID is empty.
        """
        if not strategy.APP_ID:
            raise ValueError("Strategy.APP_ID must be set")
        self._strategies[strategy.APP_ID] = strategy
        logger.info("Registered strategy for app %s", strategy.APP_ID)

    def remove_strategy(self, app_id: str) -> bool:
        """Remove a registered strategy. Returns True if it existed."""
        return self._strategies.pop(app_id, None) is not None

    def get_strategy(self, app_id: str) -> Strategy | None:
        """Get the registered strategy for an app_id, or None."""
        return self._strategies.get(app_id)

    def initialize(self, config: dict[str, Any]) -> None:
        self._chain_ids = config.get("chain_ids", [1])
        # Store RPC URLs so strategies can query on-chain pool state.
        # The benchmark harness passes these from the validator's Anvil
        # fork endpoints (sandboxed network, iptables-restricted).
        self._rpc_urls: dict[str, str] = {}
        raw_urls = config.get("rpc_urls", {})
        if isinstance(raw_urls, dict):
            self._rpc_urls = {str(k): v for k, v in raw_urls.items()}
        # Also set as env vars for legacy strategies that read os.environ
        # directly. New strategies should use Strategy.rpc_for(chain_id)
        # instead — that's the canonical contract.
        import os
        for chain_str, url in self._rpc_urls.items():
            chain_id = int(chain_str)
            if chain_id in (1, 31337):
                os.environ.setdefault("ANVIL_RPC_URL", url)
            elif chain_id == 8453:
                os.environ.setdefault("BASE_RPC_URL", url)
            elif chain_id == 964:
                os.environ.setdefault("BITTENSOR_EVM_RPC_URL", url)
        # Propagate the full config (rpc_urls included) to every
        # registered strategy so Strategy.rpc_for(chain_id) works inside
        # generate_plan. Validator passes URLs once, strategies read via
        # the base-class accessor — no hardcoding, no env-var lookup
        # convention to remember.
        for strategy in self._strategies.values():
            init = getattr(strategy, "initialize", None)
            if callable(init):
                try:
                    init(config)
                except Exception as exc:
                    logger.warning(
                        "Strategy %s.initialize raised: %s — strategy "
                        "will operate without RPC URLs",
                        getattr(strategy, "APP_ID", "?"), exc,
                    )

    def generate_plan(
        self,
        intent: AppIntentDefinition,
        state: IntentState,
        snapshot: MarketSnapshot | None = None,
    ) -> ExecutionPlan:
        """Route to the matching strategy, or use fallback."""
        intent_function = _intent_function_from_state(state)

        # Find matching strategy
        strategy = self._strategies.get(intent.app_id)
        if strategy and strategy.accepts(intent.app_id, intent_function):
            try:
                return strategy.generate_plan(intent, state, snapshot)
            except Exception as exc:
                logger.error(
                    "Strategy for %s failed: %s, using fallback",
                    intent.app_id, exc,
                )

        return self._fallback_plan(intent, state, snapshot)

    def quote(
        self,
        intent: AppIntentDefinition,
        state: IntentState,
        snapshot: MarketSnapshot | None = None,
    ) -> QuoteResult:
        """Dispatch to the matching strategy's quote(), or fall through."""
        intent_function = _intent_function_from_state(state)
        strategy = self._strategies.get(intent.app_id)
        if strategy and strategy.accepts(intent.app_id, intent_function):
            return strategy.quote(intent, state, snapshot)
        raise NotImplementedError("No strategy with quoting support for this app")

    def check_trigger(
        self,
        intent: AppIntentDefinition,
        state: IntentState,
        snapshot: MarketSnapshot | None = None,
    ) -> bool:
        """Delegate to strategy's check_trigger if available."""
        strategy = self._strategies.get(intent.app_id)
        if strategy:
            try:
                return strategy.check_trigger(intent, state, snapshot)
            except Exception as exc:
                logger.error(
                    "check_trigger for %s failed: %s", intent.app_id, exc,
                )
        return False

    def metadata(self) -> SolverMetadata:
        strategy_count = len(self._strategies)
        app_ids = sorted(self._strategies.keys())
        return SolverMetadata(
            name="routing-solver",
            version="1.0.0",
            author="minotaur-agent",
            description=f"Routes to {strategy_count} app strategies: {', '.join(app_ids[:5])}",
            supported_chains=self._chain_ids or [1],
            supported_intent_types=["swap", "vault", "limit_order"],
        )

    def _fallback_plan(
        self,
        intent: AppIntentDefinition,
        state: IntentState,
        snapshot: MarketSnapshot | None = None,
    ) -> ExecutionPlan:
        """Generate a minimal valid plan when no strategy matches.

        Creates a simple WETH deposit interaction. This will score low
        but passes structural validation.
        """
        chain_id = state.chain_id or (snapshot.chain_id if snapshot else 0) or 1
        snapshot_ts = snapshot.timestamp if snapshot else 0
        deadline = max(snapshot_ts + 300, int(time.time()) + 300)

        return ExecutionPlan(
            intent_id=intent.app_id,
            interactions=[
                Interaction(
                    target=_WETH,
                    value="1000000000000000",  # 0.001 ETH
                    call_data=_DEPOSIT_SELECTOR,
                    chain_id=chain_id,
                ),
            ],
            deadline=deadline,
            nonce=state.nonce,
            metadata={
                "plan_type": "fallback",
                "reason": "no_matching_strategy",
            },
        )


SOLVER_CLASS = RoutingSolver


def _intent_function_from_state(state: IntentState) -> str:
    return (
        getattr(state.typed_context, "intent_function", "")
        or state.control_view().get("_intent_function", "")
    )
