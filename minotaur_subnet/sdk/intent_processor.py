"""Base IntentProcessor abstract class.

Miners extend IntentProcessor to build their solving strategies. The
IntentProcessor is the core abstraction of the Minotaur SDK -- it defines
how solvers generate execution plans for App Intents.

Key design principle: solvers never see the JS scoring function. They
submit plans, receive scores back, and must learn to optimize through
that black-box feedback loop.

Example usage::

    class MySwapSolver(IntentProcessor):
        def supported_intent_types(self) -> list[str]:
            return ["swap"]

        async def generate_plan(self, intent, state, context):
            if getattr(state, "typed_context", None) is not None:
                params = state.typed_context
            else:
                params = state.raw_params_view()

            # Your solving strategy here
            return ExecutionPlan(
                intent_id=intent.app_id,
                interactions=[...],
                deadline=context.timestamp + 300,
                nonce=state.nonce,
            )

        async def on_score_received(self, intent, plan, score):
            # Learn from feedback
            self.history.append({"plan": plan, "score": score.score})
"""

from abc import ABC, abstractmethod

from minotaur_subnet.shared.types import (
    AppIntentDefinition,
    ExecutionPlan,
    IntentState,
    ScoreResult,
)

from minotaur_subnet.sdk.processor_context import ProcessorContext


class IntentProcessor(ABC):
    """Base class for intent processing. Miners extend this to build solvers.

    The IntentProcessor is responsible for:
    1. Generating execution plans for intents
    2. Monitoring trigger conditions (for auto-triggered intents)
    3. Learning from score feedback to improve over time

    Miners compete by writing better IntentProcessor implementations.
    The generate_plan method is the core competition surface -- better
    plans earn higher scores, which translate to higher Bittensor weights
    and more TAO emissions.

    Subclasses MUST implement:
        - generate_plan: the core solving logic
        - supported_intent_types: what intent types this processor handles

    Subclasses MAY override:
        - on_score_received: to implement learning/feedback loops
        - check_trigger: for auto-triggered intents
    """

    @abstractmethod
    async def generate_plan(
        self,
        intent: AppIntentDefinition,
        state: IntentState,
        context: ProcessorContext,
    ) -> ExecutionPlan:
        """Generate an execution plan for the given intent.

        This is the core method miners implement. Given an intent definition
        and current on-chain state, produce the best possible execution plan.

        The plan will be scored by a hidden JS scoring function on validators.
        You never see the scoring logic -- you only receive scores back via
        on_score_received. Better scores mean higher Bittensor weights.

        Args:
            intent: The App Intent definition including type, configuration,
                and metadata describing what needs to be accomplished.
            state: Current on-chain state of the intent's contract, including
                the contract address, nonce, and any app-specific state.
                Prefer ``state.typed_context`` when present; ``state.raw_params``
                remains the raw compatibility payload.
            context: Execution context with chain info, prices, RPC access,
                historical scores, and DEX configuration.

        Returns:
            An ExecutionPlan with ordered interactions to fulfill the intent.

        Raises:
            Any exception will be caught by the framework and result in a
            score of 0.0 for this round.
        """

    async def on_score_received(
        self,
        intent: AppIntentDefinition,
        plan: ExecutionPlan,
        score: ScoreResult,
    ) -> None:
        """Called when a score is received for a plan you generated.

        Override this method to implement learning and feedback loops. The
        score is all you get -- you never see the scoring logic itself.
        This is by design: it prevents solvers from gaming the scoring
        function and forces genuine optimization.

        Common strategies:
        - Track score history to identify what works
        - Adjust parameters (slippage, gas, routing) based on feedback
        - Train ML models on (plan_features, score) pairs
        - A/B test different strategies

        Args:
            intent: The intent this plan was generated for.
            plan: The execution plan that was scored.
            score: The scoring result, including overall score (0.0-1.0),
                validity flag, and optional breakdown of score components.
        """

    async def check_trigger(
        self,
        intent: AppIntentDefinition,
        state: IntentState,
        context: ProcessorContext,
    ) -> bool:
        """For auto-triggered intents: check if conditions are met to execute.

        User-triggered intents (the default) do not need this -- they are
        initiated explicitly by users. Auto-triggered intents (e.g.,
        rebalancing, stop-loss, DCA) rely on solvers monitoring conditions
        and signaling when execution should happen.

        Override this for auto-triggered intent types. The default
        implementation returns False (no trigger).

        Args:
            intent: The intent definition to check triggers for.
            state: Current on-chain state of the intent contract.
            context: Execution context with chain info and prices.

        Returns:
            True if the intent should be triggered for execution now,
            False otherwise.
        """
        return False

    @abstractmethod
    def supported_intent_types(self) -> list[str]:
        """Return the intent types this processor can handle.

        Each processor declares which intent types it supports (e.g.,
        ["swap"], ["limit_order", "stop_loss"]). The ProcessorRegistry
        uses this to route intents to the appropriate processor.

        Returns:
            A list of intent type strings this processor handles.
        """
