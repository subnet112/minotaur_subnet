"""Phase 0 (deterministic-budget): pin the SOLVER's read fork to fork_block.

run_benchmark must reset the solver's read fork to the round's ``fork_block``
BEFORE generate_plan when ``PIN_SOLVER_READ_BLOCK`` is on, so the solver reads
the SAME block the simulator scores at (cross-host deterministic, and the
solver's quote finally matches the executed block). Default OFF (consensus-
relevant — ships inert, flips fleet-uniformly).
"""
import asyncio

from minotaur_subnet.harness.orchestrator import BenchmarkConfig, run_benchmark
from minotaur_subnet.shared.types import ExecutionPlan, ScoreResult, SimulationResult


class _FakeSession:
    def __init__(self, plan):
        self._plan = plan

    async def initialize(self, config):
        return None

    async def metadata(self):
        return {}

    async def on_benchmark_start(self, n):
        return None

    async def on_benchmark_end(self, summary):
        return None

    async def generate_plan(self, intent, state, snapshot):
        return self._plan


class _PinRecordingSimulator:
    """Records pin_read_fork + simulate calls in order, to assert the pin runs
    BEFORE the solver phase + the simulator scores at the same block."""

    def __init__(self):
        self.pins: list[tuple] = []
        self.order: list[tuple] = []

    def pin_read_fork(self, chain_id, block_number):
        self.pins.append((chain_id, block_number))
        self.order.append(("pin", block_number))
        return True

    async def simulate(self, plan, **kwargs):
        self.order.append(("simulate", kwargs.get("fork_block")))
        return SimulationResult(success=True, gas_used=100_000)


def _run(pin_env, monkeypatch):
    from minotaur_subnet.harness.test_harness import (
        make_intent, make_snapshot, make_state,
    )
    if pin_env is None:
        monkeypatch.delenv("PIN_SOLVER_READ_BLOCK", raising=False)
    else:
        monkeypatch.setenv("PIN_SOLVER_READ_BLOCK", pin_env)
    intent, state, snapshot = make_intent(), make_state(), make_snapshot()
    plan = ExecutionPlan(intent_id=intent.app_id, interactions=[], deadline=0, nonce=0)
    sim = _PinRecordingSimulator()

    async def score_fn(app_id, plan, simulation, st):
        return ScoreResult(score=0.5)

    asyncio.run(run_benchmark(
        _FakeSession(plan), [(intent, state, snapshot)],
        config=BenchmarkConfig(chain_ids=[state.chain_id]),
        score_fn=score_fn, simulator=sim, fork_block=12345,
    ))
    return sim, state.chain_id


def test_pin_called_before_generate_plan_when_enabled(monkeypatch):
    sim, chain_id = _run("1", monkeypatch)
    assert sim.pins == [(chain_id, 12345)]            # pinned the scenario chain to fork_block
    assert sim.order[0] == ("pin", 12345)             # BEFORE the simulator scores
    assert ("simulate", 12345) in sim.order           # simulator scores at the SAME block


def test_pin_not_called_when_disabled(monkeypatch):
    sim, _ = _run(None, monkeypatch)
    assert sim.pins == []                             # default OFF — inert


def test_fail_closed_on_unrouted_chain(monkeypatch):
    """Firewall hardening: when the block-pin proxy is the configured read path,
    a benchmarked chain NOT in SOLVER_READ_PROXY_CHAINS makes run_benchmark raise
    RealSimulationUnavailable — never hands the solver a raw/dead URL or an
    un-pinned read path. Fires BEFORE any proxy session is opened (the proxy URL
    here points at a dead port and is never contacted)."""
    import pytest
    from minotaur_subnet.harness.orchestrator import RealSimulationUnavailable
    from minotaur_subnet.harness.test_harness import (
        make_intent, make_snapshot, make_state,
    )
    monkeypatch.setenv("SOLVER_READ_PROXY", "http://127.0.0.1:1")  # never contacted
    monkeypatch.setenv("SOLVER_READ_PROXY_CHAINS", "8453")          # only Base routed
    monkeypatch.setenv("ANVIL_RPC_URL", "http://eth-anvil:8545")    # chain 1 resolvable
    monkeypatch.setenv("BASE_RPC_URL", "http://base-anvil:8546")    # chain 8453 resolvable
    monkeypatch.delenv("PIN_SOLVER_READ_BLOCK", raising=False)
    intent, state, snapshot = make_intent(), make_state(), make_snapshot()
    plan = ExecutionPlan(intent_id=intent.app_id, interactions=[], deadline=0, nonce=0)
    sim = _PinRecordingSimulator()

    async def score_fn(app_id, plan, simulation, st):
        return ScoreResult(score=0.5)

    with pytest.raises(RealSimulationUnavailable, match="NOT routed through"):
        asyncio.run(run_benchmark(
            _FakeSession(plan), [(intent, state, snapshot)],
            config=BenchmarkConfig(chain_ids=[1, 8453]),  # eth(1) is NOT routed
            score_fn=score_fn, simulator=sim, fork_block=12345,
        ))
    assert sim.order == []  # raised before any simulate/generate_plan ran


def test_anvil_pin_read_fork_noop_when_already_at_block():
    from minotaur_subnet.simulator.anvil_simulator import AnvilSimulator
    sim = AnvilSimulator.__new__(AnvilSimulator)      # bypass __init__ / no real RPC

    class _Eth:
        block_number = 999

    class _W3:
        eth = _Eth()

    sim.w3 = _W3()
    resets: list = []
    sim._reset_fork = lambda block_number=None: resets.append(block_number)

    assert sim.pin_read_fork(1, 999) is False         # already at block → no re-fork
    assert resets == []
    assert sim.pin_read_fork(1, 1000) is True          # different block → re-fork
    assert resets == [1000]
