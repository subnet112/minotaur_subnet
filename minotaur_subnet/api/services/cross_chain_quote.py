"""Unified cross-chain quote assembly.

Turns a solver plan's ``metadata["cross_chain_plan"]`` into a real quote:
the plan is dry-compiled through the CrossChainCompiler (the same trust
boundary the execution path uses), which validates its structure and fetches
LIVE bridge quotes — so the user sees the actual bridge fee, ETA, and
min-guaranteed output instead of the solver's self-reported estimate, plus
the revert plan that will apply at each failure point.

Simulation (§10 step 2): when a multi-chain simulator is wired, the compiled
journey ALSO runs per-leg on the per-chain quote forks — solver legs execute,
bridge legs are estimated live (never executed on a fork), and the
destination fork is seeded with the bridge's estimated output. A successful
run replaces the solver's declared number with a PLATFORM-VERIFIED
destination output (``estimated_output_source: "leg_simulation"``,
``simulated: true``). Any failure — no simulator, a leg reverting, nothing
delivered — falls back to exactly the pre-simulation payload
(``solver_declared`` / ``bridge_quote``, ``simulated: false``), labelled,
never raising: a quote must degrade, not 500.
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
    simulator: Any = None,
    bridge_registry: Any = None,
    params: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Compile the solver's CrossChainPlan and assemble a quote payload.

    Args:
        plan_metadata: The solver plan's metadata dict (must contain
            ``cross_chain_plan``).
        compiler: The blockloop's CrossChainCompiler (or None).
        simulator: MultiChainSimulator for per-leg simulation (or None —
            the quote degrades to the unsimulated payload).
        bridge_registry: BridgeRegistry for the simulator's live bridge
            estimates (destination-fork seeding).
        params: The RESOLVED intent params (output_token/receiver/
            input_token/input_amount) — used to fund the source leg and to
            recognise the delivered output.

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

    # Per-leg simulation (§10 step 2): run the compiled journey on the
    # per-chain forks. Failure of any kind leaves ``leg_sim`` None and the
    # payload degrades to the pre-simulation shape, labelled as such.
    leg_sim = await _simulate_compiled_legs(
        compiled, simulator, bridge_registry, params or {},
    )

    # Delivered estimate, by provenance (best first):
    #   "leg_simulation"  — the destination leg EXECUTED on a seeded fork and
    #       this is the output observed arriving; platform-verified.
    #   "solver_declared" — the solver's own number; labelled, not endorsed.
    #   "bridge_quote"    — the last bridge's live estimated delivery; a
    #       destination-leg swap can legitimately exceed it, so this is
    #       context, not a cap.
    bridge_floor = bridges[-1]["estimated_output"] if bridges else 0
    declared = plan_metadata.get("dst_amount") or plan_metadata.get("expected_output")
    try:
        declared_int = int(declared) if declared is not None else 0
    except (ValueError, TypeError):
        declared_int = 0

    if leg_sim is not None and leg_sim["delivered"] > 0:
        estimated_output = leg_sim["delivered"]
        estimated_output_source = "leg_simulation"
    elif declared_int > 0:
        estimated_output = declared_int
        estimated_output_source = "solver_declared"
    else:
        estimated_output = bridge_floor
        estimated_output_source = "bridge_quote"

    payload = {
        "estimated_output": estimated_output,
        "estimated_output_source": estimated_output_source,
        "bridge_floor": bridge_floor,
        "bridges": bridges,
        "legs": legs,
        "revert_plan": revert_plan,
        "escrow_deadlines": [p.get("deadline") for p in compiled.escrow_params],
        "total_bridge_eta_s": sum(b["estimated_duration_s"] for b in bridges),
        "simulated": estimated_output_source == "leg_simulation",
    }
    if leg_sim is not None:
        # Diagnostics even when the sim didn't win the estimate (e.g. a
        # reverting leg) — a client can show WHY a quote is unverified.
        payload["leg_simulation"] = leg_sim["report"]
    return payload


async def _simulate_compiled_legs(
    compiled: Any,
    simulator: Any,
    bridge_registry: Any,
    params: dict[str, Any],
) -> dict[str, Any] | None:
    """Run the compiled multi-leg journey per-leg on the quote forks.

    Solver legs execute on their chain's fork; bridge legs are never
    executed (``simulate_cross_chain`` estimates them via the live
    registry) and their estimated output seeds the destination fork. The
    delivered amount is what the DESTINATION legs were observed moving of
    the intent's output token.

    Returns ``{"delivered": int, "report": {...}}`` or None when the
    simulation couldn't run or produced nothing usable. Never raises.
    """
    if simulator is None or not hasattr(simulator, "simulate_cross_chain"):
        return None
    try:
        from minotaur_subnet.shared.types import ExecutionPlan

        plan = ExecutionPlan(
            intent_id="quote",
            interactions=[],
            deadline=int(time.time()) + 3600,
            nonce=0,
            metadata={"multi_leg_plan": compiled.multi_leg_plan.to_dict()},
        )

        # Fund the source leg's executor with the intent's input so the
        # journey starts the way the real order would.
        token_balances: dict[str, int] | None = None
        _tok = str(params.get("input_token") or "")
        try:
            _amt = int(params.get("input_amount") or 0)
        except (ValueError, TypeError):
            _amt = 0
        if _tok and _amt > 0:
            token_balances = {_tok: _amt}

        result = await simulator.simulate_cross_chain(
            plan,
            bridge_registry=bridge_registry,
            token_balances=token_balances,
        )
    except Exception as exc:  # noqa: BLE001 — a quote degrades, never 500s
        logger.info("Cross-chain quote leg simulation failed: %s", exc)
        return None

    leg_results = getattr(result, "leg_results", None) or {}
    if not leg_results:
        return None

    # Destination legs = everything after the first bridge leg (the
    # normalized walk orders legs source -> bridge -> destination).
    bridge_ids = [
        lid for lid, lr in leg_results.items()
        if isinstance(lr, dict) and lr.get("type") == "bridge"
    ]
    first_bridge = min(bridge_ids) if bridge_ids else None

    output_token = str(params.get("output_token") or "").lower()
    receiver = str(params.get("receiver") or "").lower()
    delivered = 0
    dest_ok = True
    saw_dest = False
    per_leg: list[dict[str, Any]] = []
    for lid in sorted(leg_results):
        lr = leg_results[lid]
        if not isinstance(lr, dict):
            continue
        is_dest = first_bridge is not None and lid > first_bridge \
            and lr.get("type") != "bridge"
        per_leg.append({
            "leg_id": lid,
            "success": bool(lr.get("success")),
            "gas_used": lr.get("gas_used", 0),
            "error": lr.get("error"),
            "destination": is_dest,
        })
        if not is_dest:
            continue
        saw_dest = True
        if not lr.get("success"):
            dest_ok = False
            continue
        # What arrived: output-token transfers to the quote recipient (or
        # the receiver param); when neither matches — quote-time calldata
        # carries placeholder addresses — fall back to the LARGEST single
        # output-token transfer, which for a swap's final hop is the
        # delivery edge.
        matched = 0
        largest = 0
        for t in lr.get("token_transfers") or []:
            if str(t.get("token") or "").lower() != output_token:
                continue
            try:
                amt = int(t.get("amount") or 0)
            except (ValueError, TypeError):
                continue
            to_addr = str(t.get("to") or "").lower()
            if to_addr in (receiver, _QUOTE_USER.lower()) and to_addr:
                matched += amt
            largest = max(largest, amt)
        delivered += matched if matched > 0 else largest

    if not saw_dest:
        return None
    if not dest_ok:
        delivered = 0

    return {
        "delivered": delivered,
        "report": {
            "destination_success": dest_ok,
            "delivered": str(delivered),
            "per_leg": per_leg,
        },
    }
