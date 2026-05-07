"""Tests for ScoreTracker."""

import time
import pytest

from minotaur_subnet.miner.agent.score_tracker import (
    ScoreTracker,
    ScoreFeedback,
    ImprovementTarget,
)


@pytest.fixture
def tracker():
    return ScoreTracker(min_executions=3, cooldown=120.0)


class TestUpdate:
    def test_update_stores_stats(self, tracker):
        tracker.update("app-001", {
            "total_executions": 10,
            "avg_score": 0.8,
            "best_score": 0.95,
            "recent_scores": [0.7, 0.8, 0.9],
        })
        feedback = tracker.get_feedback("app-001")
        assert feedback.total_executions == 10
        assert feedback.avg_score == 0.8
        assert feedback.best_score == 0.95

    def test_update_overwrites_previous(self, tracker):
        tracker.update("app-001", {"total_executions": 5, "avg_score": 0.5})
        tracker.update("app-001", {"total_executions": 10, "avg_score": 0.8})
        feedback = tracker.get_feedback("app-001")
        assert feedback.total_executions == 10

    def test_update_stores_champion_and_scenarios(self, tracker):
        tracker.update("app-001", {
            "champion_score": 0.72,
            "scenario_scores": {"app-001:WETH_to_USDC": 0.8, "app-001:DAI_to_WETH": 0.3},
        })
        feedback = tracker.get_feedback("app-001")
        assert feedback.champion_score == 0.72
        assert feedback.target_score == pytest.approx(0.756)
        assert feedback.scenario_scores["app-001:DAI_to_WETH"] == 0.3


class TestGetUnderperformers:
    def test_no_strategy_highest_priority(self, tracker):
        targets = tracker.get_underperformers(["app-001", "app-002"])
        assert len(targets) == 2
        assert all(t.reason == "no_strategy" for t in targets)
        assert all(t.priority == 10.0 for t in targets)

    def test_always_returns_improve_target(self, tracker):
        """Even high-scoring apps get an improve target — no ceiling."""
        tracker.mark_has_strategy("app-001")
        tracker.update("app-001", {"total_executions": 10, "avg_score": 0.95})
        # Reset cooldown so it doesn't skip
        tracker._last_improved["app-001"] = 0
        targets = tracker.get_underperformers(["app-001"])
        assert len(targets) == 1
        assert targets[0].reason == "improve"
        assert targets[0].priority == pytest.approx(0.05)

    def test_low_score_higher_priority_than_high_score(self, tracker):
        tracker.mark_has_strategy("app-a")
        tracker.mark_has_strategy("app-b")
        tracker._last_improved["app-a"] = 0
        tracker._last_improved["app-b"] = 0
        tracker.update("app-a", {"total_executions": 10, "avg_score": 0.9})
        tracker.update("app-b", {"total_executions": 10, "avg_score": 0.3})
        targets = tracker.get_underperformers(["app-a", "app-b"])
        assert len(targets) == 2
        assert targets[0].app_id == "app-b"  # 0.7 headroom > 0.1 headroom
        assert targets[0].priority == pytest.approx(0.7)

    def test_no_strategy_beats_improve(self, tracker):
        tracker.mark_has_strategy("app-001")
        tracker._last_improved["app-001"] = 0
        tracker.update("app-001", {"total_executions": 10, "avg_score": 0.3})
        targets = tracker.get_underperformers(["app-001", "app-002"])
        assert len(targets) == 2
        assert targets[0].app_id == "app-002"
        assert targets[0].reason == "no_strategy"

    def test_empty_available_apps(self, tracker):
        targets = tracker.get_underperformers([])
        assert targets == []


class TestCooldown:
    def test_recently_improved_skipped(self, tracker):
        tracker.mark_has_strategy("app-001")
        tracker.update("app-001", {"total_executions": 10, "avg_score": 0.5})
        # Just marked — within cooldown
        targets = tracker.get_underperformers(["app-001"])
        assert len(targets) == 0

    def test_past_cooldown_included(self):
        tracker = ScoreTracker(cooldown=0.0)  # No cooldown
        tracker.mark_has_strategy("app-001")
        tracker.update("app-001", {"total_executions": 10, "avg_score": 0.5})
        targets = tracker.get_underperformers(["app-001"])
        assert len(targets) == 1
        assert targets[0].reason == "improve"

    def test_mark_has_strategy_resets_cooldown(self, tracker):
        tracker.mark_has_strategy("app-001")
        tracker.update("app-001", {"total_executions": 10, "avg_score": 0.5})
        # Within cooldown
        targets = tracker.get_underperformers(["app-001"])
        assert len(targets) == 0


class TestCoverageGap:
    def test_coverage_gap_boosts_priority(self):
        tracker = ScoreTracker(cooldown=0.0)
        tracker.mark_has_strategy("app-001")
        tracker.update("app-001", {
            "total_executions": 10,
            "avg_score": 0.8,
            "quote_stats": {"total_quotes": 10, "failed_quotes": 5},
        })
        targets = tracker.get_underperformers(["app-001"])
        assert len(targets) == 1
        assert targets[0].reason == "coverage_gap"
        # headroom (0.2) + boost (0.5) = 0.7
        assert targets[0].priority == pytest.approx(0.7)

    def test_coverage_gap_not_triggered_below_min_quotes(self):
        tracker = ScoreTracker(cooldown=0.0)
        tracker.mark_has_strategy("app-001")
        tracker.update("app-001", {
            "total_executions": 10,
            "avg_score": 0.8,
            "quote_stats": {"total_quotes": 3, "failed_quotes": 3},
        })
        targets = tracker.get_underperformers(["app-001"])
        assert len(targets) == 1
        assert targets[0].reason == "improve"  # Not coverage_gap

    def test_coverage_gap_not_triggered_on_low_failure_rate(self):
        tracker = ScoreTracker(cooldown=0.0)
        tracker.mark_has_strategy("app-001")
        tracker.update("app-001", {
            "total_executions": 10,
            "avg_score": 0.8,
            "quote_stats": {"total_quotes": 20, "failed_quotes": 2},
        })
        targets = tracker.get_underperformers(["app-001"])
        assert len(targets) == 1
        assert targets[0].reason == "improve"

    def test_coverage_gap_higher_priority_than_plain_improve(self):
        tracker = ScoreTracker(cooldown=0.0)
        tracker.mark_has_strategy("app-gap")
        tracker.mark_has_strategy("app-ok")
        # Same avg_score, but app-gap has coverage issues
        tracker.update("app-gap", {
            "avg_score": 0.8,
            "quote_stats": {"total_quotes": 10, "failed_quotes": 5},
        })
        tracker.update("app-ok", {"avg_score": 0.8})
        targets = tracker.get_underperformers(["app-gap", "app-ok"])
        assert len(targets) == 2
        assert targets[0].app_id == "app-gap"
        assert targets[0].reason == "coverage_gap"


class TestMarkStrategy:
    def test_mark_and_unmark(self):
        tracker = ScoreTracker(cooldown=0.0)
        tracker.mark_has_strategy("app-001")
        tracker.update("app-001", {"avg_score": 0.5})
        targets = tracker.get_underperformers(["app-001"])
        assert len(targets) == 1
        assert targets[0].reason == "improve"

        tracker.mark_no_strategy("app-001")
        targets = tracker.get_underperformers(["app-001"])
        assert len(targets) == 1
        assert targets[0].reason == "no_strategy"


class TestGetFeedback:
    def test_feedback_with_data(self, tracker):
        tracker.update("app-001", {
            "total_executions": 20,
            "avg_score": 0.6,
            "best_score": 0.9,
            "recent_scores": [0.5, 0.6, 0.7, 0.6, 0.5, 0.7, 0.8, 0.9],
        })
        feedback = tracker.get_feedback("app-001")
        assert isinstance(feedback, ScoreFeedback)
        assert feedback.app_id == "app-001"
        assert feedback.avg_score == 0.6
        assert feedback.best_score == 0.9
        assert len(feedback.recent_scores) <= 10

    def test_feedback_empty_app(self, tracker):
        feedback = tracker.get_feedback("nonexistent")
        assert feedback.total_executions == 0
        assert feedback.avg_score == 0.0
        assert feedback.recent_scores == []
        assert feedback.champion_score == 0.0
        assert feedback.scenario_scores == {}

    def test_feedback_trend_stable(self, tracker):
        tracker.update("app-001", {
            "recent_scores": [0.5, 0.5, 0.5, 0.5, 0.5, 0.5],
        })
        feedback = tracker.get_feedback("app-001")
        assert feedback.trend == "stable"

    def test_feedback_trend_improving(self, tracker):
        tracker.update("app-001", {
            "recent_scores": [0.3, 0.3, 0.3, 0.7, 0.8, 0.9],
        })
        feedback = tracker.get_feedback("app-001")
        assert feedback.trend == "improving"

    def test_feedback_trend_declining(self, tracker):
        tracker.update("app-001", {
            "recent_scores": [0.8, 0.9, 0.8, 0.3, 0.2, 0.3],
        })
        feedback = tracker.get_feedback("app-001")
        assert feedback.trend == "declining"

    def test_feedback_trend_few_scores(self, tracker):
        tracker.update("app-001", {
            "recent_scores": [0.5, 0.9],
        })
        feedback = tracker.get_feedback("app-001")
        assert feedback.trend == "stable"  # Too few for trend

    def test_feedback_includes_quote_stats(self, tracker):
        tracker.update("app-001", {
            "total_executions": 10,
            "avg_score": 0.6,
            "quote_stats": {
                "total_quotes": 20,
                "failed_quotes": 8,
                "recent_errors": ["error: timeout", "zero_output"],
            },
        })
        feedback = tracker.get_feedback("app-001")
        assert feedback.total_quotes == 20
        assert feedback.quote_failure_rate == 0.4
        assert len(feedback.recent_quote_errors) == 2

    def test_feedback_no_quote_stats_defaults(self, tracker):
        tracker.update("app-001", {"total_executions": 5, "avg_score": 0.5})
        feedback = tracker.get_feedback("app-001")
        assert feedback.total_quotes == 0
        assert feedback.quote_failure_rate == 0.0
        assert feedback.recent_quote_errors == []

    def test_feedback_champion_and_target(self, tracker):
        tracker.update("app-001", {
            "champion_score": 0.68,
            "scenario_scores": {"app-001:WETH_to_USDC": 0.82},
        })
        feedback = tracker.get_feedback("app-001")
        assert feedback.champion_score == 0.68
        assert feedback.target_score == pytest.approx(0.714)
        assert "app-001:WETH_to_USDC" in feedback.scenario_scores
