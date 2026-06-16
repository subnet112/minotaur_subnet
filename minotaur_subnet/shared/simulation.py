"""Shared mock-simulation and mock-scoring helpers.

These utilities were originally private to the block loop but are also needed
by the REST API, validator, and test suites.  Extracting them here avoids
circular imports and gives every consumer a single, canonical location.
"""

from __future__ import annotations

from typing import Any

from minotaur_subnet.shared.types import (
    ExecutionPlan,
    SimulationResult,
    TokenTransfer,
)


def build_mock_simulation(
    plan: ExecutionPlan, params: dict[str, Any],
) -> SimulationResult:
    """Build a mock simulation result based on the plan."""
    gas_per_ix = 80_000
    gas_used = 21_000 + len(plan.interactions) * gas_per_ix

    transfers: list[TokenTransfer] = []
    input_token = params.get("input_token", "")
    output_token = params.get("output_token", "")
    input_amount = params.get("input_amount", "0")

    if input_token and output_token:
        transfers.append(TokenTransfer(
            token=input_token,
            from_addr="0x" + "00" * 20,
            to_addr="0x" + "11" * 20,
            amount=input_amount,
        ))
        output_amount = str(int(int(input_amount) * 0.98)) if input_amount.isdigit() else "0"
        transfers.append(TokenTransfer(
            token=output_token,
            from_addr="0x" + "11" * 20,
            to_addr="0x" + "00" * 20,
            amount=output_amount,
        ))

    return SimulationResult(
        success=True,
        gas_used=gas_used,
        token_transfers=transfers,
    )


def compute_mock_score(plan: ExecutionPlan, params: dict[str, Any]) -> float:
    """Compute a simple mock score based on plan characteristics."""
    score = 0.6  # Base score
    if len(plan.interactions) >= 2:
        score += 0.1  # Multi-step plans get a bonus
    if plan.metadata.get("fallback"):
        score = 0.3  # Fallback plans score low
    if params.get("input_token") and params.get("output_token"):
        score += 0.05  # Valid token pair bonus
    return min(score, 1.0)
