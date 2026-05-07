"""CrossChainCompiler — converts solver CrossChainPlans into executable MultiLegPlans.

This is the trust boundary between untrusted solver code and platform-controlled
bridge mechanics. The solver provides business-logic legs and bridge requests.
The compiler:

1. Validates the plan structure (chain continuity, no bridge selectors in solver code)
2. Gets real bridge quotes from the BridgeRegistry
3. Builds bridge calldata via the selected adapter
4. Wraps each bridge with escrow deposit parameters
5. Generates rollback legs (reverse bridge quotes)
6. Produces simulation mock configs (per bridge adapter)
7. Sets all metadata flags (solver metadata is untrusted for security-critical fields)
"""

from __future__ import annotations

import logging
import time
from dataclasses import asdict, dataclass
from typing import Any

from minotaur_subnet.bridge.base import BridgeAdapter, BridgeQuote
from minotaur_subnet.bridge.registry import BridgeRegistry
from minotaur_subnet.shared.types import (
    BridgeRequest,
    ChainLeg,
    CrossChainPlan,
    Interaction,
    LegPlan,
    MultiLegPlan,
    _BRIDGE_CALL_SELECTORS,
)

logger = logging.getLogger(__name__)


class CrossChainCompileError(ValueError):
    """Raised when a solver's CrossChainPlan is invalid or uncompilable."""


class CrossChainCompiler:
    """Converts solver CrossChainPlans into executable MultiLegPlans.

    The solver NEVER touches bridge calldata, escrow parameters, or
    security-critical metadata flags. All of these are generated here.
    """

    def __init__(self, bridge_registry: BridgeRegistry) -> None:
        self.bridge_registry = bridge_registry

    async def compile(
        self,
        solver_plan: CrossChainPlan,
        order_id: str,
        user_address: str,
        contract_address: str,
        deadline: int,
    ) -> "CompiledCrossChainPlan":
        """Compile a solver's cross-chain plan into executable form.

        Args:
            solver_plan: The solver's CrossChainPlan (untrusted).
            order_id: The intent order ID.
            user_address: The user's address (bridge recipient override).
            contract_address: The App contract address on the source chain.
            deadline: Order deadline timestamp.

        Returns:
            CompiledCrossChainPlan with executable MultiLegPlan, escrow params,
            bridge quotes, and simulation mock configs.

        Raises:
            CrossChainCompileError: If the plan is structurally invalid or
                contains bridge selectors in solver interactions.
        """
        # 1. Validate structure
        errors = self._validate(solver_plan)
        if errors:
            raise CrossChainCompileError(
                f"Invalid CrossChainPlan: {'; '.join(errors)}"
            )

        forward_legs: list[LegPlan] = []
        rollback_legs: list[LegPlan] = []
        escrow_params: list[dict[str, Any]] = []
        bridge_quotes: list[BridgeQuote] = []
        simulation_mocks: dict[int, dict[str, Any]] = {}
        leg_index = 0

        for i, solver_leg in enumerate(solver_plan.legs):
            # Add solver's business-logic leg
            forward_legs.append(LegPlan(
                leg_index=leg_index,
                chain_id=solver_leg.chain_id,
                intent_selector=solver_leg.intent_selector,
                intent_params_hex=solver_leg.intent_params_hex,
                interactions=list(solver_leg.interactions),
                depends_on=[leg_index - 1] if leg_index > 0 else [],
                metadata={
                    **solver_leg.metadata,
                    "type": "solver_leg",
                    "_platform_compiled": True,
                },
            ))
            leg_index += 1

            # If there's a bridge request after this leg, insert bridge leg
            if i < len(solver_plan.bridge_requests):
                br = solver_plan.bridge_requests[i]

                # Override recipient with user address (solver can't control this)
                safe_recipient = user_address

                # Get bridge quote
                quote = await self.bridge_registry.best_quote(
                    br.token, br.amount, br.src_chain_id, br.dst_chain_id,
                )
                if quote is None:
                    raise CrossChainCompileError(
                        f"No bridge route for {br.token[:10]}.. "
                        f"from chain {br.src_chain_id} to {br.dst_chain_id}"
                    )

                # Verify min_output
                min_out = br.min_output or int(quote.estimated_output * 99 / 100)
                if quote.estimated_output < min_out:
                    raise CrossChainCompileError(
                        f"Bridge output {quote.estimated_output} below "
                        f"min_output {min_out}"
                    )

                bridge_quotes.append(quote)

                # Build bridge interactions via adapter
                adapter = self.bridge_registry.get(quote.protocol)
                if adapter is None:
                    raise CrossChainCompileError(
                        f"Bridge adapter '{quote.protocol}' not found"
                    )
                bridge_interactions = adapter.build_bridge_interactions(
                    quote, safe_recipient,
                )

                # Get simulation mock config from adapter
                mock_cfg = adapter.mock_config(quote)
                simulation_mocks[leg_index] = mock_cfg

                # Build bridge leg
                bridge_meta = {
                    "type": "bridge",
                    "bridge_protocol": quote.protocol,
                    "bridge_token_in": quote.token_in,
                    "bridge_token_out": quote.token_out,
                    "bridge_amount": quote.amount_in,
                    "bridge_estimated_output": quote.estimated_output,
                    "bridge_fee": quote.fee,
                    "bridge_recipient": safe_recipient,
                    "_platform_compiled": True,
                }
                forward_legs.append(LegPlan(
                    leg_index=leg_index,
                    chain_id=br.src_chain_id,
                    intent_selector="",  # Bridge legs use the _bridge intent
                    intent_params_hex="",
                    interactions=bridge_interactions,
                    depends_on=[leg_index - 1],
                    metadata=bridge_meta,
                ))

                # Escrow params for the NEXT leg (destination)
                escrow_deadline = deadline if deadline > 0 else int(time.time()) + 7200
                escrow_params.append({
                    "order_id": order_id,
                    "leg_index": leg_index + 1,  # The destination leg after bridge
                    "token": quote.token_out,
                    "amount": quote.estimated_output,
                    "user": user_address,
                    "deadline": escrow_deadline,
                })

                # Rollback: reverse bridge
                try:
                    reverse_quote = await self.bridge_registry.best_quote(
                        quote.token_out, quote.estimated_output,
                        br.dst_chain_id, br.src_chain_id,
                    )
                    if reverse_quote and adapter:
                        reverse_ixs = adapter.build_bridge_interactions(
                            reverse_quote, user_address,
                        )
                        rollback_legs.append(LegPlan(
                            leg_index=100 + len(rollback_legs),
                            chain_id=br.dst_chain_id,
                            intent_selector="",
                            intent_params_hex="",
                            interactions=reverse_ixs,
                            rollback_for=leg_index,
                            metadata={
                                "type": "rollback_bridge",
                                "bridge_protocol": quote.protocol,
                                "_platform_compiled": True,
                            },
                        ))
                except Exception as exc:
                    logger.warning("Reverse bridge quote failed: %s", exc)

                leg_index += 1

        multi_leg = MultiLegPlan(
            forward_legs=forward_legs,
            rollback_legs=rollback_legs,
        )

        return CompiledCrossChainPlan(
            multi_leg_plan=multi_leg,
            escrow_params=escrow_params,
            bridge_quotes=bridge_quotes,
            simulation_mocks=simulation_mocks,
            solver_plan=solver_plan,
        )

    def _validate(self, plan: CrossChainPlan) -> list[str]:
        """Validate solver's plan. Returns list of errors (empty = valid)."""
        errors: list[str] = []

        if not plan.legs:
            errors.append("No legs in plan")
            return errors

        if len(plan.bridge_requests) != len(plan.legs) - 1:
            errors.append(
                f"bridge_requests count ({len(plan.bridge_requests)}) "
                f"must be len(legs)-1 ({len(plan.legs) - 1})"
            )

        for i, br in enumerate(plan.bridge_requests):
            if br.amount <= 0:
                errors.append(f"bridge_request[{i}]: amount must be positive")
            if br.src_chain_id == br.dst_chain_id:
                errors.append(f"bridge_request[{i}]: src == dst chain")
            if i < len(plan.legs):
                if plan.legs[i].chain_id != br.src_chain_id:
                    errors.append(
                        f"bridge_request[{i}]: src_chain {br.src_chain_id} "
                        f"doesn't match leg[{i}].chain_id {plan.legs[i].chain_id}"
                    )
            if i + 1 < len(plan.legs):
                if plan.legs[i + 1].chain_id != br.dst_chain_id:
                    errors.append(
                        f"bridge_request[{i}]: dst_chain {br.dst_chain_id} "
                        f"doesn't match leg[{i+1}].chain_id "
                        f"{plan.legs[i + 1].chain_id}"
                    )

        # Verify no bridge protocol selectors in solver interactions
        for i, leg in enumerate(plan.legs):
            for ix in leg.interactions:
                raw_cd = (ix.call_data or "")
                if raw_cd.startswith("0x"):
                    raw_cd = raw_cd[2:]
                selector = raw_cd[:8] if len(raw_cd) >= 8 else ""
                if selector in _BRIDGE_CALL_SELECTORS:
                    errors.append(
                        f"leg[{i}] contains bridge selector {selector} — "
                        f"solvers must not include bridge calldata"
                    )

        return errors


@dataclass
class CompiledCrossChainPlan:
    """Platform-compiled cross-chain plan.

    Output of CrossChainCompiler.compile(). Contains everything needed
    for execution: the MultiLegPlan with bridge interactions injected,
    escrow parameters, simulation mocks, and the original solver plan.
    """
    multi_leg_plan: MultiLegPlan
    escrow_params: list[dict[str, Any]]
    bridge_quotes: list[BridgeQuote]
    simulation_mocks: dict[int, dict[str, Any]]
    solver_plan: CrossChainPlan

    def to_dict(self) -> dict[str, Any]:
        return {
            "multi_leg_plan": self.multi_leg_plan.to_dict(),
            "escrow_params": self.escrow_params,
            "bridge_quotes": [asdict(q) for q in self.bridge_quotes],
            "simulation_mocks": {str(k): v for k, v in self.simulation_mocks.items()},
        }
