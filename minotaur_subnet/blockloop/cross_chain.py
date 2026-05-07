"""Cross-chain submission orchestration for the block loop pipeline."""

from __future__ import annotations

import logging
from typing import Any

from minotaur_subnet.shared.types import ExecutionPlan
from minotaur_subnet.orderbook.orderbook import Order, OrderStatus
from minotaur_subnet.blockloop.persistence import OrderPersistence

logger = logging.getLogger(__name__)


class CrossChainOrchestrator:
    """Handles cross-chain order submission via multi-phase lifecycle.

    Handles both EVM-only and substrate+EVM mixed-runtime plans:
    1. Execute substrate legs sequentially (unstake, bridge deposit)
    2. Execute EVM source legs via relayer
    3. Register with BridgeTracker -> order enters BRIDGING
    4. BridgeTracker polls bridge, submits EVM dest leg on completion

    Args:
        relayer: Relayer for submitting approved plans.
        substrate_relayer: Substrate relayer for substrate legs (optional).
        bridge_tracker: BridgeTracker for monitoring in-flight bridge transfers (optional).
        order_persistence: OrderPersistence for syncing order state.
        orderbook: The Intent OrderBook.
        app_store: Persistent store for app definitions and stats.
    """

    def __init__(
        self,
        relayer: Any,
        substrate_relayer: Any = None,
        bridge_tracker: Any = None,
        order_persistence: "OrderPersistence | None" = None,
        orderbook: Any = None,
        app_store: Any = None,
    ) -> None:
        self.relayer = relayer
        self.substrate_relayer = substrate_relayer
        self.bridge_tracker = bridge_tracker
        self.order_persistence = order_persistence
        self.orderbook = orderbook
        self.app_store = app_store

    async def submit(
        self,
        order: Order,
        plan: ExecutionPlan,
        score: float,
        consensus_result: Any,
        deployed_contract: str,
    ) -> bool:
        """Submit a cross-chain order via multi-phase lifecycle.

        Handles both EVM-only and substrate+EVM mixed-runtime plans:
        1. Execute substrate legs sequentially (unstake, bridge deposit)
        2. Execute EVM source legs via relayer
        3. Register with BridgeTracker -> order enters BRIDGING
        4. BridgeTracker polls bridge, submits EVM dest leg on completion
        """
        legs = plan.metadata.get("legs", [])

        # Separate substrate vs EVM legs
        substrate_legs = [l for l in legs if l.get("runtime") == "substrate"]
        evm_source_legs = [
            l for l in legs
            if l.get("runtime") != "substrate" and l.get("type") in ("source", "bridge")
        ]

        # Phase A: Execute substrate legs sequentially (unstake -> bridge deposit)
        bridge_extrinsic_hash = ""
        if substrate_legs and self.substrate_relayer is not None:
            from minotaur_subnet.shared.types import SubstrateAction

            for leg in substrate_legs:
                actions = leg.get("substrate_actions", [])
                for action_dict in actions:
                    action = SubstrateAction.from_dict(action_dict)

                    if action.action == "remove_stake":
                        self.orderbook.update_order(
                            order.order_id, status=OrderStatus.UNSTAKING,
                        )
                        self._sync(order.order_id)

                    result = await self.substrate_relayer.submit_action(action)
                    if not result.success:
                        self.orderbook.update_order(
                            order.order_id,
                            status=OrderStatus.REJECTED,
                            error=f"Substrate {action.action} failed: {result.error}",
                        )
                        self._sync(order.order_id)
                        return False

                    # Track bridge deposit extrinsic hash for BridgeTracker
                    if action.action == "bridge_deposit":
                        bridge_extrinsic_hash = result.tx_hash

                    logger.info(
                        "Cross-chain %s: substrate %s completed (tx=%s)",
                        order.order_id, action.action, result.tx_hash[:16] if result.tx_hash else "?",
                    )
        elif substrate_legs:
            self.orderbook.update_order(
                order.order_id,
                status=OrderStatus.REJECTED,
                error="Substrate relayer not configured for substrate legs",
            )
            self._sync(order.order_id)
            return False

        # Phase B: Execute EVM source legs (if any)
        source_indices: list[int] = []
        for leg in evm_source_legs:
            source_indices.extend(leg.get("interaction_indices", []))

        submit_result = None
        if source_indices:
            source_interactions = [
                plan.interactions[i] for i in sorted(set(source_indices))
                if i < len(plan.interactions)
            ]
            source_plan = ExecutionPlan(
                intent_id=plan.intent_id,
                interactions=source_interactions,
                deadline=plan.deadline,
                nonce=plan.nonce,
                metadata={**plan.metadata, "phase": "source"},
            )

            submit_result = await self.relayer.submit_plan(
                order, source_plan, score, consensus_result,
                contract_address=deployed_contract or None,
            )

            if not submit_result.success:
                self.orderbook.update_order(
                    order.order_id,
                    status=OrderStatus.REJECTED,
                    error=f"EVM source leg failed: {submit_result.error}",
                )
                self._sync(order.order_id)
                return False

        # Phase C: Register with bridge tracker
        tx_hash = (
            (submit_result.tx_hash if submit_result else "")
            or bridge_extrinsic_hash
        )

        if tx_hash:
            self.orderbook.update_order(
                order.order_id,
                status=OrderStatus.SUBMITTED,
                tx_hash=tx_hash,
            )
            self._sync(order.order_id)

        if self.bridge_tracker is not None and tx_hash:
            self.bridge_tracker.track(
                order_id=order.order_id,
                src_tx_hash=tx_hash,
                plan=plan,
            )
            self.orderbook.update_order(
                order.order_id, status=OrderStatus.BRIDGING,
            )
            self._sync(order.order_id)
            logger.info(
                "Cross-chain order %s: entering BRIDGING (tx=%s)",
                order.order_id, tx_hash[:16],
            )
        else:
            # No bridge tracker — mark as filled (e.g. mock bridge completes instantly)
            self.orderbook.update_order(
                order.order_id, status=OrderStatus.FILLED,
            )
            self._sync(order.order_id)

        self.app_store.record_execution(order.app_id, score, success=True)
        return True

    def _sync(self, order_id: str) -> None:
        """Sync order to store via persistence layer."""
        if self.order_persistence is not None:
            self.order_persistence.sync(order_id)
