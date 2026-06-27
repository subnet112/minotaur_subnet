"""Unit tests for API server security health helpers."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import pytest
from fastapi.testclient import TestClient

from minotaur_subnet.api import server as api_server


def test_env_true_parses_expected_values():
    with patch.dict(
        os.environ,
        {
            "A_TRUE": "1",
            "B_TRUE": "true",
            "C_TRUE": "YES",
            "D_FALSE": "0",
            "E_FALSE": "off",
        },
        clear=False,
    ):
        assert api_server._env_true("A_TRUE") is True
        assert api_server._env_true("B_TRUE") is True
        assert api_server._env_true("C_TRUE") is True
        assert api_server._env_true("D_FALSE") is False
        assert api_server._env_true("E_FALSE") is False
        assert api_server._env_true("MISSING", default=True) is True


def test_resolve_solver_round_hotkey_prefers_explicit_env():
    with patch.dict(
        os.environ,
        {"VALIDATOR_HOTKEY_SS58": "5ExplicitHotkey"},
        clear=False,
    ):
        assert api_server._resolve_solver_round_hotkey() == "5ExplicitHotkey"


def test_build_provenance_health_snapshot_mode_asymmetric():
    snapshot = api_server._build_provenance_health_snapshot(
        require_signed=True,
        require_asymmetric=True,
        submissions_accepting=True,
        hmac_key="",
        allowed_signers={"0xabc"},
        signing_private_key="0xkey",
        policy_ok=True,
        policy_error="",
        startup_validated=True,
    )
    assert snapshot["valid"] is True
    assert snapshot["mode"] == "asymmetric_only"
    assert snapshot["allowed_signers_count"] == 1
    assert snapshot["verifier_configured"] is True
    assert snapshot["signer_configured"] is True


def test_build_runtime_security_health_snapshot_fields():
    snapshot = api_server._build_runtime_security_health_snapshot(
        enforce=True,
        violations=["x", "y"],
        enable_source_submissions=False,
        allow_subprocess_benchmark=False,
        require_signed_provenance=True,
        require_asymmetric_provenance=True,
        allowed_signers={"0xabc", "0xdef"},
        hmac_key="",
        submissions_accepting=True,
        submissions_api_key_configured=True,
        submissions_rate_limit_per_minute=60,
        startup_validated=True,
    )
    assert snapshot["enforced"] is True
    assert snapshot["valid"] is False
    assert snapshot["violations"] == ["x", "y"]
    assert snapshot["allowed_signers_count"] == 2
    assert snapshot["submissions_api_key_configured"] is True


def test_health_includes_security_sections():
    from minotaur_subnet.api.server_context import ctx
    with patch.object(ctx, "benchmark_worker", None), patch.object(ctx, "block_loop", None):
        with patch.object(
            ctx,
            "provenance_policy_health",
            {
                "valid": True,
                "startup_validated": True,
                "mode": "optional",
                "require_signed": False,
                "require_asymmetric": False,
                "submissions_accepting": True,
                "signer_configured": False,
                "verifier_configured": False,
                "allowed_signers_count": 0,
                "hmac_configured": False,
                "error": "",
            },
        ), patch.object(
            ctx,
            "runtime_security_policy_health",
            {
                "valid": True,
                "startup_validated": True,
                "enforced": False,
                "violations": [],
                "enable_source_submissions": False,
                "allow_subprocess_benchmark": False,
                "require_signed_provenance": False,
                "require_asymmetric_provenance": False,
                "allowed_signers_count": 0,
                "hmac_configured": False,
                "submissions_accepting": True,
                "submissions_api_key_configured": False,
                "submissions_rate_limit_per_minute": 60,
            },
        ):
            data = api_server.health()
    assert data["status"] == "ok"
    assert "solver_round_role" in data
    assert "solver_round_epoch" in data
    assert "solver_round_epoch_clock" in data
    assert "champion_consensus" in data
    assert "provenance_policy" in data
    assert "runtime_security_policy" in data


def _health_with_store(store_obj):
    """Call api_server.health() with a patched persistent store + the ctx
    patches the security-sections test relies on. Returns the /health dict."""
    from minotaur_subnet.api.server_context import ctx
    prov = {
        "valid": True, "startup_validated": True, "mode": "optional",
        "require_signed": False, "require_asymmetric": False,
        "submissions_accepting": True, "signer_configured": False,
        "verifier_configured": False, "allowed_signers_count": 0,
        "hmac_configured": False, "error": "",
    }
    runtime = {
        "valid": True, "startup_validated": True, "enforced": False,
        "violations": [], "enable_source_submissions": False,
        "allow_subprocess_benchmark": False, "require_signed_provenance": False,
        "require_asymmetric_provenance": False, "allowed_signers_count": 0,
        "hmac_configured": False, "submissions_accepting": True,
        "submissions_api_key_configured": False,
        "submissions_rate_limit_per_minute": 60,
    }
    with patch.object(api_server, "store", store_obj), \
            patch.object(ctx, "benchmark_worker", None), \
            patch.object(ctx, "block_loop", None), \
            patch.object(ctx, "provenance_policy_health", prov), \
            patch.object(ctx, "runtime_security_policy_health", runtime):
        return api_server.health()


def test_health_orderbook_is_store_backed():
    """/health 'orderbook' reflects the DURABLE store count
    (count_orders_by_status) — not the daemon's in-memory working set — so the
    validator-health monitor sees real persisted orders on the leader AND
    followers, making an order-sync drift visible."""
    class _FakeStore:
        def count_orders_by_status(self):
            return {"filled": 32, "rejected": 46}
    data = _health_with_store(_FakeStore())
    assert data["orderbook"] == {"filled": 32, "rejected": 46}


def test_health_orderbook_defensive_on_store_error():
    """A store hiccup must never 500 /health — 'orderbook' degrades to None
    (rendered as '—'), and the rest of /health still answers."""
    class _BoomStore:
        def count_orders_by_status(self):
            raise RuntimeError("db is locked")
    data = _health_with_store(_BoomStore())
    assert data["orderbook"] is None
    assert data["status"] == "ok"


def test_looks_like_mainnet_bittensor_target_detects_finney():
    assert api_server._looks_like_mainnet_bittensor_target("finney") is True
    assert api_server._looks_like_mainnet_bittensor_target("wss://entrypoint-finney.opentensor.ai:443") is True
    assert api_server._looks_like_mainnet_bittensor_target("https://lite.chain.opentensor.ai") is True
    assert api_server._looks_like_mainnet_bittensor_target("ws://127.0.0.1:9944") is False


def test_looks_like_local_or_test_subtensor_url_accepts_local_and_test():
    assert api_server._looks_like_local_or_test_subtensor_url("ws://127.0.0.1:9944") is True
    assert api_server._looks_like_local_or_test_subtensor_url("ws://localhost:9944") is True
    assert api_server._looks_like_local_or_test_subtensor_url("wss://test.chain.opentensor.ai:443") is True
    assert api_server._looks_like_local_or_test_subtensor_url("wss://entrypoint-finney.opentensor.ai:443") is False


def test_validate_native_bittensor_demo_guard_requires_explicit_subtensor_url():
    ok, error = api_server._validate_native_bittensor_demo_guard(
        mvp_demo_mode=True,
        native_proxy_requested=True,
        subtensor_url="",
        resolved_target="finney",
    )
    assert ok is False
    assert "SUBTENSOR_URL" in error


def test_validate_native_bittensor_demo_guard_rejects_finney_target():
    ok, error = api_server._validate_native_bittensor_demo_guard(
        mvp_demo_mode=True,
        native_proxy_requested=True,
        subtensor_url="ws://127.0.0.1:9944",
        resolved_target="finney",
    )
    assert ok is False
    assert "finney" in error.lower()


def test_validate_native_bittensor_demo_guard_accepts_local_subtensor_target():
    ok, error = api_server._validate_native_bittensor_demo_guard(
        mvp_demo_mode=True,
        native_proxy_requested=True,
        subtensor_url="ws://127.0.0.1:9944",
        resolved_target="ws://127.0.0.1:9944",
    )
    assert ok is True
    assert error == ""


def test_demo_mode_native_proxy_startup_guard_rejects_missing_subtensor_url():
    with patch.dict(
        os.environ,
        {
            "DISABLE_BENCHMARK_WORKER": "1",
            "DISABLE_BLOCK_LOOP": "1",
            "MVP_DEMO_MODE": "1",
            "ENABLE_NATIVE_BITTENSOR_PROXY": "1",
            "SUBTENSOR_URL": "",
            "SUBTENSOR_NETWORK": "finney",
            "NATIVE_BITTENSOR_NETWORK": "",
            # Required by api/startup env_check (added 2026-05). Set to
            # non-empty stubs + skip the contract-presence check so this
            # test reaches the SUBTENSOR_URL guard rather than earlier
            # guards.
            "VALIDATOR_REGISTRY_8453": "0x" + "00" * 20,
            "VALIDATOR_REGISTRY_964": "0x" + "00" * 20,
            "SKIP_CONTRACT_PRESENCE_CHECK": "1",
        },
        clear=False,
    ):
        with pytest.raises(RuntimeError, match="SUBTENSOR_URL"):
            with TestClient(api_server.app):
                pass
