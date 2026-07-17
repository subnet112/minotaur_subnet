"""B2 fork-pin anchor fix: anchor to the round's real-open wall-clock epoch.

Root cause: the benchmark fork-pin anchored to ``opened_epoch``, but a round's
``opened_epoch`` is the champion ACTIVATION schedule (``close_epoch +
activation_delay``), deliberately ~1 tempo in the FUTURE for commit-reveal
alignment — so ``anchor = opened_epoch * epoch_seconds`` landed ~40 min ahead of
wall-clock and the pin deferred every round.

Fix: stamp ``benchmark_anchor_epoch`` (the leader's real wall-clock epoch at
open) on the round, broadcast + adopt it, and anchor the pin to it instead. The
whole anchor derivation machinery (``round_anchor_ts``, per-chain lookback,
``find_pin_block``, pack-hash folding) is unchanged; only the epoch fed in moves.

The anchor selection is DEFAULT ON in code (``round_anchor.
benchmark_anchor_real_epoch_enabled``), with ``BENCHMARK_ANCHOR_REAL_EPOCH`` kept
only as an ``{0,false,no,off}`` emergency override — it shipped env-gated
default-OFF, but a default-OFF env gate can never reach the fleet, because
third-party validators run our canonical compose and never set flags we ask for.
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
from minotaur_subnet.consensus import round_anchor
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
    # Emergency override: an EXPLICIT off value falls back to the legacy anchor.
    monkeypatch.setenv("BENCHMARK_ANCHOR_REAL_EPOCH", "0")
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


# ── the default is ON, in code (image-baked, not env) ────────────────────────
#
# Third-party validators run our canonical compose and never set flags we ask for,
# so an env-gated default-OFF gate is permanently OFF on every node we don't
# operate — it could never go fleet-wide. The default therefore lives in CODE.

def test_anchor_gate_defaults_on_when_env_unset(monkeypatch):
    monkeypatch.delenv("BENCHMARK_ANCHOR_REAL_EPOCH", raising=False)
    assert round_anchor.benchmark_anchor_real_epoch_enabled() is True
    # …and the selection follows: unset env → the real-open stamp wins.
    assert startup._round_fork_anchor_epoch(_round(500, 42)) == 42


@pytest.mark.parametrize("raw", ["0", "false", "no", "off", "OFF", "  False  "])
def test_anchor_gate_explicit_off_values_disable(monkeypatch, raw):
    monkeypatch.setenv("BENCHMARK_ANCHOR_REAL_EPOCH", raw)
    assert round_anchor.benchmark_anchor_real_epoch_enabled() is False


@pytest.mark.parametrize("raw", ["1", "true", "yes", "on", "", "   ", "flase", "banana"])
def test_anchor_gate_anything_else_stays_enabled(monkeypatch, raw):
    # A TYPO must never silently drop ONE validator back to the legacy anchor and
    # split it off the fleet (PACK_HASH_MISMATCH). Only explicit off values disable.
    monkeypatch.setenv("BENCHMARK_ANCHOR_REAL_EPOCH", raw)
    assert round_anchor.benchmark_anchor_real_epoch_enabled() is True


def test_startup_gate_delegates_to_round_anchor(monkeypatch):
    # One source of truth for the default: startup must not re-implement the read.
    monkeypatch.delenv("BENCHMARK_ANCHOR_REAL_EPOCH", raising=False)
    assert (
        startup._benchmark_anchor_real_epoch_enabled()
        is round_anchor.benchmark_anchor_real_epoch_enabled()
    )
    monkeypatch.setenv("BENCHMARK_ANCHOR_REAL_EPOCH", "off")
    assert startup._benchmark_anchor_real_epoch_enabled() is False


def test_anchor_gate_matches_round_anchored_pin_off_values():
    # Both fleet-uniform default-ON gates share ONE off-value set, so an operator
    # who knows how to disable one knows how to disable the other.
    for raw in round_anchor._GATE_OFF_VALUES:
        assert raw in {"0", "false", "no", "off"}


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


# ── B1: the AUTOMATED close broadcast (nested builder) now carries the field ──
#
# The coordinator-loop close builder used to duplicate the payload field list and
# had DRIFTED — it omitted benchmark_anchor_epoch, so the leader's per-tempo close
# broadcast materialized the round on every follower with None → PACK_HASH_MISMATCH
# at quorum>1. It now delegates to the single module-level _close_round_sync_payload,
# so the two can never diverge again (covered by the Builder-A test above). Here we
# lock the CONSUMER side: a follower adopting a close that carries the anchor applies
# it, and a None anchor (pre-B2 leader) never clobbers an existing value.

def test_adopt_round_applies_benchmark_anchor_epoch(tmp_path):
    store = RoundStore(persist_path=tmp_path / "rounds.json")
    adopted = store.adopt_round(
        round_id="round-e100-n1", opened_epoch=100, status=RoundStatus.CLOSED,
        close_epoch=100, benchmark_anchor_epoch=77,
    )
    assert adopted.benchmark_anchor_epoch == 77
    # The pin then anchors to the adopted real-open epoch (default-ON), not opened_epoch.
    assert startup._round_fork_anchor_epoch(adopted) == 77


def test_adopt_round_none_anchor_does_not_clobber(tmp_path):
    store = RoundStore(persist_path=tmp_path / "rounds.json")
    store.adopt_round(
        round_id="round-e100-n1", opened_epoch=100, status=RoundStatus.CLOSED,
        benchmark_anchor_epoch=77,
    )
    # A later re-sync from a pre-B2 leader carries None → adopt_round SKIPS it, so the
    # already-adopted anchor survives (never falls back to opened_epoch).
    re = store.adopt_round(
        round_id="round-e100-n1", opened_epoch=100, status=RoundStatus.CLOSED,
        benchmark_anchor_epoch=None,
    )
    assert re.benchmark_anchor_epoch == 77
