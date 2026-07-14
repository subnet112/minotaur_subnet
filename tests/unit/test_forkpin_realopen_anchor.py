"""B2 fork-pin anchor fix: anchor to the round's real-open wall-clock epoch.

Root cause: the benchmark fork-pin anchored to ``opened_epoch``, but a round's
``opened_epoch`` is the champion ACTIVATION schedule (``close_epoch +
activation_delay``), deliberately ~1 tempo in the FUTURE for commit-reveal
alignment — so ``anchor = opened_epoch * epoch_seconds`` landed ~40 min ahead of
wall-clock and the pin deferred every round.

Fix: stamp ``benchmark_anchor_epoch`` (the leader's real wall-clock epoch at
open) on the round, broadcast + adopt it, and — gated by
``BENCHMARK_ANCHOR_REAL_EPOCH`` — anchor the pin to it instead. The whole anchor
derivation machinery (``round_anchor_ts``, per-chain lookback, ``find_pin_block``,
pack-hash folding) is unchanged; only the epoch fed in moves.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from minotaur_subnet.api.routes.submissions.models import CloseRoundRequest
from minotaur_subnet.api.routes.submissions.round_manager import (
    _close_round_sync_payload,
)
from minotaur_subnet.epoch.clock import EPOCH_SECONDS
from minotaur_subnet.harness.round_store import RoundState, RoundStatus, RoundStore
import minotaur_subnet.api.startup as startup


# ── the field + serialization ────────────────────────────────────────────────

def test_roundstate_serializes_benchmark_anchor_epoch():
    rs = RoundState(
        round_id="round-e100-n1", status=RoundStatus.OPEN,
        opened_epoch=100, benchmark_anchor_epoch=42,
    )
    assert rs.to_dict()["benchmark_anchor_epoch"] == 42
    assert RoundState.from_dict(rs.to_dict()).benchmark_anchor_epoch == 42


def test_legacy_round_without_field_loads_as_none():
    # A round persisted before this shipped has no key → None (fall back to opened).
    raw = {"round_id": "round-e5-n1", "status": "open", "opened_epoch": 5}
    assert RoundState.from_dict(raw).benchmark_anchor_epoch is None


# ── stamped at OPEN, decoupled from the future opened_epoch ───────────────────

def test_ensure_open_round_stamps_real_wallclock_epoch(tmp_path):
    store = RoundStore(persist_path=tmp_path / "rounds.json")
    # opened_epoch is deliberately far in the FUTURE (the activation schedule).
    opened = store.ensure_open_round(opened_epoch=999_999)
    now_epoch = int(time.time() // EPOCH_SECONDS)
    assert opened.opened_epoch == 999_999
    assert opened.benchmark_anchor_epoch is not None
    # stamped to real wall-clock now, NOT the future opened_epoch
    assert abs(opened.benchmark_anchor_epoch - now_epoch) <= 1
    assert opened.benchmark_anchor_epoch != opened.opened_epoch


# ── the gated anchor selection ────────────────────────────────────────────────

def _round(opened, anchor):
    return RoundState(
        round_id=f"round-e{opened}-n1", status=RoundStatus.OPEN,
        opened_epoch=opened, benchmark_anchor_epoch=anchor,
    )


def test_anchor_selection_gate_off_uses_opened_epoch(monkeypatch):
    monkeypatch.delenv("BENCHMARK_ANCHOR_REAL_EPOCH", raising=False)
    assert startup._round_fork_anchor_epoch(_round(500, 42)) == 500


def test_anchor_selection_gate_on_uses_benchmark_anchor_epoch(monkeypatch):
    monkeypatch.setenv("BENCHMARK_ANCHOR_REAL_EPOCH", "1")
    assert startup._round_fork_anchor_epoch(_round(500, 42)) == 42


def test_anchor_selection_gate_on_but_missing_falls_back(monkeypatch):
    monkeypatch.setenv("BENCHMARK_ANCHOR_REAL_EPOCH", "1")
    # A round opened before the field existed → fall back to opened_epoch (safe).
    assert startup._round_fork_anchor_epoch(_round(500, None)) == 500


def test_anchor_selection_none_when_no_opened(monkeypatch):
    monkeypatch.delenv("BENCHMARK_ANCHOR_REAL_EPOCH", raising=False)

    class _Bare:
        pass

    assert startup._round_fork_anchor_epoch(_Bare()) is None


# ── propagation: broadcast payload + CloseRoundRequest carry it ───────────────

def test_close_sync_payload_carries_benchmark_anchor_epoch():
    state = RoundState(
        round_id="round-e100-n1", status=RoundStatus.CLOSED,
        opened_epoch=100, close_epoch=100, benchmark_anchor_epoch=77,
    )
    payload = _close_round_sync_payload(state)
    assert payload["benchmark_anchor_epoch"] == 77


def test_close_round_request_accepts_benchmark_anchor_epoch():
    body = CloseRoundRequest(close_epoch=100, benchmark_anchor_epoch=77)
    assert body.benchmark_anchor_epoch == 77
    # absent on a pre-B2 leader's payload → None (follower falls back)
    assert CloseRoundRequest(close_epoch=100).benchmark_anchor_epoch is None
