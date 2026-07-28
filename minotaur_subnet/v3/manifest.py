"""Intent manifest models for Architecture V3."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from eth_hash.auto import keccak

from minotaur_subnet.shared.types import AppIntentDefinition, PolicyTier, TriggerType


@dataclass
class IntentFieldSpec:
    """Schema for a single intent parameter."""

    name: str
    value_type: str
    required: bool = True
    description: str = ""
    default: Any = None
    source: str = "user"
    # Whether this param is part of the on-chain function SIGNATURE (and thus
    # the intent selector). Computational/quoted params that are appended to
    # intentParams but are NOT in the contract's `<intent>(...)` signature
    # (e.g. platform fee, a quoted-output fee reference, an unwrap flag) set
    # this False so the selector still matches the contract. Default True.
    in_signature: bool = True


@dataclass
class IntentFunctionSpec:
    """Manifest entry for one intent function."""

    name: str
    selector: str = ""
    trigger_type: TriggerType = TriggerType.USER_TRIGGERED
    description: str = ""
    params: list[IntentFieldSpec] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def get_param(self, name: str) -> IntentFieldSpec | None:
        """Return the named field spec if present."""
        for param in self.params:
            if param.name == name:
                return param
        return None


@dataclass
class IntentManifest:
    """Authoritative app schema for runtime, API, and solver integration."""

    app_name: str
    manifest_version: str = "v1"
    intent_functions: list[IntentFunctionSpec] = field(default_factory=list)
    default_policy_tier: PolicyTier = PolicyTier.HYBRID
    supported_policy_tiers: list[PolicyTier] = field(
        default_factory=lambda: [PolicyTier.STRICT, PolicyTier.HYBRID, PolicyTier.EXPERT]
    )
    simulation_hints: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    def get_intent(self, name: str) -> IntentFunctionSpec | None:
        """Return a function spec by intent name."""
        for intent in self.intent_functions:
            if intent.name == name:
                return intent
        return None

    def requires_field(self, intent_name: str, field_name: str) -> bool:
        """Return True when the named field is required for the given intent."""
        intent = self.get_intent(intent_name)
        if intent is None:
            return False
        field = intent.get_param(field_name)
        return bool(field and field.required)


@dataclass
class ManifestValidationResult:
    """Validation result for manifest-level trigger and policy semantics."""

    valid: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def manifest_from_definition(intent: AppIntentDefinition) -> IntentManifest | None:
    """Return a typed manifest for an app definition when one exists."""
    raw_manifest = getattr(intent, "manifest", None)
    if isinstance(raw_manifest, IntentManifest):
        return raw_manifest
    if isinstance(raw_manifest, dict):
        return manifest_from_legacy_dict(
            raw_manifest,
            app_name=intent.name or intent.app_id,
        )
    return None


def manifest_from_legacy_dict(
    raw: dict[str, Any],
    *,
    app_name: str = "",
) -> IntentManifest:
    """Convert the current JS manifest dict shape into a typed IntentManifest."""
    functions: list[IntentFunctionSpec] = []
    for fn_def in raw.get("intent_functions", []) or []:
        raw_params = fn_def.get("params", {}) or {}
        params: list[IntentFieldSpec] = []
        if isinstance(raw_params, dict):
            for field_name, field_def in raw_params.items():
                if isinstance(field_def, dict):
                    params.append(
                        IntentFieldSpec(
                            name=field_name,
                            value_type=field_def.get("type", "any"),
                            required=field_def.get("required", True),
                            description=field_def.get("description", ""),
                            default=field_def.get("default"),
                            source=field_def.get("source", "user"),
                            in_signature=field_def.get("in_signature", True),
                        )
                    )
                else:
                    params.append(
                        IntentFieldSpec(name=field_name, value_type=str(field_def))
                    )
        functions.append(
            IntentFunctionSpec(
                name=fn_def.get("name", ""),
                selector=fn_def.get("selector", ""),
                trigger_type=TriggerType(fn_def.get("trigger_type", TriggerType.USER_TRIGGERED.value)),
                description=fn_def.get("description", ""),
                params=params,
                metadata=fn_def.get("metadata", {}) or {},
            )
        )

    tier_value = raw.get("default_policy_tier", PolicyTier.HYBRID.value)
    supported_tiers = [
        PolicyTier(v) for v in raw.get(
            "supported_policy_tiers",
            [PolicyTier.STRICT.value, PolicyTier.HYBRID.value, PolicyTier.EXPERT.value],
        )
    ]

    return IntentManifest(
        app_name=app_name or raw.get("name", "") or raw.get("app_name", ""),
        manifest_version=raw.get("manifest_version", "v1"),
        intent_functions=functions,
        default_policy_tier=PolicyTier(tier_value),
        supported_policy_tiers=supported_tiers,
        simulation_hints=raw.get("simulation_hints", {}) or {},
        metadata=raw.get("metadata", {}) or {},
    )


def validate_manifest_semantics(
    manifest: IntentManifest,
    *,
    config: Any | None = None,
) -> ManifestValidationResult:
    """Validate policy-tier and trigger semantics for a typed manifest.

    This intentionally focuses on V3 semantic alignment, not exhaustive schema
    validation. It can be used by API/runtime layers without making manifest
    parsing itself stricter for compatibility.
    """
    errors: list[str] = []
    warnings: list[str] = []

    if not manifest.intent_functions:
        errors.append("Manifest must declare at least one intent function.")

    seen_names: set[str] = set()
    auto_trigger_present = False
    for fn in manifest.intent_functions:
        if not fn.name:
            errors.append("Manifest intent functions must have non-empty names.")
            continue
        if fn.name in seen_names:
            errors.append(f"Manifest intent function names must be unique: {fn.name!r}.")
        seen_names.add(fn.name)
        if fn.trigger_type == TriggerType.AUTO_TRIGGERED:
            auto_trigger_present = True

    supported = list(manifest.supported_policy_tiers or [])
    if not supported:
        errors.append("Manifest supported_policy_tiers must not be empty.")
    elif len(set(supported)) != len(supported):
        errors.append("Manifest supported_policy_tiers must not contain duplicates.")

    if supported and manifest.default_policy_tier not in supported:
        errors.append(
            "Manifest default_policy_tier must be included in supported_policy_tiers."
        )

    if config is not None:
        expected_trigger = (
            TriggerType.AUTO_TRIGGERED
            if auto_trigger_present
            else TriggerType.USER_TRIGGERED
        )
        if getattr(config, "trigger_type", expected_trigger) != expected_trigger:
            errors.append(
                "App config.trigger_type must be AUTO_TRIGGERED when any manifest "
                "intent function is auto-triggered; otherwise it must remain "
                "USER_TRIGGERED."
            )

        config_policy_tier = getattr(config, "policy_tier", manifest.default_policy_tier)
        if config_policy_tier != manifest.default_policy_tier:
            errors.append(
                "App config.policy_tier must match manifest.default_policy_tier."
            )

        config_supported = list(
            getattr(config, "supported_policy_tiers", manifest.supported_policy_tiers) or []
        )
        if set(config_supported) != set(supported):
            errors.append(
                "App config.supported_policy_tiers must match manifest.supported_policy_tiers."
            )
        elif config_supported != supported:
            warnings.append(
                "App config.supported_policy_tiers uses a different ordering than "
                "manifest.supported_policy_tiers."
            )

    return ManifestValidationResult(
        valid=not errors,
        errors=errors,
        warnings=warnings,
    )


def canonical_intent_signature(
    manifest: IntentManifest,
    intent_name: str,
    *,
    skip_fields_with_prefixes: tuple[str, ...] = ("permit_",),
) -> str | None:
    """Build the canonical Solidity signature for an intent from manifest schema."""
    fn = manifest.get_intent(intent_name)
    if fn is None:
        return None

    param_types = [
        field.value_type
        for field in fn.params
        if field.in_signature and not field.name.startswith(skip_fields_with_prefixes)
    ]
    return f"{intent_name}({','.join(param_types)})"


def compute_selector_from_manifest(
    manifest: IntentManifest,
    intent_name: str,
    *,
    skip_fields_with_prefixes: tuple[str, ...] = ("permit_",),
) -> str | None:
    """Compute the 4-byte selector for an intent manifest entry."""
    signature = canonical_intent_signature(
        manifest,
        intent_name,
        skip_fields_with_prefixes=skip_fields_with_prefixes,
    )
    if signature is None:
        return None
    return keccak(signature.encode())[:4].hex()


def _intent_field_default(
    manifest: IntentManifest | None,
    intent_name: str,
    field_name: str,
    fallback: Any,
) -> Any:
    if manifest is None:
        return fallback
    intent = manifest.get_intent(intent_name)
    if intent is None:
        return fallback
    field = intent.get_param(field_name)
    if field is None or field.default is None:
        return fallback
    return field.default
