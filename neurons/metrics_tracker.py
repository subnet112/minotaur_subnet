"""Simple metrics tracker persisting window stats to disk."""
from __future__ import annotations

import json
import os
from typing import Dict, Any


class MetricsTracker:
    def __init__(self, base_dir: str, filename: str = "metrics.json"):
        self.path = os.path.join(base_dir, filename)
        self._metrics: Dict[str, Any] = {
            "windows_processed": 0,
            "successful_emits": 0,
            "failed_emits": 0,
            "events_processed": 0,
            "invalid_events_total": 0,
            "invalid_submissions_total": 0,
            "signature_failures_total": 0,
            "ttl_violations_total": 0,
            "latency_violations_total": 0,
            "last_window": None,
            "last_error": None,
            "error_counts": {},
        }
        self._load()

    def _load(self) -> None:
        try:
            if os.path.exists(self.path):
                with open(self.path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    self._metrics.update(data)
        except Exception:
            pass

    def _save(self) -> None:
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self._metrics, f, indent=2)
        except Exception:
            pass

    def record_window(self, window: str, events_count: int, emitted: bool, stats: Dict[str, Any] | None = None) -> None:
        self._metrics["windows_processed"] += 1
        if emitted:
            self._metrics["successful_emits"] += 1
        else:
            self._metrics["failed_emits"] += 1
        self._metrics["events_processed"] += int(events_count)
        self._metrics["last_window"] = window
        if stats:
            self._metrics["last_stats"] = stats
            self._metrics["invalid_events_total"] += int(stats.get("dropped_events", 0))
            self._metrics["invalid_submissions_total"] += int(stats.get("dropped_submissions", 0))
            self._metrics["signature_failures_total"] += int(stats.get("signature_failures", 0))
            self._metrics["ttl_violations_total"] += int(stats.get("ttl_violations", 0))
            self._metrics["latency_violations_total"] += int(stats.get("latency_violations", 0))
        self._save()

    def record_error(self, context: str, error: str) -> None:
        self._metrics["last_error"] = {"context": context, "error": error}
        error_counts = self._metrics.setdefault("error_counts", {})
        error_counts[context] = error_counts.get(context, 0) + 1
        self._save()

    @property
    def snapshot(self) -> Dict[str, Any]:
        return dict(self._metrics)


