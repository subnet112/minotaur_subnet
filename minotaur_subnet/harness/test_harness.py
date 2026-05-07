"""Tests for the benchmarking harness.

Tests the full protocol chain:
1. Protocol serialization/deserialization
2. Runner command dispatch (in-process, no Docker)
3. Orchestrator session via subprocess mode
4. End-to-end benchmark run
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import pickle
import sys
import tempfile
import unittest
from dataclasses import asdict
from typing import Any

from minotaur_subnet.shared.types import (
    AppIntentConfig,
    AppIntentDefinition,
    ExecutionPlan,
    Interaction,
    IntentState,
    TriggerType,
)
from minotaur_subnet.sdk.intent_solver import IntentSolver, MarketSnapshot, SolverMetadata
from minotaur_subnet.harness.protocol import (
    Command,
    HarnessRequest,
    HarnessResponse,
    make_initialize_request,
    make_generate_plan_request,
    make_check_trigger_request,
    make_benchmark_start_request,
    make_benchmark_end_request,
    make_serialize_state_request,
    make_restore_state_request,
    make_metadata_request,
    make_shutdown_request,
    parse_plan_response,
    dict_to_intent,
    dict_to_state,
    dict_to_snapshot,
)
from minotaur_subnet.harness.runner import SolverRunner, load_solver
from minotaur_subnet.v3.contexts import SwapIntentContext


# ── Test fixtures ────────────────────────────────────────────────────────

USDC = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
WETH = "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"
CONTRACT = "0x5aAdFB43eF8dAF45DD80F4676345b7676f1D70e3"


def make_intent() -> AppIntentDefinition:
    return AppIntentDefinition(
        app_id="test-001",
        name="Test Swap",
        version="1.0.0",
        intent_type="swap",
        js_code="//hidden",
        config=AppIntentConfig(
            supported_chains=[1],
            trigger_type=TriggerType.USER_TRIGGERED,
        ),
    )


def make_auto_intent() -> AppIntentDefinition:
    return AppIntentDefinition(
        app_id="test-auto-001",
        name="Test Auto",
        version="1.0.0",
        intent_type="rebalance",
        js_code="//hidden",
        config=AppIntentConfig(
            supported_chains=[1],
            trigger_type=TriggerType.AUTO_TRIGGERED,
        ),
    )


def make_state() -> IntentState:
    return IntentState(
        contract_address=CONTRACT,
        chain_id=1,
        nonce=42,
        owner="0x1111111111111111111111111111111111111111",
        raw_params={
            "input_token": USDC,
            "output_token": WETH,
            "input_amount": "1000000000",
            "min_output_amount": "500000000",
        },
    )


def make_snapshot() -> MarketSnapshot:
    return MarketSnapshot(
        chain_id=1,
        block_number=18500000,
        timestamp=1700000000,
        prices={"ETH/USD": 1850.0},
        pool_states={},
        dex_config={},
    )


class StubSolver(IntentSolver):
    """Minimal solver for testing the harness."""

    def __init__(self):
        self.initialized = False
        self.config = {}
        self.benchmark_started = False
        self.results_received = []
        self.state_data = b""

    def initialize(self, config):
        self.initialized = True
        self.config = config

    def generate_plan(self, intent, state, snapshot):
        return ExecutionPlan(
            intent_id=intent.app_id,
            interactions=[
                Interaction(
                    target=USDC,
                    value="0",
                    call_data="0xdeadbeef",
                    chain_id=snapshot.chain_id,
                ),
            ],
            deadline=snapshot.timestamp + 300,
            nonce=state.nonce,
            metadata={"solver": "stub"},
        )

    def check_trigger(self, intent, state, snapshot):
        return snapshot.prices.get("ETH/USD", 0) > 2000

    def on_benchmark_start(self, intent_count):
        self.benchmark_started = True

    def on_benchmark_end(self, results):
        self.results_received = results

    def serialize_state(self):
        return pickle.dumps({"scores": [0.8, 0.9]})

    def restore_state(self, data):
        self.state_data = data

    def metadata(self):
        return SolverMetadata(
            name="stub-solver",
            version="0.1.0",
            author="test",
            supported_intent_types=["swap", "rebalance"],
        )


# ═══════════════════════════════════════════════════════════════════════════════
#                          PROTOCOL TESTS
# ═══════════════════════════════════════════════════════════════════════════════


class TestProtocolSerialization(unittest.TestCase):
    """Test JSON protocol serialization round-trips."""

    def test_request_roundtrip(self):
        """HarnessRequest survives JSON serialization."""
        req = make_initialize_request({"chain_ids": [1, 8453]})
        json_str = req.to_json()
        parsed = HarnessRequest.from_json(json_str)
        self.assertEqual(parsed.command, Command.INITIALIZE)
        self.assertEqual(parsed.params["config"]["chain_ids"], [1, 8453])

    def test_response_ok_roundtrip(self):
        """Success response survives JSON serialization."""
        resp = HarnessResponse.ok({"score": 0.85})
        json_str = resp.to_json()
        parsed = HarnessResponse.from_json(json_str)
        self.assertTrue(parsed.success)
        self.assertEqual(parsed.result["score"], 0.85)

    def test_response_fail_roundtrip(self):
        """Error response survives JSON serialization."""
        resp = HarnessResponse.fail("Something broke", "ValueError")
        json_str = resp.to_json()
        parsed = HarnessResponse.from_json(json_str)
        self.assertFalse(parsed.success)
        self.assertEqual(parsed.error, "Something broke")
        self.assertEqual(parsed.error_type, "ValueError")

    def test_generate_plan_request_serialization(self):
        """generate_plan request serializes intent/state/snapshot."""
        req = make_generate_plan_request(make_intent(), make_state(), make_snapshot())
        json_str = req.to_json()
        data = json.loads(json_str)
        self.assertEqual(data["command"], "generate_plan")
        self.assertEqual(data["intent"]["app_id"], "test-001")
        self.assertEqual(data["state"]["contract_address"], CONTRACT)
        self.assertEqual(data["snapshot"]["block_number"], 18500000)

    def test_plan_response_parsing(self):
        """parse_plan_response extracts ExecutionPlan from response."""
        resp = HarnessResponse.ok({
            "intent_id": "test-001",
            "interactions": [
                {"target": USDC, "value": "0", "call_data": "0xaa", "chain_id": 1},
            ],
            "deadline": 1700000300,
            "nonce": 42,
            "metadata": {"route": "uniswap_v3"},
        })
        plan = parse_plan_response(resp)
        self.assertIsNotNone(plan)
        self.assertEqual(plan.intent_id, "test-001")
        self.assertEqual(len(plan.interactions), 1)
        self.assertEqual(plan.interactions[0].target, USDC)
        self.assertEqual(plan.deadline, 1700000300)
        self.assertEqual(plan.nonce, 42)
        self.assertEqual(plan.metadata["route"], "uniswap_v3")

    def test_plan_response_handles_camelcase(self):
        """parse_plan_response handles camelCase field names."""
        resp = HarnessResponse.ok({
            "intent_id": "test-001",
            "interactions": [
                {"target": USDC, "value": "0", "callData": "0xbb", "chainId": 8453},
            ],
            "deadline": 1700000300,
            "nonce": 1,
        })
        plan = parse_plan_response(resp)
        self.assertEqual(plan.interactions[0].call_data, "0xbb")
        self.assertEqual(plan.interactions[0].chain_id, 8453)

    def test_plan_response_failure_returns_none(self):
        """parse_plan_response returns None for failed responses."""
        resp = HarnessResponse.fail("solver crashed")
        plan = parse_plan_response(resp)
        self.assertIsNone(plan)

    def test_dict_to_intent(self):
        """dict_to_intent reconstructs AppIntentDefinition."""
        original = make_intent()
        d = asdict(original)
        reconstructed = dict_to_intent(d)
        self.assertEqual(reconstructed.app_id, original.app_id)
        self.assertEqual(reconstructed.intent_type, original.intent_type)
        self.assertEqual(
            reconstructed.config.trigger_type, TriggerType.USER_TRIGGERED,
        )

    def test_dict_to_state(self):
        """dict_to_state reconstructs IntentState."""
        original = make_state()
        d = asdict(original)
        reconstructed = dict_to_state(d)
        self.assertEqual(reconstructed.contract_address, original.contract_address)
        self.assertEqual(reconstructed.nonce, original.nonce)
        self.assertEqual(reconstructed.raw_params["input_token"], USDC)

    def test_dict_to_state_preserves_typed_context(self):
        """dict_to_state restores typed context and v3 metadata when present."""
        original = make_state()
        original.context_version = "v3"
        original.typed_context = SwapIntentContext(
            app_id="test-001",
            intent_function="swap",
            chain_id=1,
            owner=original.owner,
            contract_address=original.contract_address,
            nonce=original.nonce,
            raw_params=dict(original.raw_params),
            input_token=USDC,
            output_token=WETH,
            input_amount=1_000_000_000,
            min_output_amount=500_000_000,
            receiver=original.contract_address,
            fee_tier=3000,
        )
        d = asdict(original)
        reconstructed = dict_to_state(d)
        self.assertEqual(reconstructed.context_version, "v3")
        self.assertIsInstance(reconstructed.typed_context, SwapIntentContext)
        self.assertEqual(reconstructed.typed_context.input_amount, 1_000_000_000)

    def test_dict_to_snapshot(self):
        """dict_to_snapshot reconstructs MarketSnapshot."""
        original = make_snapshot()
        d = asdict(original)
        reconstructed = dict_to_snapshot(d)
        self.assertEqual(reconstructed.chain_id, original.chain_id)
        self.assertEqual(reconstructed.block_number, original.block_number)
        self.assertEqual(reconstructed.prices["ETH/USD"], 1850.0)


# ═══════════════════════════════════════════════════════════════════════════════
#                          RUNNER TESTS (IN-PROCESS)
# ═══════════════════════════════════════════════════════════════════════════════


class TestSolverRunner(unittest.TestCase):
    """Test the in-container runner using in-process I/O."""

    def _run_commands(self, commands: list[HarnessRequest]) -> list[HarnessResponse]:
        """Feed commands to a SolverRunner and collect responses."""
        input_lines = "\n".join(cmd.to_json() for cmd in commands) + "\n"
        input_stream = io.StringIO(input_lines)
        output_stream = io.StringIO()

        solver = StubSolver()
        runner = SolverRunner(solver, input_stream=input_stream, output_stream=output_stream)
        runner.run()

        output_stream.seek(0)
        responses = []
        for line in output_stream:
            line = line.strip()
            if line:
                responses.append(HarnessResponse.from_json(line))
        return responses

    def test_initialize(self):
        """Runner handles initialize command."""
        responses = self._run_commands([
            make_initialize_request({"chain_ids": [1]}),
            make_shutdown_request(),
        ])
        self.assertEqual(len(responses), 2)
        self.assertTrue(responses[0].success)

    def test_metadata(self):
        """Runner returns solver metadata."""
        responses = self._run_commands([
            make_metadata_request(),
            make_shutdown_request(),
        ])
        self.assertTrue(responses[0].success)
        self.assertEqual(responses[0].result["name"], "stub-solver")
        self.assertEqual(responses[0].result["version"], "0.1.0")

    def test_generate_plan(self):
        """Runner generates a plan and returns it as JSON."""
        responses = self._run_commands([
            make_initialize_request({}),
            make_generate_plan_request(make_intent(), make_state(), make_snapshot()),
            make_shutdown_request(),
        ])
        self.assertTrue(responses[1].success)
        plan = parse_plan_response(responses[1])
        self.assertIsNotNone(plan)
        self.assertEqual(plan.intent_id, "test-001")
        self.assertEqual(len(plan.interactions), 1)
        self.assertEqual(plan.nonce, 42)

    def test_check_trigger_false(self):
        """Runner returns False for trigger check below threshold."""
        snap = make_snapshot()  # ETH/USD = 1850 < 2000
        responses = self._run_commands([
            make_initialize_request({}),
            make_check_trigger_request(make_auto_intent(), make_state(), snap),
            make_shutdown_request(),
        ])
        self.assertTrue(responses[1].success)
        self.assertFalse(responses[1].result)

    def test_check_trigger_true(self):
        """Runner returns True for trigger check above threshold."""
        snap = make_snapshot()
        snap.prices["ETH/USD"] = 2100.0
        responses = self._run_commands([
            make_initialize_request({}),
            make_check_trigger_request(make_auto_intent(), make_state(), snap),
            make_shutdown_request(),
        ])
        self.assertTrue(responses[1].success)
        self.assertTrue(responses[1].result)

    def test_benchmark_lifecycle(self):
        """Runner handles full benchmark lifecycle."""
        responses = self._run_commands([
            make_initialize_request({"chain_ids": [1]}),
            make_benchmark_start_request(2),
            make_generate_plan_request(make_intent(), make_state(), make_snapshot()),
            make_generate_plan_request(make_intent(), make_state(), make_snapshot()),
            make_benchmark_end_request([
                {"intent_id": "test-001", "score": 0.85, "elapsed_ms": 100},
                {"intent_id": "test-002", "score": 0.72, "elapsed_ms": 200},
            ]),
            make_shutdown_request(),
        ])
        # All 6 commands should succeed
        self.assertEqual(len(responses), 6)
        for resp in responses:
            self.assertTrue(resp.success, f"Failed: {resp.error}")

    def test_serialize_restore_state(self):
        """Runner handles state serialization and restoration."""
        responses = self._run_commands([
            make_initialize_request({}),
            make_serialize_state_request(),
            make_shutdown_request(),
        ])
        self.assertTrue(responses[1].success)
        state_b64 = responses[1].result
        self.assertIsInstance(state_b64, str)

        # Decode and verify
        state_bytes = base64.b64decode(state_b64)
        data = pickle.loads(state_bytes)
        self.assertEqual(data, {"scores": [0.8, 0.9]})

        # Restore in a new runner
        responses2 = self._run_commands([
            make_initialize_request({}),
            make_restore_state_request(state_b64),
            make_shutdown_request(),
        ])
        self.assertTrue(responses2[1].success)

    def test_invalid_command(self):
        """Runner returns error for unknown commands."""
        input_stream = io.StringIO('{"command": "bogus_cmd"}\n')
        output_stream = io.StringIO()

        runner = SolverRunner(StubSolver(), input_stream, output_stream)
        runner.run()

        output_stream.seek(0)
        resp = HarnessResponse.from_json(output_stream.readline())
        self.assertFalse(resp.success)
        self.assertIn("Unknown command", resp.error)

    def test_malformed_json(self):
        """Runner returns error for malformed JSON."""
        input_stream = io.StringIO('not json at all\n{"command": "shutdown"}\n')
        output_stream = io.StringIO()

        runner = SolverRunner(StubSolver(), input_stream, output_stream)
        runner.run()

        output_stream.seek(0)
        lines = [l.strip() for l in output_stream if l.strip()]
        resp = HarnessResponse.from_json(lines[0])
        self.assertFalse(resp.success)
        self.assertEqual(resp.error_type, "ProtocolError")


# ═══════════════════════════════════════════════════════════════════════════════
#                          LOAD SOLVER TESTS
# ═══════════════════════════════════════════════════════════════════════════════


class TestLoadSolver(unittest.TestCase):
    """Test dynamic solver loading from file."""

    def test_load_valid_solver(self):
        """load_solver loads a valid solver.py file."""
        code = '''
from minotaur_subnet.sdk.intent_solver import IntentSolver, MarketSnapshot, SolverMetadata
from minotaur_subnet.shared.types import ExecutionPlan

class TestSolver(IntentSolver):
    def initialize(self, config):
        pass
    def generate_plan(self, intent, state, snapshot):
        return ExecutionPlan(intent_id="x", interactions=[], deadline=0, nonce=0)
    def metadata(self):
        return SolverMetadata(name="test", version="1.0.0", author="x")

SOLVER_CLASS = TestSolver
'''
        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
            f.write(code)
            f.flush()
            path = f.name

        try:
            solver = load_solver(path)
            self.assertIsInstance(solver, IntentSolver)
            meta = solver.metadata()
            self.assertEqual(meta.name, "test")
        finally:
            os.unlink(path)

    def test_load_missing_solver_class(self):
        """load_solver raises if SOLVER_CLASS is missing."""
        code = "x = 1\n"
        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
            f.write(code)
            f.flush()
            path = f.name

        try:
            with self.assertRaises(AttributeError):
                load_solver(path)
        finally:
            os.unlink(path)

    def test_load_nonexistent_file(self):
        """load_solver raises FileNotFoundError."""
        with self.assertRaises(FileNotFoundError):
            load_solver("/nonexistent/solver.py")

    def test_load_non_intentsolver_class(self):
        """load_solver raises TypeError if SOLVER_CLASS isn't IntentSolver."""
        code = "SOLVER_CLASS = int\n"
        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
            f.write(code)
            f.flush()
            path = f.name

        try:
            with self.assertRaises(TypeError):
                load_solver(path)
        finally:
            os.unlink(path)


# ═══════════════════════════════════════════════════════════════════════════════
#                     SUBPROCESS INTEGRATION TESTS
# ═══════════════════════════════════════════════════════════════════════════════


class TestSubprocessIntegration(unittest.TestCase):
    """Test the orchestrator with subprocess mode (no Docker required).

    These tests spawn a real Python subprocess running the harness runner
    and communicate via the JSON protocol — an end-to-end test of the
    full communication stack.
    """

    def setUp(self):
        """Create a temporary solver file."""
        self._solver_code = '''
from minotaur_subnet.sdk.intent_solver import IntentSolver, MarketSnapshot, SolverMetadata
from minotaur_subnet.shared.types import ExecutionPlan, Interaction

class E2ESolver(IntentSolver):
    def initialize(self, config):
        self.config = config

    def generate_plan(self, intent, state, snapshot):
        return ExecutionPlan(
            intent_id=intent.app_id,
            interactions=[
                Interaction(
                    target="0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
                    value="0",
                    call_data="0xaabbccdd",
                    chain_id=snapshot.chain_id,
                ),
            ],
            deadline=snapshot.timestamp + 300,
            nonce=state.nonce,
            metadata={"e2e": True},
        )

    def check_trigger(self, intent, state, snapshot):
        return snapshot.prices.get("ETH/USD", 0) > 2000

    def metadata(self):
        return SolverMetadata(
            name="e2e-solver",
            version="1.0.0",
            author="integration-test",
            supported_intent_types=["swap"],
        )

SOLVER_CLASS = E2ESolver
'''
        self._tmpfile = tempfile.NamedTemporaryFile(
            mode="w", suffix=".py", delete=False,
        )
        self._tmpfile.write(self._solver_code)
        self._tmpfile.flush()
        self._tmpfile.close()
        self._solver_path = self._tmpfile.name

    def tearDown(self):
        os.unlink(self._solver_path)

    def test_subprocess_initialize_and_metadata(self):
        """Start solver subprocess, initialize, get metadata."""
        async def _test():
            from minotaur_subnet.harness.orchestrator import SolverOrchestrator

            orch = SolverOrchestrator()
            session = await orch.start_subprocess(self._solver_path)

            try:
                await session.initialize({"chain_ids": [1]})
                meta = await session.metadata()
                self.assertEqual(meta.name, "e2e-solver")
                self.assertEqual(meta.version, "1.0.0")
            finally:
                await session.shutdown()

        asyncio.run(_test())

    def test_subprocess_generate_plan(self):
        """Generate a plan via subprocess and verify structure."""
        async def _test():
            from minotaur_subnet.harness.orchestrator import SolverOrchestrator

            orch = SolverOrchestrator()
            session = await orch.start_subprocess(self._solver_path)

            try:
                await session.initialize({})
                plan = await session.generate_plan(
                    make_intent(), make_state(), make_snapshot(),
                )
                self.assertIsNotNone(plan)
                self.assertEqual(plan.intent_id, "test-001")
                self.assertEqual(len(plan.interactions), 1)
                self.assertEqual(plan.nonce, 42)
                self.assertTrue(plan.metadata.get("e2e"))
            finally:
                await session.shutdown()

        asyncio.run(_test())

    def test_subprocess_check_trigger(self):
        """Check trigger via subprocess."""
        async def _test():
            from minotaur_subnet.harness.orchestrator import SolverOrchestrator

            orch = SolverOrchestrator()
            session = await orch.start_subprocess(self._solver_path)

            try:
                await session.initialize({})

                # Below threshold
                snap_low = make_snapshot()
                snap_low.prices["ETH/USD"] = 1800.0
                result_low = await session.check_trigger(
                    make_auto_intent(), make_state(), snap_low,
                )
                self.assertFalse(result_low)

                # Above threshold
                snap_high = make_snapshot()
                snap_high.prices["ETH/USD"] = 2100.0
                result_high = await session.check_trigger(
                    make_auto_intent(), make_state(), snap_high,
                )
                self.assertTrue(result_high)
            finally:
                await session.shutdown()

        asyncio.run(_test())

    def test_subprocess_full_benchmark(self):
        """Run a full benchmark via subprocess."""
        async def _test():
            from minotaur_subnet.harness.orchestrator import (
                SolverOrchestrator,
                BenchmarkConfig,
                run_benchmark,
            )

            orch = SolverOrchestrator()
            session = await orch.start_subprocess(self._solver_path)

            try:
                intents = [
                    (make_intent(), make_state(), make_snapshot()),
                    (make_intent(), make_state(), make_snapshot()),
                ]

                results = await run_benchmark(session, intents)
                self.assertEqual(len(results), 2)

                for r in results:
                    self.assertIsNone(r.error, f"Unexpected error: {r.error}")
                    self.assertIsNotNone(r.plan)
                    self.assertEqual(r.plan.intent_id, "test-001")
                    self.assertGreaterEqual(r.elapsed_ms, 0)
            finally:
                await session.shutdown()

        asyncio.run(_test())


if __name__ == "__main__":
    unittest.main()
