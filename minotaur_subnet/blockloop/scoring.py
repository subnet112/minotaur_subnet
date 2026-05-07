"""Scoring step of the block loop pipeline."""

from __future__ import annotations

import logging
from typing import Any

from minotaur_subnet.shared.types import (
    AppIntentDefinition,
    ExecutionPlan,
    IntentState,
    ScoreResult,
    SimulationResult,
)
from minotaur_subnet.shared.simulation import compute_mock_score

logger = logging.getLogger(__name__)


class PlanScorer:
    """Scores execution plans via the JS engine with mock fallback.

    Args:
        js_engine: JS scoring engine (optional, scores mocked if None).
    """

    def __init__(self, js_engine: Any = None) -> None:
        self.js_engine = js_engine

    async def score(
        self,
        app_id: str,
        app: AppIntentDefinition,
        plan: ExecutionPlan,
        simulation: SimulationResult,
        state: IntentState,
    ) -> ScoreResult | None:
        """Score a plan via JS engine. Falls back to mock score if no engine."""
        if self.js_engine is not None:
            try:
                # Ensure the app's JS is loaded
                if app_id not in self.js_engine._intents:
                    await self.js_engine.load_intent(app_id, app.js_code)
                return await self.js_engine.score(app_id, plan, simulation, state)
            except Exception as exc:
                logger.warning("JS scoring failed for %s: %s", app_id, exc)

        # Mock score based on plan quality
        mock_score = compute_mock_score(plan, state.raw_params_view())
        return ScoreResult(
            score=mock_score,
            valid=True,
            reason="mock scoring (no JS engine)",
            breakdown={"base": mock_score},
        )
