"""Tests for snapshot builder/serializer and screening pipeline.

Snapshot tests verify serialization round-trips and synthetic data.
Screening tests verify Stage 1 static checks using temp directories
(Stages 2-3 require Docker and are integration tests).
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from minotaur_subnet.shared.types import (
    AppIntentConfig,
    AppIntentDefinition,
    IntentState,
    PolicyTier,
    TriggerType,
)
from minotaur_subnet.sdk.intent_solver import MarketSnapshot
from minotaur_subnet.v3.contexts import SwapIntentContext
from minotaur_subnet.harness.snapshot import (
    SnapshotMeta,
    build_synthetic_snapshot,
    build_synthetic_intents,
    save_snapshot,
    load_snapshot,
    load_chain_snapshot,
    _dict_to_state,
    _state_to_dict,
    MONITORED_TOKENS,
    UNISWAP_V3_CONFIG,
)
from minotaur_subnet.harness.screening import (
    run_stage_1,
    ScreeningPipeline,
    StageResult,
)


# ═══════════════════════════════════════════════════════════════════════════════
#                          SNAPSHOT TESTS
# ═══════════════════════════════════════════════════════════════════════════════


class TestSnapshotSerialization(unittest.TestCase):
    """Test save/load round-trip for snapshots."""

    def test_save_and_load_roundtrip(self):
        """Snapshot survives save → load round-trip."""
        meta = SnapshotMeta(epoch=100, timestamp=1700000000, chains=[1])
        snapshot = MarketSnapshot(
            chain_id=1,
            block_number=18500000,
            timestamp=1700000000,
            prices={"ETH/USD": 1850.0, "USDC/USD": 1.0},
            pool_states={
                "0xpool1": {"token0": "0xa", "token1": "0xb", "fee": 3000},
            },
            balances={"0xa": "1000000"},
            dex_config={"router": "0xrouter"},
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            save_snapshot(tmpdir, meta, {1: snapshot})

            loaded_meta, loaded_snapshots, loaded_intents = load_snapshot(tmpdir)

            self.assertEqual(loaded_meta.epoch, 100)
            self.assertEqual(loaded_meta.timestamp, 1700000000)
            self.assertIn(1, loaded_snapshots)

            ls = loaded_snapshots[1]
            self.assertEqual(ls.chain_id, 1)
            self.assertEqual(ls.block_number, 18500000)
            self.assertEqual(ls.prices["ETH/USD"], 1850.0)
            self.assertIn("0xpool1", ls.pool_states)
            self.assertEqual(ls.balances["0xa"], "1000000")
            self.assertEqual(ls.dex_config["router"], "0xrouter")
            self.assertEqual(loaded_intents, [])

    def test_save_and_load_with_intents(self):
        """Intent definitions survive round-trip."""
        meta = SnapshotMeta(epoch=1, timestamp=1700000000, chains=[1])
        snapshot = MarketSnapshot(chain_id=1, block_number=1, timestamp=1700000000)

        intent = AppIntentDefinition(
            app_id="test-001",
            name="Test Swap",
            version="1.0.0",
            intent_type="swap",
            js_code="// scoring",
            config=AppIntentConfig(
                supported_chains=[1],
                trigger_type=TriggerType.USER_TRIGGERED,
                score_threshold=0.6,
            ),
        )
        state = IntentState(
            contract_address="0x1234567890abcdef1234567890abcdef12345678",
            chain_id=1,
            nonce=42,
            owner="0xowner",
            raw_params={"input_token": "0xa", "output_token": "0xb"},
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            save_snapshot(tmpdir, meta, {1: snapshot}, intents=[(intent, state)])

            _, _, loaded_intents = load_snapshot(tmpdir)

            self.assertEqual(len(loaded_intents), 1)
            li, ls = loaded_intents[0]

            self.assertEqual(li.app_id, "test-001")
            self.assertEqual(li.intent_type, "swap")
            self.assertEqual(li.config.trigger_type, TriggerType.USER_TRIGGERED)
            self.assertAlmostEqual(li.config.score_threshold, 0.6)

            self.assertEqual(ls.contract_address, "0x1234567890abcdef1234567890abcdef12345678")
            self.assertEqual(ls.nonce, 42)
            self.assertEqual(ls.raw_params["input_token"], "0xa")

    def test_save_and_load_multi_chain(self):
        """Multi-chain snapshots round-trip correctly."""
        meta = SnapshotMeta(epoch=1, timestamp=1700000000, chains=[1, 8453])
        snap_eth = MarketSnapshot(chain_id=1, block_number=18500000, timestamp=1700000000)
        snap_base = MarketSnapshot(chain_id=8453, block_number=5000000, timestamp=1700000000)

        with tempfile.TemporaryDirectory() as tmpdir:
            save_snapshot(tmpdir, meta, {1: snap_eth, 8453: snap_base})

            loaded_meta, loaded_snapshots, _ = load_snapshot(tmpdir)

            self.assertEqual(loaded_meta.chains, [1, 8453])
            self.assertIn(1, loaded_snapshots)
            self.assertIn(8453, loaded_snapshots)
            self.assertEqual(loaded_snapshots[1].block_number, 18500000)
            self.assertEqual(loaded_snapshots[8453].block_number, 5000000)

    def test_load_chain_snapshot(self):
        """load_chain_snapshot loads a single chain file."""
        meta = SnapshotMeta(epoch=1, timestamp=1700000000, chains=[1])
        snapshot = MarketSnapshot(
            chain_id=1, block_number=18500000, timestamp=1700000000,
            prices={"ETH/USD": 1850.0},
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            save_snapshot(tmpdir, meta, {1: snapshot})

            loaded = load_chain_snapshot(tmpdir, chain_id=1)
            self.assertEqual(loaded.chain_id, 1)
            self.assertEqual(loaded.prices["ETH/USD"], 1850.0)

    def test_load_chain_snapshot_missing(self):
        """load_chain_snapshot raises for missing chain."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with self.assertRaises(FileNotFoundError):
                load_chain_snapshot(tmpdir, chain_id=999)

    def test_load_snapshot_missing_dir(self):
        """load_snapshot raises for missing directory."""
        with self.assertRaises(FileNotFoundError):
            load_snapshot("/nonexistent/snapshot")

    def test_snapshot_directory_structure(self):
        """save_snapshot creates expected file layout."""
        meta = SnapshotMeta(epoch=1, timestamp=1700000000, chains=[1, 8453])
        snap1 = MarketSnapshot(chain_id=1, block_number=1, timestamp=1700000000)
        snap2 = MarketSnapshot(chain_id=8453, block_number=1, timestamp=1700000000)

        with tempfile.TemporaryDirectory() as tmpdir:
            save_snapshot(tmpdir, meta, {1: snap1, 8453: snap2})

            files = set(os.listdir(tmpdir))
            self.assertIn("meta.json", files)
            self.assertIn("chain_1.json", files)
            self.assertIn("chain_8453.json", files)
            self.assertIn("intents.json", files)
            self.assertIn("prices.json", files)


class TestSyntheticSnapshot(unittest.TestCase):
    """Test synthetic snapshot generation for screening."""

    def test_synthetic_snapshot_has_prices(self):
        """Synthetic snapshot includes price feeds."""
        snap = build_synthetic_snapshot(chain_id=1)
        self.assertIn("ETH/USD", snap.prices)
        self.assertIn("USDC/USD", snap.prices)
        self.assertEqual(snap.prices["ETH/USD"], 1850.0)

    def test_synthetic_snapshot_has_pools(self):
        """Synthetic snapshot includes pool states."""
        snap = build_synthetic_snapshot(chain_id=1)
        self.assertGreater(len(snap.pool_states), 0)

        # Check pool structure
        pool = list(snap.pool_states.values())[0]
        self.assertIn("token0", pool)
        self.assertIn("token1", pool)
        self.assertIn("fee", pool)
        self.assertIn("sqrtPriceX96", pool)
        self.assertIn("liquidity", pool)

    def test_synthetic_snapshot_has_dex_config(self):
        """Synthetic snapshot includes DEX configuration."""
        snap = build_synthetic_snapshot(chain_id=1)
        self.assertIn("router", snap.dex_config)

    def test_synthetic_intents_count(self):
        """build_synthetic_intents returns 3 intents."""
        intents = build_synthetic_intents()
        self.assertEqual(len(intents), 3)

    def test_synthetic_intents_types(self):
        """Synthetic intents cover user-triggered and auto-triggered."""
        intents = build_synthetic_intents()

        types = [i[0].config.trigger_type for i in intents]
        self.assertIn(TriggerType.USER_TRIGGERED, types)
        self.assertIn(TriggerType.AUTO_TRIGGERED, types)

    def test_synthetic_intents_have_valid_state(self):
        """Synthetic intents have state with required swap params."""
        intents = build_synthetic_intents()

        for intent, state, snapshot in intents:
            self.assertTrue(state.contract_address.startswith("0x"))
            self.assertEqual(len(state.contract_address), 42)
            self.assertGreater(state.nonce, 0)
            self.assertIsInstance(snapshot, MarketSnapshot)

    def test_synthetic_intent_swap_params(self):
        """First synthetic intent (swap) has proper input/output tokens."""
        intents = build_synthetic_intents()
        intent, state, snapshot = intents[0]

        self.assertEqual(intent.intent_type, "swap")
        self.assertIn("input_token", state.raw_params)
        self.assertIn("output_token", state.raw_params)
        self.assertIn("input_amount", state.raw_params)
        self.assertTrue(state.raw_params["input_token"].startswith("0x"))

    def test_snapshot_state_roundtrip_preserves_typed_context_metadata(self):
        """Snapshot helpers preserve typed context, policy tier, and context version."""
        state = IntentState(
            contract_address="0x" + "11" * 20,
            chain_id=1,
            nonce=7,
            owner="0x" + "22" * 20,
            raw_params={
                "input_token": "0x" + "aa" * 20,
                "output_token": "0x" + "bb" * 20,
                "input_amount": "1000",
                "min_output_amount": "900",
            },
            context_version="v3",
            policy_tier=PolicyTier.STRICT,
        )
        state.typed_context = SwapIntentContext(
            app_id="dex-app",
            intent_function="swap",
            chain_id=1,
            owner=state.owner,
            contract_address=state.contract_address,
            nonce=state.nonce,
            raw_params=dict(state.raw_params),
            input_token="0x" + "aa" * 20,
            output_token="0x" + "bb" * 20,
            input_amount=1000,
            min_output_amount=900,
            receiver=state.contract_address,
            fee_tier=3000,
        )

        serialized = _state_to_dict(state)
        reconstructed = _dict_to_state(serialized)

        self.assertEqual(reconstructed.context_version, "v3")
        self.assertEqual(reconstructed.policy_tier, PolicyTier.STRICT)
        self.assertIsInstance(reconstructed.typed_context, SwapIntentContext)
        self.assertEqual(reconstructed.typed_context.receiver, state.contract_address)


# ═══════════════════════════════════════════════════════════════════════════════
#                     SCREENING STAGE 1 TESTS
# ═══════════════════════════════════════════════════════════════════════════════


def _make_valid_repo(tmpdir: str) -> str:
    """Create a minimal valid solver repo in tmpdir."""
    repo = Path(tmpdir) / "repo"
    repo.mkdir()
    (repo / "Dockerfile").write_text(
        "FROM ghcr.io/subnet112/solver-base:v1\n"
        "COPY . /app/solver/\n"
    )
    (repo / "solver.py").write_text(
        "from minotaur_subnet.sdk.intent_solver import IntentSolver\n"
        "SOLVER_CLASS = None  # placeholder\n"
    )
    (repo / "README.md").write_text("# My Solver\n")
    return str(repo)


class TestScreeningStage1(unittest.TestCase):
    """Test Stage 1 static checks."""

    def test_valid_repo_passes(self):
        """A correctly structured repo passes Stage 1."""
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = _make_valid_repo(tmpdir)
            result = run_stage_1(repo)
            self.assertTrue(result.passed, f"Failed: {result.details}")
            self.assertEqual(result.stage, 1)
            self.assertGreaterEqual(result.duration_ms, 0)

    def test_missing_dockerfile(self):
        """Missing Dockerfile fails with missing_dockerfile."""
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir) / "repo"
            repo.mkdir()
            (repo / "solver.py").write_text("x = 1")
            (repo / "README.md").write_text("# hi")

            result = run_stage_1(str(repo))
            self.assertFalse(result.passed)
            self.assertEqual(result.error_code, "missing_dockerfile")

    def test_missing_solver(self):
        """Missing solver.py fails with missing_solver_py."""
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir) / "repo"
            repo.mkdir()
            (repo / "Dockerfile").write_text("FROM ghcr.io/subnet112/solver-base:v1\n")
            (repo / "README.md").write_text("# hi")

            result = run_stage_1(str(repo))
            self.assertFalse(result.passed)
            self.assertEqual(result.error_code, "missing_solver_py")

    def test_missing_readme(self):
        """Missing README.md fails with missing_readme_md."""
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir) / "repo"
            repo.mkdir()
            (repo / "Dockerfile").write_text("FROM ghcr.io/subnet112/solver-base:v1\n")
            (repo / "solver.py").write_text("x = 1")

            result = run_stage_1(str(repo))
            self.assertFalse(result.passed)
            self.assertEqual(result.error_code, "missing_readme_md")

    def test_invalid_base_image(self):
        """Dockerfile not using solver-base fails."""
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir) / "repo"
            repo.mkdir()
            (repo / "Dockerfile").write_text("FROM python:3.12-slim\n")
            (repo / "solver.py").write_text("x = 1")
            (repo / "README.md").write_text("# hi")

            result = run_stage_1(str(repo))
            self.assertFalse(result.passed)
            self.assertEqual(result.error_code, "invalid_base_image")

    def test_custom_cmd(self):
        """Dockerfile with CMD fails."""
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir) / "repo"
            repo.mkdir()
            (repo / "Dockerfile").write_text(
                "FROM ghcr.io/subnet112/solver-base:v1\n"
                'CMD ["python", "solver.py"]\n'
            )
            (repo / "solver.py").write_text("x = 1")
            (repo / "README.md").write_text("# hi")

            result = run_stage_1(str(repo))
            self.assertFalse(result.passed)
            self.assertEqual(result.error_code, "custom_entrypoint")

    def test_custom_entrypoint(self):
        """Dockerfile with ENTRYPOINT fails."""
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir) / "repo"
            repo.mkdir()
            (repo / "Dockerfile").write_text(
                "FROM ghcr.io/subnet112/solver-base:v1\n"
                'ENTRYPOINT ["python"]\n'
            )
            (repo / "solver.py").write_text("x = 1")
            (repo / "README.md").write_text("# hi")

            result = run_stage_1(str(repo))
            self.assertFalse(result.passed)
            self.assertEqual(result.error_code, "custom_entrypoint")

    def test_nonexistent_repo(self):
        """Nonexistent path fails with repo_not_found."""
        result = run_stage_1("/nonexistent/repo/path")
        self.assertFalse(result.passed)
        self.assertEqual(result.error_code, "repo_not_found")

    def test_suspicious_binary(self):
        """Large binary file outside models/ fails."""
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = _make_valid_repo(tmpdir)

            # Create a large .so file
            big_file = Path(repo) / "lib" / "plugin.so"
            big_file.parent.mkdir()
            big_file.write_bytes(b"\x00" * (11 * 1024 * 1024))  # 11MB

            result = run_stage_1(repo)
            self.assertFalse(result.passed)
            self.assertEqual(result.error_code, "suspicious_binary")

    def test_binary_in_models_allowed(self):
        """Large files in models/ directory are allowed."""
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = _make_valid_repo(tmpdir)

            # Create a large model file in models/
            models_dir = Path(repo) / "src" / "models"
            models_dir.mkdir(parents=True)
            (models_dir / "router_v3.pt").write_bytes(b"\x00" * (15 * 1024 * 1024))

            result = run_stage_1(repo)
            self.assertTrue(result.passed, f"Failed: {result.details}")

    def test_requirements_txt_optional(self):
        """requirements.txt is not required (but solver.py, Dockerfile, README are)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = _make_valid_repo(tmpdir)
            # Valid repo without requirements.txt should pass
            result = run_stage_1(repo)
            self.assertTrue(result.passed)

    def test_base_image_case_insensitive(self):
        """FROM line match is case-insensitive."""
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir) / "repo"
            repo.mkdir()
            (repo / "Dockerfile").write_text(
                "from ghcr.io/subnet112/solver-base:v1\n"
                "COPY . /app/solver/\n"
            )
            (repo / "solver.py").write_text("x = 1")
            (repo / "README.md").write_text("# hi")

            result = run_stage_1(str(repo))
            self.assertTrue(result.passed, f"Failed: {result.details}")


class TestScreeningResultFormat(unittest.TestCase):
    """Test ScreeningResult serialization."""

    def test_stage_result_to_dict(self):
        """StageResult fields are accessible."""
        r = StageResult(stage=1, passed=True, duration_ms=150, details="OK")
        self.assertEqual(r.stage, 1)
        self.assertTrue(r.passed)
        self.assertEqual(r.duration_ms, 150)

    def test_screening_result_to_dict(self):
        """ScreeningResult.to_dict produces API-friendly format."""
        from minotaur_subnet.harness.screening import ScreeningResult

        result = ScreeningResult(
            passed=True,
            stages=[
                StageResult(stage=1, passed=True, duration_ms=100, details="OK"),
                StageResult(stage=2, passed=True, duration_ms=5000, details="Built"),
                StageResult(stage=3, passed=True, duration_ms=12000, details="3/3 valid"),
            ],
            image_tag="solver-abc123:screening",
            solver_name="my-solver",
            solver_version="2.0.0",
        )

        d = result.to_dict()
        self.assertTrue(d["passed"])
        self.assertEqual(d["image_tag"], "solver-abc123:screening")
        self.assertIn("stage_1", d["stages"])
        self.assertIn("stage_2", d["stages"])
        self.assertIn("stage_3", d["stages"])
        self.assertTrue(d["stages"]["stage_1"]["passed"])


if __name__ == "__main__":
    unittest.main()
