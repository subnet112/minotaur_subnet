"""Driver-level tests for the benchmark scenario pool (``_run_scenarios``).

Exercises the PARALLEL execution path without real solvers/anvil by monkeypatching
``_process_scenario``. Proves the three properties the K-runtime pool relies on:

1. K>1 produces byte-identical results to K=1.
2. Results are written back by INPUT index even when scenarios finish out of order
   (the load-bearing constraint from test_benchmark_order_independence).
3. K>1 genuinely overlaps (concurrency), which is the whole point — the round is
   network-latency-bound on a ~90%-idle CPU.

The order-independence of the SCORES themselves is additionally guarded by
test_benchmark_order_independence; the end-to-end byte-identical behavior of the
K=1 path is guarded by the existing benchmark suite (test_benchmark_*).
"""

import asyncio
import types

import pytest

from minotaur_subnet.harness import orchestrator as orch
from minotaur_subnet.harness.orchestrator import (
    BenchmarkResult,
    _BenchmarkRuntime,
    _run_scenarios,
)


def _intents(n: int):
    """n fake (intent, state, snapshot) tuples; intent.app_id encodes the index."""
    out = []
    for i in range(n):
        intent = types.SimpleNamespace(app_id=f"app{i}")
        state = types.SimpleNamespace(control_view=lambda: {"_scenario_name": ""})
        out.append((intent, state, object()))
    return out


def _runtimes(k: int):
    # session only needs ._relaunch (touched on respawn, which the happy-path
    # fake never triggers); _process_scenario is faked so it ignores the session.
    return [
        _BenchmarkRuntime(session=types.SimpleNamespace(_relaunch=None), proxy_session_id=None)
        for _ in range(k)
    ]


def _make_fake(n: int):
    """Fake _process_scenario: deterministic score per index, finishes in REVERSE
    index order (idx 0 sleeps longest) so completion order != input order. Records
    peak in-flight count to prove concurrency."""
    inflight = {"cur": 0, "max": 0}

    async def fake(intent, state, snapshot, **kw):
        i = int(intent.app_id.removeprefix("app"))
        inflight["cur"] += 1
        inflight["max"] = max(inflight["max"], inflight["cur"])
        await asyncio.sleep(0.002 * (n - i))  # later index finishes first
        inflight["cur"] -= 1
        return BenchmarkResult(intent_id=f"s{i}", score=round(i * 0.1, 3)), False

    return fake, inflight


_COMMON = dict(
    simulator=None,
    init_config={},
    read_proxy=None,
    config=None,
    score_fn=None,
    fork_block=None,
    require_real_sim=False,
    trigger_ground_truth={},
)


@pytest.mark.asyncio
@pytest.mark.parametrize("k", [1, 2, 3, 6])
async def test_pool_results_identical_and_in_input_order_across_k(monkeypatch, k):
    n = 6
    intents = _intents(n)
    fake, inflight = _make_fake(n)
    monkeypatch.setattr(orch, "_process_scenario", fake)

    results = await _run_scenarios(intents, runtimes=_runtimes(k), **_COMMON)

    # (2) Written back by INPUT index, despite reverse completion order.
    assert [br.intent_id for br in results] == [f"s{i}" for i in range(n)]
    # (1) Byte-identical scores regardless of K.
    assert [br.score for br in results] == [round(i * 0.1, 3) for i in range(n)]
    assert len(results) == n
    # (3) K>1 actually overlapped; K=1 never did.
    if k == 1:
        assert inflight["max"] == 1
    else:
        assert inflight["max"] >= 2


@pytest.mark.asyncio
async def test_pool_k_equals_one_matches_k_many_exactly(monkeypatch):
    n = 8
    intents = _intents(n)

    fake1, _ = _make_fake(n)
    monkeypatch.setattr(orch, "_process_scenario", fake1)
    r1 = await _run_scenarios(intents, runtimes=_runtimes(1), **_COMMON)

    fake4, inflight4 = _make_fake(n)
    monkeypatch.setattr(orch, "_process_scenario", fake4)
    r4 = await _run_scenarios(intents, runtimes=_runtimes(4), **_COMMON)

    assert [(b.intent_id, b.score) for b in r1] == [(b.intent_id, b.score) for b in r4]
    assert inflight4["max"] >= 2  # the 4-runtime pool really ran concurrently


@pytest.mark.asyncio
async def test_pool_drains_every_scenario_no_holes(monkeypatch):
    # More scenarios than runtimes — workers must drain the shared queue fully.
    n = 25
    intents = _intents(n)
    fake, _ = _make_fake(n)
    monkeypatch.setattr(orch, "_process_scenario", fake)

    results = await _run_scenarios(intents, runtimes=_runtimes(4), **_COMMON)

    assert len(results) == n
    assert all(br is not None for br in results)
    assert [br.intent_id for br in results] == [f"s{i}" for i in range(n)]
