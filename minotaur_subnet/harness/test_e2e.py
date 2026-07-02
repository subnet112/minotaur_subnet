"""End-to-end integration test for the IntentSolver submission pipeline.

Tests the complete lifecycle:
    submit → screen (stages 1-3) → benchmark → score → adopt

Requires Docker to be running. These tests build real images and run
real containers, so they're slower than unit tests (~30-60s total).

Run:
    pytest minotaur_subnet/harness/test_e2e.py -v

Skip if Docker is not available:
    pytest minotaur_subnet/harness/test_e2e.py -v -m "not docker"
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

# Check if Docker is available
def _docker_available() -> bool:
    try:
        result = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            timeout=10,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False

DOCKER_OK = _docker_available()
SKIP_REASON = "Docker not available or not running"

# Project paths
REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_SOLVER_DIR = REPO_ROOT / "minotaur_subnet" / "docker" / "example-solver"


@unittest.skipUnless(DOCKER_OK, SKIP_REASON)
class TestE2ESubmissionPipeline(unittest.TestCase):
    """End-to-end test for the full submission → adopt pipeline."""

    @classmethod
    def setUpClass(cls):
        """Ensure solver-base image exists (built once for all tests)."""
        result = subprocess.run(
            ["docker", "image", "inspect", "ghcr.io/subnet112/solver-base:v1"],
            capture_output=True,
        )
        if result.returncode != 0:
            # Build solver-base
            subprocess.run(
                [
                    "docker", "build",
                    "-t", "ghcr.io/subnet112/solver-base:v1",
                    "-f", str(REPO_ROOT / "minotaur_subnet" / "docker" / "Dockerfile.solver-base"),
                    str(REPO_ROOT),
                ],
                check=True,
                capture_output=True,
                timeout=120,
            )

    def setUp(self):
        """Create a temp directory with a valid solver repo."""
        self.tmpdir = tempfile.mkdtemp(prefix="e2e-solver-")
        self.image_tag = f"e2e-test-solver-{int(time.time())}:latest"

        # Copy the example-solver into the temp directory
        for fname in ["Dockerfile", "solver.py", "requirements.txt"]:
            src = EXAMPLE_SOLVER_DIR / fname
            if src.exists():
                shutil.copy2(src, self.tmpdir)

        # Create a README.md (required by stage 1)
        readme = Path(self.tmpdir) / "README.md"
        readme.write_text("# Test Solver\nE2E test solver for pipeline validation.\n")

    def tearDown(self):
        """Clean up temp directory and Docker image."""
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        # Clean up test Docker image
        subprocess.run(
            ["docker", "rmi", "-f", self.image_tag],
            capture_output=True,
        )
        # Also clean up the screening-tagged image
        subprocess.run(
            ["docker", "rmi", "-f", f"solver-e2etest12345:screening"],
            capture_output=True,
        )

    # ── Stage 1: Static Checks ───────────────────────────────────────────

    def test_stage_1_passes_valid_repo(self):
        """Stage 1 should pass for a properly structured repo."""
        from minotaur_subnet.harness.screening import run_stage_1

        result = run_stage_1(self.tmpdir)
        self.assertTrue(result.passed, f"Stage 1 failed: {result.details}")
        self.assertEqual(result.stage, 1)

    def test_stage_1_rejects_missing_solver(self):
        """Stage 1 should reject repo without solver.py."""
        from minotaur_subnet.harness.screening import run_stage_1

        os.remove(Path(self.tmpdir) / "solver.py")
        result = run_stage_1(self.tmpdir)
        self.assertFalse(result.passed)
        self.assertEqual(result.error_code, "missing_solver_py")

    def test_stage_1_rejects_custom_entrypoint(self):
        """Stage 1 should reject Dockerfile with CMD/ENTRYPOINT."""
        from minotaur_subnet.harness.screening import run_stage_1

        dockerfile = Path(self.tmpdir) / "Dockerfile"
        dockerfile.write_text(
            "FROM ghcr.io/subnet112/solver-base:v1\n"
            "COPY solver.py /app/solver/solver.py\n"
            "CMD [\"python\", \"solver.py\"]\n"
        )
        result = run_stage_1(self.tmpdir)
        self.assertFalse(result.passed)
        self.assertEqual(result.error_code, "custom_entrypoint")

    # ── Stage 2: Build Check ─────────────────────────────────────────────

    def test_stage_2_builds_and_validates(self):
        """Stage 2 should build image and validate import + init."""
        from minotaur_subnet.harness.screening import run_stage_2

        result = asyncio.run(run_stage_2(self.tmpdir, self.image_tag))
        self.assertTrue(result.passed, f"Stage 2 failed: {result.details}")
        self.assertIn("ExampleSolver", result.details)

    def test_stage_2_rejects_broken_solver(self):
        """Stage 2 should reject solver that fails to import."""
        from minotaur_subnet.harness.screening import run_stage_2

        # Write a broken solver.py
        (Path(self.tmpdir) / "solver.py").write_text(
            "raise ImportError('intentionally broken')\n"
        )
        result = asyncio.run(run_stage_2(self.tmpdir, self.image_tag))
        self.assertFalse(result.passed)
        self.assertEqual(result.error_code, "import_failed")

    # ── Stage 3: Smoke Test ──────────────────────────────────────────────

    def test_stage_3_smoke_test_passes(self):
        """Stage 3 should pass with valid solver producing plans."""
        from minotaur_subnet.harness.screening import run_stage_2, run_stage_3

        # First build the image (stage 2 prerequisite)
        s2 = asyncio.run(run_stage_2(self.tmpdir, self.image_tag))
        self.assertTrue(s2.passed, f"Stage 2 prerequisite failed: {s2.details}")

        # Now run stage 3 smoke test
        result = asyncio.run(run_stage_3(self.image_tag))
        self.assertTrue(result.passed, f"Stage 3 failed: {result.details}")
        self.assertIn("plans valid", result.details)

    # ── Full Screening Pipeline ──────────────────────────────────────────

    def test_full_screening_pipeline(self):
        """Full 3-stage screening should pass for example solver."""
        from minotaur_subnet.harness.screening import ScreeningPipeline

        pipeline = ScreeningPipeline()
        result = asyncio.run(pipeline.run_all(self.tmpdir, commit_hash="e2etest12345"))
        self.assertTrue(result.passed, f"Screening failed: {result.rejection_reason}")
        self.assertIsNotNone(result.image_tag)
        self.assertEqual(result.solver_name, "ExampleSolver")
        self.assertEqual(len(result.stages), 3)
        for stage in result.stages:
            self.assertTrue(stage.passed, f"Stage {stage.stage} failed: {stage.details}")

    # ── Orchestrator + Harness ───────────────────────────────────────────

    def test_orchestrator_docker_session(self):
        """Orchestrator should start a Docker session and communicate."""
        from minotaur_subnet.harness.orchestrator import SolverOrchestrator

        # Build image first
        subprocess.run(
            ["docker", "build", "-t", self.image_tag, self.tmpdir],
            check=True,
            capture_output=True,
            timeout=60,
        )

        async def _run():
            orch = SolverOrchestrator()
            session = await orch.start_docker(self.image_tag)
            try:
                await session.initialize({"chain_ids": [1]})
                meta = await session.metadata()
                self.assertEqual(meta.name, "ExampleSolver")
                self.assertEqual(meta.version, "0.1.0")
                self.assertIn("swap", meta.supported_intent_types)
            finally:
                await session.shutdown()

        asyncio.run(_run())

    def test_benchmark_with_scoring(self):
        """Full benchmark run with scoring against Docker container."""
        from minotaur_subnet.harness.orchestrator import (
            SolverOrchestrator,
            BenchmarkConfig,
            run_benchmark,
        )
        from minotaur_subnet.harness.snapshot import build_synthetic_intents
        from minotaur_subnet.shared.types import ScoreResult

        # Build image first
        subprocess.run(
            ["docker", "build", "-t", self.image_tag, self.tmpdir],
            check=True,
            capture_output=True,
            timeout=60,
        )

        # Build a simple scoring function
        async def mock_score_fn(app_id, plan, simulation, state):
            # Score based on whether plan has interactions
            score = 0.8 if plan and len(plan.interactions) > 0 else 0.0
            return ScoreResult(
                score=score,
                breakdown={"has_interactions": score},
            )

        intents = build_synthetic_intents()

        async def _run():
            orch = SolverOrchestrator()
            session = await orch.start_docker(self.image_tag)
            try:
                results = await run_benchmark(
                    session,
                    intents,
                    config=BenchmarkConfig(chain_ids=[1]),
                    score_fn=mock_score_fn,
                )
                return results
            finally:
                await session.shutdown()

        results = asyncio.run(_run())

        # Should have results for all synthetic intents
        self.assertEqual(len(results), len(intents))

        # At least the swap intents should produce plans with scores
        swap_results = [r for r in results if "swap" in r.intent_id]
        self.assertTrue(len(swap_results) > 0)

        for r in swap_results:
            if r.plan is not None:
                self.assertGreater(r.score, 0.0, f"Swap {r.intent_id} scored 0")
                self.assertIsNotNone(r.plan_score)

    # ── Submission Store Integration ─────────────────────────────────────

    def test_submission_lifecycle(self):
        """Submission store tracks full lifecycle correctly."""
        from minotaur_subnet.harness.submission_store import (
            SubmissionStore,
            SubmissionStatus,
        )
        from minotaur_subnet.epoch.relative_scoring import has_delivered_value_rows

        store = SubmissionStore()

        # Create submission
        sub = store.create(
            repo_url="https://github.com/test/solver",
            commit_hash="abc123def456",
            epoch=42,
            hotkey="5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY",
        )
        self.assertEqual(sub.status, SubmissionStatus.QUEUED)

        # Progress through screening
        store.update_status(sub.submission_id, SubmissionStatus.SCREENING_STAGE_1)
        store.set_screening_result(sub.submission_id, stage=1, passed=True, details="OK")
        refreshed = store.get(sub.submission_id)
        self.assertTrue(refreshed.screening["stage_1"]["passed"])

        store.update_status(sub.submission_id, SubmissionStatus.SCREENING_STAGE_2)
        store.set_screening_result(sub.submission_id, stage=2, passed=True, details="Built")
        store.set_image_tag(sub.submission_id, "solver-abc123:screening")
        store.set_solver_info(sub.submission_id, name="ExampleSolver", version="0.1.0")

        store.update_status(sub.submission_id, SubmissionStatus.SCREENING_STAGE_3)
        store.set_screening_result(sub.submission_id, stage=3, passed=True, details="3/3 plans")

        # Move to benchmarking
        store.update_status(sub.submission_id, SubmissionStatus.BENCHMARKING)
        refreshed = store.get(sub.submission_id)
        self.assertEqual(refreshed.status, SubmissionStatus.BENCHMARKING)

        # Set benchmark results. Under the relative / single-stage contract there
        # is no scalar benchmark_score: a submission is SCORED iff at least one
        # order DELIVERED value (raw_output parses to > 0), which replaced the
        # retired `benchmark_score > 0` gate. benchmark_rank is now a display-only
        # relative rank. Build per-order rows where one order delivered value so
        # this submission passes the validity gate and scores.
        per_intent = [
            {"intent_id": "app:swap", "raw_output": "1000"},
            {"intent_id": "app:limit", "raw_output": "0"},
        ]
        self.assertTrue(has_delivered_value_rows(per_intent))
        store.set_benchmark_result(
            sub.submission_id,
            valid=has_delivered_value_rows(per_intent),
            rank=1,
            details={
                "total_intents": 3,
                "plans_generated": 2,
                "per_intent": per_intent,
            },
        )
        refreshed = store.get(sub.submission_id)
        self.assertEqual(refreshed.status, SubmissionStatus.SCORED)
        self.assertEqual(refreshed.benchmark_rank, 1)
        # The delivered-value gate (which replaced benchmark_score > 0) is satisfied.
        self.assertTrue(
            has_delivered_value_rows(refreshed.benchmark_details["per_intent"])
        )

        # Adopt
        store.adopt(sub.submission_id)
        refreshed = store.get(sub.submission_id)
        self.assertEqual(refreshed.status, SubmissionStatus.ADOPTED)

    # ── Full E2E Pipeline ────────────────────────────────────────────────

    def test_full_e2e_submit_to_adopt(self):
        """Full end-to-end: screen → benchmark → score → adopt.

        This is the integration test that exercises the entire pipeline
        from submission creation to champion adoption.
        """
        from minotaur_subnet.harness.screening import ScreeningPipeline
        from minotaur_subnet.harness.submission_store import (
            SubmissionStore,
            SubmissionStatus,
        )
        from minotaur_subnet.harness.orchestrator import (
            SolverOrchestrator,
            BenchmarkConfig,
            run_benchmark,
        )
        from minotaur_subnet.harness.snapshot import build_synthetic_intents
        from minotaur_subnet.shared.types import ScoreResult
        from minotaur_subnet.epoch.relative_scoring import has_delivered_value_rows

        store = SubmissionStore()
        commit_hash = f"e2e{int(time.time())}"

        # 1. Create submission
        sub = store.create(
            repo_url="file://" + self.tmpdir,
            commit_hash=commit_hash,
            epoch=1,
            hotkey="5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY",
        )
        self.assertEqual(sub.status, SubmissionStatus.QUEUED)

        # 2. Run screening pipeline
        pipeline = ScreeningPipeline()
        screening_result = asyncio.run(
            pipeline.run_all(self.tmpdir, commit_hash=commit_hash)
        )

        # Update store with screening results
        for stage_result in screening_result.stages:
            store.update_status(
                sub.submission_id,
                SubmissionStatus(f"screening_stage_{stage_result.stage}"),
            )
            store.set_screening_result(
                sub.submission_id,
                stage=stage_result.stage,
                passed=stage_result.passed,
                duration_ms=stage_result.duration_ms,
                details=stage_result.details,
                error_code=stage_result.error_code,
            )

        self.assertTrue(
            screening_result.passed,
            f"Screening failed: {screening_result.rejection_reason}",
        )

        # Record image + solver metadata
        store.set_image_tag(sub.submission_id, screening_result.image_tag)
        store.set_solver_info(
            sub.submission_id,
            name=screening_result.solver_name,
            version=screening_result.solver_version,
        )

        # 3. Move to benchmarking
        store.update_status(sub.submission_id, SubmissionStatus.BENCHMARKING)
        refreshed = store.get(sub.submission_id)
        self.assertEqual(refreshed.status, SubmissionStatus.BENCHMARKING)
        self.assertIsNotNone(refreshed.image_tag)

        # 4. Run benchmark with scoring
        intents = build_synthetic_intents()

        async def score_fn(app_id, plan, simulation, state):
            score = 0.85 if plan and len(plan.interactions) > 0 else 0.1
            return ScoreResult(
                score=score,
                breakdown={"plan_quality": score},
            )

        async def _benchmark():
            orch = SolverOrchestrator()
            session = await orch.start_docker(refreshed.image_tag)
            try:
                return await run_benchmark(
                    session,
                    intents,
                    config=BenchmarkConfig(chain_ids=[1]),
                    score_fn=score_fn,
                )
            finally:
                await session.shutdown()

        results = asyncio.run(_benchmark())
        self.assertTrue(len(results) > 0, "No benchmark results")

        # Determine which orders delivered value. Under the relative / single-stage
        # contract, a submission is SCORED iff at least one order DELIVERED value
        # (its raw_output parses to > 0) — this replaced the scalar avg-score > 0
        # gate. Build per-order rows carrying an exact-decimal-wei raw_output: an
        # order that produced a scoring plan delivered value, the rest delivered
        # nothing.
        scored = [r for r in results if r.error is None and r.plan is not None]
        per_intent = [
            {
                "intent_id": r.intent_id,
                "raw_output": (
                    "1000000000000000000"
                    if (r.error is None and r.plan is not None and r.score > 0)
                    else "0"
                ),
                "score": r.score,
                "error": r.error,
                "has_plan": r.plan is not None,
            }
            for r in results
        ]
        valid = has_delivered_value_rows(per_intent)

        # 5. Record benchmark results
        store.set_benchmark_result(
            sub.submission_id,
            valid=valid,
            rank=1,
            details={
                "total_intents": len(results),
                "plans_generated": len(scored),
                "per_intent": per_intent,
            },
        )

        refreshed = store.get(sub.submission_id)
        self.assertEqual(refreshed.status, SubmissionStatus.SCORED)
        self.assertTrue(
            has_delivered_value_rows(refreshed.benchmark_details["per_intent"]),
            "No order delivered value during benchmarking",
        )

        # 6. Adopt as champion
        store.adopt(sub.submission_id)
        refreshed = store.get(sub.submission_id)
        self.assertEqual(refreshed.status, SubmissionStatus.ADOPTED)

        # Verify final state
        self.assertEqual(refreshed.solver_name, "ExampleSolver")
        self.assertIsNotNone(refreshed.benchmark_details)
        self.assertGreater(
            refreshed.benchmark_details["plans_generated"], 0,
            "No plans were generated during benchmarking",
        )

        # Print summary for debugging
        delivered = sum(
            1
            for row in refreshed.benchmark_details["per_intent"]
            if has_delivered_value_rows([row])
        )
        print(f"\n  E2E Pipeline Summary:")
        print(f"  Solver: {refreshed.solver_name} v{refreshed.solver_version}")
        print(f"  Rank:   {refreshed.benchmark_rank}")
        print(f"  Delivered: {delivered}/{refreshed.benchmark_details['total_intents']} orders")
        print(f"  Plans:  {refreshed.benchmark_details['plans_generated']}/{refreshed.benchmark_details['total_intents']}")
        print(f"  Status: {refreshed.status.value}")

        # Clean up the screening image
        subprocess.run(
            ["docker", "rmi", "-f", screening_result.image_tag],
            capture_output=True,
        )


@unittest.skipUnless(DOCKER_OK, SKIP_REASON)
class TestE2EHarnessProtocol(unittest.TestCase):
    """Tests for the harness stdin/stdout protocol via Docker container."""

    @classmethod
    def setUpClass(cls):
        """Ensure example-solver image is built."""
        result = subprocess.run(
            ["docker", "image", "inspect", "example-solver:test"],
            capture_output=True,
        )
        if result.returncode != 0:
            subprocess.run(
                [
                    "docker", "build",
                    "-t", "example-solver:test",
                    "-f", str(EXAMPLE_SOLVER_DIR / "Dockerfile"),
                    str(EXAMPLE_SOLVER_DIR),
                ],
                check=True,
                capture_output=True,
                timeout=60,
            )

    def _run_commands(self, commands: list[dict]) -> list[dict]:
        """Send JSON commands to the container and parse responses."""
        stdin_data = "\n".join(json.dumps(cmd) for cmd in commands) + "\n"

        result = subprocess.run(
            ["docker", "run", "--rm", "-i", "example-solver:test"],
            input=stdin_data,
            capture_output=True,
            text=True,
            timeout=30,
        )

        responses = []
        for line in result.stdout.strip().split("\n"):
            if line.strip():
                responses.append(json.loads(line))

        return responses

    def test_metadata_command(self):
        """Metadata command should return solver info."""
        responses = self._run_commands([
            {"command": "metadata"},
            {"command": "shutdown"},
        ])

        self.assertEqual(len(responses), 2)
        self.assertTrue(responses[0]["success"])
        meta = responses[0]["result"]
        self.assertEqual(meta["name"], "ExampleSolver")
        self.assertEqual(meta["version"], "0.1.0")

    def test_initialize_command(self):
        """Initialize command should succeed."""
        responses = self._run_commands([
            {"command": "initialize", "config": {"benchmark_mode": True}},
            {"command": "shutdown"},
        ])

        self.assertEqual(len(responses), 2)
        self.assertTrue(responses[0]["success"])

    def test_generate_plan_valid_swap(self):
        """Generate plan should produce a valid swap execution plan."""
        from minotaur_subnet.harness.snapshot import MONITORED_TOKENS

        eth_tokens = MONITORED_TOKENS[1]
        contract = "0x5aAdFB43eF8dAF45DD80F4676345b7676f1D70e3"

        responses = self._run_commands([
            {"command": "initialize", "config": {}},
            {
                "command": "generate_plan",
                "intent": {
                    "app_id": "e2e-swap",
                    "name": "E2E Swap",
                    "version": "1.0",
                    "intent_type": "swap",
                    "js_code": "",
                    "config": {
                        "supported_chains": [1],
                        "score_threshold": 0.5,
                        "on_chain_threshold": 5000,
                        "trigger_type": "user_triggered",
                        "max_gas": 500000,
                    },
                    "deployer": "0x0001",
                    "description": "test",
                },
                "state": {
                    "contract_address": contract,
                    "chain_id": 1,
                    "nonce": 1,
                    "owner": "0x0000000000000000000000000000000000000001",
                    "raw_params": {
                        "input_token": eth_tokens["USDC"],
                        "output_token": eth_tokens["WETH"],
                        "input_amount": "1000000000",
                    },
                },
                "snapshot": {
                    "chain_id": 1,
                    "block_number": 18500000,
                    "timestamp": 1700000000,
                    "prices": {"ETH/USD": 1850.0, "USDC/USD": 1.0},
                    "pool_states": {},
                    "balances": {},
                    "dex_config": {
                        "router": "0xE592427A0AEce92De3Edee1F18E0157C05861564",
                    },
                    "raw_state": {},
                },
            },
            {"command": "shutdown"},
        ])

        self.assertEqual(len(responses), 3)
        # Init OK
        self.assertTrue(responses[0]["success"])
        # Plan generated
        self.assertTrue(responses[1]["success"], f"Plan failed: {responses[1]}")
        plan = responses[1]["result"]
        self.assertEqual(plan["intent_id"], "e2e-swap")
        self.assertGreater(len(plan["interactions"]), 0)
        # Each interaction has valid target address
        for ix in plan["interactions"]:
            self.assertTrue(ix["target"].startswith("0x"))
            self.assertEqual(len(ix["target"]), 42)
            self.assertTrue(ix["call_data"].startswith("0x"))

    def test_check_trigger_returns_bool(self):
        """Check trigger should return a boolean."""
        contract = "0x5aAdFB43eF8dAF45DD80F4676345b7676f1D70e3"

        responses = self._run_commands([
            {"command": "initialize", "config": {}},
            {
                "command": "check_trigger",
                "intent": {
                    "app_id": "e2e-auto",
                    "name": "E2E Auto",
                    "version": "1.0",
                    "intent_type": "limit_order",
                    "js_code": "",
                    "config": {
                        "supported_chains": [1],
                        "score_threshold": 0.5,
                        "on_chain_threshold": 5000,
                        "trigger_type": "auto_triggered",
                        "max_gas": 500000,
                    },
                    "deployer": "0x0001",
                    "description": "test",
                },
                "state": {
                    "contract_address": contract,
                    "chain_id": 1,
                    "nonce": 1,
                    "owner": "0x0000000000000000000000000000000000000001",
                    "raw_params": {},
                },
                "snapshot": {
                    "chain_id": 1,
                    "block_number": 18500000,
                    "timestamp": 1700000000,
                    "prices": {},
                    "pool_states": {},
                    "balances": {},
                    "dex_config": {},
                    "raw_state": {},
                },
            },
            {"command": "shutdown"},
        ])

        self.assertEqual(len(responses), 3)
        self.assertTrue(responses[1]["success"])
        self.assertIsInstance(responses[1]["result"], bool)

    def test_serialize_restore_state(self):
        """Serialize and restore state should round-trip."""
        responses = self._run_commands([
            {"command": "initialize", "config": {}},
            {"command": "serialize_state"},
            {"command": "restore_state", "state_b64": "dGVzdA=="},  # base64("test")
            {"command": "shutdown"},
        ])

        self.assertEqual(len(responses), 4)
        # serialize_state returns base64 string
        self.assertTrue(responses[1]["success"])
        # restore_state succeeds
        self.assertTrue(responses[2]["success"])

    def test_invalid_command_returns_error(self):
        """Unknown command should return a protocol error."""
        responses = self._run_commands([
            {"command": "nonexistent_command"},
            {"command": "shutdown"},
        ])

        self.assertEqual(len(responses), 2)
        self.assertFalse(responses[0]["success"])
        self.assertIn("Unknown command", responses[0]["error"])


if __name__ == "__main__":
    unittest.main()
