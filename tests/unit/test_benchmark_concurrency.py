"""Step-2b: run_benchmark-level tests for K-runtime provisioning (BENCHMARK_CONCURRENCY).

The #362 pool engine (variable-length runtimes, by-index write-back, order-independence)
is verified by test_benchmark_pool + test_benchmark_order_independence. THESE tests cover
the 2b PROVISIONING layer in run_benchmark: spawning K-1 extra solver sessions via the
factory, K distinct runtimes, the env/kill-switch, leak-safety on partial failure, and
the clamp. _run_scenarios is faked so the focus is provisioning + lifecycle, not scoring.
"""

import types

import pytest

from minotaur_subnet.harness import orchestrator as orch
from minotaur_subnet.harness.orchestrator import (
    BenchmarkConfig,
    BenchmarkResult,
    _benchmark_concurrency,
    run_benchmark,
)


class FakeSession:
    def __init__(self):
        self._relaunch = None
        self.shutdown_called = 0
        self.inited = 0
        self.started = 0
        self.ended = 0

    async def initialize(self, cfg):
        self.inited += 1

    async def metadata(self):
        return types.SimpleNamespace(name="s", version="1", author="a")

    async def on_benchmark_start(self, n):
        self.started += 1

    async def on_benchmark_end(self, summary):
        self.ended += 1

    async def shutdown(self):
        self.shutdown_called += 1


def _intents(n):
    return [
        (
            types.SimpleNamespace(app_id=f"app{i}"),
            types.SimpleNamespace(chain_id=None, control_view=lambda: {"_scenario_name": ""}),
            object(),
        )
        for i in range(n)
    ]


@pytest.fixture
def fake_pool(monkeypatch):
    """Fake _run_scenarios (record runtimes) + inert proxy + no-RPC, so the test
    exercises ONLY the provisioning/lifecycle in run_benchmark."""
    rec = {}

    async def fake_run_scenarios(intents, *, runtimes, **kw):
        rec["k"] = len(runtimes)
        rec["proxy_ids"] = [rt.proxy_session_id for rt in runtimes]
        rec["init_configs"] = [rt.init_config for rt in runtimes]
        return [BenchmarkResult(intent_id=f"app:s{i}", score=0.5) for i in range(len(intents))]

    monkeypatch.setattr(orch, "_run_scenarios", fake_run_scenarios)
    monkeypatch.setattr(orch, "build_rpc_url_map", lambda chain_ids: {})
    monkeypatch.delenv("SOLVER_READ_PROXY", raising=False)
    monkeypatch.delenv("BENCHMARK_CONCURRENCY", raising=False)
    return rec


@pytest.mark.asyncio
@pytest.mark.parametrize("k", [1, 2, 4])
async def test_provisions_k_runtimes_and_owns_only_spawned(fake_pool, k):
    primary = FakeSession()
    spawned = []

    async def factory():
        s = FakeSession()
        spawned.append(s)
        return s

    res = await run_benchmark(
        primary, _intents(5), config=BenchmarkConfig(chain_ids=[1]),
        session_factory=factory, session_count=k,
    )
    assert fake_pool["k"] == k                       # K runtimes reach the pool
    assert len(spawned) == k - 1                     # K-1 extras spawned via the factory
    assert all(s.shutdown_called == 1 for s in spawned)   # run_benchmark shuts down extras
    assert primary.shutdown_called == 0              # ...but NOT the caller's primary session
    assert primary.ended == 1 and all(s.ended == 1 for s in spawned)  # on_benchmark_end on all
    assert all(s.inited == 1 and s.started == 1 for s in spawned)     # extras initialized + started
    assert len(res) == 5


@pytest.mark.asyncio
async def test_env_overrides_k_when_session_count_none(fake_pool, monkeypatch):
    monkeypatch.setenv("BENCHMARK_CONCURRENCY", "3")

    async def factory():
        return FakeSession()

    await run_benchmark(
        primary := FakeSession(), _intents(3),
        config=BenchmarkConfig(chain_ids=[1]), session_factory=factory,
    )
    assert fake_pool["k"] == 3
    assert primary.ended == 1


@pytest.mark.asyncio
async def test_no_factory_falls_back_to_one(fake_pool, monkeypatch):
    monkeypatch.setenv("BENCHMARK_CONCURRENCY", "4")
    # K>1 requested but no factory to spawn extras -> degrade to the single runtime.
    await run_benchmark(FakeSession(), _intents(3), config=BenchmarkConfig(chain_ids=[1]))
    assert fake_pool["k"] == 1


@pytest.mark.asyncio
async def test_factory_failure_degrades_with_no_leak(fake_pool):
    spawned = []
    calls = {"n": 0}

    async def factory():
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("docker oom on extra #2")
        s = FakeSession()
        spawned.append(s)
        return s

    res = await run_benchmark(
        FakeSession(), _intents(4), config=BenchmarkConfig(chain_ids=[1]),
        session_factory=factory, session_count=4,
    )
    # Asked for K=4; the 2nd extra's factory throws -> degrade to primary + 1 extra.
    assert fake_pool["k"] == 2
    assert len(spawned) == 1
    assert spawned[0].shutdown_called == 1   # the created extra is cleaned up — no leak
    assert len(res) == 4                      # the run still completes


def test_benchmark_concurrency_clamps(monkeypatch):
    monkeypatch.setenv("BENCHMARK_CONCURRENCY", "999")
    assert _benchmark_concurrency() == 63    # clamped to proxy MAX_SESSIONS - 1
    monkeypatch.setenv("BENCHMARK_CONCURRENCY", "0")
    assert _benchmark_concurrency() == 1     # floor is 1 (kill-switch)
    monkeypatch.setenv("BENCHMARK_CONCURRENCY", "garbage")
    assert _benchmark_concurrency() == 1
    monkeypatch.delenv("BENCHMARK_CONCURRENCY", raising=False)
    assert _benchmark_concurrency() == 1     # default 1


def test_pack_hash_signature_excludes_concurrency():
    """K must NEVER enter the pack hash — else a mixed-K fleet splits consensus."""
    import inspect

    from minotaur_subnet.harness.benchmark_pack import compute_pack_hash

    params = {p.lower() for p in inspect.signature(compute_pack_hash).parameters}
    assert not (params & {"session_count", "concurrency", "k", "runtimes"})
