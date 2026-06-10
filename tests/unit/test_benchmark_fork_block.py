"""Phase 4 (safe slice) — fork-block pin plumbing.

`run_benchmark(fork_block=N)` must forward `fork_block` to every
`simulator.simulate(...)` call so a benchmark round can be pinned to a shared
block (the keystone for cross-validator reproducibility). The default (`None`)
must preserve today's live-head behavior — this is opt-in and changes nothing
until an epoch block is actually set.
"""
import asyncio

from minotaur_subnet.harness.orchestrator import BenchmarkConfig, run_benchmark
from minotaur_subnet.shared.types import (
    ExecutionPlan,
    ScoreResult,
    SimulationResult,
)


# Minimal in-memory SolverSession: just enough surface for run_benchmark.
class _FakeSession:
    def __init__(self, plan):
        self._plan = plan

    async def initialize(self, config):
        return None

    async def metadata(self):
        return {}

    async def on_benchmark_start(self, n):
        return None

    async def generate_plan(self, intent, state, snapshot):
        return self._plan

    async def on_benchmark_end(self, summary):
        return None


# Recording simulator: captures the fork_block it was handed on each call.
class _RecordingSimulator:
    def __init__(self):
        self.fork_blocks = []

    async def simulate(self, plan, **kwargs):
        self.fork_blocks.append(kwargs.get("fork_block"))
        return SimulationResult(success=True, gas_used=100_000)


def _intent_state_snapshot():
    # Reuse the harness's factories so the domain objects stay realistic.
    from minotaur_subnet.harness.test_harness import (
        make_intent,
        make_snapshot,
        make_state,
    )

    return make_intent(), make_state(), make_snapshot()


def _run(fork_block):
    intent, state, snapshot = _intent_state_snapshot()
    plan = ExecutionPlan(intent_id=intent.app_id, interactions=[], deadline=0, nonce=0)
    sim = _RecordingSimulator()

    async def score_fn(app_id, plan, simulation, st):
        return ScoreResult(score=0.5)

    async def _go():
        return await run_benchmark(
            _FakeSession(plan),
            [(intent, state, snapshot)],
            config=BenchmarkConfig(chain_ids=[state.chain_id]),
            score_fn=score_fn,
            simulator=sim,
            fork_block=fork_block,
        )

    results = asyncio.run(_go())
    return results, sim


def test_fork_block_is_forwarded_to_simulate():
    results, sim = _run(fork_block=46_904_887)
    assert len(results) == 1
    assert sim.fork_blocks == [46_904_887], (
        "run_benchmark must forward its fork_block to simulator.simulate"
    )


def test_default_fork_block_is_none_preserving_live_head():
    # The opt-in contract: with no fork_block, simulate is called with None,
    # i.e. the fork stays at upstream head exactly as before.
    results, sim = _run(fork_block=None)
    assert len(results) == 1
    assert sim.fork_blocks == [None]


# ── BENCHMARK_EPOCH_BLOCK pin (deterministic fork-pin, opt-in) ────────────────

def _bare_worker():
    from minotaur_subnet.harness.benchmark_worker import BenchmarkWorker
    w = BenchmarkWorker.__new__(BenchmarkWorker)
    w._epoch_block_number = None
    return w


def test_epoch_block_pin_set_from_env(monkeypatch):
    monkeypatch.setenv("BENCHMARK_EPOCH_BLOCK", "46904887")
    w = _bare_worker()
    w._apply_epoch_block_pin()
    assert w._epoch_block_number == 46904887


def test_epoch_block_pin_unset_stays_none(monkeypatch):
    monkeypatch.delenv("BENCHMARK_EPOCH_BLOCK", raising=False)
    w = _bare_worker()
    w._apply_epoch_block_pin()
    assert w._epoch_block_number is None  # default = live head, unchanged


def test_epoch_block_pin_invalid_ignored(monkeypatch):
    monkeypatch.setenv("BENCHMARK_EPOCH_BLOCK", "not-an-int")
    w = _bare_worker()
    w._apply_epoch_block_pin()
    assert w._epoch_block_number is None
