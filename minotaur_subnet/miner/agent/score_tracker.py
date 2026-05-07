"""Score tracker — monitors per-app performance and identifies improvement targets.

Tracks execution scores from the validator, detects underperforming apps,
and provides feedback for LLM-driven strategy improvement.

Miners always work to improve — there is no "good enough" score. The goal
is to become champion by beating the incumbent by the dethrone margin (5%).

Improvement target reasons:
- no_strategy: App has no strategy at all (new app discovered)
- improve: Strategy exists but can be improved (always — priority = headroom)
- coverage_gap: High quote failure rate indicates unserved demand
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ScoreFeedback:
    """Feedback bundle for the LLM to improve a strategy."""
    app_id: str
    avg_score: float
    best_score: float
    recent_scores: list[float]
    total_executions: int
    trend: str  # "improving", "declining", "stable"
    # Champion target
    champion_score: float = 0.0
    target_score: float = 0.0  # champion_score * 1.05 (dethrone margin)
    # Per-scenario benchmark breakdown
    scenario_scores: dict[str, float] = field(default_factory=dict)
    # Quote demand signals
    quote_failure_rate: float = 0.0
    total_quotes: int = 0
    recent_quote_errors: list[str] = field(default_factory=list)
    # Last pre-submission scoring result (includes revert reasons)
    last_score: float = 0.0
    last_score_message: str = ""


@dataclass
class ImprovementTarget:
    """An app that needs a new or improved strategy."""
    app_id: str
    reason: str  # "no_strategy" | "improve" | "coverage_gap"
    avg_score: float
    priority: float  # Higher = more urgent


class ScoreTracker:
    """Tracks per-app scores and identifies apps needing improvement.

    Miners always compete to become champion. There is no score threshold
    at which work stops — the priority is based on headroom (1.0 - score)
    so the biggest gains get attention first.

    Args:
        min_executions: Minimum executions before considering an app
            for improvement (avoids reacting to noise).
        cooldown: Seconds after improving before re-targeting the same app.
            Prevents thrashing on the same app every cycle.
    """

    def __init__(
        self,
        min_executions: int = 3,
        cooldown: float = 120.0,
        # Legacy param kept for backward compat (ignored)
        improvement_threshold: float = 0.7,
        stale_after: float = 600.0,
    ) -> None:
        self.min_executions = min_executions
        self.cooldown = cooldown
        self._stats: dict[str, dict[str, Any]] = {}  # app_id -> stats dict
        self._known_strategies: set[str] = set()  # app_ids with strategies
        self._last_improved: dict[str, float] = {}  # app_id -> timestamp

    def update(self, app_id: str, stats: dict[str, Any]) -> None:
        """Update stats for an app from validator response.

        Args:
            app_id: The app identifier.
            stats: Stats dict from GET /apps/{app_id}/status, containing:
                total_executions, avg_score, best_score, recent_scores,
                quote_stats, champion_score, scenario_scores.
        """
        self._stats[app_id] = {
            "total_executions": stats.get("total_executions", 0),
            "avg_score": stats.get("avg_score", 0.0),
            "best_score": stats.get("best_score", 0.0),
            "recent_scores": stats.get("recent_scores", []),
            "quote_stats": stats.get("quote_stats", {}),
            "champion_score": stats.get("champion_score", 0.0),
            "scenario_scores": stats.get("scenario_scores", {}),
            "updated_at": time.time(),
        }

    def set_last_score_feedback(self, app_id: str, score: float, message: str) -> None:
        """Store the last scoring result for feedback to the LLM."""
        if app_id not in self._stats:
            self._stats[app_id] = {}
        self._stats[app_id]["last_pre_submission_score"] = score
        self._stats[app_id]["last_pre_submission_message"] = message

    def mark_has_strategy(self, app_id: str) -> None:
        """Mark an app as having a strategy registered."""
        self._known_strategies.add(app_id)
        self._last_improved[app_id] = time.time()

    def mark_no_strategy(self, app_id: str) -> None:
        """Mark an app as not having a strategy."""
        self._known_strategies.discard(app_id)
        self._last_improved.pop(app_id, None)

    def get_underperformers(
        self,
        available_app_ids: list[str],
    ) -> list[ImprovementTarget]:
        """Identify apps to work on, sorted by priority.

        Priority ordering:
        1. Apps with no strategy (priority 10.0)
        2. Apps with coverage gaps from quote failures (priority = headroom + 0.5 boost)
        3. All apps with strategies (priority = 1.0 - avg_score = headroom)

        Miners always have work — there's no score at which improvement stops.
        A cooldown prevents re-targeting the same app too quickly.

        Args:
            available_app_ids: List of app_ids from the validator.

        Returns:
            Sorted list of ImprovementTarget (highest priority first).
        """
        targets: list[ImprovementTarget] = []
        now = time.time()

        for app_id in available_app_ids:
            has_strategy = app_id in self._known_strategies
            stats = self._stats.get(app_id, {})
            avg_score = stats.get("avg_score", 0.0)

            if not has_strategy:
                targets.append(ImprovementTarget(
                    app_id=app_id,
                    reason="no_strategy",
                    avg_score=avg_score,
                    priority=10.0,
                ))
                continue

            # Respect cooldown — don't re-target the same app too quickly
            if self._in_cooldown(app_id, now):
                continue

            headroom = 1.0 - avg_score

            # Coverage gap gets a priority boost
            if self._has_coverage_gap(stats):
                targets.append(ImprovementTarget(
                    app_id=app_id,
                    reason="coverage_gap",
                    avg_score=avg_score,
                    priority=headroom + 0.5,
                ))
            else:
                targets.append(ImprovementTarget(
                    app_id=app_id,
                    reason="improve",
                    avg_score=avg_score,
                    priority=headroom,
                ))

        targets.sort(key=lambda t: t.priority, reverse=True)
        return targets

    def _in_cooldown(self, app_id: str, now: float) -> bool:
        """Check if an app was recently improved and should be skipped."""
        last = self._last_improved.get(app_id)
        if last is None:
            return False
        return (now - last) < self.cooldown

    @staticmethod
    def _has_coverage_gap(stats: dict[str, Any]) -> bool:
        """Check if quote failure rate indicates a coverage gap."""
        qs = stats.get("quote_stats", {})
        total_quotes = qs.get("total_quotes", 0)
        if total_quotes < 5:
            return False
        failed = qs.get("failed_quotes", 0)
        return (failed / total_quotes) > 0.3

    def get_feedback(self, app_id: str) -> ScoreFeedback:
        """Build a feedback bundle for the LLM to improve a strategy.

        Args:
            app_id: The app to get feedback for.

        Returns:
            ScoreFeedback with score history, champion target, and
            per-scenario breakdown.
        """
        stats = self._stats.get(app_id, {})
        recent = stats.get("recent_scores", [])
        total = stats.get("total_executions", 0)
        avg = stats.get("avg_score", 0.0)
        best = stats.get("best_score", 0.0)

        trend = self._compute_trend(recent)

        # Champion target
        champion_score = stats.get("champion_score", 0.0)
        target_score = champion_score * 1.05  # dethrone margin

        # Quote demand signals
        qs = stats.get("quote_stats", {})
        total_quotes = qs.get("total_quotes", 0)
        failed_quotes = qs.get("failed_quotes", 0)
        quote_failure_rate = failed_quotes / total_quotes if total_quotes > 0 else 0.0

        return ScoreFeedback(
            app_id=app_id,
            avg_score=avg,
            best_score=best,
            recent_scores=recent[-10:],
            total_executions=total,
            trend=trend,
            champion_score=champion_score,
            target_score=target_score,
            scenario_scores=stats.get("scenario_scores", {}),
            quote_failure_rate=quote_failure_rate,
            total_quotes=total_quotes,
            recent_quote_errors=qs.get("recent_errors", [])[-10:],
            last_score=stats.get("last_pre_submission_score", 0.0),
            last_score_message=stats.get("last_pre_submission_message", ""),
        )

    def _compute_trend(self, scores: list[float]) -> str:
        """Compute score trend from recent scores."""
        if len(scores) < 4:
            return "stable"
        mid = len(scores) // 2
        first_half = sum(scores[:mid]) / mid
        second_half = sum(scores[mid:]) / (len(scores) - mid)
        diff = second_half - first_half
        if diff > 0.05:
            return "improving"
        elif diff < -0.05:
            return "declining"
        return "stable"
