"""CloudWatch metrics publisher for Minotaur production.

Single-instance scale — we push directly to CloudWatch rather than run a
Prometheus sidecar. Publishes a handful of gauges every 60s:

  AnvilHealthy{Chain=…}         — 1 if RPC responded within 5s
  ConsensusPeersOnline          — cached from last ValidatorPeerNetwork broadcast
  ConsensusQuorumMargin         — collected_approvals - quorum_required
  ConsensusRejectionsTotal{Reason=…} — counter from Phase 3 DissentLog
  BlockloopTickSeconds          — last tick duration (seconds)
  DiskUsagePercent              — root volume

Fail-open: if boto3 / CloudWatch is unreachable, we just log and keep
running. Alarms are created once via setup-instance.sh.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import time
from typing import Any

from minotaur_subnet.chains import registry

logger = logging.getLogger(__name__)

NAMESPACE = "Minotaur/Production"

# Metrics probe targets: proxy slug → chain id (the registry holds the RPC ladder).
_SLUG_TO_CHAIN: dict[str, int] = {"eth": 1, "base": 8453, "btevm": 964}


def _enabled() -> bool:
    return os.environ.get("CLOUDWATCH_METRICS_ENABLED", "0").strip().lower() in (
        "1", "true", "yes", "on",
    )


def _resolve_rpc(chain: str) -> str | None:
    chain_id = _SLUG_TO_CHAIN.get(chain)
    if chain_id is None:
        return None
    return registry.check_rpc(chain_id) or None


def _anvil_healthy(rpc_url: str) -> bool:
    """Cheap latest-block probe. True = RPC responded quickly with a number."""
    if not rpc_url:
        return False
    try:
        from minotaur_subnet.blockchain.web3_retry import build_retrying_web3
        w3 = build_retrying_web3(rpc_url, request_kwargs={"timeout": 5})
        block = w3.eth.block_number
        return int(block) > 0
    except Exception as exc:
        logger.debug("[metrics] anvil_healthy(%s) failed: %s", rpc_url, exc)
        return False


def _disk_percent(path: str = "/") -> float:
    try:
        usage = shutil.disk_usage(path)
        return 100.0 * usage.used / usage.total
    except Exception:
        return 0.0


def _collect_samples(
    *,
    peer_network: Any | None = None,
    blockloop: Any | None = None,
) -> list[dict[str, Any]]:
    """Shape the CloudWatch PutMetricData list for one tick."""
    import datetime as _dt
    now = _dt.datetime.now(_dt.timezone.utc)
    samples: list[dict[str, Any]] = []

    for chain in ("eth", "base", "btevm"):
        rpc = _resolve_rpc(chain)
        healthy = 1 if _anvil_healthy(rpc or "") else 0
        samples.append({
            "MetricName": "AnvilHealthy",
            "Dimensions": [{"Name": "Chain", "Value": chain}],
            "Value": healthy,
            "Unit": "None",
            "Timestamp": now,
        })

    samples.append({
        "MetricName": "DiskUsagePercent",
        "Value": _disk_percent("/"),
        "Unit": "Percent",
        "Timestamp": now,
    })

    if peer_network is not None:
        online = int(getattr(peer_network, "_last_peers_online", 0) or 0)
        samples.append({
            "MetricName": "ConsensusPeersOnline",
            "Value": online,
            "Unit": "Count",
            "Timestamp": now,
        })

    if blockloop is not None:
        last_tick = float(getattr(blockloop, "_last_tick_seconds", 0.0) or 0.0)
        if last_tick > 0:
            samples.append({
                "MetricName": "BlockloopTickSeconds",
                "Value": last_tick,
                "Unit": "Seconds",
                "Timestamp": now,
            })

    try:
        from minotaur_subnet.consensus.dissent import get_dissent_log
        for reason, count in get_dissent_log().counts().items():
            samples.append({
                "MetricName": "ConsensusRejectionsTotal",
                "Dimensions": [{"Name": "Reason", "Value": reason}],
                "Value": float(count),
                "Unit": "Count",
                "Timestamp": now,
            })
    except Exception as exc:
        logger.debug("[metrics] dissent log unavailable: %s", exc)

    return samples


async def publish_loop(
    *,
    peer_network: Any | None = None,
    blockloop: Any | None = None,
    interval_seconds: float = 60.0,
) -> None:
    """Background task: publish metrics every ``interval_seconds``.

    Silent no-op when CLOUDWATCH_METRICS_ENABLED is off. Fail-open on any
    boto3/CloudWatch error — publishing metrics should never be the reason
    consensus breaks.
    """
    if not _enabled():
        logger.info("[metrics] CLOUDWATCH_METRICS_ENABLED=0; publisher not started")
        return

    try:
        import boto3  # type: ignore
    except ImportError:
        logger.warning("[metrics] boto3 not installed; metrics disabled")
        return

    try:
        cw = boto3.client("cloudwatch",
                          region_name=os.environ.get("AWS_REGION", "us-east-1"))
    except Exception as exc:
        logger.warning("[metrics] could not construct CloudWatch client: %s", exc)
        return

    logger.info(
        "[metrics] publisher started (namespace=%s interval=%.0fs)",
        NAMESPACE, interval_seconds,
    )
    while True:
        try:
            samples = _collect_samples(
                peer_network=peer_network,
                blockloop=blockloop,
            )
            if samples:
                # Batch into chunks of 25 (CloudWatch max per PutMetricData)
                for i in range(0, len(samples), 25):
                    cw.put_metric_data(
                        Namespace=NAMESPACE,
                        MetricData=samples[i:i + 25],
                    )
        except Exception as exc:
            logger.warning("[metrics] publish failed: %s", exc)
        await asyncio.sleep(interval_seconds)
