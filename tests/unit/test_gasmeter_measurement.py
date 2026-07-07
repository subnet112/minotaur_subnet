"""GasMeter measurement plumbing (C1) — measurement ONLY, no verdict changes.

Covers the three cheap-insurance surfaces of the pre-refund gas plumbing:

1. the pure receipt-log parser ``parse_gas_measured`` (the meter self-test
   hook): present / absent / wrong-address / wrong-topic / bytes-vs-hexstr
   rows — the address+topic filter is what makes app-side spoofing of the
   ``GasMeasured`` event impossible;
2. the row writer ``_results_to_details``: a row carries ``gas_metered`` +
   ``gas_basis`` iff the probe measured it — mock rows and reverted/errored
   rows NEVER carry gas keys;
3. the benchmark-only gating: ``meter_gas`` defaults to False at every
   simulator entry point, the live rail (order processing / fee
   certification, which consumes receipt ``gas_used``) never passes it, and
   the orchestrator's benchmark sim call is the single site that does.

The end-to-end meter mechanism itself is exercised against a real anvil in
``test_gasmeter_anvil_integration.py`` (skipped when anvil is absent).
"""

from __future__ import annotations

import inspect
from pathlib import Path

from minotaur_subnet.harness.benchmark_worker import (
    GAS_BASIS,
    BenchmarkWorker,
    log_gas_shadow,
)
from minotaur_subnet.harness.orchestrator import BenchmarkResult
from minotaur_subnet.simulator.anvil_simulator import (
    GAS_MEASURED_TOPIC0,
    GAS_METER_RUNTIME_HEX,
    GAS_METER_SENDER_EOA,
    AnvilSimulator,
    parse_gas_measured,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]

RELAYER = "0x1111000000000000000000000000000000001111"
APP = "0x0CDe9A7Eb2313662f3E9d64Ab4A6bb0Cf5A4A000"


def _word(value: int) -> str:
    return "0x" + value.to_bytes(32, "big").hex()


def _mklog(
    address: str = RELAYER,
    topic0: str = GAS_MEASURED_TOPIC0,
    value: int = 73_978,
) -> dict:
    return {"address": address, "topics": [topic0], "data": _word(value)}


# ── 1. receipt-log parser (meter self-test hook) ─────────────────────────────


class TestParseGasMeasured:
    def test_present(self):
        assert parse_gas_measured([_mklog(value=73_978)], RELAYER) == 73_978

    def test_absent_no_logs(self):
        assert parse_gas_measured([], RELAYER) is None
        assert parse_gas_measured(None, RELAYER) is None

    def test_absent_on_revert_only_app_logs(self):
        # A reverted metered tx emits NO GasMeasured (structural: the meter
        # bubbles the revert before logging) — app-side logs don't count.
        logs = [_mklog(address=APP)]
        assert parse_gas_measured(logs, RELAYER) is None

    def test_wrong_address_rejected(self):
        # An app emitting its own GasMeasured(uint256) cannot spoof the
        # meter: its log carries the APP address, not the relayer's.
        assert parse_gas_measured([_mklog(address=APP, value=1)], RELAYER) is None

    def test_wrong_topic_rejected(self):
        wrong = "0x" + "ab" * 32
        assert parse_gas_measured([_mklog(topic0=wrong)], RELAYER) is None

    def test_case_insensitive_address_match(self):
        assert parse_gas_measured([_mklog(address=RELAYER.upper().replace("0X", "0x"))], RELAYER.lower()) == 73_978

    def test_bytes_fields(self):
        # web3 receipts carry HexBytes topics/data — bytes must parse too.
        log = {
            "address": RELAYER,
            "topics": [bytes.fromhex(GAS_MEASURED_TOPIC0[2:])],
            "data": (73_978).to_bytes(32, "big"),
        }
        assert parse_gas_measured([log], RELAYER) == 73_978

    def test_last_match_wins(self):
        logs = [_mklog(value=1), _mklog(address=APP, value=2), _mklog(value=3)]
        assert parse_gas_measured(logs, RELAYER) == 3

    def test_malformed_rows_skipped(self):
        logs = [
            {},                                  # empty
            {"address": RELAYER},                # no topics
            {"address": RELAYER, "topics": []},  # empty topics
            {"address": RELAYER, "topics": [GAS_MEASURED_TOPIC0], "data": "0x"},
            _mklog(value=42),
        ]
        assert parse_gas_measured(logs, RELAYER) == 42

    def test_vendored_runtime_and_topic_shape(self):
        # Provenance sanity on the vendored constants: 154-byte runtime,
        # topic0 == keccak("GasMeasured(uint256)") baked into the bytecode.
        assert GAS_METER_RUNTIME_HEX.startswith("0x")
        assert (len(GAS_METER_RUNTIME_HEX) - 2) // 2 == 154
        assert GAS_MEASURED_TOPIC0[2:] in GAS_METER_RUNTIME_HEX


# ── 2. row writer: gas keys iff measured ─────────────────────────────────────


def _details(results: list[BenchmarkResult]) -> dict:
    worker = BenchmarkWorker.__new__(BenchmarkWorker)  # _results_to_details is state-free
    return worker._results_to_details(results)


class TestRowWriterGasKeys:
    def test_metered_row_carries_both_keys(self):
        rows = _details([
            BenchmarkResult(intent_id="app:swap", gas_metered=73_978),
        ])["per_intent"]
        assert rows[0]["gas_metered"] == 73_978
        assert rows[0]["gas_basis"] == GAS_BASIS

    def test_unmeasured_row_carries_neither_key(self):
        rows = _details([BenchmarkResult(intent_id="app:swap")])["per_intent"]
        assert "gas_metered" not in rows[0]
        assert "gas_basis" not in rows[0]

    def test_mock_row_never_carries_gas_keys(self):
        # The orchestrator write gate forces gas_metered=None on mock rows;
        # the writer's None-check keeps the keys out.
        rows = _details([
            BenchmarkResult(intent_id="app:swap", mock_simulation=True),
        ])["per_intent"]
        assert rows[0]["mock_simulation"] is True
        assert "gas_metered" not in rows[0]
        assert "gas_basis" not in rows[0]

    def test_reverted_row_never_carries_gas_keys(self):
        rows = _details([
            BenchmarkResult(
                intent_id="app:swap",
                error="real_sim_reverted: boom",
                revert_reason="boom",
            ),
        ])["per_intent"]
        assert "gas_metered" not in rows[0]
        assert "gas_basis" not in rows[0]

    def test_gas_basis_constant_value(self):
        # Fleet-uniform measurement-version tag — bump only on a
        # re-mechanism of the measurement, never configurable.
        assert GAS_BASIS == "scoreintent_prerefund_v1"

    def test_benchmarkresult_default_is_none(self):
        assert BenchmarkResult(intent_id="x").gas_metered is None


# ── 3. benchmark-only gating ─────────────────────────────────────────────────


class TestMeterGasGating:
    def test_simulator_defaults_off(self):
        for fn in (
            AnvilSimulator._simulate_inner,
            AnvilSimulator._simulate_via_score_intent,
        ):
            p = inspect.signature(fn).parameters["meter_gas"]
            assert p.default is False
            assert p.kind is inspect.Parameter.KEYWORD_ONLY

    def test_live_rail_never_passes_meter_gas(self):
        # The live rail consumes receipt gas_used for fee certification
        # (validator/scoring_engine.py certify_fee call); metering there
        # would shift receipt.gasUsed ~+5.9k. Grep-proof: no live-rail
        # simulate() call site mentions meter_gas at all — the default-False
        # keyword keeps their direct-send path byte-identical.
        live_rail = [
            "minotaur_subnet/validator/scoring_engine.py",
            "minotaur_subnet/blockloop/simulation.py",
            "minotaur_subnet/blockloop/order_processor.py",
            "minotaur_subnet/blockloop/multi_leg.py",
            "minotaur_subnet/api/routes/orders.py",
            "minotaur_subnet/api/routes/apps.py",
            "minotaur_subnet/relayer/bridge_tracker.py",
        ]
        for rel in live_rail:
            src = (_REPO_ROOT / rel).read_text()
            assert "meter_gas" not in src, f"live rail file passes meter_gas: {rel}"

    def test_benchmark_path_is_the_single_on_switch(self):
        src = (_REPO_ROOT / "minotaur_subnet/harness/orchestrator.py").read_text()
        assert src.count("meter_gas=True") == 1

    def test_simulation_result_default_is_none(self):
        from minotaur_subnet.shared.types import SimulationResult

        assert SimulationResult(success=True).gas_metered is None


# ── [gas-shadow] soak log: None-safe over mixed row shapes ───────────────────


class TestWriteGateThroughRunBenchmark:
    """The bench-loop WRITE gate: br.gas_metered is set only for a real,
    successful sim with a positive probe value — and the benchmark caller
    chain is what threads meter_gas=True down to the simulator."""

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

    def _run(self, simulator, monkeypatch):
        import asyncio

        from minotaur_subnet.harness.orchestrator import (
            BenchmarkConfig,
            run_benchmark,
        )
        from minotaur_subnet.harness.test_harness import (
            make_intent,
            make_snapshot,
            make_state,
        )
        from minotaur_subnet.shared.types import ExecutionPlan, ScoreResult

        monkeypatch.setenv("ANVIL_RPC_URL", "http://localhost:8545")
        monkeypatch.delenv("SOLVER_READ_PROXY", raising=False)
        intent, state, snapshot = make_intent(), make_state(), make_snapshot()
        plan = ExecutionPlan(
            intent_id=intent.app_id, interactions=[], deadline=0, nonce=0,
        )

        async def score_fn(app_id, p, simulation, st):
            return ScoreResult(score=0.9)

        return asyncio.run(run_benchmark(
            self._FakeSession(plan),
            [(intent, state, snapshot)],
            config=BenchmarkConfig(chain_ids=[state.chain_id]),
            score_fn=score_fn,
            simulator=simulator,
        ))

    def test_successful_metered_sim_writes_value_and_threads_flag(self, monkeypatch):
        from minotaur_subnet.shared.types import SimulationResult

        seen_kwargs = {}

        class _MeteredSim:
            async def simulate(self, plan, **kwargs):
                seen_kwargs.update(kwargs)
                return SimulationResult(
                    success=True, gas_used=92_559,
                    on_chain_score=7500, gas_metered=73_978,
                )

        results = self._run(_MeteredSim(), monkeypatch)
        assert results[0].gas_metered == 73_978
        # The benchmark caller chain is the ON switch.
        assert seen_kwargs.get("meter_gas") is True

    def test_failed_sim_stays_none(self, monkeypatch):
        from minotaur_subnet.shared.types import SimulationResult

        class _RevertSim:
            async def simulate(self, plan, **kwargs):
                return SimulationResult(
                    success=False, error="execution reverted",
                    gas_metered=12_345,  # hostile/garbage value: gate must drop it
                )

        results = self._run(_RevertSim(), monkeypatch)
        assert results[0].gas_metered is None

    def test_nonpositive_probe_value_stays_none(self, monkeypatch):
        from minotaur_subnet.shared.types import SimulationResult

        class _ZeroSim:
            async def simulate(self, plan, **kwargs):
                return SimulationResult(
                    success=True, gas_used=92_559, gas_metered=0,
                )

        results = self._run(_ZeroSim(), monkeypatch)
        assert results[0].gas_metered is None

    def test_mock_simulation_stays_none(self, monkeypatch):
        # No simulator -> fabricated mock sim path: rows must never carry gas.
        results = self._run(None, monkeypatch)
        assert results[0].mock_simulation is True
        assert results[0].gas_metered is None


class TestGasShadowLog:
    def test_never_raises_on_mixed_and_malformed_rows(self):
        champ = [
            {"intent_id": "a", "gas_metered": 100, "gas_basis": GAS_BASIS},
            {"intent_id": "b"},                     # unmeasured dict row
            {"intent_id": "c", "gas_metered": 90, "gas_basis": "other_v0"},
            {"no_intent_id": True},                 # malformed
        ]
        chal = [
            BenchmarkResult(intent_id="a", gas_metered=95),   # attr row
            BenchmarkResult(intent_id="b", gas_metered=80),
            BenchmarkResult(intent_id="c", gas_metered=70),
            BenchmarkResult(intent_id="d", gas_metered=60),   # unjoined
        ]
        # display/log only — the contract is simply "never raises"
        log_gas_shadow(champ, chal, ctx="unit")
        log_gas_shadow(None, None, ctx="unit-empty")
        log_gas_shadow([], [], ctx="unit-empty2")

    def test_meter_sender_is_a_fixed_codeless_eoa_constant(self):
        assert GAS_METER_SENDER_EOA == "0x2222000000000000000000000000000000002222"
