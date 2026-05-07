"""Plan generation step of the block loop pipeline."""

from __future__ import annotations

import inspect
import logging
from typing import Any

from minotaur_subnet.shared.types import (
    AppIntentDefinition,
    ExecutionPlan,
    IntentState,
)
from minotaur_subnet.blockloop.utils import _build_fallback_plan

logger = logging.getLogger(__name__)


class PlanGenerator:
    """Generates execution plans from the current solver.

    Falls back to a minimal stub plan when no solver is loaded,
    so the order can still flow through simulation and scoring.

    Args:
        solver: IntentSolver instance (optional, uses fallback if None).
    """

    def __init__(self, solver: Any = None) -> None:
        self.solver = solver

    async def generate(
        self,
        app: AppIntentDefinition,
        state: IntentState,
        snapshot: Any,
    ) -> ExecutionPlan | None:
        """Generate a plan using the current solver.

        Falls back to a minimal stub plan when no solver is loaded,
        so the order can still flow through simulation and scoring.
        """
        if self.solver is None:
            return _build_fallback_plan(state)

        try:
            plan = self.solver.generate_plan(app, state, snapshot)
            if inspect.isawaitable(plan):
                plan = await plan
            return plan
        except Exception as exc:
            logger.error("Solver failed: %s", exc, exc_info=True)
            return None
