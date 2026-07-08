"""P4: leader run_once + corpus read the round-anchored pin (full score parity).

Tests the worker's injected-resolver pin application, the leader resolver
adapter, and the corpus to_block override — all the leader-side pieces that make
the leader benchmark at the same canonical block the follower (P3) derives.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from minotaur_subnet.api import startup
from minotaur_subnet.api.startup import _leader_fork_pin_resolver
from minotaur_subnet.consensus.round_anchor import ForkPinUnavailable
from minotaur_subnet.harness.benchmark_worker import BenchmarkWorker
from minotaur_subnet.harness.chain_corpus import _corpus_to_block
from minotaur_subnet.harness.round_store import RoundStatus


def _worker(pin_resolver=None, epoch_block_number=None):
    return BenchmarkWorker(
        submission_store=MagicMock(),
        pin_resolver=pin_resolver,
        epoch_block_number=epoch_block_number,
    )


# ── BenchmarkWorker._apply_round_anchored_pin ─────────────────────────────────


def test_worker_applies_round_pin_from_resolver():
    w = _worker(pin_resolver=lambda rid: 3000)
    w._apply_round_anchored_pin("r1")
    assert w._epoch_block_number == 3000


def _gate_on(monkeypatch):
    monkeypatch.delenv("ROUND_ANCHORED_PIN", raising=False)  # default-on


def _gate_off(monkeypatch):
    monkeypatch.setenv("ROUND_ANCHORED_PIN", "0")


# ── gate ON (default, production): the pin is MANDATORY — DEFER LOUD, never the
#    live-head/stale fallback that silently scores a different block than peers ──


def test_worker_raises_when_resolver_returns_none_gate_on(monkeypatch):
    _gate_on(monkeypatch)
    w = _worker(pin_resolver=lambda rid: None, epoch_block_number=42)
    with pytest.raises(ForkPinUnavailable):
        w._apply_round_anchored_pin("r1")
    assert w._epoch_block_number == 42  # unchanged — and the caller will not benchmark


def test_worker_reraises_forkpinunavailable_gate_on(monkeypatch):
    _gate_on(monkeypatch)

    def _defer(_rid):
        raise ForkPinUnavailable("deferred — anchor not yet confirmed")

    w = _worker(pin_resolver=_defer, epoch_block_number=42)
    with pytest.raises(ForkPinUnavailable):
        w._apply_round_anchored_pin("r1")


def test_worker_raises_on_resolver_error_gate_on(monkeypatch):
    _gate_on(monkeypatch)

    def _boom(_rid):
        raise RuntimeError("resolve down")

    w = _worker(pin_resolver=_boom, epoch_block_number=42)
    with pytest.raises(ForkPinUnavailable):
        w._apply_round_anchored_pin("r1")


def test_worker_raises_without_resolver_or_round_id_gate_on(monkeypatch):
    _gate_on(monkeypatch)
    with pytest.raises(ForkPinUnavailable):
        _worker(pin_resolver=None, epoch_block_number=42)._apply_round_anchored_pin("r1")
    with pytest.raises(ForkPinUnavailable):
        _worker(pin_resolver=lambda rid: 3000, epoch_block_number=42)._apply_round_anchored_pin(None)


# ── gate OFF (dev / live-head): best-effort no-op, never raises ──


def test_worker_noop_when_unavailable_gate_off(monkeypatch):
    _gate_off(monkeypatch)
    for resolver in (None, lambda rid: None, _raise):
        w = _worker(pin_resolver=resolver, epoch_block_number=42)
        w._apply_round_anchored_pin("r1")  # must not raise
        assert w._epoch_block_number == 42


def _raise(_rid):
    raise RuntimeError("resolve down")


# ── run_once DEFERS (does not benchmark) when the mandatory pin is unavailable ──


@pytest.mark.asyncio
async def test_run_once_defers_when_pin_unavailable_gate_on(monkeypatch):
    _gate_on(monkeypatch)
    from types import SimpleNamespace as NS

    def _defer(_rid):
        raise ForkPinUnavailable("deferred")

    rs = MagicMock()
    rs.get_current_round.return_value = NS(round_id="r1", status=RoundStatus.OPEN)
    sub = MagicMock()
    w = BenchmarkWorker(submission_store=sub, round_store=rs, pin_resolver=_defer)
    w._simulator = MagicMock()  # pass the startup-race guard
    processed = await w.run_once()
    assert processed == 0  # deferred
    sub.list_by_status.assert_not_called()  # never reached the benchmark loop


# ── _leader_fork_pin_resolver (API adapter injected into the worker) ──────────


def test_leader_resolver_extracts_benchmark_chain(monkeypatch):
    monkeypatch.delenv("ROUND_ANCHOR_CHAINS", raising=False)  # default [8453]
    with patch.object(startup, "_resolve_round_fork_pins", return_value={8453: 3000, 964: 7}):
        assert _leader_fork_pin_resolver("r1") == 3000


def test_leader_resolver_none_when_unresolved(monkeypatch):
    with patch.object(startup, "_resolve_round_fork_pins", return_value=None):
        assert _leader_fork_pin_resolver("r1") is None


# ── _corpus_to_block explicit pin wins ────────────────────────────────────────

_W3 = SimpleNamespace(eth=SimpleNamespace(block_number=1000))


def test_corpus_to_block_explicit_wins_over_env(monkeypatch):
    monkeypatch.setenv("BENCHMARK_EPOCH_BLOCK", "999999")  # would win if no explicit
    assert _corpus_to_block(_W3, 1, to_block=46_904_887) == 46_904_887


def test_corpus_to_block_none_falls_back_to_live_head(monkeypatch):
    monkeypatch.delenv("BENCHMARK_CORPUS_TO_BLOCK", raising=False)
    monkeypatch.delenv("BENCHMARK_EPOCH_BLOCK", raising=False)
    assert _corpus_to_block(_W3, 1, to_block=None) == 999
