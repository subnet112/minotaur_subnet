"""Strategy ABC — per-app plan generation for the RoutingSolver.

A Strategy is a lightweight, app-specific plan generator. Unlike IntentSolver
(which handles lifecycle, serialization, benchmarking), Strategy focuses on
one thing: generating plans for a specific app.

The RoutingSolver dispatches generate_plan() calls to the matching Strategy
based on app_id and intent_function.

Example::

    class MyVaultStrategy(Strategy):
        APP_ID = "vault-abc123"
        INTENT_FUNCTIONS = ["buyDip"]

        def generate_plan(self, intent, state, snapshot):
            intent_function = (
                getattr(state.typed_context, "intent_function", "")
                or state.control_view().get("_intent_function", "")
            )
            return ExecutionPlan(
                intent_id=intent.app_id,
                interactions=[...],
                deadline=snapshot.timestamp + 300,
                nonce=state.nonce,
            )

    STRATEGY_CLASS = MyVaultStrategy
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from minotaur_subnet.shared.types import (
    AppIntentDefinition,
    ExecutionPlan,
    IntentState,
    QuoteResult,
)
from minotaur_subnet.sdk.intent_solver import MarketSnapshot


class Strategy(ABC):
    """Base class for per-app plan generation strategies.

    Each Strategy handles a single app (identified by APP_ID). Optionally,
    it can restrict itself to specific intent functions via INTENT_FUNCTIONS.

    Attributes:
        APP_ID: The app_id this strategy handles. Must be set by subclasses.
        INTENT_FUNCTIONS: List of intent function names this strategy handles.
            Empty list means handle all functions for the app.
    """

    APP_ID: str = ""
    INTENT_FUNCTIONS: list[str] = []

    # Per-chain RPC URLs supplied by the validator at runtime via
    # ``initialize(config)``. Strategies access them via ``rpc_for(chain_id)``
    # — they should NEVER hardcode RPC URLs in their source. The validator
    # is the source of truth: it knows where its Anvil forks live, the
    # strategy doesn't. Empty when no URL is available for a chain (e.g.
    # local testing) — the strategy can choose to fall back to a static
    # routing path or refuse to plan.
    _rpc_urls: dict[int, str]

    def __init__(self) -> None:
        # Default-init so subclasses that don't call super() still get a
        # sane empty dict. Important: this is per-instance, not class-level,
        # to avoid cross-strategy state leaking through the class attr.
        self._rpc_urls = {}

    def initialize(self, config: dict[str, Any]) -> None:
        """Called once before any plan generation.

        The validator/harness passes ``rpc_urls`` here keyed by chain ID
        (as strings or ints). Strategies that need additional setup can
        override but should call ``super().initialize(config)`` first.
        """
        raw_urls = (config or {}).get("rpc_urls") or {}
        if isinstance(raw_urls, dict):
            self._rpc_urls = {
                int(k): str(v) for k, v in raw_urls.items() if v
            }

    def rpc_for(self, chain_id: int) -> str:
        """Live RPC URL for ``chain_id``, supplied by the validator.

        Returns an empty string when no URL is registered for this chain
        — strategies should treat this as "no RPC, use static fallback"
        rather than crash.
        """
        return self._rpc_urls.get(int(chain_id), "")

    @abstractmethod
    def generate_plan(
        self,
        intent: AppIntentDefinition,
        state: IntentState,
        snapshot: MarketSnapshot | None = None,
    ) -> ExecutionPlan:
        """Generate an execution plan for the given intent.

        Args:
            intent: App Intent definition.
            state: Current on-chain state. Prefer ``state.typed_context`` when
                present. ``state.raw_params`` remains the canonical raw payload,
                including ``_intent_function``.
            snapshot: Optional market data. May be None when the solver
                builds its own data from RPC.

        Returns:
            ExecutionPlan with ordered interactions.
        """

    def quote(
        self,
        intent: AppIntentDefinition,
        state: IntentState,
        snapshot: MarketSnapshot | None = None,
    ) -> QuoteResult:
        """Compute a quote without generating a full execution plan.

        Override to provide fast quoting. Prefer RPC data, fall back
        to snapshot pool state.
        """
        raise NotImplementedError("This strategy does not support quoting")

    def check_trigger(
        self,
        intent: AppIntentDefinition,
        state: IntentState,
        snapshot: MarketSnapshot | None = None,
    ) -> bool:
        """For auto-triggered intents: should this intent execute now?

        Default returns False. Override for strategies that support
        auto-triggered orders.
        """
        return False

    def accepts(self, app_id: str, intent_function: str = "") -> bool:
        """Check if this strategy handles the given app_id and intent function.

        Args:
            app_id: The app identifier to check.
            intent_function: Optional intent function name to check.

        Returns:
            True if this strategy handles the given app/function combo.
        """
        if self.APP_ID != app_id:
            return False
        if self.INTENT_FUNCTIONS and intent_function:
            return intent_function in self.INTENT_FUNCTIONS
        return True
