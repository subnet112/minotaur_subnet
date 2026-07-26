"""Multi-leg intent orchestration for the block loop pipeline."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from minotaur_subnet.shared.types import ExecutionPlan, IntentState, SimulationResult
from minotaur_subnet.shared.simulation import build_mock_simulation
from minotaur_subnet.orderbook.orderbook import Order, OrderStatus
from minotaur_subnet.blockloop.persistence import OrderPersistence

logger = logging.getLogger(__name__)


class MultiLegOrchestrator:
    """Orchestrates multi-leg intent execution with forward and rollback legs.

    Each forward leg is simulated, scored, consensus'd, and executed independently.
    If any leg fails, rollback legs execute in reverse order to restore the user
    to an acceptable state.

    Args:
        simulator: AnvilSimulator or MultiChainSimulator (optional).
        plan_scorer: PlanScorer for JS scoring.
        consensus: ConsensusManager (optional).
        peer_network: PeerNetwork for multi-validator broadcast (optional).
        relayer: Relayer for submitting approved plans.
        order_persistence: OrderPersistence for syncing order state.
        orderbook: The Intent OrderBook.
        app_store: Persistent store for app definitions and stats.
    """

    def __init__(
        self,
        simulator: Any = None,
        plan_scorer: Any = None,
        consensus: Any = None,
        peer_network: Any = None,
        relayer: Any = None,
        order_persistence: "OrderPersistence | None" = None,
        orderbook: Any = None,
        app_store: Any = None,
    ) -> None:
        self.simulator = simulator
        self.plan_scorer = plan_scorer
        self.consensus = consensus
        self.peer_network = peer_network
        self.relayer = relayer
        self.order_persistence = order_persistence
        self.orderbook = orderbook
        self.app_store = app_store
        self.bridge_tracker: Any = None

    async def process(
        self,
        order: Order,
        multi_leg_plan: Any,
        contract_address: str,
        plan_metadata: dict | None = None,
    ) -> bool:
        """Execute a multi-leg intent with forward execution and rollback on failure.

        Each forward leg is simulated, scored, consensus'd, and executed independently.
        If any leg fails, rollback legs execute in reverse order to restore the user
        to an acceptable state.

        Args:
            order: The intent order.
            multi_leg_plan: Forward and rollback leg plans from the solver.
            contract_address: App contract address on the source chain.
        """
        from minotaur_subnet.shared.types import MultiLegPlan, LegPlan, ExecutionPlan

        completed_legs: list[LegPlan] = []
        # Best on-chain delivered-quality BPS seen across legs — recorded into
        # app stats at the end (post relative-cutover stats are BPS, not 0..1).
        # The per-leg ``score`` passed to consensus/relayer below is the JS-style
        # value and is intentionally left unchanged (out of this PR's scope).
        best_leg_oc: int | None = None

        for leg in multi_leg_plan.forward_legs:
            leg_label = f"leg {leg.leg_index} (chain {leg.chain_id})"
            logger.info("Multi-leg %s: processing %s", order.order_id, leg_label)

            self.orderbook.update_order(
                order.order_id,
                status=OrderStatus.EXECUTING_LEG,
                error=None,
            )
            self._sync(order.order_id)

            # Build an ExecutionPlan from this leg — through the canonical
            # builder: the plan-set signature hashes legs via the SAME
            # constructor, and any metadata drift breaks on-chain set
            # membership (consensus/plan_set.py invariant).
            from minotaur_subnet.consensus.plan_set import build_leg_execution_plan
            leg_plan = build_leg_execution_plan(
                order.app_id, int(order.deadline), leg,
            )

            # Check if this leg depends on a bridge (wait for bridge completion)
            if leg.depends_on:
                for dep_idx in leg.depends_on:
                    dep = next((l for l in completed_legs if l.leg_index == dep_idx), None)
                    if dep and dep.metadata.get("bridge_protocol"):
                        logger.info("Multi-leg %s: waiting for bridge (leg %d)", order.order_id, dep_idx)

                        # Get the source leg TX hash from the completed leg's metadata
                        src_tx_hash = dep.metadata.get("tx_hash", "")
                        bridge_protocol = dep.metadata.get("bridge_protocol", "hyperlane")
                        src_chain_id = dep.chain_id
                        dst_chain_id = leg.chain_id

                        # Store remaining legs and multi-leg plan in a tracking plan
                        remaining_legs = [l for l in multi_leg_plan.forward_legs if l.leg_index >= leg.leg_index]
                        tracking_plan = ExecutionPlan(
                            intent_id=order.app_id,
                            interactions=[],
                            deadline=int(order.deadline),
                            nonce=0,
                            metadata={
                                "src_chain_id": src_chain_id,
                                "dst_chain_id": dst_chain_id,
                                "bridge_protocol": bridge_protocol,
                                # Which leg the hop belongs to — reported as
                                # failed_leg_index if the bridge never delivers.
                                "bridge_leg_index": dep.leg_index,
                                "contract_address": contract_address,
                                "multi_leg_plan": multi_leg_plan.to_dict(),
                                "remaining_legs": [l.to_dict() for l in remaining_legs],
                                "rollback_legs": [l.to_dict() for l in multi_leg_plan.rollback_legs],
                                "completed_leg_indices": [l.leg_index for l in completed_legs],
                                "plan_set": (plan_metadata or {}).get("plan_set"),
                                "escrow_params": (plan_metadata or {}).get("escrow_params"),
                            },
                        )

                        # Register with bridge tracker
                        if self.bridge_tracker is not None and src_tx_hash:
                            self.bridge_tracker.track(
                                order_id=order.order_id,
                                src_tx_hash=src_tx_hash,
                                plan=tracking_plan,
                            )
                            logger.info("[MULTI-LEG] %s: registered with bridge tracker (tx=%s, %s->%s)", order.order_id, src_tx_hash[:16], src_chain_id, dst_chain_id)

                        self.orderbook.update_order(order.order_id, status=OrderStatus.BRIDGING)
                        self._sync(order.order_id)
                        return True

            # Simulate this leg
            logger.info("[MULTI-LEG] %s: simulating %s", order.order_id, leg_label)
            simulation = None

            # Detect bridge leg from platform-compiled simulation_mocks (preferred)
            # or legacy metadata+selector check (backward compat)
            _plan_sim_mocks = (plan_metadata or {}).get("simulation_mocks", {})
            _mock_cfg = _plan_sim_mocks.get(str(leg.leg_index)) or _plan_sim_mocks.get(leg.leg_index)
            is_bridge_leg = _mock_cfg is not None
            if not is_bridge_leg:
                # Legacy fallback: check metadata + selectors
                _has_bridge_metadata = leg.metadata.get("type") in ("bridge_source", "bridge") or leg.metadata.get("bridge_protocol")
                _has_bridge_selectors = any(
                    (ix.call_data or "")[2:10] in ("81b4e8b4",)
                    for ix in leg.interactions
                ) if leg.interactions else False
                is_bridge_leg = _has_bridge_metadata and (_has_bridge_selectors or not leg.interactions)

            if self.simulator is not None:
                try:
                    # Build simulation plan — mock bridge targets for bridge legs
                    sim_plan = leg_plan
                    if is_bridge_leg:
                        if _mock_cfg:
                            # Platform-compiled: use adapter mock config
                            from minotaur_subnet.shared.types import mock_bridge_interactions_from_config
                            mock_ixs = mock_bridge_interactions_from_config(leg_plan.interactions, _mock_cfg)
                        else:
                            # Legacy: use hardcoded selectors
                            from minotaur_subnet.shared.types import mock_bridge_interactions
                            input_token = order.params.get("input_token", "")
                            input_amount = int(order.params.get("input_amount", "0") or "0")
                            mock_ixs = mock_bridge_interactions(
                                leg_plan.interactions, token_address=input_token, amount=input_amount,
                            )
                        sim_plan = ExecutionPlan(
                            intent_id=leg_plan.intent_id,
                            interactions=mock_ixs,
                            deadline=leg_plan.deadline,
                            nonce=leg_plan.nonce,
                            metadata=leg_plan.metadata,
                        )
                        logger.debug("[MULTI-LEG] %s: %s using mock bridge for simulation", order.order_id, leg_label)

                    # Build intent_order for scoreIntent simulation
                    intent_order_dict = {
                        "order_id": order.order_id,
                        "app": contract_address,
                        "intent_selector": leg.intent_selector,
                        "intent_params": leg.intent_params_hex,
                        "submitted_by": order.submitted_by,
                        "chain_id": leg.chain_id,
                        "deadline": int(order.deadline),
                        "nonce": 0,
                        "perpetual": order.perpetual,
                        "max_executions": order.max_executions,
                        "cooldown": int(order.cooldown),
                    } if contract_address and leg.intent_params_hex else None

                    # Seed tokens for simulation
                    sim_token_balances = None
                    input_token = order.params.get("input_token", "")
                    input_amount = order.params.get("input_amount", "")
                    if input_token and input_amount and input_token.startswith("0x"):
                        try:
                            sim_token_balances = {input_token: int(input_amount)}
                        except (ValueError, TypeError):
                            pass

                    simulation = await self.simulator.simulate(
                        sim_plan,
                        contract_address=contract_address,
                        intent_order=intent_order_dict,
                        token_balances=sim_token_balances,
                    )
                    logger.info("[MULTI-LEG] %s: %s sim success=%s transfers=%s", order.order_id, leg_label, simulation.success, len(simulation.token_transfers or []))
                except Exception as exc:
                    logger.error("[MULTI-LEG] %s: %s sim exception: %s", order.order_id, leg_label, exc, exc_info=True)
                    logger.warning("Multi-leg %s: %s simulation failed: %s", order.order_id, leg_label, exc)

            if simulation is None:
                simulation = build_mock_simulation(leg_plan, order.params)
                logger.debug("[MULTI-LEG] %s: %s using mock simulation", order.order_id, leg_label)

            # Carry the best on-chain BPS for the stats recording at the end.
            _leg_oc = getattr(simulation, "on_chain_score", None)
            if _leg_oc is not None and (best_leg_oc is None or _leg_oc > best_leg_oc):
                best_leg_oc = _leg_oc

            # Score via JS engine
            app = self.app_store.get_app(order.app_id)
            state = IntentState(
                contract_address=contract_address or "",
                chain_id=leg.chain_id,
                nonce=0,
                owner=order.submitted_by,
                raw_params=order.params,
            )
            # Score the leg
            if is_bridge_leg and simulation and simulation.success:
                # Bridge legs: JS swap scoring doesn't apply (no output tokens).
                # The on-chain _bridge invariant already verified tokens left the proxy.
                # Use a passing score based on simulation success.
                score = 0.6
                logger.info("[MULTI-LEG] %s: %s bridge leg sim passed, score=%s", order.order_id, leg_label, score)
            else:
                score_result = await self.plan_scorer.score(order.app_id, app, leg_plan, simulation, state)
                score = score_result.score if score_result else 0.5
                logger.info("[MULTI-LEG] %s: %s score=%s", order.order_id, leg_label, score)

            # Consensus — include per-leg params so validators simulate the
            # correct intent function (bridge vs swap)
            consensus_result = None
            if self.consensus is not None:
                from minotaur_subnet.consensus.signatures import hash_plan
                from copy import copy
                plan_hash = hash_plan(leg_plan)

                # Create a modified order with per-leg params for the proposal
                leg_order = copy(order)
                leg_params = dict(order.params)
                leg_params["intent_selector"] = leg.intent_selector
                if leg.intent_params_hex:
                    leg_params["intent_params_hex"] = leg.intent_params_hex
                leg_params["_is_bridge_leg"] = is_bridge_leg
                leg_order.params = leg_params
                leg_order.intent_function = "bridge" if is_bridge_leg else order.intent_function

                if self.peer_network is not None:
                    broadcast_task = asyncio.create_task(
                        self.peer_network.broadcast_proposal(
                            order_id=order.order_id, plan=leg_plan,
                            score=score, plan_hash=plan_hash,
                            order=leg_order, app_id=order.app_id,
                            simulation=simulation,
                        )
                    )

                try:
                    consensus_result = await self.consensus.propose(
                        order.order_id, leg_plan, score, plan_hash,
                        chain_id=leg.chain_id,
                        contract_address=contract_address,
                    )
                except Exception as exc:
                    logger.warning("Multi-leg %s: %s consensus failed: %s", order.order_id, leg_label, exc)

                reached = consensus_result.reached if consensus_result else False
                collected = getattr(consensus_result, 'collected', 0) if consensus_result else 0
                quorum = getattr(consensus_result, 'quorum', 0) if consensus_result else 0
                logger.info("[MULTI-LEG] %s: %s consensus reached=%s collected=%s/%s", order.order_id, leg_label, reached, collected, quorum)

                if consensus_result and not consensus_result.reached:
                    logger.warning("[MULTI-LEG] %s: %s consensus failed, rolling back", order.order_id, leg_label)
                    await self._fail_leg(
                        order, completed_legs, multi_leg_plan.rollback_legs,
                        contract_address, plan_metadata, leg.leg_index,
                        f"Leg {leg.leg_index} consensus failed",
                    )
                    return False

            # Submit this leg via relayer — use per-leg params so the
            # on-chain order struct has the correct intent selector + params
            from copy import copy as _copy
            submit_order = _copy(order)
            submit_params = dict(order.params)
            submit_params["intent_selector"] = leg.intent_selector
            if leg.intent_params_hex:
                submit_params["intent_params_hex"] = leg.intent_params_hex
            # Plan-set signed path: when the user signed the plan set, the
            # relayer submits via executeLegSigned (membership-proved).
            from minotaur_subnet.consensus.plan_set import thread_plan_set_params
            thread_plan_set_params(
                submit_params,
                (plan_metadata or {}).get("plan_set"),
                order.params.get("plan_set_signature", ""),
                leg.leg_index,
            )
            submit_order.params = submit_params

            logger.info("[MULTI-LEG] %s: submitting %s to relayer (selector=%s)", order.order_id, leg_label, leg.intent_selector)
            try:
                submit_result = await self.relayer.submit_plan(
                    submit_order, leg_plan, score, consensus_result,
                    contract_address=contract_address,
                )
                logger.info("[MULTI-LEG] %s: %s relayer result: success=%s tx=%s err=%s", order.order_id, leg_label, submit_result.success, submit_result.tx_hash, submit_result.error)
            except Exception as exc:
                logger.error("[MULTI-LEG] %s: %s relayer EXCEPTION: %s", order.order_id, leg_label, exc, exc_info=True)
                await self._fail_leg(
                    order, completed_legs, multi_leg_plan.rollback_legs,
                    contract_address, plan_metadata, leg.leg_index,
                    f"Leg {leg.leg_index} relayer exception: {exc}",
                )
                return False

            if not submit_result.success:
                logger.warning(
                    "Multi-leg %s: %s submission failed: %s",
                    order.order_id, leg_label, submit_result.error,
                )
                await self._fail_leg(
                    order, completed_legs, multi_leg_plan.rollback_legs,
                    contract_address, plan_metadata, leg.leg_index,
                    f"Leg {leg.leg_index} submission failed: {submit_result.error}",
                )
                return False

            logger.info("Multi-leg %s: %s completed (tx=%s)", order.order_id, leg_label, submit_result.tx_hash)
            # Track tx hash per-leg for bridge tracker registration
            leg.metadata["tx_hash"] = submit_result.tx_hash or ""
            completed_legs.append(leg)

        # All forward legs completed
        self.orderbook.update_order(
            order.order_id,
            status=OrderStatus.FILLED,
            tx_hash=submit_result.tx_hash if completed_legs else None,
        )
        self._sync(order.order_id)
        # Record the on-chain delivered-quality BPS (0..10000), not a 0..1 score.
        # Multi-leg/bridge sims may not produce an on-chain score; fall back to 0.
        self.app_store.record_execution(
            order.app_id, best_leg_oc if best_leg_oc is not None else 0, success=True,
        )
        return True

    async def resume_after_plan_set_signature(self, order_id: str) -> dict:
        """Start orchestration for an order parked awaiting its plan-set sig.

        Called by POST /orders/{id}/plan-set-signature once the signature has
        been verified and attached. The compiled plan is read back from the
        stored order (order_processor persists it before parking), so this
        survives a restart between compile and signature — the user's wallet
        prompt can take as long as it takes.

        Raises ValueError on bad state; the caller maps it to an HTTP error.
        """
        from minotaur_subnet.shared.types import MultiLegPlan

        order = self.orderbook.get(order_id)
        if order is None:
            raise ValueError(f"Order not found: {order_id}")
        if order.status != OrderStatus.AWAITING_PLAN_SET_SIGNATURE:
            raise ValueError(
                f"Order {order_id} is not awaiting a plan-set signature "
                f"(status={getattr(order.status, 'value', order.status)})"
            )
        if not order.params.get("plan_set_signature"):
            raise ValueError(f"Order {order_id} has no plan-set signature attached")

        plan = order.plan or {}
        metadata = plan.get("metadata", {}) if isinstance(plan, dict) else {}
        multi_leg_dict = metadata.get("multi_leg_plan")
        if not multi_leg_dict:
            raise ValueError(
                f"Order {order_id} has no compiled multi-leg plan to resume"
            )
        multi_leg = MultiLegPlan.from_dict(multi_leg_dict)

        logger.info("Multi-leg %s: resuming after plan-set signature", order_id)
        ok = await self.process(
            order, multi_leg, metadata.get("contract_address", ""),
            plan_metadata=metadata,
        )
        final = self.orderbook.get(order_id)
        return {
            "resumed": True,
            "success": bool(ok),
            "status": getattr(final.status, "value", str(final.status)) if final else "unknown",
        }

    async def _fail_leg(
        self,
        order: Order,
        completed_legs: list,
        rollback_legs: list,
        contract_address: str,
        plan_metadata: dict | None,
        failed_leg_index: int,
        reason: str,
    ) -> None:
        """Route a leg failure: park for the user's revert-or-refresh
        decision when the feature is on, else the legacy auto-rollback."""
        from minotaur_subnet.shared.feature_flags import (
            cross_chain_user_decision_enabled,
        )
        if cross_chain_user_decision_enabled():
            await self.park_for_user_decision(
                order, completed_legs, rollback_legs, contract_address,
                plan_metadata, failed_leg_index, reason,
            )
        else:
            await self._execute_rollback(
                order, completed_legs, rollback_legs, contract_address,
                plan_metadata,
            )

    async def park_for_user_decision(
        self,
        order: Order,
        completed_legs: list,
        rollback_legs: list,
        contract_address: str,
        plan_metadata: dict | None,
        failed_leg_index: int,
        reason: str,
        options: list[str] | None = None,
        expiry_status: Any = None,
    ) -> None:
        """Park a failed multi-leg order in AWAITING_USER_DECISION.

        The user's funds are in a safe state at every parkable failure point
        (origin refund received, or asset held in on-chain escrow with a
        permissionless timelocked refund), so waiting is safe. The user
        chooses via POST /orders/{id}/decision:
          - revert  → execute the pre-agreed revert plan (the same rollback
                      legs the plan-set signature covers)
          - refresh → this order's orchestration ends (REFRESHING); the
                      failed leg re-quotes as a NEW single-chain order over
                      the escrowed asset
        No decision by the deadline → auto-revert (never leave funds waiting
        on an absent user past the escrow window). If this process restarts
        mid-window the watcher dies: the decision endpoint still resolves,
        and the on-chain escrowRefund timelock remains the unconditional
        backstop.

        ``options`` narrows the offer when a choice isn't actually available.
        A failed bridge HOP passes ["refresh"]: nothing reached the
        destination, so the revert legs (reverse-bridges that run there)
        have no funds to move and offering "revert" would be a lie. With no
        revert available, expiry falls through to ``expiry_status`` (a
        terminal state) instead of auto-reverting.
        """
        import time as _time

        from minotaur_subnet.shared.feature_flags import (
            cross_chain_decision_window_s,
        )

        options = list(options) if options else ["revert", "refresh"]
        deadline = int(_time.time()) + cross_chain_decision_window_s()
        # Everything the resolver needs must survive in order.params — the
        # in-memory leg objects don't outlive a restart.
        order.params["recovery"] = {
            "failed_leg_index": failed_leg_index,
            "reason": reason,
            "options": options,
            "expiry_status": (
                getattr(expiry_status, "value", expiry_status)
                if expiry_status is not None else None
            ),
            "decision_deadline": deadline,
            "completed_legs": [l.to_dict() for l in completed_legs],
            "rollback_legs": [l.to_dict() for l in rollback_legs],
            "contract_address": contract_address,
            "plan_set": (plan_metadata or {}).get("plan_set"),
            "escrow_params": (plan_metadata or {}).get("escrow_params"),
            "resolved": None,
        }
        self.orderbook.update_order(
            order.order_id,
            status=OrderStatus.AWAITING_USER_DECISION,
            params=order.params,
            error=reason,
        )
        self._sync(order.order_id)
        logger.info(
            "Multi-leg %s: parked for user decision (leg %d, deadline %d): %s",
            order.order_id, failed_leg_index, deadline, reason,
        )
        asyncio.create_task(self._decision_timeout_watch(order.order_id, deadline))

    async def _decision_timeout_watch(self, order_id: str, deadline: int) -> None:
        """Auto-revert when the decision window expires undecided."""
        import time as _time

        while True:
            await asyncio.sleep(10)
            order = self.orderbook.get(order_id)
            if order is None:
                return
            recovery = order.params.get("recovery") or {}
            if recovery.get("resolved"):
                return  # endpoint already resolved it
            if order.status != OrderStatus.AWAITING_USER_DECISION:
                return
            if _time.time() >= deadline:
                options = recovery.get("options") or ["revert", "refresh"]
                if "revert" not in options:
                    # Nothing to revert (failed bridge hop — the origin
                    # refund is already the resolution). Settle into the
                    # terminal state rather than pretending a revert ran.
                    expiry = recovery.get("expiry_status") or OrderStatus.BRIDGE_FAILED
                    logger.info(
                        "Multi-leg %s: decision window expired with no revert "
                        "option — settling as %s", order_id, expiry,
                    )
                    self.orderbook.update_order(order_id, status=expiry)
                    self._sync(order_id)
                    return
                logger.info(
                    "Multi-leg %s: decision window expired — auto-revert",
                    order_id,
                )
                try:
                    await self.resolve_user_decision(order_id, "revert")
                except Exception as exc:
                    logger.error(
                        "Multi-leg %s: timeout auto-revert failed: %s",
                        order_id, exc,
                    )
                return

    async def resolve_user_decision(self, order_id: str, action: str) -> dict:
        """Execute the user's (or the timeout's) revert-or-refresh choice.

        Returns a result dict for the API. Raises ValueError on bad
        action/state — the caller maps it to an HTTP error.
        """
        from minotaur_subnet.shared.types import LegPlan

        if action not in ("revert", "refresh"):
            raise ValueError(f"Unknown recovery action: {action}")
        order = self.orderbook.get(order_id)
        if order is None:
            raise ValueError(f"Order not found: {order_id}")
        if order.status != OrderStatus.AWAITING_USER_DECISION:
            raise ValueError(
                f"Order {order_id} is not awaiting a decision "
                f"(status={getattr(order.status, 'value', order.status)})"
            )
        recovery = order.params.get("recovery") or {}
        if recovery.get("resolved"):
            raise ValueError(f"Order {order_id} decision already resolved")
        offered = recovery.get("options") or ["revert", "refresh"]
        if action not in offered:
            raise ValueError(
                f"Action '{action}' is not available for order {order_id} "
                f"(offered: {', '.join(offered)})"
            )

        recovery["resolved"] = action
        order.params["recovery"] = recovery
        self.orderbook.update_order(order_id, params=order.params)
        self._sync(order_id)

        if action == "refresh":
            # This order's orchestration ends here. The client submits a NEW
            # single-chain order over the escrowed asset (an ordinary atomic
            # intent — no solver-side concept of "refresh" needed). Escrow
            # stays claimable via the timelocked escrowRefund if the refresh
            # never completes.
            self.orderbook.update_order(order_id, status=OrderStatus.REFRESHING)
            self._sync(order_id)
            return {
                "action": "refresh",
                "escrow_params": recovery.get("escrow_params"),
                "note": (
                    "Submit a new single-chain order over the escrowed asset "
                    "to re-quote the failed leg; escrowRefund remains "
                    "available after the escrow deadline."
                ),
            }

        # revert: run the pre-agreed rollback legs (plan-set-covered)
        completed = [LegPlan.from_dict(d) for d in recovery.get("completed_legs", [])]
        rollbacks = [LegPlan.from_dict(d) for d in recovery.get("rollback_legs", [])]
        await self._execute_rollback(
            order, completed, rollbacks,
            recovery.get("contract_address", ""),
            {"plan_set": recovery.get("plan_set")},
        )
        final = self.orderbook.get(order_id)
        return {
            "action": "revert",
            "status": getattr(final.status, "value", str(final.status)) if final else "unknown",
        }

    async def _execute_rollback(
        self,
        order: Order,
        completed_legs: list,
        rollback_legs: list,
        contract_address: str,
        plan_metadata: dict | None = None,
    ) -> None:
        """Execute rollback legs in reverse order for completed forward legs.

        Each rollback leg is simulated, consensus'd, and executed independently.
        If a rollback leg itself fails, the order is marked PARTIAL_ROLLBACK.
        """
        from minotaur_subnet.shared.types import ExecutionPlan

        logger.info("Multi-leg %s: triggering rollback for %d completed legs",
                     order.order_id, len(completed_legs))

        self.orderbook.update_order(order.order_id, status=OrderStatus.ROLLING_BACK)
        self._sync(order.order_id)

        rollback_failures = []
        for completed_leg in reversed(completed_legs):
            rollback = next(
                (r for r in rollback_legs if r.rollback_for == completed_leg.leg_index),
                None,
            )
            if rollback is None:
                logger.warning("No rollback leg for forward leg %d", completed_leg.leg_index)
                continue

            from minotaur_subnet.consensus.plan_set import (
                build_leg_execution_plan,
                thread_plan_set_params,
            )
            rollback_plan = build_leg_execution_plan(
                order.app_id, int(order.deadline), rollback, is_rollback=True,
            )

            # Rollback legs are members of the user-signed plan set too —
            # the agreed revert plan executes under the same signature.
            from copy import copy as _copy
            submit_order = _copy(order)
            submit_params = dict(order.params)
            thread_plan_set_params(
                submit_params,
                (plan_metadata or {}).get("plan_set"),
                order.params.get("plan_set_signature", ""),
                rollback.leg_index,
            )
            submit_order.params = submit_params

            try:
                result = await self.relayer.submit_plan(
                    submit_order, rollback_plan, 0.5, None,
                    contract_address=contract_address,
                )
                if result.success:
                    logger.info("Rollback leg %d succeeded", rollback.leg_index)
                else:
                    logger.warning("Rollback leg %d failed: %s", rollback.leg_index, result.error)
                    rollback_failures.append(rollback.leg_index)
            except Exception as exc:
                logger.error("Rollback leg %d exception: %s", rollback.leg_index, exc)
                rollback_failures.append(rollback.leg_index)

        if rollback_failures:
            self.orderbook.update_order(
                order.order_id,
                status=OrderStatus.PARTIAL_ROLLBACK,
                error=f"Rollback failed for legs: {rollback_failures}",
            )
        else:
            self.orderbook.update_order(
                order.order_id,
                status=OrderStatus.ROLLED_BACK,
            )
        self._sync(order.order_id)

    def _sync(self, order_id: str) -> None:
        """Sync order to store via persistence layer."""
        if self.order_persistence is not None:
            self.order_persistence.sync(order_id)
