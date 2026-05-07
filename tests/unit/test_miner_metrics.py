"""Tests for the per-miner CloudWatch metrics publisher."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from minotaur_subnet.miner import metrics as miner_metrics


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    monkeypatch.delenv("CLOUDWATCH_METRICS_ENABLED", raising=False)
    yield


def test_counters_accumulate():
    c = miner_metrics.MinerCounters()
    c.record_skip("CHAMPION_UNCHALLENGED")
    c.record_skip("CHAMPION_UNCHALLENGED")
    c.record_skip("PLATEAU")
    c.record_submission()
    c.record_score(0.87)

    assert c.cycles_skipped_by_reason == {"CHAMPION_UNCHALLENGED": 2, "PLATEAU": 1}
    assert c.submissions_sent == 1
    assert c.last_score == 0.87


def test_build_samples_tags_with_miner_dimension():
    c = miner_metrics.MinerCounters()
    c.record_submission()
    c.record_score(0.5)
    c.record_skip("PLATEAU")

    samples = miner_metrics._build_samples(
        miner_id="alpha", counters=c, cost_gate=None,
    )
    # Every sample must carry the Miner=alpha dimension.
    for s in samples:
        assert any(d["Name"] == "Miner" and d["Value"] == "alpha" for d in s["Dimensions"])
    names = {s["MetricName"] for s in samples}
    assert "MinerSubmissionsSent" in names
    assert "MinerLastScore" in names
    assert "MinerCycleSkipped" in names


def test_build_samples_includes_token_budget_when_cost_gate_present():
    c = miner_metrics.MinerCounters()
    gate = MagicMock()
    gate.state.token_budget_used = 42_000

    samples = miner_metrics._build_samples(
        miner_id="charlie", counters=c, cost_gate=gate,
    )
    token = [s for s in samples if s["MetricName"] == "MinerTokensUsedToday"]
    assert len(token) == 1
    assert token[0]["Value"] == 42_000
    assert token[0]["Dimensions"] == [{"Name": "Miner", "Value": "charlie"}]


@pytest.mark.asyncio
async def test_publish_loop_noop_when_disabled():
    with patch("boto3.client") as boto_client:
        await miner_metrics.publish_loop(
            miner_id="alpha",
            counters=miner_metrics.MinerCounters(),
            interval_seconds=0.01,
        )
    boto_client.assert_not_called()


@pytest.mark.asyncio
async def test_publish_loop_posts_to_cloudwatch(monkeypatch):
    monkeypatch.setenv("CLOUDWATCH_METRICS_ENABLED", "1")

    cw = MagicMock()
    call_count = {"n": 0}

    def fake_put_metric_data(**kwargs):
        call_count["n"] += 1
        assert kwargs["Namespace"] == "Minotaur/Production"
        # every data point has Miner dimension
        for md in kwargs["MetricData"]:
            assert any(d["Name"] == "Miner" for d in md.get("Dimensions", []))
        return {}

    cw.put_metric_data = fake_put_metric_data

    counters = miner_metrics.MinerCounters()
    counters.record_submission()
    counters.record_score(0.9)

    async def _run():
        with patch("boto3.client", return_value=cw):
            await miner_metrics.publish_loop(
                miner_id="alpha",
                counters=counters,
                interval_seconds=0.01,
            )

    task = asyncio.create_task(_run())
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert call_count["n"] >= 2
