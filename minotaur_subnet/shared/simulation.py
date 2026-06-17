"""Shared mock-simulation and mock-scoring helpers.

These utilities were originally private to the block loop but are also needed
by the REST API, validator, and test suites.  Extracting them here avoids
circular imports and gives every consumer a single, canonical location.
"""

from __future__ import annotations

import os
from typing import Any

from minotaur_subnet.shared.types import (
    ExecutionPlan,
    SimulationResult,
    TokenTransfer,
)


def onchain_score_fail_closed() -> bool:
    """Whether the dual-scoring on-chain gate should FAIL CLOSED on a missing score.

    Dual scoring requires BOTH the JS score AND the on-chain ``scoreIntent``
    value to clear threshold. When a contract is present the on-chain score is
    EXPECTED; ``simulation.on_chain_score is None`` means the contract returned
    ``valid=False`` (the plan violates an on-chain invariant) or the score
    couldn't be read — either way the contract did NOT bless the plan. With this
    flag on, the gate REJECTS instead of silently riding on the JS score alone.

    Read at call time and DEFAULT OFF (preserves the legacy skip-on-None) so it
    can be enabled deliberately and observed, then turned on fleet-wide — leader
    and follower must agree, so flip it everywhere together.
    """
    return os.environ.get("ONCHAIN_SCORE_FAIL_CLOSED", "").strip().lower() in (
        "1", "true", "yes", "on",
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
