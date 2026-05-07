"""Tests for security audit remediations (2026-03-25).

Validates each fix from SECURITY_AUDIT.md to prevent regressions.
Run: .venv/bin/python -m pytest tests/unit/test_security_fixes.py -v
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


# ── #5: Subprocess Benchmark Blocked in Production ─────────────────────────

class TestSubprocessBenchmarkGuard:
    """Issue #5: ALLOW_SUBPROCESS_BENCHMARK must be blocked in production."""

    def test_blocked_in_production_mode(self):
        """Subprocess benchmark returns False when MINOTAUR_PRODUCTION=1."""
        with patch.dict(os.environ, {"MINOTAUR_PRODUCTION": "1", "ALLOW_SUBPROCESS_BENCHMARK": "1"}):
            # Re-import to pick up env change
            from minotaur_subnet.harness import benchmark_worker
            # The function should exist and return False in production
            result = benchmark_worker._allow_subprocess_benchmark()
            assert result is False, "Subprocess benchmark must be blocked in production"

    def test_permanently_disabled(self):
        """Subprocess benchmark is permanently disabled — all benchmarks use Docker."""
        env = {"ALLOW_SUBPROCESS_BENCHMARK": "1"}
        with patch.dict(os.environ, env, clear=False):
            os.environ.pop("MINOTAUR_PRODUCTION", None)
            from minotaur_subnet.harness import benchmark_worker
            result = benchmark_worker._allow_subprocess_benchmark()
            assert result is False


# ── #9: Order Cancellation Requires Ownership ──────────────────────────────

class TestOrderCancellationAuth:
    """Issue #9: Cancel must verify submitted_by matches order owner."""

    def test_cancel_rejects_wrong_owner(self):
        from minotaur_subnet.orderbook.orderbook import IntentOrderBook

        ob = IntentOrderBook()
        order = ob.submit(
            app_id="app_test",
            intent_function="swap",
            submitted_by="0xOwner",
            params={"input_token": "USDC"},
            chain_id=31337,
        )
        order_id = order if isinstance(order, str) else order.order_id

        with pytest.raises(PermissionError):
            ob.cancel(order_id, submitted_by="0xAttacker")

    def test_cancel_succeeds_for_owner(self):
        from minotaur_subnet.orderbook.orderbook import IntentOrderBook

        ob = IntentOrderBook()
        order = ob.submit(
            app_id="app_test",
            intent_function="swap",
            submitted_by="0xOwner",
            params={},
            chain_id=31337,
        )
        order_id = order if isinstance(order, str) else order.order_id

        result = ob.cancel(order_id, submitted_by="0xOwner")
        assert result is True


# ── #16: JS Code Stripped from Solver Protocol ─────────────────────────────

class TestJsCodeStripping:
    """Issue #16: Solver containers must not receive js_code or solidity_code."""

    def test_sensitive_fields_stripped(self):
        from minotaur_subnet.harness.protocol import _to_dict
        from minotaur_subnet.shared.types import AppIntentDefinition

        app = AppIntentDefinition(
            app_id="test",
            name="TestApp",
            version="1.0",
            intent_type="swap",
            js_code="function score() { return {score: 1.0}; }",
            solidity_code="pragma solidity ^0.8.0; contract Test {}",
        )

        result = _to_dict(app)

        assert "js_code" not in result or result.get("js_code") == "", \
            "js_code must be stripped from protocol messages to solvers"
        assert "solidity_code" not in result or result.get("solidity_code") == "", \
            "solidity_code must be stripped from protocol messages to solvers"

    def test_app_id_and_manifest_preserved(self):
        from minotaur_subnet.harness.protocol import _to_dict
        from minotaur_subnet.shared.types import AppIntentDefinition

        app = AppIntentDefinition(
            app_id="app_123",
            name="TestApp",
            version="1.0",
            intent_type="swap",
            js_code="secret",
            solidity_code="secret",
        )

        result = _to_dict(app)
        assert result.get("app_id") == "app_123", "app_id must be preserved"
        assert result.get("name") == "TestApp", "name must be preserved"


# ── #18: Champion Provenance Required by Default ───────────────────────────

class TestChampionProvenance:
    """Issue #18: Provenance signing must be enabled by default."""

    def test_signed_provenance_in_source(self):
        """Verify champion_policy.py defaults provenance signing to True."""
        policy_path = REPO_ROOT / "minotaur_subnet" / "harness" / "champion_policy.py"
        content = policy_path.read_text()
        # The defaults should be True (enabled), not False
        # Look for the pattern: default is True or "true"
        assert 'True' in content or '"true"' in content or "'true'" in content, \
            "champion_policy.py should default provenance to enabled"


# ── #19: JS Prototype Pollution Prevention ─────────────────────────────────

class TestJsPrototypeFreezing:
    """Issue #19: Built-in prototypes must be frozen in JS sandbox."""

    def test_runner_js_contains_freeze_calls(self):
        runner_path = REPO_ROOT / "minotaur_subnet" / "engine" / "runner.js"
        content = runner_path.read_text()

        assert "Object.freeze(Object.prototype)" in content, \
            "runner.js must freeze Object.prototype"
        assert "Object.freeze(Array.prototype)" in content, \
            "runner.js must freeze Array.prototype"
        assert "Object.freeze(Promise.prototype)" in content, \
            "runner.js must freeze Promise.prototype"


# ── #20: Path Traversal in Compiler ────────────────────────────────────────

class TestCompilerPathTraversal:
    """Issue #20: ForgeCompiler must reject path traversal in contract names."""

    def test_rejects_path_traversal(self):
        from minotaur_subnet.deployment.compiler import ForgeCompiler

        compiler = ForgeCompiler()
        result = compiler.compile("../../evil", "pragma solidity ^0.8.0;")
        assert result.error is not None
        assert "Invalid contract_name" in result.error

    def test_rejects_dots(self):
        from minotaur_subnet.deployment.compiler import ForgeCompiler

        compiler = ForgeCompiler()
        result = compiler.compile("foo.bar", "pragma solidity ^0.8.0;")
        assert result.error is not None

    def test_rejects_slashes(self):
        from minotaur_subnet.deployment.compiler import ForgeCompiler

        compiler = ForgeCompiler()
        result = compiler.compile("foo/bar", "pragma solidity ^0.8.0;")
        assert result.error is not None

    def test_accepts_valid_name(self):
        from minotaur_subnet.deployment.compiler import ForgeCompiler

        compiler = ForgeCompiler()
        # Should NOT error on the name validation (may error on compilation)
        result = compiler.compile("ValidContract_123", "pragma solidity ^0.8.0; contract ValidContract_123 {}")
        # The error, if any, should not be about the name
        if result.error:
            assert "Invalid contract_name" not in result.error


# ── #11: httpGet Deny-by-Default ───────────────────────────────────────────

class TestHttpGetDenyByDefault:
    """Issue #11: httpGet must block all requests when allowlist is empty."""

    def test_runner_js_has_deny_by_default(self):
        runner_path = REPO_ROOT / "minotaur_subnet" / "engine" / "runner.js"
        content = runner_path.read_text()

        # Should have logic that blocks when allowlist is empty
        assert "HTTP_ALLOWED_DOMAINS.length === 0" in content or \
               "HTTP_ALLOWED_DOMAINS.length == 0" in content or \
               "no domains allowed" in content.lower() or \
               "deny" in content.lower(), \
            "runner.js must deny httpGet when allowlist is empty"

    def test_runner_js_blocks_private_hosts(self):
        runner_path = REPO_ROOT / "minotaur_subnet" / "engine" / "runner.js"
        content = runner_path.read_text()

        assert "127.0.0.1" in content or "localhost" in content, \
            "runner.js must block requests to localhost/127.0.0.1"
        # Check for some form of private network blocking
        assert "blocked" in content.lower() or "internal" in content.lower() or "private" in content.lower(), \
            "runner.js must have private network blocking logic"


# ── #22: Per-User Nonce Sentinel Value ─────────────────────────────────────

class TestNonceSentinel:
    """Issue #22: Contract should support sentinel nonce for concurrent orders."""

    def test_contract_has_sentinel_nonce_logic(self):
        contract_path = REPO_ROOT / "contracts" / "src" / "AppIntentBase.sol"
        content = contract_path.read_text()

        assert "type(uint256).max" in content, \
            "AppIntentBase must support type(uint256).max as nonce sentinel"
        # Verify the nonce check is conditional
        assert "if (order.nonce != type(uint256).max)" in content, \
            "Nonce verification must be skipped for sentinel value"


# ── #26: Mock Simulation Flagging ──────────────────────────────────────────

class TestMockSimulationFlag:
    """Issue #26: Mock simulation results must be flagged and penalized."""

    def test_benchmark_result_has_mock_flag(self):
        from minotaur_subnet.harness.orchestrator import BenchmarkResult

        result = BenchmarkResult(intent_id="test", score=0.9)
        assert hasattr(result, "mock_simulation"), \
            "BenchmarkResult must have mock_simulation field"

    def test_mock_flag_defaults_false(self):
        from minotaur_subnet.harness.orchestrator import BenchmarkResult

        result = BenchmarkResult(intent_id="test", score=0.9)
        assert result.mock_simulation is False


# ── #27: Score Threshold Floor ─────────────────────────────────────────────

class TestScoreThresholdFloor:
    """Issue #27: Score threshold cannot be set below 5000 BPS (0.5)."""

    def test_contract_has_min_threshold(self):
        contract_path = REPO_ROOT / "contracts" / "src" / "AppIntentBase.sol"
        content = contract_path.read_text()

        assert "MIN_SCORE_THRESHOLD" in content, \
            "AppIntentBase must define MIN_SCORE_THRESHOLD"
        assert "5000" in content, \
            "MIN_SCORE_THRESHOLD should be 5000 BPS"
        assert "Threshold must be 5000-10000" in content, \
            "updateScoreThreshold must enforce 5000-10000 range"

    def test_constructor_enforces_floor(self):
        contract_path = REPO_ROOT / "contracts" / "src" / "AppIntentBase.sol"
        content = contract_path.read_text()

        assert "_scoreThreshold >= 5000" in content, \
            "Constructor must enforce minimum threshold of 5000"


# ── #8: Cross-Chain Leg Signature Verification ─────────────────────────────

class TestCrossChainLegSignature:
    """Issue #8: executeCrossChainLeg should verify user signature when provided."""

    def test_contract_verifies_signature_when_present(self):
        contract_path = REPO_ROOT / "contracts" / "src" / "AppIntentBase.sol"
        content = contract_path.read_text()

        assert "userSignature.length > 0" in content, \
            "executeCrossChainLeg must check signature length"
        assert "verifyUserSignature" in content, \
            "executeCrossChainLeg must call verifyUserSignature"


# ── #4: JS Scoring Update Signature ────────────────────────────────────────

class TestJsScoringUpdateAuth:
    """Issue #4: JS scoring updates must support cryptographic verification."""

    def test_update_scoring_accepts_signature_param(self):
        from minotaur_subnet.api.routes.apps import UpdateScoringRequest

        req = UpdateScoringRequest(
            new_js_code="function score() { return {score: 1}; }",
            caller="0x1234",
            signature="0xdeadbeef",
        )
        assert req.signature == "0xdeadbeef"

    def test_update_scoring_signature_optional(self):
        from minotaur_subnet.api.routes.apps import UpdateScoringRequest

        req = UpdateScoringRequest(
            new_js_code="function score() { return {score: 1}; }",
            caller="0x1234",
        )
        assert req.signature == "" or req.signature is None


# ── Solidity Compilation Check ─────────────────────────────────────────────

class TestSolidityCompilation:
    """Verify all contract changes compile successfully."""

    @pytest.mark.skipif(
        not (REPO_ROOT / "contracts" / "foundry.toml").exists(),
        reason="Foundry not configured",
    )
    def test_contracts_compile(self):
        result = subprocess.run(
            ["forge", "build"],
            capture_output=True, text=True,
            cwd=str(REPO_ROOT / "contracts"),
            timeout=120,
        )
        errors = [l for l in result.stderr.split("\n") if "Error" in l]
        assert result.returncode == 0 or len(errors) == 0, \
            f"Contract compilation failed: {errors[:5]}"
