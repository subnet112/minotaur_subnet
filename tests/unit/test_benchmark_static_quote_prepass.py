"""BENCHMARK_STATIC_QUOTE also short-circuits the champion reference-quote
pre-pass (it would be wasted work — the enrichment injects a static zero).
DEFAULT ON: the pre-pass is skipped unless the legacy mode is explicitly
re-enabled with BENCHMARK_STATIC_QUOTE=0 (instant-revert switch, slated for
removal after the fleet soaks ON)."""
from __future__ import annotations

import asyncio

from minotaur_subnet.harness.benchmark_worker import BenchmarkWorker


def test_prepass_returns_empty_when_static_quote_on(monkeypatch):
    monkeypatch.setenv("BENCHMARK_STATIC_QUOTE", "1")
    # Bypass the heavy __init__: the flag check is the FIRST statement of
    # _get_or_build_reference_quotes and returns before touching any self attr.
    worker = object.__new__(BenchmarkWorker)
    out = asyncio.run(worker._get_or_build_reference_quotes([]))
    assert out == {}


def test_prepass_returns_empty_by_default(monkeypatch):
    # NO env set — static quoting is the default, so the pre-pass must be
    # skipped on a validator with no host env (identical to the leader, which
    # has run ON since 2026-07-03).
    monkeypatch.delenv("BENCHMARK_STATIC_QUOTE", raising=False)
    worker = object.__new__(BenchmarkWorker)
    out = asyncio.run(worker._get_or_build_reference_quotes([]))
    assert out == {}


def test_prepass_not_short_circuited_when_flag_zero(monkeypatch):
    monkeypatch.setenv("BENCHMARK_STATIC_QUOTE", "0")
    worker = object.__new__(BenchmarkWorker)
    # With the legacy mode explicitly re-enabled the method proceeds past the
    # guard and hits real self attributes (unset on the bare object) →
    # AttributeError, proving it did NOT early-return. (We don't build a full
    # worker; we only assert the guard is bypassed when the env is 0.)
    import pytest
    with pytest.raises(AttributeError):
        asyncio.run(worker._get_or_build_reference_quotes([]))
