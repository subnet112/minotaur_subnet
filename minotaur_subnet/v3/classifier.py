"""Lightweight execution-plan classifier for Architecture V3 policy tiers."""

from __future__ import annotations

from minotaur_subnet.sdk.selectors import (
    APPROVE_SELECTOR,
    EXACT_INPUT_SELECTOR,
    EXACT_INPUT_SINGLE_SELECTOR,
)
from minotaur_subnet.shared.types import ExecutionPlan, PolicyTier
from minotaur_subnet.v3.assessment import (
    InteractionAssessment,
    InteractionClassification,
    InteractionRiskLevel,
    PlanAssessment,
)

_KNOWN_SELECTOR_PROTOCOLS = {
    APPROVE_SELECTOR.hex(): "erc20",
    EXACT_INPUT_SINGLE_SELECTOR.hex(): "uniswap_v3",
    EXACT_INPUT_SELECTOR.hex(): "uniswap_v3",
}


def assess_execution_plan(
    plan: ExecutionPlan,
    tier: PolicyTier,
) -> PlanAssessment:
    """Classify a plan and decide whether the tier would accept it."""
    interactions: list[InteractionAssessment] = []
    warnings: list[str] = []
    requires_extra_scrutiny = False
    accepted = True
    rejection_reason = ""

    for index, interaction in enumerate(plan.interactions):
        assessment = _classify_interaction(
            index=index,
            target=interaction.target,
            call_data=interaction.call_data,
        )
        interactions.append(assessment)

        if assessment.classification != InteractionClassification.KNOWN_SAFE:
            warnings.append(
                f"interaction {index} classified as {assessment.classification.value}: "
                f"{assessment.reason}"
            )

        if tier == PolicyTier.STRICT:
            if assessment.classification != InteractionClassification.KNOWN_SAFE:
                accepted = False
                rejection_reason = (
                    f"strict tier requires known-safe interactions; "
                    f"interaction {index} is {assessment.classification.value}"
                )
                break
        elif tier == PolicyTier.HYBRID:
            if assessment.classification != InteractionClassification.KNOWN_SAFE:
                requires_extra_scrutiny = True

    overall_risk = _highest_risk(interactions)
    if tier == PolicyTier.EXPERT:
        accepted = True

    return PlanAssessment(
        tier=tier,
        accepted=accepted,
        interactions=interactions,
        overall_risk=overall_risk,
        warnings=warnings,
        rejection_reason=rejection_reason,
        requires_extra_scrutiny=requires_extra_scrutiny,
        metadata={
            "interaction_count": len(plan.interactions),
            "known_safe_count": sum(
                1
                for item in interactions
                if item.classification == InteractionClassification.KNOWN_SAFE
            ),
        },
    )


def _classify_interaction(
    *,
    index: int,
    target: str,
    call_data: str,
) -> InteractionAssessment:
    selector = _selector(call_data)
    if selector in _KNOWN_SELECTOR_PROTOCOLS:
        return InteractionAssessment(
            index=index,
            target=target,
            classification=InteractionClassification.KNOWN_SAFE,
            risk=InteractionRiskLevel.LOW,
            reason=f"recognized selector 0x{selector}",
            protocol_hint=_KNOWN_SELECTOR_PROTOCOLS[selector],
        )

    if selector is not None:
        return InteractionAssessment(
            index=index,
            target=target,
            classification=InteractionClassification.DECODABLE_UNKNOWN,
            risk=InteractionRiskLevel.MEDIUM,
            reason=f"unknown selector 0x{selector}",
        )

    return InteractionAssessment(
        index=index,
        target=target,
        classification=InteractionClassification.OPAQUE,
        risk=InteractionRiskLevel.HIGH,
        reason="missing or non-decodable calldata",
    )


def _selector(call_data: str) -> str | None:
    if not isinstance(call_data, str) or not call_data.startswith("0x"):
        return None
    if len(call_data) < 10:
        return None
    selector = call_data[2:10].lower()
    if any(ch not in "0123456789abcdef" for ch in selector):
        return None
    return selector


def _highest_risk(
    interactions: list[InteractionAssessment],
) -> InteractionRiskLevel:
    if any(item.risk == InteractionRiskLevel.HIGH for item in interactions):
        return InteractionRiskLevel.HIGH
    if any(item.risk == InteractionRiskLevel.MEDIUM for item in interactions):
        return InteractionRiskLevel.MEDIUM
    return InteractionRiskLevel.LOW
