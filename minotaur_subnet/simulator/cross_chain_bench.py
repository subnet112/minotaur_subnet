"""Deterministic cross-chain simulation inputs for the BENCHMARK.

Two things stand between a cross-chain plan and a destination-side
measurement the champion contest can trust:

1. ``MultiChainSimulator.simulate_cross_chain`` only understands the LEGACY
   leg convention (``metadata["legs"]`` with ``interaction_indices``). The
   modern shapes — the solver's ``cross_chain_plan`` and the compiler's
   ``multi_leg_plan`` — fall straight through to single-chain ``simulate()``,
   so nothing on the destination chain is ever executed. ``normalize_to_legs``
   projects both modern shapes onto the legacy one; per
   docs/architecture/cross-chain-intents.md §8 trap 1 the two conventions
   coexist deliberately, so this ADAPTS rather than consolidates.

2. The bridged amount. ``simulate_cross_chain`` gets it from
   ``bridge_registry.best_quote()`` — a LIVE Across/Iris HTTP call. That is
   correct for the live rail and disqualifying for the benchmark: two
   validators quoting the same plan seconds apart would seed the destination
   fork differently and score it differently. The obvious alternative, the
   solver's own declared ``estimated_output``, is deterministic but
   self-reported — a miner inflates it and is credited for delivery that
   never happened.

``benchmark_bridge_estimate`` is the third option: a CODE CONSTANT haircut
applied to an amount the simulation OBSERVED. Same reasoning as
``CHAMPION_MINER_WEIGHT_FRACTION`` being a constant rather than an env knob —
anything that feeds scoring and can differ between two nodes eventually will.
"""

from __future__ import annotations

import logging
from typing import Any

from minotaur_subnet.shared.types import (
    ExecutionPlan,
    Interaction,
    _MOCK_BRIDGE_TARGET,
)

logger = logging.getLogger(__name__)

# Bridge haircut used for BENCHMARK scoring only. A CODE CONSTANT — never an
# env var, never a live quote.
#
# Deliberately above the observed live range (measured 2026-07-26 from the
# leader: Across WETH 2.78 bps, Across USDC 1.03–1.45 bps, CCTP USDC 1.00
# bps) so the benchmark never flatters a bridged route relative to a
# single-chain one. The exact value is common-mode — every solver's plan
# takes the identical haircut, so it cannot advantage one over another; what
# matters is that it is fixed and not solver-controlled.
BENCHMARK_BRIDGE_FEE_BPS = 5

# Legacy leg "type" values that simulate_cross_chain branches on.
_LEG_SOURCE = "source"
_LEG_BRIDGE = "bridge"
_LEG_DESTINATION = "destination"


def is_cross_chain_plan(plan: ExecutionPlan) -> bool:
    """Does this plan DECLARE cross-chain intent?

    Declaration, not calldata — the same gate the benchmark's bridge mocking
    uses, so the two can never disagree about what is cross-chain.
    """
    meta = plan.metadata or {}
    return bool(
        meta.get("legs")
        or meta.get("multi_leg_plan")
        or meta.get("cross_chain_plan")
        or meta.get("cross_chain")
    )


def normalize_to_legs(plan: ExecutionPlan) -> ExecutionPlan | None:
    """Project a modern cross-chain plan onto the legacy ``legs`` convention.

    Returns a NEW ExecutionPlan whose ``interactions`` is the flattened
    per-leg concatenation and whose ``metadata["legs"]`` carries
    ``interaction_indices`` into it — the shape ``simulate_cross_chain``
    already knows how to walk. Returns:

      - ``plan`` unchanged when it already carries ``metadata["legs"]``;
      - ``None`` when there is nothing multi-leg to normalize (the caller
        keeps its single-chain path).

    Rollback/revert legs are excluded: they are the recovery path, not the
    forward outcome being measured.
    """
    meta = plan.metadata or {}
    if meta.get("legs"):
        return plan

    legs_src = _forward_legs(meta)
    if not legs_src or len(legs_src) < 2:
        # A single leg is not a multi-chain journey — nothing to measure on a
        # destination fork.
        return None

    flat: list[Interaction] = []
    legs_meta: list[dict[str, Any]] = []
    seen_bridge = False

    for leg in legs_src:
        interactions = leg.get("interactions") or []
        start = len(flat)
        flat.extend(
            ix if isinstance(ix, Interaction) else Interaction(**ix)
            for ix in interactions
        )
        leg_type = leg.get("type")
        if leg_type == _LEG_BRIDGE:
            seen_bridge = True
        elif seen_bridge:
            leg_type = _LEG_DESTINATION
        else:
            leg_type = _LEG_SOURCE

        legs_meta.append({
            "leg_id": len(legs_meta),
            "chain_id": leg.get("chain_id"),
            "type": leg_type,
            "interaction_indices": list(range(start, len(flat))),
            **({"bridge_amount": leg["bridge_amount"]}
               if leg.get("bridge_amount") is not None else {}),
            **({"token_out": leg["token_out"]}
               if leg.get("token_out") else {}),
            **({"token_in": leg["token_in"]}
               if leg.get("token_in") else {}),
        })

    if not seen_bridge:
        return None

    normalized_meta = dict(meta)
    normalized_meta["legs"] = legs_meta
    return ExecutionPlan(
        intent_id=plan.intent_id,
        interactions=flat,
        deadline=plan.deadline,
        nonce=plan.nonce,
        metadata=normalized_meta,
    )


def _forward_legs(meta: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalize either modern shape into a flat forward-leg list.

    ``multi_leg_plan`` (compiler output, carries real bridge calldata) wins
    over ``cross_chain_plan`` (the solver's request, where bridge legs are
    still abstract) — if both are present the compiled one is what would
    actually execute.
    """
    mlp = meta.get("multi_leg_plan")
    if isinstance(mlp, dict) and mlp.get("forward_legs"):
        out = []
        for leg in mlp["forward_legs"]:
            lm = leg.get("metadata") or {}
            out.append({
                "chain_id": leg.get("chain_id"),
                "interactions": leg.get("interactions") or [],
                "type": _LEG_BRIDGE if lm.get("type") in ("bridge", "bridge_source")
                        else None,
                "bridge_amount": lm.get("bridge_amount"),
                "token_out": lm.get("bridge_token_out"),
                "token_in": lm.get("bridge_token_in"),
            })
        return out

    ccp = meta.get("cross_chain_plan")
    if isinstance(ccp, dict) and ccp.get("legs"):
        # Solver shape: legs alternate with bridge_requests, and the bridge
        # itself has no interactions (the compiler injects that calldata
        # later — the benchmark never runs the compiler, which is exactly why
        # the bridged amount can't come from a quote here).
        out = []
        bridges = ccp.get("bridge_requests") or []
        for i, leg in enumerate(ccp["legs"]):
            out.append({
                "chain_id": leg.get("chain_id"),
                "interactions": leg.get("interactions") or [],
                "type": None,
            })
            if i < len(bridges):
                br = bridges[i]
                out.append({
                    "chain_id": br.get("src_chain_id"),
                    "interactions": [],
                    "type": _LEG_BRIDGE,
                    "bridge_amount": br.get("amount"),
                    "token_out": br.get("token") or "",
                    "token_in": br.get("token") or "",
                })
        return out

    return []


def observed_bridged_amount(transfers: Any) -> int:
    """How much actually left for the bridge, read off the simulation.

    The benchmark rewrites bridge calldata to ``transfer(_MOCK_BRIDGE_TARGET,
    amount)`` (harness/orchestrator._mock_bridge_for_benchmark), so a
    transfer to that address IS the bridge deposit as executed. Reading the
    amount back from the sim — rather than from the plan — keeps the number
    both deterministic and outside the solver's control.

    Returns 0 when no such transfer is present.
    """
    total = 0
    target = _MOCK_BRIDGE_TARGET.lower()
    for t in transfers or []:
        to_addr = (
            getattr(t, "to_addr", None)
            or (t.get("to") if isinstance(t, dict) else None)
            or ""
        )
        if str(to_addr).lower() != target:
            continue
        amount = (
            getattr(t, "amount", None)
            if not isinstance(t, dict) else t.get("amount")
        )
        try:
            total += int(amount or 0)
        except (ValueError, TypeError):
            continue
    return total


def benchmark_bridge_estimate(
    amount_in: int,
    token_out: str,
    source: str,
) -> dict[str, Any]:
    """The deterministic bridge estimate the benchmark seeds the destination
    fork with.

    ``source`` records WHERE ``amount_in`` came from — "simulated" (observed
    leaving the source fork, the trustworthy case) or "declared" (the plan's
    own number, used only when no bridge transfer was observed). Phase-0
    analysis needs to tell those apart before any of this is allowed to move
    a score.
    """
    amount_in = max(0, int(amount_in))
    fee = amount_in * BENCHMARK_BRIDGE_FEE_BPS // 10_000
    return {
        "protocol": "benchmark_constant",
        "token_out": token_out or "",
        "amount_in": amount_in,
        "estimated_output": amount_in - fee,
        "fee": fee,
        "fee_bps": BENCHMARK_BRIDGE_FEE_BPS,
        "amount_source": source,
    }
