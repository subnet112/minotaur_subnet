"""Scoring strategy for converting per-hotkey metrics into scores.

Provides a default v1 strategy with configurable weights and EMA smoothing.
"""
from __future__ import annotations

import os
from typing import Dict, Any, Tuple
import math


def _safe_get(d: Dict[str, float], k: str, default: float = 0.0) -> float:
    try:
        return float(d.get(k, default))
    except Exception:
        return default


def _min_max_normalize(values: Dict[str, float]) -> Dict[str, float]:
    if not values:
        return {}
    vmin = min(values.values())
    vmax = max(values.values())
    if math.isclose(vmin, vmax):
        # All equal; return 0.5 for everyone to avoid division by zero
        return {k: 0.5 for k in values}
    span = (vmax - vmin) or 1.0
    return {k: (v - vmin) / span for k, v in values.items()}


class DefaultScoringV1:
    def __init__(self):
        # Weights
        self.w_volume = float(os.getenv("SCORING_W_VOLUME", "0.5"))
        self.w_win = float(os.getenv("SCORING_W_WIN", "0.3"))
        self.w_latency = float(os.getenv("SCORING_W_LATENCY", "0.2"))
        self.w_reverts = float(os.getenv("SCORING_W_REVERTS", "0.5"))  # penalty weight
        # EMA smoothing
        self.ema_alpha = float(os.getenv("SCORING_EMA_ALPHA", "0"))
        # Guardrails
        self.min_participations = int(os.getenv("SCORING_MIN_PARTICIPATIONS", "1"))
        self.min_wins = int(os.getenv("SCORING_MIN_WINS", "0"))
        self.score_cap = float(os.getenv("SCORING_SCORE_CAP", "1.0"))
        self.max_revert_ratio = float(os.getenv("SCORING_MAX_REVERT_RATIO", "0.5"))

    def compute_scores(
        self,
        metrics_by_hotkey: Dict[str, Dict[str, float]],
        prev_scores: Dict[str, float],
    ) -> Tuple[Dict[str, float], Dict[str, float]]:
        # Prepare raw metric series
        raw_volume: Dict[str, float] = {}
        raw_winrate: Dict[str, float] = {}
        raw_latency: Dict[str, float] = {}
        raw_reverts_rate: Dict[str, float] = {}

        for hk, m in metrics_by_hotkey.items():
            part = max(0.0, _safe_get(m, "participations"))
            wins = max(0.0, _safe_get(m, "wins"))
            reverts = max(0.0, _safe_get(m, "reverts"))
            filled_notional = max(0.0, _safe_get(m, "filled_notional"))
            p95_latency = max(0.0, _safe_get(m, "p95_latency_ms"))

            raw_volume[hk] = math.log1p(filled_notional)
            raw_winrate[hk] = (wins / part) if part > 0 else 0.0
            raw_latency[hk] = p95_latency  # lower is better; invert later
            raw_reverts_rate[hk] = (reverts / max(1.0, wins)) if wins > 0 else 0.0

        # Normalize metrics [0,1]
        vol = _min_max_normalize(raw_volume)
        win = _min_max_normalize(raw_winrate)
        lat = _min_max_normalize(raw_latency)

        # Invert latency (lower latency -> higher score)
        lat_inv = {k: 1.0 - v for k, v in lat.items()} if lat else {}

        # Combine
        combined: Dict[str, float] = {}
        keys = set(metrics_by_hotkey.keys())
        for hk in keys:
            s = 0.0
            part = max(0.0, _safe_get(metrics_by_hotkey[hk], "participations"))
            wins = max(0.0, _safe_get(metrics_by_hotkey[hk], "wins"))
            if part < self.min_participations or wins < self.min_wins:
                combined[hk] = 0.0
                continue
            s += self.w_volume * vol.get(hk, 0.0)
            s += self.w_win * win.get(hk, 0.0)
            s += self.w_latency * lat_inv.get(hk, 0.0)
            s -= self.w_reverts * raw_reverts_rate.get(hk, 0.0)
            if self.max_revert_ratio > 0 and raw_reverts_rate.get(hk, 0.0) > self.max_revert_ratio:
                combined[hk] = 0.0
                continue
            combined[hk] = max(0.0, min(self.score_cap, s))

        # EMA smoothing
        if self.ema_alpha > 0 and prev_scores:
            alpha = max(0.0, min(1.0, self.ema_alpha))
            smoothed: Dict[str, float] = {}
            all_keys = set(prev_scores.keys()) | set(combined.keys())
            for hk in all_keys:
                cur = combined.get(hk, 0.0)
                prev = prev_scores.get(hk, 0.0)
                smoothed[hk] = alpha * cur + (1 - alpha) * prev
            return combined, smoothed

        return combined, combined


