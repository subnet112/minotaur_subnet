"""P4: leader run_once + corpus read the round-anchored pin (full score parity).

Tests the worker's injected-resolver pin application, the leader resolver
adapter, and the corpus to_block override — all the leader-side pieces that make
the leader benchmark at the same canonical block the follower (P3) derives.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from minotaur_subnet.api import startup
from minotaur_subnet.api.startup import _leader_fork_pin_resolver
from minotaur_subnet.harness.benchmark_worker import BenchmarkWorker
from minotaur_subnet.harness.chain_corpus import _corpus_to_block


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


def test_worker_noop_without_resolver():
    w = _worker(pin_resolver=None, epoch_block_number=42)
    w._apply_round_anchored_pin("r1")
    assert w._epoch_block_number == 42  # untouched


def test_worker_noop_when_resolver_returns_none():
    w = _worker(pin_resolver=lambda rid: None, epoch_block_number=42)
    w._apply_round_anchored_pin("r1")
    assert w._epoch_block_number == 42


def test_worker_noop_without_round_id():
    w = _worker(pin_resolver=lambda rid: 3000, epoch_block_number=42)
    w._apply_round_anchored_pin(None)
    assert w._epoch_block_number == 42


def test_worker_swallows_resolver_errors():
    def _boom(_rid):
        raise RuntimeError("resolve down")

    w = _worker(pin_resolver=_boom, epoch_block_number=42)
    w._apply_round_anchored_pin("r1")  # must not raise
    assert w._epoch_block_number == 42


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
