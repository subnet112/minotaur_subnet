"""Plan assessment models for Architecture V3."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from minotaur_subnet.shared.types import PolicyTier


class InteractionClassification(str, Enum):
    """High-level classification of an interaction."""

    KNOWN_SAFE = "known_safe"
    DECODABLE_UNKNOWN = "decodable_unknown"
    OPAQUE = "opaque"


class InteractionRiskLevel(str, Enum):
    """Risk level assigned to an interaction or plan."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


@dataclass
class InteractionAssessment:
    """Assessment of one interaction in an execution plan."""

    index: int
    target: str
    classification: InteractionClassification
    risk: InteractionRiskLevel
    reason: str = ""
    protocol_hint: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class PlanAssessment:
    """Validator-side policy assessment of a plan."""

    tier: PolicyTier
    accepted: bool
    interactions: list[InteractionAssessment] = field(default_factory=list)
    overall_risk: InteractionRiskLevel = InteractionRiskLevel.LOW
    warnings: list[str] = field(default_factory=list)
    rejection_reason: str = ""
    requires_extra_scrutiny: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)
