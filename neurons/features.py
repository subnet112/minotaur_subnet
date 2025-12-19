"""Feature extraction from events to per-hotkey metrics.

Computes metrics like participations, wins, filled_notional, p95 latency, reverts.
"""
from __future__ import annotations

from typing import Dict, Any, List, Tuple, DefaultDict
from collections import defaultdict
import math


def _percentile(values: List[float], p: float) -> float:
    if not values:
        return 0.0
    arr = sorted(values)
    if len(arr) == 1:
        return arr[0]
    k = (len(arr) - 1) * p
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return arr[int(k)]
    d0 = arr[f] * (c - k)
    d1 = arr[c] * (k - f)
    return d0 + d1


def compute_metrics(events: List[Dict[str, Any]]) -> Dict[str, Dict[str, float]]:
    """Aggregate per-hotkey metrics from a list of event dicts.

    Returns a dict: hotkey -> metrics dict with keys:
        - participations
        - wins
        - filled_notional
        - p95_latency_ms
        - reverts
    """
    participations: DefaultDict[str, int] = defaultdict(int)
    wins: DefaultDict[str, int] = defaultdict(int)
    filled_notional: DefaultDict[str, float] = defaultdict(float)
    latencies: DefaultDict[str, List[float]] = defaultdict(list)
    reverts: DefaultDict[str, int] = defaultdict(int)

    for ev in events:
        submissions = ev.get("submissions") or []
        selection = ev.get("selection") or {}
        outcome = ev.get("outcome") or {}

        winner_hotkey = selection.get("winner_hotkey")
        # Count participations and latency per submission
        for sub in submissions:
            hotkey = sub.get("hotkey")
            if not isinstance(hotkey, str):
                continue
            participations[hotkey] += 1
            lat = sub.get("latency_ms")
            try:
                if lat is not None:
                    latencies[hotkey].append(float(lat))
            except Exception:
                pass

        # Wins
        if isinstance(winner_hotkey, str):
            wins[winner_hotkey] += 1

        # Filled notional and reverts attribution to winner only
        status = outcome.get("status")
        if isinstance(winner_hotkey, str):
            if status == "filled" or status == "partial":
                try:
                    size = float(outcome.get("filled_size", 0.0))
                    price = float(outcome.get("fill_price", 0.0))
                    notional = max(0.0, size * price)
                    filled_notional[winner_hotkey] += notional
                except Exception:
                    pass
            elif status == "reverted":
                reverts[winner_hotkey] += 1

    # Compose per-hotkey metrics
    metrics: Dict[str, Dict[str, float]] = {}
    hotkeys = set(participations.keys()) | set(wins.keys()) | set(filled_notional.keys()) | set(latencies.keys()) | set(reverts.keys())
    for hk in hotkeys:
        p = participations.get(hk, 0)
        w = wins.get(hk, 0)
        fnot = float(filled_notional.get(hk, 0.0))
        p95 = float(_percentile(latencies.get(hk, []), 0.95))
        rv = reverts.get(hk, 0)
        metrics[hk] = {
            "participations": float(p),
            "wins": float(w),
            "filled_notional": fnot,
            "p95_latency_ms": p95,
            "reverts": float(rv),
        }
    return metrics


