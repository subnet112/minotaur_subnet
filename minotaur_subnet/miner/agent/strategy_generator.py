"""Strategy code generation via Claude CLI.

Spawns Claude Code with MCP tools to autonomously research apps,
write strategy code, test it, and iterate on failures. Replaces
the stateless _call_llm() approach with an agentic workflow.
"""

from __future__ import annotations

import logging

from minotaur_subnet.miner.agent.app_discovery import AppContext
from minotaur_subnet.miner.agent.claude_runner import ClaudeRunner
from minotaur_subnet.miner.agent.score_tracker import ScoreFeedback

logger = logging.getLogger(__name__)


class StrategyGenerator:
    """Generates and improves Strategy code via Claude CLI.

    Delegates to ClaudeRunner which spawns `claude -p` with MCP tools.
    Claude autonomously reads contracts, searches docs, writes code,
    tests it, and iterates until passing.

    Args:
        strategy_dir: Directory for strategy files.
        validator_url: Validator URL for app discovery and scoring.
        anvil_rpc_url: Optional Anvil RPC URL for on-chain queries.
        timeout: Max seconds per Claude invocation.
        model: Claude model to use.
    """

    def __init__(
        self,
        strategy_dir: str = "strategies",
        validator_url: str = "http://localhost:8080",
        anvil_rpc_url: str | None = None,
        timeout: float = 300.0,
        model: str = "sonnet",
    ) -> None:
        self._runner = ClaudeRunner(
            strategy_dir=strategy_dir,
            validator_url=validator_url,
            anvil_rpc_url=anvil_rpc_url,
            timeout=timeout,
            model=model,
        )

    def generate(self, app_context: AppContext) -> "ClaudeResult":
        """Generate new strategy code from app metadata.

        Returns the full ClaudeResult so the caller can inspect tokens
        used / cost for budget accounting. ``result.strategy_code`` holds
        the generated source (None on failure).
        """
        logger.info(
            "Generating strategy for %s (%s)", app_context.app_id, app_context.name,
        )
        result = self._runner.generate_strategy(app_context)
        if result.strategy_code:
            logger.info("Strategy generated: %d chars", len(result.strategy_code))
        else:
            logger.warning("Strategy generation failed for %s", app_context.app_id)
        return result

    def improve(
        self,
        app_context: AppContext,
        current_code: str,
        feedback: ScoreFeedback,
    ) -> "ClaudeResult":
        """Improve existing strategy based on score feedback.

        Returns the full ClaudeResult; ``result.strategy_code`` holds
        the improved source (None on failure).
        """
        logger.info(
            "Improving strategy for %s (avg=%.3f, trend=%s)",
            app_context.app_id, feedback.avg_score, feedback.trend,
        )
        result = self._runner.improve_strategy(app_context, feedback)
        if result.strategy_code:
            logger.info(
                "Improved strategy generated: %d chars", len(result.strategy_code),
            )
        else:
            logger.warning("Strategy improvement failed for %s", app_context.app_id)
        return result
