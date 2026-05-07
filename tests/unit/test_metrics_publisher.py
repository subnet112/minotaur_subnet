"""Tests for the CloudWatch metrics publisher."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from minotaur_subnet.api import metrics


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    for var in (
        "CLOUDWATCH_METRICS_ENABLED",
        "ETH_RPC_URL", "BASE_RPC_URL", "BITTENSOR_EVM_RPC_URL",
        "ANVIL_RPC_URL", "BITTENSOR_EVM_FORK_RPC_URL",
    ):
        monkeypatch.delenv(var, raising=False)
    yield


def test_enabled_env_parsing(monkeypatch):
    assert metrics._enabled() is False
    monkeypatch.setenv("CLOUDWATCH_METRICS_ENABLED", "1")
    assert metrics._enabled() is True
    monkeypatch.setenv("CLOUDWATCH_METRICS_ENABLED", "true")
    assert metrics._enabled() is True
    monkeypatch.setenv("CLOUDWATCH_METRICS_ENABLED", "0")
    assert metrics._enabled() is False


def test_collect_samples_emits_anvil_plus_disk(monkeypatch):
    monkeypatch.setenv("BASE_RPC_URL", "http://stub-base")
    # Force _anvil_healthy to return True for base, False elsewhere.
    with patch.object(metrics, "_anvil_healthy", side_effect=lambda rpc: rpc == "http://stub-base"):
        samples = metrics._collect_samples()
    by_name: dict[str, list] = {}
    for s in samples:
        by_name.setdefault(s["MetricName"], []).append(s)
    assert "AnvilHealthy" in by_name
    assert len(by_name["AnvilHealthy"]) == 3  # eth/base/btevm
    chains = {tuple(d["Value"] for d in s["Dimensions"])[0]: s["Value"] for s in by_name["AnvilHealthy"]}
    assert chains == {"eth": 0, "base": 1, "btevm": 0}
    assert "DiskUsagePercent" in by_name


def test_collect_includes_peer_and_blockloop_when_available(monkeypatch):
    peer = MagicMock()
    peer._last_peers_online = 3
    bl = MagicMock()
    bl._last_tick_seconds = 0.42

    with patch.object(metrics, "_anvil_healthy", return_value=False):
        samples = metrics._collect_samples(peer_network=peer, blockloop=bl)

    names = {s["MetricName"] for s in samples}
    assert "ConsensusPeersOnline" in names
    assert "BlockloopTickSeconds" in names


def test_collect_reports_dissent_counts(monkeypatch):
    from minotaur_subnet.consensus.dissent import record_dissent, RejectionCode
    record_dissent(
        peer_id="0x" + "1" * 40,
        code=RejectionCode.BENCHMARK_MISMATCH,
        subject_kind="round",
        subject_id="round-metrics-test",
        reason="",
    )
    with patch.object(metrics, "_anvil_healthy", return_value=False):
        samples = metrics._collect_samples()
    rejections = [s for s in samples if s["MetricName"] == "ConsensusRejectionsTotal"]
    assert rejections  # at least one (the BENCHMARK_MISMATCH we just recorded)


@pytest.mark.asyncio
async def test_publish_loop_no_op_when_disabled(monkeypatch):
    monkeypatch.delenv("CLOUDWATCH_METRICS_ENABLED", raising=False)
    # Should return immediately without calling boto3.
    with patch("boto3.client") as boto_client:
        await metrics.publish_loop(interval_seconds=0.01)
    boto_client.assert_not_called()


@pytest.mark.asyncio
async def test_publish_loop_posts_and_survives_errors(monkeypatch):
    monkeypatch.setenv("CLOUDWATCH_METRICS_ENABLED", "1")

    cw = MagicMock()
    call_count = {"n": 0}

    def fake_put_metric_data(**kw):
        call_count["n"] += 1
        if call_count["n"] == 2:
            raise ConnectionError("transient outage")
        return {}

    cw.put_metric_data = fake_put_metric_data

    async def _run():
        with patch("boto3.client", return_value=cw), \
             patch.object(metrics, "_anvil_healthy", return_value=False):
            await metrics.publish_loop(interval_seconds=0.01)

    # Let the loop run briefly then cancel.
    task = asyncio.create_task(_run())
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    # At least two ticks should have attempted — the failure in tick 2
    # must not have stopped the loop.
    assert call_count["n"] >= 2
