"""Feature flags for Architecture V3 rollout."""

from __future__ import annotations

import os
from dataclasses import dataclass


def _flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name, "")
    if raw == "":
        return default
    return raw.lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class V3Flags:
    manifest_enabled: bool = False
    typed_contexts_enabled: bool = False
    policy_assessment_enabled: bool = False
    policy_enforcement_enabled: bool = False
    automations_enabled: bool = False
    intent_plan_enabled: bool = False


def load_v3_flags() -> V3Flags:
    """Load V3 feature flags from the environment."""
    return V3Flags(
        manifest_enabled=_flag("V3_MANIFEST_ENABLED"),
        typed_contexts_enabled=_flag("V3_TYPED_CONTEXTS_ENABLED"),
        policy_assessment_enabled=_flag("V3_POLICY_ASSESSMENT_ENABLED"),
        policy_enforcement_enabled=_flag("V3_POLICY_ENFORCEMENT_ENABLED"),
        automations_enabled=_flag("V3_AUTOMATIONS_ENABLED"),
        intent_plan_enabled=_flag("V3_INTENT_PLAN_ENABLED"),
    )
