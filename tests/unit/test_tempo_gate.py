"""Unit tests for ``TempoEmitGate`` — the tempo-aligned commit scheduler.

The gate's contract (see tempo_gate.py for the chain mechanics it encodes):

- True  → estimated block is within ``lead_blocks`` of the next epoch step
          and no commit has been made for that boundary yet;
- False → chain state known, but mid-epoch or boundary already committed;
- None  → chain state unavailable → caller falls back to wall-clock cadence.

All tests drive the gate with a fake subtensor and an injected monotonic
clock — no chain, no sleeps.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from minotaur_subnet.validator.tempo_gate import TempoEmitGate

NETUID = 112
TEMPO = 360


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _fake_subtensor(*, block: int, tempo: int = TEMPO, since: int = 0):
    """A stand-in exposing exactly what _sync_blocking touches."""
    sub = SimpleNamespace()
    sub.get_current_block = lambda: block
    storage = {"Tempo": tempo, "BlocksSinceLastStep": since}

    def query(module, name, params):
        assert module == "SubtensorModule"
        assert params == [NETUID]
        return SimpleNamespace(value=storage[name])

    sub.substrate = SimpleNamespace(query=query)
    return sub


def _gate(sub, clock, **kwargs) -> TempoEmitGate:
    return TempoEmitGate(
        get_subtensor=lambda: sub,
        netuid=NETUID,
        block_time=12.0,
        lead_blocks=kwargs.pop("lead_blocks", 20),
        monotonic=clock,
        **kwargs,
    )


# ── window detection ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_mid_epoch_returns_false():
    """Fresh epoch (since=0 → 360 blocks to the step) is far outside a
    20-block window."""
    clock = FakeClock()
    gate = _gate(_fake_subtensor(block=8_537_035, since=0), clock)
    assert await gate.should_emit_now() is False


@pytest.mark.asyncio
async def test_pre_boundary_window_returns_true():
    """since=345 → 15 blocks to the step, inside the 20-block window."""
    clock = FakeClock()
    gate = _gate(_fake_subtensor(block=8_537_380, since=345), clock)
    assert await gate.should_emit_now() is True


@pytest.mark.asyncio
async def test_committed_boundary_not_recommitted():
    """After mark_committed, the SAME boundary reads False — one commit per
    epoch is the whole point."""
    clock = FakeClock()
    gate = _gate(_fake_subtensor(block=8_537_380, since=345), clock)
    assert await gate.should_emit_now() is True
    gate.mark_committed()
    assert await gate.should_emit_now() is False


@pytest.mark.asyncio
async def test_failed_emit_stays_retryable():
    """No mark_committed (emit failed) → the window keeps answering True on
    subsequent ticks."""
    clock = FakeClock()
    gate = _gate(_fake_subtensor(block=8_537_380, since=345), clock)
    assert await gate.should_emit_now() is True
    clock.advance(5.0)  # one loop tick later, still in window
    assert await gate.should_emit_now() is True


@pytest.mark.asyncio
async def test_next_boundary_opens_a_new_window():
    """After committing for boundary N, advancing one full epoch period puts
    us in boundary N+1's window — which must answer True again.

    Extrapolation drives the boundary advance (resync interval not yet
    elapsed), pinning the local-clock block estimation path too.
    """
    clock = FakeClock()
    # 15 blocks to boundary at block 8_537_380 → boundary 8_537_395.
    gate = _gate(_fake_subtensor(block=8_537_380, since=345), clock)
    assert await gate.should_emit_now() is True
    gate.mark_committed()
    assert await gate.should_emit_now() is False
    # Advance one epoch period (tempo+1 = 361 blocks) minus nothing: the
    # estimated block lands 15 blocks before boundary N+1 (resync_seconds
    # default 300 < 361*12, so a resync WOULD fire — keep the fake chain
    # consistent with the advanced clock).
    clock.advance(361 * 12.0)
    gate._get_subtensor = lambda: _fake_subtensor(block=8_537_380 + 361, since=345)
    assert await gate.should_emit_now() is True


@pytest.mark.asyncio
async def test_commit_dedup_tolerates_resync_jitter():
    """A resync can re-derive the boundary a block or two off (the two chain
    reads straddle a block). That must still count as the SAME boundary —
    tolerance-based dedup, not equality."""
    clock = FakeClock()
    sub_holder = {"sub": _fake_subtensor(block=8_537_380, since=345)}
    gate = TempoEmitGate(
        get_subtensor=lambda: sub_holder["sub"],
        netuid=NETUID,
        block_time=12.0,
        lead_blocks=20,
        resync_seconds=1.0,  # force a resync on the second call
        monotonic=clock,
    )
    assert await gate.should_emit_now() is True
    gate.mark_committed()
    # Resync now reports the boundary 2 blocks later (jitter), same epoch.
    clock.advance(5.0)
    sub_holder["sub"] = _fake_subtensor(block=8_537_384, since=347)
    assert await gate.should_emit_now() is False


# ── fallback semantics ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_unreachable_chain_before_first_sync_returns_none():
    """Never synced + query failure → None (legacy wall-clock fallback)."""
    clock = FakeClock()
    sub = MagicMock()
    sub.get_current_block.side_effect = RuntimeError("ws dead")
    gate = _gate(sub, clock)
    assert await gate.should_emit_now() is None


@pytest.mark.asyncio
async def test_degenerate_tempo_returns_none():
    """tempo=0 (odd local testnet) → None, never a divide-by-zero."""
    clock = FakeClock()
    gate = _gate(_fake_subtensor(block=100, tempo=0, since=0), clock)
    assert await gate.should_emit_now() is None


@pytest.mark.asyncio
async def test_sync_failure_after_a_good_sync_extrapolates():
    """Once synced, later chain failures do NOT flip the gate to None — it
    extrapolates the block height from the local clock. (A follower whose
    RPC blips mid-epoch must not fall back to the overwrite-prone cadence.)"""
    clock = FakeClock()
    sub_holder = {"sub": _fake_subtensor(block=8_537_035, since=0)}
    gate = TempoEmitGate(
        get_subtensor=lambda: sub_holder["sub"],
        netuid=NETUID,
        block_time=12.0,
        lead_blocks=20,
        resync_seconds=1.0,
        monotonic=clock,
    )
    assert await gate.should_emit_now() is False  # good sync, mid-epoch

    # Chain goes dark; advance to 15 blocks before the boundary (345 * 12s).
    broken = MagicMock()
    broken.get_current_block.side_effect = RuntimeError("ws dead")
    sub_holder["sub"] = broken
    clock.advance(345 * 12.0)
    assert await gate.should_emit_now() is True  # extrapolated into window


@pytest.mark.asyncio
async def test_sync_failures_are_backed_off():
    """A dead RPC must not be re-queried on every 5s tick — failures are
    rate-limited by SYNC_RETRY_BACKOFF_SECONDS."""
    clock = FakeClock()
    sub = MagicMock()
    sub.get_current_block.side_effect = RuntimeError("ws dead")
    gate = _gate(sub, clock)

    assert await gate.should_emit_now() is None
    clock.advance(5.0)
    assert await gate.should_emit_now() is None
    clock.advance(5.0)
    assert await gate.should_emit_now() is None
    # 3 ticks inside the 30s backoff → exactly ONE chain attempt.
    assert sub.get_current_block.call_count == 1


# ── observability ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_debug_state_reports_schedule():
    clock = FakeClock()
    gate = _gate(_fake_subtensor(block=8_537_380, since=345), clock)
    await gate.should_emit_now()
    state = gate.debug_state()
    assert state["mode"] == "tempo"
    assert state["active"] is True
    assert state["tempo"] == TEMPO
    assert state["next_boundary_block"] == 8_537_380 + 15
    assert state["blocks_until_boundary"] == 15


def test_debug_state_before_any_sync():
    gate = _gate(MagicMock(), FakeClock())
    state = gate.debug_state()
    assert state["mode"] == "tempo"
    assert state["active"] is False
