"""Shared helper functions for the block loop pipeline."""

from __future__ import annotations

import time
from typing import Any

from minotaur_subnet.shared.types import (
    AppIntentDefinition,
    ExecutionPlan,
    Interaction,
    IntentState,
    PolicyTier,
)
from minotaur_subnet.orderbook.orderbook import Order
from minotaur_subnet.v3.assessment import PlanAssessment


def _json_safe(value: Any) -> Any:
    """Recursively convert dataclass-like objects into JSON-safe primitives."""
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, tuple):
        return [_json_safe(item) for item in value]
    if hasattr(value, "__dict__"):
        return {
            key: _json_safe(item)
            for key, item in vars(value).items()
            if not key.startswith("_")
        }
    return value


def _plan_to_dict(plan: ExecutionPlan) -> dict[str, Any]:
    """Convert an ExecutionPlan to a JSON-safe dict."""
    return {
        "intent_id": plan.intent_id,
        "interactions": [
            {
                "target": ix.target,
                "value": ix.value,
                "call_data": ix.call_data,
                "chain_id": ix.chain_id,
            }
            for ix in plan.interactions
        ],
        "deadline": plan.deadline,
        "nonce": plan.nonce,
        "metadata": plan.metadata,
    }


def _resolve_effective_policy_tier(
    order: Order,
    app: AppIntentDefinition,
) -> PolicyTier:
    app_tier = _coerce_policy_tier(getattr(app.config, "policy_tier", PolicyTier.HYBRID))
    requested = _coerce_policy_tier(getattr(order, "policy_tier", ""))
    supported = getattr(app.config, "supported_policy_tiers", None) or list(PolicyTier)
    if requested not in supported:
        return app_tier
    ranks = {
        PolicyTier.STRICT: 0,
        PolicyTier.HYBRID: 1,
        PolicyTier.EXPERT: 2,
    }
    return min((app_tier, requested), key=lambda tier: ranks[tier])


def _coerce_policy_tier(value: Any) -> PolicyTier:
    if isinstance(value, PolicyTier):
        return value
    if isinstance(value, str):
        for tier in PolicyTier:
            if tier.value == value:
                return tier
    return PolicyTier.HYBRID


def _plan_assessment_to_dict(assessment: PlanAssessment) -> dict[str, Any]:
    return {
        "tier": assessment.tier.value,
        "accepted": assessment.accepted,
        "interactions": [
            {
                "index": item.index,
                "target": item.target,
                "classification": item.classification.value,
                "risk": item.risk.value,
                "reason": item.reason,
                "protocol_hint": item.protocol_hint,
                "metadata": item.metadata,
            }
            for item in assessment.interactions
        ],
        "overall_risk": assessment.overall_risk.value,
        "warnings": assessment.warnings,
        "rejection_reason": assessment.rejection_reason,
        "requires_extra_scrutiny": assessment.requires_extra_scrutiny,
        "metadata": assessment.metadata,
    }


def _build_fallback_plan(state: IntentState) -> ExecutionPlan:
    """Build a minimal stub plan when no solver is loaded.

    The plan flows through simulation and mock scoring so orders can
    still progress.  Marked with ``metadata["fallback"] = True`` so
    ``compute_mock_score`` assigns it a low (but non-zero) score.
    """
    return ExecutionPlan(
        intent_id=state.control_view().get("_intent_function", "execute"),
        interactions=[
            Interaction(
                target="0x" + "00" * 20,
                value="0",
                call_data="0x",
                chain_id=state.chain_id,
            ),
        ],
        deadline=int(time.time()) + 300,
        nonce=0,
        metadata={"fallback": True},
    )
