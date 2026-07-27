"""Typed intent contexts for Architecture V3."""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from typing import Any

from minotaur_subnet.shared.types import AppIntentDefinition, IntentState


@dataclass
class BaseIntentContext:
    """Base typed context shared by all intents."""

    app_id: str
    intent_function: str
    chain_id: int
    owner: str
    contract_address: str = ""
    nonce: int = 0
    raw_params: dict[str, Any] = field(default_factory=dict)
    context_version: str = "v3"


def build_typed_context(
    intent: AppIntentDefinition,
    intent_function: str,
    state: IntentState,
) -> BaseIntentContext:
    """Build the typed context for the current intent/state.

    APP-AGNOSTIC. This used to dispatch on the intent being literally named
    "swap", "twap" or "rebalance" and return a correspondingly-shaped context
    (SwapIntentContext with input_token/min_output_amount/fee_tier, and so
    on), normalising each through its own param-alias table. That baked three
    specific app designs into a path every app goes through, and an app that
    was none of them silently got the base context anyway.

    Every consumer in the tree reads only ``intent_function`` and
    ``raw_params`` — both of which live on the base — so the archetypes were
    carrying no weight beyond their own construction. An app that wants its
    params interpreted does that in its own solver, where it knows what they
    mean.
    """
    return BaseIntentContext(
        app_id=intent.app_id,
        intent_function=intent_function,
        chain_id=state.chain_id,
        owner=state.owner,
        contract_address=state.contract_address,
        nonce=state.nonce,
        raw_params=dict(state.raw_params_view()),
    )


def typed_context_from_dict(data: dict[str, Any] | None) -> BaseIntentContext | None:
    """Reconstruct a typed context object from serialized state data."""
    if not isinstance(data, dict):
        return None

    # Tolerant of contexts serialised by an older validator, which carried
    # archetype-specific fields (input_token, num_chunks, target_allocations,
    # …). Anything the base does not declare is folded into raw_params rather
    # than rejected, so the two sides can roll independently.
    known = {f.name for f in fields(BaseIntentContext)}
    base = {k: v for k, v in data.items() if k in known}
    extra = {k: v for k, v in data.items() if k not in known}
    if extra:
        base["raw_params"] = {**extra, **(base.get("raw_params") or {})}
    return BaseIntentContext(**base)
