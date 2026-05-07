"""Unit tests for Architecture V3 foundation modules."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from minotaur_subnet.shared.types import (
    AppIntentConfig,
    AppIntentDefinition,
    ExecutionPlan,
    Interaction,
    IntentState,
    PolicyTier,
    TriggerType,
    WalletInfo,
)
from minotaur_subnet.shared.builders import build_intent_state
from minotaur_subnet.store import AppIntentStore
from minotaur_subnet.v3.contexts import (
    RebalanceIntentContext,
    SwapIntentContext,
    TwapIntentContext,
    build_typed_context,
)
from minotaur_subnet.v3.flags import load_v3_flags
from minotaur_subnet.v3.manifest import (
    IntentFieldSpec,
    IntentFunctionSpec,
    IntentManifest,
    ManifestValidationResult,
    canonical_intent_signature,
    canonical_swap_receiver_field,
    compute_selector_from_manifest,
    manifest_from_definition,
    manifest_from_legacy_dict,
    normalize_rebalance_intent_params,
    normalize_swap_intent_params,
    normalize_twap_intent_params,
    validate_manifest_semantics,
)
from minotaur_subnet.v3.assessment import (
    InteractionClassification,
    InteractionRiskLevel,
)
from minotaur_subnet.v3.classifier import assess_execution_plan
from minotaur_subnet.sdk.solvers.rebalance_solver import RebalanceIntentProcessor
from minotaur_subnet.sdk.solvers.twap_solver import TWAPIntentProcessor


def test_load_v3_flags_from_environment(monkeypatch):
    monkeypatch.setenv("V3_MANIFEST_ENABLED", "1")
    monkeypatch.setenv("V3_TYPED_CONTEXTS_ENABLED", "true")
    monkeypatch.setenv("V3_POLICY_ASSESSMENT_ENABLED", "yes")

    flags = load_v3_flags()

    assert flags.manifest_enabled is True
    assert flags.typed_contexts_enabled is True
    assert flags.policy_assessment_enabled is True
    assert flags.policy_enforcement_enabled is False


def test_build_intent_state_uses_structured_runtime_contract():
    state = build_intent_state(
        contract_address="0x" + "22" * 20,
        chain_id=1,
        nonce=3,
        owner="0x" + "11" * 20,
        params={"input_token": "0x" + "aa" * 20, "input_amount": "1000"},
        intent_function="swap",
    )

    assert state.raw_params == {
        "input_token": "0x" + "aa" * 20,
        "input_amount": "1000",
    }
    assert state.control == {"_intent_function": "swap"}


def test_intent_state_extra_is_derived_compatibility_payload():
    state = IntentState(
        contract_address="0x" + "22" * 20,
        chain_id=1,
        nonce=3,
        owner="0x" + "11" * 20,
        raw_params={"input_token": "0x" + "aa" * 20},
        control={"_intent_function": "swap"},
    )

    state.extra["input_token"] = "0x" + "ff" * 20
    state.extra["_intent_function"] = "execute"

    assert state.raw_params_view() == {"input_token": "0x" + "aa" * 20}
    assert state.control_view() == {"_intent_function": "swap"}

    state.sync_extra()
    assert state.extra == {
        "input_token": "0x" + "aa" * 20,
        "_intent_function": "swap",
    }


def test_swap_intent_context_from_params_defaults_receiver_to_contract():
    ctx = SwapIntentContext.from_params(
        app_id="dex-app",
        intent_function="swap",
        chain_id=1,
        owner="0x" + "11" * 20,
        contract_address="0x" + "22" * 20,
        nonce=7,
        params={
            "input_token": "0x" + "aa" * 20,
            "output_token": "0x" + "bb" * 20,
            "input_amount": "1000",
            "min_output_amount": "900",
        },
    )

    assert ctx.receiver == "0x" + "22" * 20
    assert ctx.input_amount == 1000
    assert ctx.min_output_amount == 900
    assert ctx.fee_tier == 3000


def test_build_typed_context_returns_swap_context_for_swap_app():
    intent = AppIntentDefinition(
        app_id="dex-app",
        name="Dex Aggregator",
        version="1.0.0",
        intent_type="swap",
        js_code="module.exports = {};",
        config=AppIntentConfig(supported_chains=[1]),
    )
    state = IntentState(
        contract_address="0x" + "22" * 20,
        chain_id=1,
        nonce=1,
        owner="0x" + "11" * 20,
        raw_params={
            "input_token": "0x" + "aa" * 20,
            "output_token": "0x" + "bb" * 20,
            "input_amount": "1000",
            "min_output_amount": "900",
        },
    )

    ctx = build_typed_context(intent, "swap", state)

    assert isinstance(ctx, SwapIntentContext)
    assert ctx.receiver == "0x" + "22" * 20


def test_build_typed_context_uses_manifest_recipient_alias():
    intent = AppIntentDefinition(
        app_id="dex-app",
        name="Dex Aggregator",
        version="1.0.0",
        intent_type="swap",
        js_code="module.exports = {};",
        config=AppIntentConfig(supported_chains=[1]),
        manifest={
            "intent_functions": [
                {
                    "name": "swap",
                    "params": {
                        "input_token": {"type": "address"},
                        "output_token": {"type": "address"},
                        "input_amount": {"type": "uint256"},
                        "min_output_amount": {"type": "uint256"},
                        "recipient": {"type": "address"},
                    },
                }
            ]
        },
    )
    state = IntentState(
        contract_address="0x" + "22" * 20,
        chain_id=1,
        nonce=1,
        owner="0x" + "11" * 20,
        raw_params={
            "input_token": "0x" + "aa" * 20,
            "output_token": "0x" + "bb" * 20,
            "input_amount": "1000",
            "min_output_amount": "900",
            "recipient": "0x" + "44" * 20,
        },
    )

    ctx = build_typed_context(intent, "swap", state)

    assert isinstance(ctx, SwapIntentContext)
    assert ctx.receiver == "0x" + "44" * 20


def test_normalize_swap_intent_params_respects_manifest_receiver_field():
    intent = AppIntentDefinition(
        app_id="dex-app",
        name="Dex Aggregator",
        version="1.0.0",
        intent_type="swap",
        js_code="module.exports = {};",
        config=AppIntentConfig(supported_chains=[1]),
        manifest={
            "intent_functions": [
                {
                    "name": "swap",
                    "params": {
                        "input_token": {"type": "address"},
                        "output_token": {"type": "address"},
                        "input_amount": {"type": "uint256"},
                        "recipient": {"type": "address"},
                    },
                }
            ]
        },
    )

    manifest = manifest_from_definition(intent)

    assert manifest is not None
    assert canonical_swap_receiver_field(manifest) == "recipient"

    normalized = normalize_swap_intent_params(
        {
            "input_token": "0x" + "aa" * 20,
            "output_token": "0x" + "bb" * 20,
            "input_amount": "1000",
            "receiver": "0x" + "55" * 20,
        },
        manifest=manifest,
        receiver_default="0x" + "22" * 20,
        slippage_bps=50,
    )

    assert normalized["receiver"] == "0x" + "55" * 20
    assert normalized["receiver_field"] == "recipient"
    assert normalized["min_output_amount"] == 995


def test_build_typed_context_returns_twap_context():
    intent = AppIntentDefinition(
        app_id="twap-app",
        name="TWAP",
        version="1.0.0",
        intent_type="twap",
        js_code="module.exports = {};",
        config=AppIntentConfig(supported_chains=[1]),
    )
    state = IntentState(
        contract_address="0x" + "22" * 20,
        chain_id=1,
        nonce=5,
        owner="0x" + "11" * 20,
        raw_params={
            "input_token": "0x" + "aa" * 20,
            "output_token": "0x" + "bb" * 20,
            "total_amount": "1000",
            "num_chunks": "4",
            "interval_seconds": "300",
            "chunks_executed": "1",
        },
    )

    ctx = build_typed_context(intent, "twap", state)

    assert isinstance(ctx, TwapIntentContext)
    assert ctx.total_amount == 1000
    assert ctx.num_chunks == 4
    assert ctx.interval_seconds == 300
    assert ctx.receiver == "0x" + "22" * 20


def test_build_typed_context_returns_rebalance_context():
    intent = AppIntentDefinition(
        app_id="rebalance-app",
        name="Rebalance",
        version="1.0.0",
        intent_type="rebalance",
        js_code="module.exports = {};",
        config=AppIntentConfig(supported_chains=[1]),
    )
    state = IntentState(
        contract_address="0x" + "22" * 20,
        chain_id=1,
        nonce=5,
        owner="0x" + "11" * 20,
        raw_params={
            "target_allocations": {"ETH": 0.6, "USDC": 0.4},
            "current_allocations": {"ETH": 0.7, "USDC": 0.3},
            "threshold_pct": 0.05,
            "total_value_usd": 100000,
            "token_addresses": {"ETH": "0x" + "aa" * 20},
            "token_decimals": {"ETH": "18"},
        },
    )

    ctx = build_typed_context(intent, "rebalance", state)

    assert isinstance(ctx, RebalanceIntentContext)
    assert ctx.threshold_pct == 0.05
    assert ctx.total_value_usd == 100000.0
    assert ctx.token_decimals == {"ETH": 18}


def test_normalize_twap_intent_params_applies_slippage_default():
    normalized = normalize_twap_intent_params(
        {
            "input_token": "0x" + "aa" * 20,
            "output_token": "0x" + "bb" * 20,
            "total_amount": "1000",
            "num_chunks": "4",
            "interval_seconds": "300",
        },
        receiver_default="0x" + "22" * 20,
        slippage_bps=50,
    )

    assert normalized["min_output_per_chunk"] == 248
    assert normalized["receiver"] == "0x" + "22" * 20


def test_normalize_rebalance_intent_params_coerces_numeric_fields():
    normalized = normalize_rebalance_intent_params(
        {
            "target_allocations": {"ETH": 0.6},
            "current_allocations": {"ETH": 1.0},
            "threshold_pct": "0.05",
            "total_value_usd": "2500",
            "token_decimals": {"ETH": "18"},
        }
    )

    assert normalized["threshold_pct"] == 0.05
    assert normalized["total_value_usd"] == 2500.0
    assert normalized["token_decimals"] == {"ETH": 18}


def test_twap_processor_extracts_from_typed_context():
    processor = TWAPIntentProcessor()
    intent = AppIntentDefinition(
        app_id="twap-app",
        name="TWAP",
        version="1.0.0",
        intent_type="twap",
        js_code="module.exports = {};",
        config=AppIntentConfig(supported_chains=[1]),
    )
    state = IntentState(
        contract_address="0x" + "22" * 20,
        chain_id=1,
        nonce=5,
        owner="0x" + "11" * 20,
        typed_context=TwapIntentContext(
            app_id="twap-app",
            intent_function="twap",
            chain_id=1,
            owner="0x" + "11" * 20,
            contract_address="0x" + "22" * 20,
            nonce=5,
            input_token="0x" + "aa" * 20,
            output_token="0x" + "bb" * 20,
            total_amount=1000,
            num_chunks=4,
            interval_seconds=300,
            chunks_executed=1,
            min_output_per_chunk=240,
            receiver="0x" + "22" * 20,
            fee_tier=3000,
        ),
    )

    params = processor._extract_twap_params(intent, state)

    assert params["total_amount"] == 1000
    assert params["receiver"] == "0x" + "22" * 20


def test_rebalance_processor_extracts_from_typed_context():
    processor = RebalanceIntentProcessor()
    intent = AppIntentDefinition(
        app_id="rebalance-app",
        name="Rebalance",
        version="1.0.0",
        intent_type="rebalance",
        js_code="module.exports = {};",
        config=AppIntentConfig(supported_chains=[1]),
    )
    state = IntentState(
        contract_address="0x" + "22" * 20,
        chain_id=1,
        nonce=5,
        owner="0x" + "11" * 20,
        typed_context=RebalanceIntentContext(
            app_id="rebalance-app",
            intent_function="rebalance",
            chain_id=1,
            owner="0x" + "11" * 20,
            contract_address="0x" + "22" * 20,
            nonce=5,
            target_allocations={"ETH": 0.6, "USDC": 0.4},
            current_allocations={"ETH": 0.7, "USDC": 0.3},
            threshold_pct=0.05,
            total_value_usd=100000.0,
            token_addresses={"ETH": "0x" + "aa" * 20},
            token_decimals={"ETH": 18},
        ),
    )

    params = processor._extract_rebalance_params(intent, state)

    assert params["threshold_pct"] == 0.05
    assert params["token_decimals"] == {"ETH": 18}


def test_manifest_requires_field():
    manifest = IntentManifest(
        app_name="DexAggregatorApp",
        intent_functions=[
            IntentFunctionSpec(
                name="swap",
                trigger_type=TriggerType.USER_TRIGGERED,
                params=[
                    IntentFieldSpec(name="input_token", value_type="address", required=True),
                    IntentFieldSpec(name="receiver", value_type="address", required=False),
                ],
            )
        ],
    )

    assert manifest.requires_field("swap", "input_token") is True
    assert manifest.requires_field("swap", "receiver") is False
    assert manifest.requires_field("swap", "missing") is False


def test_legacy_manifest_adapter_computes_selector_signature():
    manifest = manifest_from_legacy_dict(
        {
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
                    },
                }
            ]
        },
        app_name="DexAggregatorApp",
    )

    assert canonical_intent_signature(
        manifest, "swap"
    ) == "swap(address,address,uint256,uint256,address)"
    assert compute_selector_from_manifest(manifest, "swap") == "d5bcb9b5"


def test_validate_manifest_semantics_accepts_matching_policy_and_trigger_config():
    manifest = IntentManifest(
        app_name="DexAggregatorApp",
        default_policy_tier=PolicyTier.STRICT,
        supported_policy_tiers=[PolicyTier.STRICT, PolicyTier.HYBRID],
        intent_functions=[
            IntentFunctionSpec(
                name="swap",
                trigger_type=TriggerType.USER_TRIGGERED,
                params=[IntentFieldSpec(name="input_token", value_type="address")],
            )
        ],
    )
    config = AppIntentConfig(
        supported_chains=[1],
        trigger_type=TriggerType.USER_TRIGGERED,
        policy_tier=PolicyTier.STRICT,
        supported_policy_tiers=[PolicyTier.STRICT, PolicyTier.HYBRID],
    )

    result = validate_manifest_semantics(manifest, config=config)

    assert isinstance(result, ManifestValidationResult)
    assert result.valid is True
    assert result.errors == []


def test_validate_manifest_semantics_rejects_missing_default_policy_support():
    manifest = IntentManifest(
        app_name="DexAggregatorApp",
        default_policy_tier=PolicyTier.EXPERT,
        supported_policy_tiers=[PolicyTier.STRICT, PolicyTier.HYBRID],
        intent_functions=[IntentFunctionSpec(name="swap")],
    )

    result = validate_manifest_semantics(manifest)

    assert result.valid is False
    assert "default_policy_tier" in result.errors[0]


def test_validate_manifest_semantics_rejects_trigger_mismatch_for_auto_intent():
    manifest = IntentManifest(
        app_name="TWAP",
        intent_functions=[
            IntentFunctionSpec(
                name="twap",
                trigger_type=TriggerType.AUTO_TRIGGERED,
            )
        ],
    )
    config = AppIntentConfig(
        supported_chains=[1],
        trigger_type=TriggerType.USER_TRIGGERED,
    )

    result = validate_manifest_semantics(manifest, config=config)

    assert result.valid is False
    assert any("config.trigger_type" in error for error in result.errors)


def test_validate_manifest_semantics_rejects_policy_tier_mismatch_with_config():
    manifest = IntentManifest(
        app_name="DexAggregatorApp",
        default_policy_tier=PolicyTier.HYBRID,
        supported_policy_tiers=[PolicyTier.STRICT, PolicyTier.HYBRID],
        intent_functions=[IntentFunctionSpec(name="swap")],
    )
    config = AppIntentConfig(
        supported_chains=[1],
        trigger_type=TriggerType.USER_TRIGGERED,
        policy_tier=PolicyTier.STRICT,
        supported_policy_tiers=[PolicyTier.STRICT, PolicyTier.HYBRID],
    )

    result = validate_manifest_semantics(manifest, config=config)

    assert result.valid is False
    assert any("config.policy_tier" in error for error in result.errors)


def test_assess_execution_plan_strict_rejects_opaque_interaction():
    plan = ExecutionPlan(
        intent_id="app_v3",
        interactions=[
            Interaction(
                target="0x" + "11" * 20,
                value="0",
                call_data="0x",
                chain_id=1,
            )
        ],
        deadline=9999999999,
        nonce=1,
        metadata={},
    )

    assessment = assess_execution_plan(plan, PolicyTier.STRICT)

    assert assessment.accepted is False
    assert assessment.overall_risk == InteractionRiskLevel.HIGH
    assert assessment.interactions[0].classification == InteractionClassification.OPAQUE


def test_assess_execution_plan_hybrid_marks_unknown_selector_for_scrutiny():
    plan = ExecutionPlan(
        intent_id="app_v3",
        interactions=[
            Interaction(
                target="0x" + "22" * 20,
                value="0",
                call_data="0xdeadbeef" + "00" * 32,
                chain_id=1,
            )
        ],
        deadline=9999999999,
        nonce=1,
        metadata={},
    )

    assessment = assess_execution_plan(plan, PolicyTier.HYBRID)

    assert assessment.accepted is True
    assert assessment.requires_extra_scrutiny is True
    assert assessment.overall_risk == InteractionRiskLevel.MEDIUM
    assert (
        assessment.interactions[0].classification
        == InteractionClassification.DECODABLE_UNKNOWN
    )


def test_store_roundtrip_preserves_v3_app_and_wallet_fields(tmp_path):
    store = AppIntentStore(store_path=tmp_path / "store.json")
    store.save_app(
        AppIntentDefinition(
            app_id="app_v3",
            name="Dex Aggregator",
            version="1.0.0",
            intent_type="swap",
            js_code="module.exports = {};",
            config=AppIntentConfig(
                supported_chains=[1],
                trigger_type=TriggerType.USER_TRIGGERED,
                policy_tier=PolicyTier.STRICT,
                supported_policy_tiers=[PolicyTier.STRICT, PolicyTier.HYBRID],
                manifest_version="v3-draft",
            ),
            schema_id="schema.dex.v1",
            policy_metadata={"owner_controls": True},
            manifest={"intent_functions": [{"name": "swap"}]},
        )
    )
    store.save_wallet(
        WalletInfo(
            address="0x" + "33" * 20,
            chain_ids=[1],
            wallet_type="lit_mpc",
            policy_tier=PolicyTier.STRICT,
            policy_id="wallet-policy-1",
            policy_overrides={"max_notional_usd": 5000},
        )
    )

    reloaded = AppIntentStore(store_path=tmp_path / "store.json")
    app = reloaded.get_app("app_v3")
    wallet = reloaded.get_wallet("0x" + "33" * 20)

    assert app is not None
    assert app.config.policy_tier == PolicyTier.STRICT
    assert app.config.supported_policy_tiers == [PolicyTier.STRICT, PolicyTier.HYBRID]
    assert app.config.manifest_version == "v3-draft"
    assert app.schema_id == "schema.dex.v1"
    assert app.policy_metadata == {"owner_controls": True}
    assert app.manifest == {"intent_functions": [{"name": "swap"}]}

    assert wallet is not None
    assert wallet.policy_tier == PolicyTier.STRICT
    assert wallet.policy_id == "wallet-policy-1"
    assert wallet.policy_overrides == {"max_notional_usd": 5000}
