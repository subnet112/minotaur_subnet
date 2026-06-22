"""Unit tests for API service helpers."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

from eth_abi import decode as abi_decode

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from minotaur_subnet.api import services
from minotaur_subnet.shared.types import (
    AppIntentConfig,
    AppIntentDefinition,
    CodeValidationResult,
    PolicyTier,
    TriggerType,
)
from minotaur_subnet.store import AppIntentStore


def test_compute_intent_selector_uses_sandboxed_manifest_extraction(tmp_path):
    store = AppIntentStore(store_path=tmp_path / "store.json")
    store.save_app(
        AppIntentDefinition(
            app_id="app_test",
            name="Dex Aggregator",
            version="1.0.0",
            intent_type="swap",
            js_code="module.exports = { score() { return { score: 1, valid: true }; } };",
            config=AppIntentConfig(supported_chains=[1]),
        )
    )

    manifest = {
        "intent_functions": [
            {
                "name": "swap",
                "params": {
                    "input_token": {"type": "address"},
                    "output_token": {"type": "address"},
                    "input_amount": {"type": "uint256"},
                    "min_output_amount": {"type": "uint256"},
                    "receiver": {"type": "address"},
                    "permit_deadline": {"type": "uint256"},
                    "permit_v": {"type": "uint8"},
                    "permit_r": {"type": "bytes32"},
                    "permit_s": {"type": "bytes32"},
                },
            }
        ]
    }

    with patch(
        "minotaur_subnet.engine.validation.validate_js_code",
        new=AsyncMock(
            return_value=CodeValidationResult(
                valid=True,
                js_manifest=manifest,
            )
        ),
    ):
        selector = services.compute_intent_selector(
            store=store,
            js_engine=None,
            app_id="app_test",
            intent_function="swap",
        )

    assert selector == "d5bcb9b5"


def test_compute_intent_selector_prefers_loaded_manifest(tmp_path):
    store = AppIntentStore(store_path=tmp_path / "store.json")
    store.save_app(
        AppIntentDefinition(
            app_id="app_loaded",
            name="Dex Aggregator",
            version="1.0.0",
            intent_type="swap",
            js_code="module.exports = {};",
            config=AppIntentConfig(supported_chains=[1]),
        )
    )

    class LoadedEngine:
        def get_manifest(self, app_id: str):
            assert app_id == "app_loaded"
            return {
                "intent_functions": [
                    {
                        "name": "swap",
                        "params": {
                            "input_token": {"type": "address"},
                            "output_token": {"type": "address"},
                            "input_amount": {"type": "uint256"},
                            "min_output_amount": {"type": "uint256"},
                            "receiver": {"type": "address"},
                        },
                    }
                ]
            }

    selector = services.compute_intent_selector(
        store=store,
        js_engine=LoadedEngine(),
        app_id="app_loaded",
        intent_function="swap",
    )

    assert selector == "d5bcb9b5"


def test_build_swap_intent_params_hex_matches_dex_aggregator_layout():
    params_hex = services.build_swap_intent_params_hex(
        params={
            "input_token": "0x" + "11" * 20,
            "output_token": "0x" + "22" * 20,
            "input_amount": "123",
            "min_output_amount": "100",
            "receiver": "0x" + "33" * 20,
            "permit_deadline": "999",
            "permit_v": "27",
            "permit_r": "0x" + "44" * 32,
            "permit_s": "0x" + "55" * 32,
        },
        submitted_by="0x" + "aa" * 20,
    )

    assert params_hex is not None

    decoded = abi_decode(
        ["address", "address", "uint256", "uint256", "address",
         "uint256", "uint8", "bytes32", "bytes32"],
        bytes.fromhex(params_hex),
    )

    assert decoded[0].lower() == "0x" + "11" * 20
    assert decoded[1].lower() == "0x" + "22" * 20
    assert decoded[2] == 123
    assert decoded[3] == 100
    assert decoded[4].lower() == "0x" + "33" * 20
    assert decoded[5] == 999
    assert decoded[6] == 27
    assert decoded[7] == bytes.fromhex("44" * 32)
    assert decoded[8] == bytes.fromhex("55" * 32)


def test_build_swap_intent_params_hex_accepts_recipient_alias():
    params_hex = services.build_swap_intent_params_hex(
        params={
            "input_token": "0x" + "11" * 20,
            "output_token": "0x" + "22" * 20,
            "input_amount": "123",
            "min_output_amount": "100",
            "recipient": "0x" + "33" * 20,
        },
        submitted_by="0x" + "aa" * 20,
    )

    assert params_hex is not None

    decoded = abi_decode(
        ["address", "address", "uint256", "uint256", "address",
         "uint256", "uint8", "bytes32", "bytes32"],
        bytes.fromhex(params_hex),
    )

    assert decoded[4].lower() == "0x" + "33" * 20


def test_validate_app_intent_code_reports_manifest_semantic_errors():
    manifest = {
        "manifest_version": "v3-draft",
        "default_policy_tier": "expert",
        "supported_policy_tiers": ["strict", "hybrid"],
        "intent_functions": [{"name": "swap", "params": {}}],
    }
    with patch(
        "minotaur_subnet.engine.validation.validate_app_intent",
        new=AsyncMock(
            return_value=CodeValidationResult(
                valid=True,
                errors=[],
                warnings=[],
                js_config={"name": "Dex Aggregator"},
                js_manifest=manifest,
            )
        ),
    ):
        result = asyncio.run(
            services.validate_app_intent_code(
                js_code="module.exports = { score() { return { score: 1.0 }; } };",
                solidity_code="",
                skip_solidity=True,
            )
        )

    assert result["valid"] is False
    assert any("default_policy_tier" in error for error in result["errors"])


def test_create_app_intent_persists_manifest_derived_config(tmp_path):
    store = AppIntentStore(store_path=tmp_path / "store.json")
    manifest = {
        "manifest_version": "v3-draft",
        "default_policy_tier": "strict",
        "supported_policy_tiers": ["strict", "hybrid"],
        "intent_functions": [
            {
                "name": "rebalance",
                "trigger_type": "auto_triggered",
                "params": {},
            }
        ],
    }
    with patch(
        "minotaur_subnet.engine.validation.validate_app_intent",
        new=AsyncMock(
            return_value=CodeValidationResult(
                valid=True,
                errors=[],
                warnings=["has manifest"],
                js_config={"name": "Rebalancer"},
                js_manifest=manifest,
            )
        ),
    ):
        result = services.create_app_intent(
            store,
            name="Rebalancer",
            description="Auto-triggered rebalance app",
            supported_chains=[1],
            js_code="module.exports = { score() { return { score: 1.0 }; } };",
            solidity_code="pragma solidity ^0.8.24; contract T {}",
        )

    app = store.get_app(result["app_id"])

    assert app is not None
    assert app.manifest == manifest
    assert app.config.trigger_type == TriggerType.AUTO_TRIGGERED
    assert app.config.policy_tier == PolicyTier.STRICT
    assert app.config.supported_policy_tiers == [PolicyTier.STRICT, PolicyTier.HYBRID]
    assert app.config.manifest_version == "v3-draft"
    assert "validation_warnings" in result


def _ok_validation():
    return patch(
        "minotaur_subnet.engine.validation.validate_app_intent",
        new=AsyncMock(return_value=CodeValidationResult(
            valid=True, errors=[], warnings=[], js_config={}, js_manifest=None,
        )),
    )


def _create(store, **kw):
    with _ok_validation():
        return services.create_app_intent(
            store, name="App", description="d", supported_chains=[1],
            js_code="module.exports = { score() { return { score: 1 }; } };",
            solidity_code="pragma solidity ^0.8.24; contract T {}", **kw,
        )


def test_create_app_intent_persists_per_app_fee_mode(tmp_path):
    # #239: fee_mode chosen at create is stored on the App config + survives reload.
    store = AppIntentStore(store_path=tmp_path / "store.json")
    res = _create(store, fee_mode="app")  # case-insensitive -> normalized to APP
    app = store.get_app(res["app_id"])
    assert app.config.fee_mode == "APP"
    # Reload from disk (exercises _config_from_dict) — must round-trip.
    store2 = AppIntentStore(store_path=tmp_path / "store.json")
    assert store2.get_app(res["app_id"]).config.fee_mode == "APP"


def test_create_app_intent_defaults_fee_mode_empty(tmp_path):
    # Omitted -> "" (deploy falls back to FEE_MODE_DEFAULT).
    store = AppIntentStore(store_path=tmp_path / "store.json")
    res = _create(store)
    assert store.get_app(res["app_id"]).config.fee_mode == ""


def test_create_app_intent_rejects_bad_fee_mode(tmp_path):
    store = AppIntentStore(store_path=tmp_path / "store.json")
    res = _create(store, fee_mode="GASLESS")
    assert "error" in res and "fee_mode" in res["error"]


def test_two_apps_under_one_operator_get_distinct_fee_modes(tmp_path):
    # The bug: every App got the operator-wide mode. Now each App keeps its own.
    store = AppIntentStore(store_path=tmp_path / "store.json")
    a = store.get_app(_create(store, fee_mode="USER")["app_id"])
    b = store.get_app(_create(store, fee_mode="APP")["app_id"])
    assert a.config.fee_mode == "USER" and b.config.fee_mode == "APP"


def test_deployer_resolves_per_app_fee_mode_over_env(monkeypatch):
    # The REAL deployer helper bakes the App's own fee_mode into the contract,
    # overriding the operator-wide FEE_MODE_DEFAULT. FeeMode enum: USER=0, APP=1.
    from minotaur_subnet.deployment.deployer import resolve_fee_mode

    monkeypatch.setenv("FEE_MODE_DEFAULT", "USER")
    assert resolve_fee_mode("APP") == ("APP", 1)    # App choice wins over env
    assert resolve_fee_mode("app") == ("APP", 1)    # case-insensitive
    monkeypatch.setenv("FEE_MODE_DEFAULT", "APP")
    assert resolve_fee_mode("USER") == ("USER", 0)  # App choice wins over env
    assert resolve_fee_mode("") == ("APP", 1)       # empty -> env fallback
    assert resolve_fee_mode(None) == ("APP", 1)     # None -> env fallback
    monkeypatch.setenv("FEE_MODE_DEFAULT", "USER")
    assert resolve_fee_mode("") == ("USER", 0)


def test_deployer_resolve_fee_mode_rejects_bad_value(monkeypatch):
    import pytest
    from minotaur_subnet.deployment.deployer import resolve_fee_mode
    monkeypatch.setenv("FEE_MODE_DEFAULT", "USER")
    with pytest.raises(ValueError, match="App config fee_mode must be USER or APP"):
        resolve_fee_mode("GASLESS")
    # A bad operator default is attributed to FEE_MODE_DEFAULT, not the App.
    monkeypatch.setenv("FEE_MODE_DEFAULT", "WRONG")
    with pytest.raises(ValueError, match="FEE_MODE_DEFAULT must be USER or APP"):
        resolve_fee_mode("")


def test_create_app_intent_rejects_invalid_manifest_semantics(tmp_path):
    store = AppIntentStore(store_path=tmp_path / "store.json")
    manifest = {
        "manifest_version": "v3-draft",
        "default_policy_tier": "expert",
        "supported_policy_tiers": ["strict", "hybrid"],
        "intent_functions": [{"name": "swap", "params": {}}],
    }
    with patch(
        "minotaur_subnet.engine.validation.validate_app_intent",
        new=AsyncMock(
            return_value=CodeValidationResult(
                valid=True,
                errors=[],
                warnings=[],
                js_config={"name": "Dex Aggregator"},
                js_manifest=manifest,
            )
        ),
    ):
        result = services.create_app_intent(
            store,
            name="Dex Aggregator",
            description="Swap app",
            supported_chains=[1],
            js_code="module.exports = { score() { return { score: 1.0 }; } };",
            solidity_code="pragma solidity ^0.8.24; contract T {}",
        )

    assert result["error"] == "Validation failed"
    assert any("default_policy_tier" in error for error in result["validation_errors"])


def test_update_scoring_refreshes_manifest_derived_config(tmp_path):
    store = AppIntentStore(store_path=tmp_path / "store.json")
    store.save_app(
        AppIntentDefinition(
            app_id="app_test",
            name="Rebalancer",
            version="1.0.0",
            intent_type="",
            js_code="module.exports = { score() { return { score: 0.5 }; } };",
            config=AppIntentConfig(supported_chains=[1]),
        )
    )
    manifest = {
        "manifest_version": "v3-draft",
        "default_policy_tier": "strict",
        "supported_policy_tiers": ["strict", "hybrid"],
        "intent_functions": [
            {"name": "rebalance", "trigger_type": "auto_triggered", "params": {}}
        ],
    }
    with patch(
        "minotaur_subnet.engine.validation.validate_js_code",
        new=AsyncMock(
            return_value=CodeValidationResult(
                valid=True,
                errors=[],
                warnings=[],
                js_config={"name": "Rebalancer"},
                js_manifest=manifest,
            )
        ),
    ):
        result = services.update_scoring(
            store,
            "app_test",
            "module.exports = { score() { return { score: 0.9 }; } };",
        )

    app = store.get_app("app_test")
    assert result["status"] == "updated"
    assert app is not None
    assert app.manifest == manifest
    assert app.config.trigger_type == TriggerType.AUTO_TRIGGERED
    assert app.config.policy_tier == PolicyTier.STRICT
