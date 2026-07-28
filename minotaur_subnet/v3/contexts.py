"""Typed intent contexts for Architecture V3."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from minotaur_subnet.shared.types import AppIntentDefinition, IntentState
from minotaur_subnet.v3.manifest import (
    manifest_from_definition,
    normalize_rebalance_intent_params,
    normalize_swap_intent_params,
    normalize_twap_intent_params,
)


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


@dataclass
class SwapIntentContext(BaseIntentContext):
    """Typed context for swap-like intents."""

    input_token: str = ""
    output_token: str = ""
    input_amount: int = 0
    min_output_amount: int = 0
    receiver: str = ""
    fee_tier: int = 3000
    permit_deadline: int = 0
    permit_v: int = 0
    permit_r: str = "0x" + "00" * 32
    permit_s: str = "0x" + "00" * 32

    @classmethod
    def from_params(
        cls,
        *,
        app_id: str,
        intent_function: str,
        chain_id: int,
        owner: str,
        contract_address: str,
        nonce: int,
        params: dict[str, Any],
    ) -> "SwapIntentContext":
        """Construct a swap context from raw runtime params."""
        normalized = normalize_swap_intent_params(
            params,
            receiver_default=contract_address or owner,
        )
        return cls(
            app_id=app_id,
            intent_function=intent_function,
            chain_id=chain_id,
            owner=owner,
            contract_address=contract_address,
            nonce=nonce,
            raw_params=dict(params),
            input_token=normalized["input_token"],
            output_token=normalized["output_token"],
            input_amount=normalized["input_amount"],
            min_output_amount=normalized["min_output_amount"],
            receiver=normalized["receiver"],
            fee_tier=normalized["fee_tier"],
            permit_deadline=normalized["permit_deadline"],
            permit_v=normalized["permit_v"],
            permit_r=normalized["permit_r"],
            permit_s=normalized["permit_s"],
        )


@dataclass
class TwapIntentContext(BaseIntentContext):
    """Typed context for TWAP-like intents."""

    input_token: str = ""
    output_token: str = ""
    total_amount: int = 0
    num_chunks: int = 0
    interval_seconds: int = 0
    chunks_executed: int = 0
    last_chunk_time: int = 0
    min_output_per_chunk: int = 0
    receiver: str = ""
    fee_tier: int = 3000

    @classmethod
    def from_params(
        cls,
        *,
        app_id: str,
        intent_function: str,
        chain_id: int,
        owner: str,
        contract_address: str,
        nonce: int,
        params: dict[str, Any],
    ) -> "TwapIntentContext":
        normalized = normalize_twap_intent_params(
            params,
            receiver_default=contract_address or owner,
        )
        return cls(
            app_id=app_id,
            intent_function=intent_function,
            chain_id=chain_id,
            owner=owner,
            contract_address=contract_address,
            nonce=nonce,
            raw_params=dict(params),
            input_token=normalized["input_token"],
            output_token=normalized["output_token"],
            total_amount=normalized["total_amount"],
            num_chunks=normalized["num_chunks"],
            interval_seconds=normalized["interval_seconds"],
            chunks_executed=normalized["chunks_executed"],
            last_chunk_time=normalized["last_chunk_time"],
            min_output_per_chunk=normalized["min_output_per_chunk"],
            receiver=normalized["receiver"],
            fee_tier=normalized["fee_tier"],
        )


@dataclass
class RebalanceIntentContext(BaseIntentContext):
    """Typed context for rebalance-like intents."""

    target_allocations: dict[str, float] = field(default_factory=dict)
    current_allocations: dict[str, float] = field(default_factory=dict)
    threshold_pct: float = 0.0
    total_value_usd: float = 0.0
    token_addresses: dict[str, str] = field(default_factory=dict)
    token_decimals: dict[str, int] = field(default_factory=dict)

    @classmethod
    def from_params(
        cls,
        *,
        app_id: str,
        intent_function: str,
        chain_id: int,
        owner: str,
        contract_address: str,
        nonce: int,
        params: dict[str, Any],
    ) -> "RebalanceIntentContext":
        normalized = normalize_rebalance_intent_params(params)
        return cls(
            app_id=app_id,
            intent_function=intent_function,
            chain_id=chain_id,
            owner=owner,
            contract_address=contract_address,
            nonce=nonce,
            raw_params=dict(params),
            target_allocations=normalized["target_allocations"],
            current_allocations=normalized["current_allocations"],
            threshold_pct=normalized["threshold_pct"],
            total_value_usd=normalized["total_value_usd"],
            token_addresses=normalized["token_addresses"],
            token_decimals=normalized["token_decimals"],
        )


def build_typed_context(
    intent: AppIntentDefinition,
    intent_function: str,
    state: IntentState,
) -> BaseIntentContext:
    """Build the best available typed context for the current intent/state."""
    params = state.raw_params_view()
    manifest = manifest_from_definition(intent)
    if intent.intent_type == "swap" or intent_function == "swap":
        normalized = normalize_swap_intent_params(
            params,
            manifest=manifest,
            intent_name=intent_function,
            receiver_default=state.contract_address or state.owner,
        )
        return SwapIntentContext.from_params(
            app_id=intent.app_id,
            intent_function=intent_function,
            chain_id=state.chain_id,
            owner=state.owner,
            contract_address=state.contract_address,
            nonce=state.nonce,
            params=normalized,
        )
    if intent.intent_type == "twap" or intent_function == "twap":
        normalized = normalize_twap_intent_params(
            params,
            manifest=manifest,
            intent_name=intent_function,
            receiver_default=state.contract_address or state.owner,
        )
        return TwapIntentContext.from_params(
            app_id=intent.app_id,
            intent_function=intent_function,
            chain_id=state.chain_id,
            owner=state.owner,
            contract_address=state.contract_address,
            nonce=state.nonce,
            params=normalized,
        )
    if intent.intent_type == "rebalance" or intent_function == "rebalance":
        normalized = normalize_rebalance_intent_params(
            params,
            manifest=manifest,
            intent_name=intent_function,
        )
        return RebalanceIntentContext.from_params(
            app_id=intent.app_id,
            intent_function=intent_function,
            chain_id=state.chain_id,
            owner=state.owner,
            contract_address=state.contract_address,
            nonce=state.nonce,
            params=normalized,
        )

    return BaseIntentContext(
        app_id=intent.app_id,
        intent_function=intent_function,
        chain_id=state.chain_id,
        owner=state.owner,
        contract_address=state.contract_address,
        nonce=state.nonce,
        raw_params=dict(params),
    )


def typed_context_from_dict(data: dict[str, Any] | None) -> BaseIntentContext | None:
    """Reconstruct a typed context object from serialized state data."""
    if not isinstance(data, dict):
        return None

    if "input_token" in data and "input_amount" in data:
        return SwapIntentContext(**data)
    if "total_amount" in data and "num_chunks" in data:
        return TwapIntentContext(**data)
    if "target_allocations" in data and "current_allocations" in data:
        return RebalanceIntentContext(**data)
    return BaseIntentContext(**data)
