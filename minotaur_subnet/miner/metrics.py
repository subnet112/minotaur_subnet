"""Per-miner CloudWatch metrics publisher.

Each miner container publishes to namespace Minotaur/Production with a
``Miner`` dimension, so the same dashboard shows alpha and charlie side
by side. Fails open on any boto3/CloudWatch error — metrics should never
be the reason mining stops.

Metrics:
  MinerCycleSkipped{Miner, Reason}    counter
  MinerSubmissionsSent{Miner}         counter
  MinerLastScore{Miner}               gauge
  MinerTokensUsedToday{Miner}         gauge (from cost_gate state)

Counters accumulate in memory during the process lifetime; on restart
they reset (we don't need a lifetime total, just a "this instance's
recent activity" signal).
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import logging
import os
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

NAMESPACE = "Minotaur/Production"


@dataclass
class MinerCounters:
    """In-memory counters updated by the AgentLoop and drained each tick."""
    cycles_skipped_by_reason: dict[str, int] = field(default_factory=dict)
    submissions_sent: int = 0
    last_score: float = 0.0

    def record_skip(self, reason: str) -> None:
        self.cycles_skipped_by_reason[reason] = (
            self.cycles_skipped_by_reason.get(reason, 0) + 1
        )

    def record_submission(self) -> None:
        self.submissions_sent += 1

    def record_score(self, score: float) -> None:
        self.last_score = float(score)


def _enabled() -> bool:
    return os.environ.get("CLOUDWATCH_METRICS_ENABLED", "0").strip().lower() in (
        "1", "true", "yes", "on",
    )


def _build_samples(*, miner_id: str, counters: MinerCounters, cost_gate: Any | None) -> list[dict[str, Any]]:
    now = _dt.datetime.now(_dt.timezone.utc)
    dim = {"Name": "Miner", "Value": miner_id}
    samples: list[dict[str, Any]] = [
        {
            "MetricName": "MinerSubmissionsSent",
            "Dimensions": [dim],
            "Value": float(counters.submissions_sent),
            "Unit": "Count",
            "Timestamp": now,
        },
        {
            "MetricName": "MinerLastScore",
            "Dimensions": [dim],
            "Value": float(counters.last_score),
            "Unit": "None",
            "Timestamp": now,
        },
    ]
    for reason, count in counters.cycles_skipped_by_reason.items():
        samples.append({
            "MetricName": "MinerCycleSkipped",
            "Dimensions": [
                {"Name": "Miner", "Value": miner_id},
                {"Name": "Reason", "Value": reason},
            ],
            "Value": float(count),
            "Unit": "Count",
            "Timestamp": now,
        })
    if cost_gate is not None:
        try:
            samples.append({
                "MetricName": "MinerTokensUsedToday",
                "Dimensions": [dim],
                "Value": float(cost_gate.state.token_budget_used),
                "Unit": "Count",
                "Timestamp": now,
            })
        except Exception:
            pass
    # Disk usage on the container's root FS — under Docker overlay this
    # reports the host filesystem's free space, so this is the host-level
    # disk-fill signal we alarm on. Two prod outages so far were impaired
    # instances after a runaway container filled the EC2 root volume; a
    # CloudWatch alarm at 80% catches it before the host hangs.
    try:
        st = os.statvfs("/")
        used_pct = (1.0 - (st.f_bavail / st.f_blocks)) * 100.0 if st.f_blocks else 0.0
        samples.append({
            "MetricName": "DiskUsagePercent",
            "Dimensions": [dim],
            "Value": float(used_pct),
            "Unit": "Percent",
            "Timestamp": now,
        })
    except OSError:
        pass
    return samples


async def publish_loop(
    *,
    miner_id: str,
    counters: MinerCounters,
    cost_gate: Any | None = None,
    interval_seconds: float = 60.0,
) -> None:
    """Background task; publishes MinerX metrics every interval_seconds."""
    if not _enabled():
        logger.info("[miner-metrics] CLOUDWATCH_METRICS_ENABLED=0; publisher not started")
        return

    try:
        import boto3  # type: ignore
    except ImportError:
        logger.warning("[miner-metrics] boto3 not installed; metrics disabled")
        return

    try:
        cw = boto3.client("cloudwatch",
                          region_name=os.environ.get("AWS_REGION", "us-east-1"))
    except Exception as exc:
        logger.warning("[miner-metrics] CloudWatch client construction failed: %s", exc)
        return

    logger.info(
        "[miner-metrics] publisher started (miner=%s namespace=%s interval=%.0fs)",
        miner_id, NAMESPACE, interval_seconds,
    )
    while True:
        try:
            samples = _build_samples(
                miner_id=miner_id, counters=counters, cost_gate=cost_gate,
            )
            if samples:
                for i in range(0, len(samples), 25):
                    cw.put_metric_data(
                        Namespace=NAMESPACE,
                        MetricData=samples[i:i + 25],
                    )
        except Exception as exc:
            logger.warning("[miner-metrics] publish failed: %s", exc)
        await asyncio.sleep(interval_seconds)
