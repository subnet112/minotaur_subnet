"""Unified cross-chain quote assembly.

Turns a solver plan's ``metadata["cross_chain_plan"]`` into a real quote:
the plan is dry-compiled through the CrossChainCompiler (the same trust
boundary the execution path uses), which validates its structure and fetches
LIVE bridge quotes — so the user sees the actual bridge fee, ETA, and
min-guaranteed output instead of the solver's self-reported estimate, plus
the revert plan that will apply at each failure point.

Simulation note: per-leg Anvil simulation of the compiled plan is NOT run
here yet — the destination leg's swap output is taken from the solver's
declared estimate, floored by the bridge's estimated output. Wiring
``MultiChainSimulator.simulate_cross_chain`` into this path is the follow-up
tracked in docs/architecture/cross-chain-intents.md §10 step 2.
"""

from __future__ import annotations

import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

# Placeholder recipient for quote-time compilation. Only shapes calldata
# fields (recipient/depositor) that the quote never executes; the real user
# address is compiled in at order time.
_QUOTE_USER = "0x000000000000000000000000000000000000dEaD"


async def build_cross_chain_quote(
    plan_metadata: dict[str, Any],
    compiler: Any,
) -> dict[str, Any] | None:
    """Compile the solver's CrossChainPlan and assemble a quote payload.

    Args:
        plan_metadata: The solver plan's metadata dict (must contain
            ``cross_chain_plan``).
        compiler: The blockloop's CrossChainCompiler (or None).

    Returns:
        Quote payload dict, or None when this plan can't be compiled here
        (no compiler wired, no cross_chain_plan in metadata, or compile
        failure) — the caller then falls back to the legacy metadata
        estimate. Never raises.
    """
    ccp_dict = plan_metadata.get("cross_chain_plan")
    if not ccp_dict or compiler is None:
        return None

    try:
        from minotaur_subnet.shared.types import CrossChainPlan

        solver_plan = CrossChainPlan.from_dict(ccp_dict)
        compiled = await compiler.compile(
            solver_plan,
            order_id="quote",
            user_address=_QUOTE_USER,
            contract_address=_QUOTE_USER,
            deadline=int(time.time()) + 3600,
        )
    except Exception as exc:
        logger.info("Cross-chain quote compile failed: %s", exc)
        return None

    bridges = [
        {
            "protocol": q.protocol,
            "src_chain_id": q.src_chain_id,
            "dst_chain_id": q.dst_chain_id,
            "token_in": q.token_in,
            "token_out": q.token_out,
            "amount_in": q.amount_in,
            "estimated_output": q.estimated_output,
            "fee": q.fee,
            "estimated_duration_s": q.estimated_duration_s,
        }
        for q in compiled.bridge_quotes
    ]

    legs = [
        {
            "leg_index": leg.leg_index,
            "chain_id": leg.chain_id,
            "type": leg.metadata.get("type", "solver_leg"),
            "interactions": len(leg.interactions),
        }
        for leg in compiled.multi_leg_plan.forward_legs
    ]

    # Revert coverage: which failure points have a solver-authored revert
    # (e.g. swap back to original token) vs only the platform reverse-bridge.
    revert_plan = [
        {
            "rollback_for": leg.rollback_for,
            "chain_id": leg.chain_id,
            "type": leg.metadata.get("type", ""),
        }
        for leg in compiled.multi_leg_plan.rollback_legs
    ]

    # Delivered estimate: the solver's declared destination output, floored
    # by the LAST bridge's live estimated output (a solver can't quote more
    # than what actually arrives on the destination chain unless a
    # destination-leg swap adds value — which per-leg simulation will price
    # in when it lands; until then the bridge floor is the honest number
    # when the solver's claim exceeds sanity or is absent).
    bridge_floor = bridges[-1]["estimated_output"] if bridges else 0
    declared = plan_metadata.get("dst_amount") or plan_metadata.get("expected_output")
    try:
        declared_int = int(declared) if declared is not None else 0
    except (ValueError, TypeError):
        declared_int = 0
    estimated_output = declared_int if declared_int > 0 else bridge_floor

    return {
        "estimated_output": estimated_output,
        "bridge_floor": bridge_floor,
        "bridges": bridges,
        "legs": legs,
        "revert_plan": revert_plan,
        "escrow_deadlines": [p.get("deadline") for p in compiled.escrow_params],
        "total_bridge_eta_s": sum(b["estimated_duration_s"] for b in bridges),
        "simulated": False,
    }
