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
from collections.abc import Mapping
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

# Canonical per-chain addresses of bridgeable assets. A CODE CONSTANT — same
# discipline as BENCHMARK_BRIDGE_FEE_BPS (anything that feeds scoring and can
# differ between two nodes eventually will). Mirrors the adapters' static
# maps (bridge/across.py TOKEN_MAP, CCTP's USDC domains); keep in sync by
# hand, never read from a live adapter here.
#
# WHY (soak finding, 2026-07-28): a solver-shape ``bridge_request`` carries
# ONE token — the SOURCE chain's address. Destination-fork seeding used it
# verbatim, so a Base→ETH plan tried to deal Base's WETH (0x4200…06) on the
# Ethereum fork (no code there), the deal failed, and the destination leg
# could never spend: correct plans measured null in exactly the direction we
# want miners to solve. The identity mapping MUST be platform-derived: were
# it solver-declared, declaring a cheap token would get its balance seeded
# equal to the bridged amount of the real one — free destination inventory.
_CANONICAL_TOKEN_BY_CHAIN: dict[str, dict[int, str]] = {
    "WETH": {
        1: "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",
        8453: "0x4200000000000000000000000000000000000006",
    },
    "USDC": {
        1: "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
        8453: "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
    },
}


def map_bridged_token(token: str, src_chain_id: Any, dst_chain_id: Any) -> str:
    """The destination-chain address of the asset a bridge leg moves.

    Unmapped tokens pass through unchanged — the seeding then fails exactly
    as it always did for an unknown asset, which is the fail-closed outcome
    (no credit), never a mis-credit.
    """
    try:
        src, dst = int(src_chain_id), int(dst_chain_id)
    except (TypeError, ValueError):
        return token
    t = str(token or "").lower()
    if not t:
        return token
    for by_chain in _CANONICAL_TOKEN_BY_CHAIN.values():
        if by_chain.get(src, "").lower() == t:
            return by_chain.get(dst) or token
    return token


def declares_cross_chain(meta: Mapping[str, Any] | None) -> bool:
    """THE cross-chain declaration predicate. Every gate must call this one.

    Four shapes mean "cross-chain", and which one a plan carries depends only
    on how far through the pipeline it is — not on whether it is cross-chain:

      ``cross_chain_plan``  the SOLVER's request (what miners emit)
      ``multi_leg_plan``    the COMPILER's output (carries real bridge calldata)
      ``cross_chain``       the post-compile marker (``order_processor`` sets it
                            and pops ``cross_chain_plan``, so it is the only key
                            a live order's stored plan is guaranteed to have)
      ``legs``              the LEGACY convention ``normalize_to_legs`` projects
                            onto, and the shape ``simulate_cross_chain`` walks

    Accepting a subset is how gates silently disagree, and every disagreement
    found so far failed in the same direction — a plan treated as cross-chain by
    one gate and as single-chain by another, which reliably reads to a miner as
    "cross-chain earns nothing". Concretely, before these were unified:

      - the miner-facing quote gate accepted only ``cross_chain``, while the
        ``build_cross_chain_quote`` behind it reads ``cross_chain_plan`` — so
        the reference solver's own EVM shape fell through to the single-chain
        sim, whose empty top-level interactions quote 0;
      - the benchmark's bridge-mocking gate omitted ``legs``, so a legacy-shape
        plan was destination-MEASURED but its bridge calldata was never mocked,
        and therefore reverted in the scored sim.

    Takes the metadata mapping, not the plan, because half the call sites
    (stored order plans, quote-time plan metadata) only ever hold the dict.
    """
    meta = meta or {}
    return bool(
        meta.get("legs")
        or meta.get("multi_leg_plan")
        or meta.get("cross_chain_plan")
        or meta.get("cross_chain")
    )


def is_cross_chain_plan(plan: ExecutionPlan) -> bool:
    """Does this plan DECLARE cross-chain intent?

    Declaration, not calldata. Thin wrapper over :func:`declares_cross_chain`
    so plan-holding and metadata-holding call sites share one definition.
    """
    return declares_cross_chain(plan.metadata)


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
                    # token_out seeds the DESTINATION fork, so it must be the
                    # asset's address on the destination chain — the request
                    # carries only the source-chain address.
                    "token_out": map_bridged_token(
                        br.get("token") or "",
                        br.get("src_chain_id"), br.get("dst_chain_id"),
                    ),
                    "token_in": br.get("token") or "",
                })
        return out

    return []


def bridge_execution_plan(
    plan: ExecutionPlan,
    bridge_leg: dict[str, Any],
) -> ExecutionPlan | None:
    """The ONE simulation that makes a bridge deposit observable.

    Two things have to hold for the observed deposit to mean anything:

    1. **The deposit must execute against what the plan actually earned.**
       Each ``simulate()`` call is snapshot-isolated, so simulating the
       bridge leg alone runs it against the fork's seeded balances — a
       swap-then-bridge plan would see its (honest) deposit revert because
       the swap's output never existed in that sim. So every PRECEDING leg
       that executes on the bridge's own chain is prepended, in leg order,
       and the whole journey up to the deposit runs as one simulation.

    2. **A solver-shape bridge leg must be executable at all.** The solver
       declares ``bridge_requests`` abstractly — no calldata — which is why
       it could only ever measure as "declared" (its own number, gameable).
       When the leg carries no interactions, the deposit is SYNTHESIZED as
       the same ``transfer(_MOCK_BRIDGE_TARGET, amount)`` the mocking path
       produces (shared encoder: ``mock_bridge_deposit``), so a declared
       amount the preceding legs never earned reverts instead of being
       credited — identical anti-gaming semantics for both plan shapes.

    Returns None when there is nothing executable to observe (no calldata
    and no token/amount to synthesize from — e.g. a native-asset bridge with
    no ERC-20 to transfer); the caller falls back to the declared amount,
    labelled as such.
    """
    from minotaur_subnet.shared.types import (
        extract_leg_plan,
        mock_bridge_deposit,
        mock_bridge_interactions,
    )

    meta = plan.metadata or {}
    legs = meta.get("legs") or []
    bridge_id = bridge_leg.get("leg_id")
    bridge_chain = bridge_leg.get("chain_id")

    try:
        declared = int(bridge_leg.get("bridge_amount") or 0)
    except (ValueError, TypeError):
        declared = 0
    token_in = str(bridge_leg.get("token_in") or "")

    # The deposit itself: mock real calldata, synthesize when there is none.
    bridge_ixs = extract_leg_plan(plan, bridge_id).interactions
    if bridge_ixs:
        deposit = mock_bridge_interactions(bridge_ixs, token_in, declared)
    elif token_in and declared > 0:
        deposit = [mock_bridge_deposit(token_in, declared, int(bridge_chain or 0))]
    else:
        return None

    combined: list[Interaction] = []
    for leg in sorted(legs, key=lambda l: l.get("leg_id", 0)):
        if leg.get("leg_id") == bridge_id:
            break
        if leg.get("runtime") == "substrate" or leg.get("type") in ("wait", "bridge"):
            # Substrate/wait legs don't run on this fork; an EARLIER bridge
            # leg (multi-hop) already moved its funds off — neither belongs
            # in this journey.
            continue
        if leg.get("chain_id") != bridge_chain:
            continue
        combined.extend(extract_leg_plan(plan, leg["leg_id"]).interactions)
    combined.extend(deposit)

    return ExecutionPlan(
        intent_id=plan.intent_id,
        interactions=combined,
        deadline=plan.deadline,
        nonce=plan.nonce,
        metadata=plan.metadata,
    )


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
