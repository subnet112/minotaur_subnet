"""Benchmark-simulation determinism (PR-1 of the gas tie-break rollout).

Three nondeterminism sources in the benchmark sim path are pinned so two
validators simulating the same plan at the same fork pin get bit-identical
results:

1. sim block timestamps — every block mined inside a simulation is pinned to
   ``fork_block_timestamp + SIM_BLOCK_TIMESTAMP_OFFSET`` (anvil_simulator);
2. the synthetic benchmark order's ``deadline`` — derived from the pinned
   fork block's timestamp instead of wall clock (orchestrator);
3. the synthetic benchmark ``order_id`` — a deterministic digest of the
   round-stable scenario identity instead of ``uuid4`` (orchestrator).

These tests cover the derivations + fallbacks without a live anvil (the
RPC-level semantics — one-shot pins, equal-timestamp acceptance, revert/reset
interplay — were verified empirically against anvil 1.5.1 and are documented
on SIM_BLOCK_TIMESTAMP_OFFSET).
"""

from __future__ import annotations

import re
import time

import pytest

from minotaur_subnet.harness.orchestrator import (
    _BENCHMARK_ORDER_DEADLINE_SECS,
    _benchmark_order_id,
    _build_benchmark_intent_order,
)
from minotaur_subnet.shared.types import ExecutionPlan, IntentState
from minotaur_subnet.simulator.anvil_simulator import (
    SIM_BLOCK_TIMESTAMP_OFFSET,
    AnvilSimulator,
    MultiChainSimulator,
)

_APP = "0x0CDe9A7Eb2313662f3E9d64Ab4A6bb0Cf5A4A000"
_FORK_BLOCK = 32_000_000
_FORK_TS = 1_751_500_000


# ── order_id derivation ─────────────────────────────────────────────────────


class TestBenchmarkOrderId:
    def test_same_inputs_same_id(self):
        a = _benchmark_order_id(_APP, 8453, "swap_small", "swap", _FORK_BLOCK)
        b = _benchmark_order_id(_APP, 8453, "swap_small", "swap", _FORK_BLOCK)
        assert a == b

    def test_matches_legacy_format(self):
        oid = _benchmark_order_id(_APP, 8453, "swap_small", "swap", _FORK_BLOCK)
        assert re.fullmatch(r"bench_[0-9a-f]{16}", oid)

    def test_different_scenarios_different_ids(self):
        base = _benchmark_order_id(_APP, 8453, "swap_small", "swap", _FORK_BLOCK)
        assert _benchmark_order_id(_APP, 8453, "swap_large", "swap", _FORK_BLOCK) != base
        assert _benchmark_order_id(_APP, 8453, "hist:ord_abc123", "swap", _FORK_BLOCK) != base

    def test_different_app_or_chain_or_fn_different_ids(self):
        base = _benchmark_order_id(_APP, 8453, "swap_small", "swap", _FORK_BLOCK)
        other_app = _APP[:-1] + "1"
        assert _benchmark_order_id(other_app, 8453, "swap_small", "swap", _FORK_BLOCK) != base
        assert _benchmark_order_id(_APP, 1, "swap_small", "swap", _FORK_BLOCK) != base
        assert _benchmark_order_id(_APP, 8453, "swap_small", "limit", _FORK_BLOCK) != base

    def test_different_fork_block_different_ids(self):
        a = _benchmark_order_id(_APP, 8453, "swap_small", "swap", _FORK_BLOCK)
        b = _benchmark_order_id(_APP, 8453, "swap_small", "swap", _FORK_BLOCK + 1)
        assert a != b

    def test_contract_case_insensitive(self):
        # Cross-validator stability: checksummed vs lowercased contract
        # renderings must not split the id.
        a = _benchmark_order_id(_APP, 8453, "swap_small", "swap", _FORK_BLOCK)
        b = _benchmark_order_id(_APP.lower(), 8453, "swap_small", "swap", _FORK_BLOCK)
        assert a == b


# ── intent_order build: deadline + id threading ─────────────────────────────


def _state(scenario_name: str = "swap_small", params: dict | None = None) -> IntentState:
    return IntentState(
        contract_address=_APP,
        chain_id=8453,
        nonce=0,
        owner="0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266",
        raw_params=dict(params or {"input_token": "0xA", "input_amount": "1000"}),
        control={"_intent_function": "swap", "_scenario_name": scenario_name},
    )


def _plan() -> ExecutionPlan:
    return ExecutionPlan(intent_id="bench", interactions=[], deadline=0, nonce=0)


@pytest.fixture()
def _patched_encoder(monkeypatch):
    """Stub the manifest-driven intentParams encoder — these tests target the
    order envelope (id/deadline), not the ABI encoding."""
    import minotaur_subnet.api.services.app_service as app_service

    monkeypatch.setattr(
        app_service,
        "build_intent_params_hex_from_manifest",
        lambda *a, **k: "deadbeef",
    )
    return {"benchmark_scenarios": []}  # any truthy manifest


class TestBuildBenchmarkIntentOrder:
    def test_deadline_from_fork_timestamp(self, _patched_encoder):
        order = _build_benchmark_intent_order(
            _state(), _plan(), _patched_encoder,
            fork_block=_FORK_BLOCK, fork_timestamp=_FORK_TS,
        )
        assert order is not None
        assert order["deadline"] == _FORK_TS + _BENCHMARK_ORDER_DEADLINE_SECS

    def test_deadline_wall_clock_fallback(self, _patched_encoder):
        before = int(time.time())
        order = _build_benchmark_intent_order(_state(), _plan(), _patched_encoder)
        after = int(time.time())
        assert order is not None
        assert (
            before + _BENCHMARK_ORDER_DEADLINE_SECS
            <= order["deadline"]
            <= after + _BENCHMARK_ORDER_DEADLINE_SECS
        )

    def test_order_identical_across_validators(self, _patched_encoder):
        """Two hosts building the same round's scenario produce the
        byte-identical order dict (the A/B acceptance property)."""
        kwargs = dict(fork_block=_FORK_BLOCK, fork_timestamp=_FORK_TS)
        a = _build_benchmark_intent_order(_state(), _plan(), _patched_encoder, **kwargs)
        b = _build_benchmark_intent_order(_state(), _plan(), _patched_encoder, **kwargs)
        assert a == b

    def test_order_id_ignores_quote_enrichment(self, _patched_encoder):
        """Champion/challenger sims of the same scenario share the order_id
        even when quote enrichment leaves different params on the state —
        removes the champ/chal calldata asymmetry."""
        a = _build_benchmark_intent_order(
            _state(params={"input_token": "0xA", "input_amount": "1000"}),
            _plan(), _patched_encoder,
            fork_block=_FORK_BLOCK, fork_timestamp=_FORK_TS,
        )
        b = _build_benchmark_intent_order(
            _state(params={"input_token": "0xA", "input_amount": "1000",
                           "quoted_output": "987654"}),
            _plan(), _patched_encoder,
            fork_block=_FORK_BLOCK, fork_timestamp=_FORK_TS,
        )
        assert a is not None and b is not None
        assert a["order_id"] == b["order_id"]

    def test_distinct_scenarios_distinct_ids(self, _patched_encoder):
        kwargs = dict(fork_block=_FORK_BLOCK, fork_timestamp=_FORK_TS)
        a = _build_benchmark_intent_order(
            _state("swap_small"), _plan(), _patched_encoder, **kwargs)
        b = _build_benchmark_intent_order(
            _state("swap_large"), _plan(), _patched_encoder, **kwargs)
        assert a["order_id"] != b["order_id"]


# ── simulator: fork-anchor cache + timestamp pin ────────────────────────────


class _RecordingProvider:
    def __init__(self, responses: dict[str, object] | None = None):
        self.calls: list[tuple[str, list]] = []
        self._responses = responses or {}

    def make_request(self, method: str, params: list):
        self.calls.append((method, list(params)))
        return {"result": self._responses.get(method, True)}


class _ExplodingEth:
    """Fails any block fetch — proves a code path never touched the RPC."""

    def get_block(self, *_a, **_k):  # pragma: no cover - failure is the assert
        raise AssertionError("unexpected eth_getBlockByNumber")


def _bare_sim(fork_number=None, fork_ts=None, ts_cache=None) -> AnvilSimulator:
    sim = AnvilSimulator.__new__(AnvilSimulator)  # skip __init__ (no RPC)
    sim.rpc_url = "http://test:0"
    sim._fork_block_number = fork_number
    sim._fork_block_timestamp = fork_ts
    sim._block_ts_cache = dict(ts_cache or {})
    return sim


class TestSimulatorForkAnchor:
    def test_get_block_timestamp_anchor(self):
        sim = _bare_sim(fork_number=_FORK_BLOCK, fork_ts=_FORK_TS,
                        ts_cache={_FORK_BLOCK: _FORK_TS})

        class _W3:
            eth = _ExplodingEth()

        sim.w3 = _W3()
        # None → the current fork anchor, no RPC.
        assert sim.get_block_timestamp() == _FORK_TS
        # Cached header → no RPC.
        assert sim.get_block_timestamp(8453, _FORK_BLOCK) == _FORK_TS

    def test_get_block_timestamp_unresolvable_is_none(self):
        sim = _bare_sim()

        class _FailingEth:
            def get_block(self, *_a, **_k):
                raise RuntimeError("boom")

        class _W3:
            eth = _FailingEth()

        sim.w3 = _W3()
        assert sim.get_block_timestamp() is None
        assert sim.get_block_timestamp(8453, _FORK_BLOCK) is None

    def test_pin_next_block_timestamp_pins_anchor_plus_offset(self):
        sim = _bare_sim(fork_number=_FORK_BLOCK, fork_ts=_FORK_TS)
        provider = _RecordingProvider()

        class _W3:
            pass

        sim.w3 = _W3()
        sim.w3.provider = provider
        sim._pin_next_block_timestamp()
        assert provider.calls == [
            ("evm_setNextBlockTimestamp", [_FORK_TS + SIM_BLOCK_TIMESTAMP_OFFSET]),
        ]

    def test_pin_is_noop_without_anchor(self):
        sim = _bare_sim(fork_ts=None)
        provider = _RecordingProvider()

        class _W3:
            pass

        sim.w3 = _W3()
        sim.w3.provider = provider
        sim._pin_next_block_timestamp()
        assert provider.calls == []

    def test_pin_failure_never_raises(self):
        sim = _bare_sim(fork_ts=_FORK_TS)

        class _W3:
            class provider:  # noqa: N801 - stub
                @staticmethod
                def make_request(_method, _params):
                    raise RuntimeError("rpc down")

        sim.w3 = _W3()
        sim._pin_next_block_timestamp()  # must not raise

    def test_multichain_routes_by_chain(self):
        base = _bare_sim(fork_number=_FORK_BLOCK, fork_ts=_FORK_TS,
                         ts_cache={_FORK_BLOCK: _FORK_TS})

        class _W3:
            eth = _ExplodingEth()

        base.w3 = _W3()
        mcs = MultiChainSimulator.__new__(MultiChainSimulator)
        mcs.simulators = {8453: base}
        mcs.default_chain_id = 8453
        assert mcs.get_block_timestamp(8453, _FORK_BLOCK) == _FORK_TS
        assert mcs.get_block_timestamp(8453) == _FORK_TS
        # unknown chain falls back to the default-chain simulator
        assert mcs.get_block_timestamp(1, _FORK_BLOCK) == _FORK_TS

    def test_multichain_no_simulator_is_none(self):
        mcs = MultiChainSimulator.__new__(MultiChainSimulator)
        mcs.simulators = {}
        mcs.default_chain_id = 8453
        assert mcs.get_block_timestamp(8453, _FORK_BLOCK) is None
