"""Architecture V3 additive foundation modules.

These modules are intentionally additive so the codebase can move toward the V3
architecture without immediately replacing the current runtime.
"""

from .assessment import (
    InteractionAssessment,
    InteractionClassification,
    InteractionRiskLevel,
    PlanAssessment,
)
from .classifier import assess_execution_plan
from .contexts import (
    BaseIntentContext,
    RebalanceIntentContext,
    SwapIntentContext,
    TwapIntentContext,
    build_typed_context,
    typed_context_from_dict,
)
from .flags import V3Flags, load_v3_flags
from .manifest import (
    IntentFieldSpec,
    IntentFunctionSpec,
    IntentManifest,
    ManifestValidationResult,
    validate_manifest_semantics,
)
from .policy import AppPolicy, EffectivePolicy, WalletPolicy

__all__ = [
    "assess_execution_plan",
    "AppPolicy",
    "BaseIntentContext",
    "build_typed_context",
    "EffectivePolicy",
    "IntentFieldSpec",
    "IntentFunctionSpec",
    "IntentManifest",
    "ManifestValidationResult",
    "InteractionAssessment",
    "InteractionClassification",
    "InteractionRiskLevel",
    "PlanAssessment",
    "RebalanceIntentContext",
    "SwapIntentContext",
    "TwapIntentContext",
    "typed_context_from_dict",
    "validate_manifest_semantics",
    "V3Flags",
    "WalletPolicy",
    "load_v3_flags",
]
