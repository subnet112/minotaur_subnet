"""Example IntentSolver submission.

This is a minimal working solver that miners can use as a starting point.
Fork the solver repo at https://github.com/subnet112/minotaur-solver
and modify solver.py + common/ modules to improve routing.

The solver repo includes:
- common/baseline_solver.py: Full routing engine (pool discovery, multi-hop)
- common/pool_math.py: Uniswap V3 swap math
- common/abi_utils.py: Swap calldata encoding
- strategies/: Per-app strategy implementations

All of this code is yours to modify. Beat the incumbent champion's
benchmark score to become the new champion.
"""

from minotaur_subnet.sdk.intent_solver import IntentSolver, SolverMetadata
from minotaur_subnet.shared.types import AppIntentDefinition, ExecutionPlan, IntentState


class ExampleSolver(IntentSolver):
    """Minimal solver skeleton.

    In practice, fork the solver repo and extend BaselineSwapSolver
    from common/baseline_solver.py — it provides RPC pool discovery,
    Uniswap V3 routing, and multi-hop via WETH.

    Override generate_plan() to add your own DEX routing.
    """

    def metadata(self) -> SolverMetadata:
        return SolverMetadata(
            name="example-solver",
            version="1.0.0",
            author="your-name",
        )

    def generate_plan(self, intent, state, snapshot=None) -> ExecutionPlan | None:
        # TODO: implement your routing logic
        return None


SOLVER_CLASS = ExampleSolver
