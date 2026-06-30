"""Tests for the DISABLE_CHAMPION_ADOPTION safety gate.

When the gate is set, submissions are scored normally (benchmark + scorecard +
feedback report still run) but NO challenger is ever adopted as champion. This
lets us exercise the real scoring pipeline on a live validator without a test
submission accidentally winning the champion slot and redirecting emissions.

Default off → normal adoption behaviour is unchanged.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from minotaur_subnet.epoch.manager import EpochManager, _adoption_disabled
from minotaur_subnet.harness.submission_store import Submission, SubmissionStatus


def _winning_sub(sid: str = "sub_win", score: float = 0.99) -> Submission:
    """A challenger that would clearly be adopted (no champion, delivers value).

    Under the relative per-order rule a bootstrap adoption requires the challenger
    to deliver RAW output on at least one order, so carry a per_intent row with a
    positive raw_output (the raw delivered output the live scorer emits)."""
    return Submission(
        submission_id=sid,
        repo_url="https://github.com/test/solver",
        commit_hash="abc1234",
        epoch=1,
        hotkey="5Gtestminerhotkey",
        round_id="r1",
        status=SubmissionStatus.SCORED,
        benchmark_score=score,
        benchmark_details={
            "total_intents": 5,
            "per_intent": [{"intent_id": "o1", "raw_output": "1000"}],
        },
    )


def test_helper_reads_env_truthy(monkeypatch):
    for v in ("1", "true", "TRUE", "yes", "on"):
        monkeypatch.setenv("DISABLE_CHAMPION_ADOPTION", v)
        assert _adoption_disabled() is True
    for v in ("0", "false", "no", "off", ""):
        monkeypatch.setenv("DISABLE_CHAMPION_ADOPTION", v)
        assert _adoption_disabled() is False
    monkeypatch.delenv("DISABLE_CHAMPION_ADOPTION", raising=False)
    assert _adoption_disabled() is False


def test_should_adopt_false_when_gate_on(monkeypatch):
    """A clear winner is NOT adopted while the gate is set."""
    monkeypatch.setenv("DISABLE_CHAMPION_ADOPTION", "1")
    mgr = EpochManager()
    assert mgr._should_adopt(_winning_sub()) is False


def test_should_adopt_true_when_gate_off(monkeypatch):
    """Default behaviour preserved: a clear winner adopts when the gate is off."""
    monkeypatch.delenv("DISABLE_CHAMPION_ADOPTION", raising=False)
    mgr = EpochManager()
    assert mgr._should_adopt(_winning_sub()) is True


@pytest.mark.asyncio
async def test_hot_swap_skips_when_gate_on(monkeypatch):
    """Belt-and-suspenders: even if reached, _hot_swap never swaps the solver."""
    monkeypatch.setenv("DISABLE_CHAMPION_ADOPTION", "1")
    runtime_builder = MagicMock(return_value=MagicMock())
    mgr = EpochManager(runtime_builder=runtime_builder)
    await mgr._hot_swap(_winning_sub(), epoch=1)
    runtime_builder.assert_not_called()


@pytest.mark.asyncio
async def test_hot_swap_runs_when_gate_off(monkeypatch):
    """Default behaviour preserved: _hot_swap builds the new runtime when off."""
    monkeypatch.delenv("DISABLE_CHAMPION_ADOPTION", raising=False)
    runtime_builder = MagicMock(return_value=MagicMock())
    mgr = EpochManager(runtime_builder=runtime_builder)
    await mgr._hot_swap(_winning_sub(), epoch=1)
    runtime_builder.assert_called_once()
