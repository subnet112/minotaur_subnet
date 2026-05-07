"""Minotaur SDK for building intent solvers.

Provides abstract interfaces that miners implement:

v1 (IntentProcessor) — miners submit execution plans directly:
- IntentProcessor: Abstract base class with live RPC access
- ProcessorContext: Execution context with RPC URL and prices
- ProcessorRegistry: Registry for discovering and managing processors

v2 (IntentSolver) — miners submit solver code, validators benchmark:
- IntentSolver: Abstract base class with frozen MarketSnapshot
- MarketSnapshot: Point-in-time market data for deterministic execution
- SolverMetadata: Solver identification and capabilities

DEX-specific solver implementations (BaselineSwapSolver, SwapIntentProcessor,
TWAP, Rebalance, etc.) have been moved to the solver repo where miners own
and improve them. See https://github.com/subnet112/minotaur-solver

Example (v2)::

    from minotaur_subnet.sdk import IntentSolver, MarketSnapshot, SolverMetadata

    class MySolver(IntentSolver):
        def initialize(self, config):
            ...
        def generate_plan(self, intent, state, snapshot):
            ...
        def metadata(self):
            return SolverMetadata(name="my-solver", version="1.0.0", author="me")

    SOLVER_CLASS = MySolver
"""

# v1 interface
from minotaur_subnet.sdk.intent_processor import IntentProcessor
from minotaur_subnet.sdk.processor_context import ProcessorContext
from minotaur_subnet.sdk.registry import ProcessorRegistry

# v2 interface
from minotaur_subnet.sdk.intent_solver import IntentSolver, MarketSnapshot, SolverMetadata

__all__ = [
    # v1
    "IntentProcessor",
    "ProcessorContext",
    "ProcessorRegistry",
    # v2
    "IntentSolver",
    "MarketSnapshot",
    "SolverMetadata",
]
