"""Follower determinism parity on the reactive-benchmark path.

The leader applies the BENCHMARK_EPOCH_BLOCK fork-pin in run_once(); a follower
re-verifying a candidate must benchmark at the SAME pinned block or the two
scores diverge for input reasons the ±15% tolerance then papers over. These
tests assert (1) the pin is honored and threaded to run_benchmark, (2) the
default (no env) stays live-head, and (3) the [reactive-determinism] log line —
the signal operators diff across the fleet — is emitted with the pinned block.
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from minotaur_subnet.api.routes.submissions.champion_consensus import (
    _reactive_benchmark_candidate,
)
from minotaur_subnet.harness.benchmark_worker import BenchmarkWorker


def _intents():
    from minotaur_subnet.harness.test_harness import (
        make_intent,
        make_snapshot,
        make_state,
    )

    return [(make_intent(), make_state(), make_snapshot())]


def _candidate():
    return MagicMock(
        submission_id="sub_pin",
        image_tag="solver-x:screening",
        image_id="",  # empty -> skips the image-id check (covered elsewhere)
    )


async def _run_reactive(captured: dict):
    """Drive _reactive_benchmark_candidate with everything below the worker
    faked out, capturing the kwargs run_benchmark receives."""

    async def fake_run_benchmark(session, intents, **kwargs):
        captured.update(kwargs)
        return []

    fake_session = MagicMock()
    fake_session.shutdown = AsyncMock()
    fake_orch = MagicMock()
    fake_orch.start_docker = AsyncMock(return_value=fake_session)

    with (
        patch(
            "minotaur_subnet.api.server_context.ctx",
            MagicMock(store=MagicMock()),
        ),
        patch(
            "minotaur_subnet.api.routes.submissions.champion_consensus.get_store",
            return_value=MagicMock(),
        ),
        patch(
            "minotaur_subnet.harness.orchestrator.run_benchmark",
            new=fake_run_benchmark,
        ),
        patch(
            "minotaur_subnet.harness.orchestrator.SolverOrchestrator",
            return_value=fake_orch,
        ),
        patch.object(
            BenchmarkWorker, "_load_benchmark_intents", return_value=_intents(),
        ),
        patch.object(
            BenchmarkWorker, "_build_score_fn",
            new=AsyncMock(return_value=AsyncMock()),
        ),
        patch.object(
            BenchmarkWorker, "_enrich_intents_with_manifests",
            side_effect=lambda self, i: i, autospec=True,
        ),
        patch.object(
            BenchmarkWorker, "_load_historical_scenarios",
            return_value=[],
        ),
    ):
        return await _reactive_benchmark_candidate(
            candidate=_candidate(), leader_score=0.5, round_id="round-pin",
        )


@pytest.mark.asyncio
async def test_reactive_benchmark_honors_epoch_block_pin(monkeypatch):
    monkeypatch.setenv("BENCHMARK_EPOCH_BLOCK", "46904887")
    captured: dict = {}
    await _run_reactive(captured)
    assert captured.get("fork_block") == 46904887, (
        "follower reactive benchmark must run at the leader's pinned block"
    )


@pytest.mark.asyncio
async def test_reactive_benchmark_default_stays_live_head(monkeypatch):
    # Opt-in contract: no env -> fork_block None -> live head, behavior unchanged.
    monkeypatch.delenv("BENCHMARK_EPOCH_BLOCK", raising=False)
    captured: dict = {}
    await _run_reactive(captured)
    assert "fork_block" in captured
    assert captured["fork_block"] is None


@pytest.mark.asyncio
async def test_reactive_determinism_log_emitted(monkeypatch, caplog):
    monkeypatch.setenv("BENCHMARK_EPOCH_BLOCK", "46904887")
    captured: dict = {}
    with caplog.at_level(logging.INFO):
        await _run_reactive(captured)
    line = next(
        (r.getMessage() for r in caplog.records
         if "[reactive-determinism]" in r.getMessage()),
        None,
    )
    assert line is not None, "the fleet-diffable determinism log line must be emitted"
    assert "fork_block=46904887" in line
    assert "round=round-pin" in line
