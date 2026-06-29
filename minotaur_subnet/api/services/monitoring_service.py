"""App monitoring service functions."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from minotaur_subnet.store import AppIntentStore

import logging

logger = logging.getLogger(__name__)


def monitor_app(
    store: AppIntentStore,
    app_id: str,
) -> dict[str, Any]:
    """Get real-time execution monitoring data for an App Intent.

    Returns the best recent scores, recent execution history, and
    per-solver performance statistics.

    Post relative-cutover the underlying ``record_execution`` stores the
    on-chain scoreIntent BPS (0..10000, delivered quality), so ``best_scores``
    and ``avg_score`` here are in BPS — NOT the saturated JS 0..1 sentinel that
    would otherwise pin every stat at ≈1.0.

    Args:
        app_id: The app to monitor.

    Returns:
        Dict with best_scores, recent_executions summary, and solver_stats.
    """
    if not app_id:
        return {"error": "app_id is required"}

    definition = store.get_app(app_id)
    if definition is None:
        return {"error": f"App not found: {app_id}"}

    stats = store.get_stats(app_id)
    recent_scores = stats.get("recent_scores", [])

    # Derive monitoring data from stats
    sorted_recent = sorted(recent_scores, reverse=True)
    best_scores = sorted_recent[:5] if sorted_recent else []

    return {
        "app_id": app_id,
        "name": definition.name,
        "best_scores": [round(s, 4) for s in best_scores],
        "recent_executions": {
            "total": stats["total_executions"],
            "successful": stats["successful_executions"],
            # avg_score blends in failures (~0 BPS); avg_success_score is the avg
            # quality of SUCCESSFUL fills only. Both in on-chain BPS (0..10000).
            "avg_score": round(stats["avg_score"], 4),
            "avg_success_score": round(stats["avg_success_score"], 4),
            "last_triggered": stats["last_triggered"],
        },
        "solver_stats": {
            "note": "Per-solver stats will be available when the engine agent reports individual solver results."
        },
    }
