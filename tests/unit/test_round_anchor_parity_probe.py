"""Round-anchor parity probe: /health-exposed fork-pin derivation (default-on).

Closes the verification gap left by log-only shadow mode: third-party operators
won't read logs for us, so every validator independently derives the canonical
fork pin for the current epoch anchor on a background task and publishes it on
``/health`` (``ctx.round_anchor_parity``). Polling /health across the fleet and
diffing ``pins`` grouped by ``anchor_epoch`` confirms pin parity before flipping
``ROUND_ANCHORED_PIN`` — no log access, no operator action.

Safety properties pinned here: the probe never blocks the event loop (derivation
runs in a thread with a bounded RPC timeout), it re-derives only when the epoch
advances, and the snapshot is pure observability (status/pins/gate flag), never
stored on a round nor bound into a pack hash.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from minotaur_subnet.api import startup

_PINS = {8453: 47_188_506, 964: 5_012_345}


# ── gate + config helpers ───────────────────────────────────────────────────


def test_parity_enabled_by_default(monkeypatch):
    monkeypatch.delenv("ROUND_ANCHOR_PARITY", raising=False)
    assert startup._round_anchor_parity_enabled() is True


def test_parity_opt_out(monkeypatch):
    monkeypatch.setenv("ROUND_ANCHOR_PARITY", "0")
    assert startup._round_anchor_parity_enabled() is False


def test_rpc_timeout_default(monkeypatch):
    monkeypatch.delenv("ROUND_ANCHOR_RPC_TIMEOUT", raising=False)
    assert startup._round_anchor_rpc_timeout() == 10.0


def test_rpc_timeout_override_and_floor(monkeypatch):
    monkeypatch.setenv("ROUND_ANCHOR_RPC_TIMEOUT", "3")
    assert startup._round_anchor_rpc_timeout() == 3.0
    # Clamped to a 1s floor so it can never be set to 0 / negative.
    monkeypatch.setenv("ROUND_ANCHOR_RPC_TIMEOUT", "0")
    assert startup._round_anchor_rpc_timeout() == 1.0
    monkeypatch.setenv("ROUND_ANCHOR_RPC_TIMEOUT", "not-a-number")
    assert startup._round_anchor_rpc_timeout() == 10.0


# ── snapshot builder ────────────────────────────────────────────────────────


def test_snapshot_ok_with_pins(monkeypatch):
    monkeypatch.delenv("ROUND_ANCHORED_PIN", raising=False)
    monkeypatch.delenv("ROUND_ANCHOR_CHAINS", raising=False)
    with patch(
        "minotaur_subnet.api.startup._derive_round_fork_pins", return_value=dict(_PINS)
    ) as derive:
        snap = startup._compute_round_anchor_parity_snapshot(14_843_049)
    derive.assert_called_once_with(14_843_049)
    assert snap["status"] == "ok"
    assert snap["anchor_epoch"] == 14_843_049
    # Pins are string-keyed (JSON-friendly) and sorted by chain id.
    assert snap["pins"] == {"964": 5_012_345, "8453": 47_188_506}
    assert snap["pin_segment"] == "964:5012345|8453:47188506"
    assert snap["gate_enabled"] is False
    assert isinstance(snap["derived_at"], int)


def test_snapshot_gate_flag_reflects_env(monkeypatch):
    monkeypatch.setenv("ROUND_ANCHORED_PIN", "1")
    with patch(
        "minotaur_subnet.api.startup._derive_round_fork_pins", return_value=dict(_PINS)
    ):
        snap = startup._compute_round_anchor_parity_snapshot(42)
    assert snap["gate_enabled"] is True


def test_snapshot_deferred_when_no_pins(monkeypatch):
    monkeypatch.delenv("ROUND_ANCHORED_PIN", raising=False)
    with patch(
        "minotaur_subnet.api.startup._derive_round_fork_pins", return_value=None
    ):
        snap = startup._compute_round_anchor_parity_snapshot(42)
    assert snap["status"] == "deferred"
    assert snap["pins"] == {}
    assert snap["pin_segment"] == ""


# ── background loop ─────────────────────────────────────────────────────────


async def _run_one_iteration(ctx, monkeypatch, *, frozen_time=1_781_166_000):
    """Start the loop, let exactly one iteration settle, then cancel it."""
    monkeypatch.setenv("ROUND_ANCHOR_PARITY_INTERVAL", "100")  # long → single pass
    monkeypatch.setattr(startup.time, "time", lambda: frozen_time)
    task = asyncio.create_task(startup._round_anchor_parity_loop(ctx))
    try:
        for _ in range(100):
            await asyncio.sleep(0.01)
            if ctx.round_anchor_parity:
                break
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_loop_publishes_snapshot(monkeypatch):
    ctx = SimpleNamespace(round_anchor_parity={})
    expected_epoch = 1_781_166_000 // 120 - 1  # one epoch back
    snap = {"status": "ok", "anchor_epoch": expected_epoch, "pins": {"8453": 1}}
    with patch(
        "minotaur_subnet.epoch.clock.SolverRoundEpochClock.from_env",
        return_value=SimpleNamespace(epoch_seconds=120),
    ), patch(
        "minotaur_subnet.api.startup._compute_round_anchor_parity_snapshot",
        return_value=snap,
    ) as comp:
        await _run_one_iteration(ctx, monkeypatch)
    assert ctx.round_anchor_parity == snap
    comp.assert_called_once_with(expected_epoch)


@pytest.mark.asyncio
async def test_loop_skips_rederive_when_epoch_unchanged(monkeypatch):
    expected_epoch = 1_781_166_000 // 120 - 1
    # Pre-seed a good snapshot for the same epoch → the loop must NOT re-derive.
    ctx = SimpleNamespace(
        round_anchor_parity={"status": "ok", "anchor_epoch": expected_epoch}
    )
    monkeypatch.setenv("ROUND_ANCHOR_PARITY_INTERVAL", "100")
    monkeypatch.setattr(startup.time, "time", lambda: 1_781_166_000)
    with patch(
        "minotaur_subnet.epoch.clock.SolverRoundEpochClock.from_env",
        return_value=SimpleNamespace(epoch_seconds=120),
    ), patch(
        "minotaur_subnet.api.startup._compute_round_anchor_parity_snapshot",
    ) as comp:
        task = asyncio.create_task(startup._round_anchor_parity_loop(ctx))
        await asyncio.sleep(0.1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    comp.assert_not_called()
