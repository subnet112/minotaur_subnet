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
