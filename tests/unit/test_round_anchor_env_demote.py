"""P5: BENCHMARK_EPOCH_BLOCK / BENCHMARK_CORPUS_TO_BLOCK demoted to dev/test-only.

When ROUND_ANCHORED_PIN is on, the env pins are IGNORED (round-anchored
derivation is authoritative) so a deferred round can't silently fall back to a
stale env block. With the gate off they still work (dev/local path).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from minotaur_subnet.harness.benchmark_worker import BenchmarkWorker
from minotaur_subnet.harness.chain_corpus import _corpus_to_block


def _worker(epoch_block_number=None):
    return BenchmarkWorker(submission_store=MagicMock(), epoch_block_number=epoch_block_number)


_W3 = SimpleNamespace(eth=SimpleNamespace(block_number=1000))


# ── BENCHMARK_EPOCH_BLOCK env demotion ────────────────────────────────────────


def test_env_pin_ignored_when_gate_on(monkeypatch):
    monkeypatch.setenv("BENCHMARK_EPOCH_BLOCK", "999")
    monkeypatch.setenv("ROUND_ANCHORED_PIN", "1")
    w = _worker(epoch_block_number=None)
    w._apply_epoch_block_pin()
    assert w._epoch_block_number is None  # env ignored under round-anchored


def test_env_pin_applies_when_gate_off(monkeypatch):
    monkeypatch.setenv("BENCHMARK_EPOCH_BLOCK", "999")
    monkeypatch.delenv("ROUND_ANCHORED_PIN", raising=False)
    w = _worker(epoch_block_number=None)
    w._apply_epoch_block_pin()
    assert w._epoch_block_number == 999  # dev/local path still works


# ── corpus to_block env demotion ──────────────────────────────────────────────


def test_corpus_to_block_skips_env_when_gate_on(monkeypatch):
    monkeypatch.setenv("ROUND_ANCHORED_PIN", "1")
    monkeypatch.setenv("BENCHMARK_EPOCH_BLOCK", "999999")
    monkeypatch.setenv("BENCHMARK_CORPUS_TO_BLOCK", "888888")
    # No explicit to_block -> env ignored -> live head (head - confirmations).
    assert _corpus_to_block(_W3, 1, to_block=None) == 999


def test_corpus_to_block_uses_env_when_gate_off(monkeypatch):
    monkeypatch.delenv("ROUND_ANCHORED_PIN", raising=False)
    monkeypatch.setenv("BENCHMARK_CORPUS_TO_BLOCK", "46900000")
    assert _corpus_to_block(_W3, 1, to_block=None) == 46900000
