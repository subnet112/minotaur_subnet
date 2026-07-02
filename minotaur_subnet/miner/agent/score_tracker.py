"""Score tracker — monitors per-app performance and identifies improvement targets.

Tracks per-app feedback from the validator, detects underperforming apps, and
provides feedback for LLM-driven strategy improvement.

Post relative-cutover the validator's JS ``score`` is a 0/1 VALIDITY sentinel
(the authoritative delivered quality lives elsewhere), so the legacy 0..1
``avg_score`` quality number is meaningless (≈saturated). The authoritative
improvement signal is now the per-submission RELATIVE COUNTS the API serves on
the submission-status ``report.relative`` block:
``{better, worse, matched, new, compared, verdict}`` — how the miner's latest
submission compares to the champion PER ORDER. ``headroom`` is therefore the
fraction of comparable orders NOT yet beating the champion
(``(compared - better) / compared``), and ``trend`` is the change in the
``better/compared`` ratio across recent rounds. When no counts are available
(legacy/old submissions, or benched before the cutover) the tracker falls back
to an ``avg_score``-derived headroom (unit-robust: it accepts both the legacy
0..1 form and the new on-chain BPS form), so it never crashes on missing data.

Miners always work to improve — there is no "good enough". The goal is to become
champion under the relative NET-BETTER rule: out-deliver the incumbent on balance
(no order cut >1%, drop none, net wins exceed regressions).

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
    # Relative counts vs the champion (the authoritative post-cutover signal).
    # ``relative`` is the {better, worse, matched, new, compared, verdict} block
    # from the submission-status ``report.relative``; None when unavailable.
    relative: dict[str, Any] | None = None
    verdict: str = ""        # "dethrone" | "matched" | "behind" | ""
    scoring_mode: str = ""   # "relative" when the API reports counts
    # Fraction of comparable orders NOT yet beating the champion (0..1). The
    # counts-based replacement for the old ``1.0 - avg_score`` headroom.
    relative_headroom: float = 1.0


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
        prev = self._stats.get(app_id, {})
        self._stats[app_id] = {
            "total_executions": stats.get("total_executions", 0),
            "avg_score": stats.get("avg_score", 0.0),
            "best_score": stats.get("best_score", 0.0),
            "recent_scores": stats.get("recent_scores", []),
            "quote_stats": stats.get("quote_stats", {}),
            "champion_score": stats.get("champion_score", 0.0),
            "scenario_scores": stats.get("scenario_scores", {}),
            "scoring_mode": stats.get("scoring_mode", prev.get("scoring_mode", "")),
            # Relative counts live on the submission report (a different endpoint),
            # not on /status — carry forward whatever was last set so a /status
            # refresh doesn't wipe the authoritative signal.
            "relative": prev.get("relative"),
            "relative_history": prev.get("relative_history", []),
            "updated_at": time.time(),
        }

    def set_relative_counts(
        self,
        app_id: str,
        counts: dict[str, Any] | None,
        scoring_mode: str = "relative",
    ) -> None:
        """Store the per-submission relative COUNTS vs the champion.

        Fed from the loop's benchmark-feedback poll, which reads
        ``report.relative`` ({better, worse, matched, new, compared, verdict})
        off the submission-status endpoint. This is the authoritative
        post-cutover improvement signal that replaces the saturated 0..1 score.
        Appends the ``better/compared`` ratio to a rolling history so the trend
        can be computed across rounds. A None/empty ``counts`` is tolerated
        (graceful no-op on the history) so legacy/old submissions don't crash.
        """
        st = self._stats.setdefault(app_id, {})
        st["scoring_mode"] = scoring_mode or st.get("scoring_mode", "")
        if not counts:
            st["relative"] = None
            return
        st["relative"] = dict(counts)
        compared = int(counts.get("compared", 0) or 0)
        better = int(counts.get("better", 0) or 0)
        ratio = (better / compared) if compared > 0 else 0.0
        hist = st.setdefault("relative_history", [])
        hist.append(ratio)
        st["relative_history"] = hist[-10:]

    @staticmethod
    def _headroom(stats: dict[str, Any]) -> float:
        """Fraction (0..1) of comparable orders NOT yet beating the champion.

        Counts-first: ``(compared - better) / compared`` when relative counts
        are present. Falls back to an ``avg_score``-derived headroom when they
        aren't — unit-robust so it works whether ``avg_score`` is the legacy
        0..1 quality number or the new on-chain BPS value (>1.0 ⇒ BPS).
        """
        rel = stats.get("relative")
        if rel and int(rel.get("compared", 0) or 0) > 0:
            compared = int(rel["compared"])
            better = int(rel.get("better", 0) or 0)
            return max(0.0, (compared - better) / compared)
        avg = float(stats.get("avg_score") or 0.0)
        if avg > 1.0:  # on-chain BPS units (0..10000)
            return max(0.0, min(1.0, (10000.0 - avg) / 10000.0))
        return max(0.0, 1.0 - avg)

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
        3. All apps with strategies (priority = headroom)

        ``headroom`` is counts-based — the fraction of orders NOT yet beating
        the champion (``(compared - better) / compared``) — falling back to an
        ``avg_score``-derived value when no relative counts are available yet.

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

            headroom = self._headroom(stats)

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

        # Trend is counts-based when relative history exists (change in the
        # better/compared ratio); else fall back to the recent-score trend.
        rel_history = stats.get("relative_history", [])
        if len(rel_history) >= 2:
            trend = self._compute_trend(rel_history)
        else:
            trend = self._compute_trend(recent)

        # Champion target: the champion has NO absolute score post-cutover (it is
        # the relative baseline; the API serves champion_score as null/0), so the
        # numeric ``target_score`` is vestigial — the real bar is the relative
        # NET-BETTER verdict (verdict == dethrone). Kept defensively for back-compat.
        champion_score = float(stats.get("champion_score") or 0.0)
        target_score = champion_score * 1.05  # dethrone margin (legacy display)

        # Relative counts vs the champion (authoritative post-cutover signal).
        relative = stats.get("relative")
        verdict = (relative or {}).get("verdict", "") if relative else ""

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
            relative=relative,
            verdict=verdict,
            scoring_mode=stats.get("scoring_mode", ""),
            relative_headroom=self._headroom(stats),
        )

    def _compute_trend(self, scores: list[float]) -> str:
        """Compute a trend from a recent series (scores OR better/compared ratios).

        Compares the mean of the older half vs the newer half. The threshold is
        proportional with a 0.05 floor so the same logic works on a 0..1 series
        (ratios, legacy scores) and on an on-chain BPS series (0..10000) without
        a BPS delta of a few points reading as a swing.
        """
        if len(scores) < 4:
            return "stable"
        mid = len(scores) // 2
        first_half = sum(scores[:mid]) / mid
        second_half = sum(scores[mid:]) / (len(scores) - mid)
        diff = second_half - first_half
        threshold = max(0.05, abs(first_half) * 0.05)
        if diff > threshold:
            return "improving"
        elif diff < -threshold:
            return "declining"
        return "stable"
