"""Order processing pipeline for the block loop.

Orchestrates the single-order pipeline: plan generation -> simulation ->
JS scoring -> policy enforcement -> consensus voting -> wallet signing ->
relayer submission -> order persistence.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any

from minotaur_subnet.shared.types import (
    AppIntentDefinition,
    ExecutionPlan,
    IntentState,
    PolicyTier,
)
from minotaur_subnet.orderbook.orderbook import Order, OrderStatus
from minotaur_subnet.blockloop.utils import (
    _json_safe,
    _plan_to_dict,
    _resolve_effective_policy_tier,
    _plan_assessment_to_dict,
)
from minotaur_subnet.shared.simulation import onchain_score_fail_closed
from minotaur_subnet.blockloop.plan_generation import PlanGenerator
from minotaur_subnet.blockloop.simulation import SimulationRunner
from minotaur_subnet.blockloop.scoring import PlanScorer
from minotaur_subnet.blockloop.persistence import OrderPersistence
from minotaur_subnet.blockloop.cross_chain import CrossChainOrchestrator
from minotaur_subnet.blockloop.multi_leg import MultiLegOrchestrator
from minotaur_subnet.v3.classifier import assess_execution_plan
from minotaur_subnet.v3.contexts import build_typed_context
from minotaur_subnet.v3.flags import load_v3_flags

logger = logging.getLogger(__name__)


def _int_env(name: str, default: int) -> int:
    """Env int with fallback — malformed values must not crash import
    (same posture as ``relayer.safeguards.Safeguards.from_env``)."""
    try:
        return int(os.environ.get(name, "").strip() or default)
    except ValueError:
        logger.warning("%s=%r is not an integer; using %d", name, os.environ.get(name), default)
        return default


# USER-fault settlement-revert attribution (#229) lives in one place —
# ``orderbook.rejection`` — so the accounting the block loop applies here and the
# ``rejection_class`` the API exposes can never drift apart. Re-exported under the
# module-private names this file (and its tests) have always used.
from minotaur_subnet.orderbook.rejection import (  # noqa: E402
    USER_FUNDS_FAULT_MARKERS as _USER_FUNDS_FAULT_MARKERS,
    USER_SIG_FAULT_MARKERS as _USER_SIG_FAULT_MARKERS,
    is_user_fault as _is_user_fault,
    is_user_fund_fault as _is_user_fund_fault,
    is_user_signature_fault as _is_user_signature_fault,
)


class OrderProcessor:
    """Processes a single order through the full execution pipeline.

    Uses injected dependencies for each pipeline step.

    Args:
        plan_generator: PlanGenerator for generating execution plans.
        simulation_runner: SimulationRunner for Anvil fork simulation.
        plan_scorer: PlanScorer for JS scoring.
        order_persistence: OrderPersistence for syncing order state.
        cross_chain_orchestrator: CrossChainOrchestrator for cross-chain orders.
        multi_leg_orchestrator: MultiLegOrchestrator for multi-leg orders.
        orderbook: The Intent OrderBook.
        app_store: Persistent store for app definitions and stats.
        relayer: Relayer for submitting approved plans.
        consensus: ConsensusManager (optional).
        peer_network: PeerNetwork for multi-validator broadcast (optional).
        wallet_manager: WalletManager for auto-signing (optional).
        cross_chain_compiler: CrossChainCompiler (optional).
        js_engine: JS scoring engine (optional).
        score_threshold: Minimum JS score for approval.
        fee_enabled: Whether fee checking is enabled.
        default_fee_wei: Default fee in wTAO wei.
        v3_flags: V3 feature flags.
    """

    def __init__(
        self,
        plan_generator: PlanGenerator,
        simulation_runner: SimulationRunner,
        plan_scorer: PlanScorer,
        order_persistence: OrderPersistence,
        cross_chain_orchestrator: CrossChainOrchestrator,
        multi_leg_orchestrator: MultiLegOrchestrator,
        orderbook: Any,
        app_store: Any,
        relayer: Any,
        consensus: Any = None,
        peer_network: Any = None,
        wallet_manager: Any = None,
        cross_chain_compiler: Any = None,
        js_engine: Any = None,
        score_threshold: float = 0.5,
        fee_enabled: bool = False,
        default_fee_wei: int = 10000000000000000,
        v3_flags: Any = None,
        simulator: Any = None,
    ) -> None:
        self.plan_generator = plan_generator
        self.simulation_runner = simulation_runner
        self.plan_scorer = plan_scorer
        self.order_persistence = order_persistence
        self.cross_chain_orchestrator = cross_chain_orchestrator
        self.multi_leg_orchestrator = multi_leg_orchestrator
        self.orderbook = orderbook
        self.app_store = app_store
        self.relayer = relayer
        self.consensus = consensus
        self.peer_network = peer_network
        self.wallet_manager = wallet_manager
        self.cross_chain_compiler = cross_chain_compiler
        self.js_engine = js_engine
        self.score_threshold = score_threshold
        self.fee_enabled = fee_enabled
        self.default_fee_wei = default_fee_wei
        self.v3_flags = v3_flags or load_v3_flags()
        self.simulator = simulator
        # Consensus retry bookkeeping: order_id -> (attempts, last_attempt_ts).
        # A consensus round can fail transiently (peer set flapped empty,
        # follower slow) — while the order deadline has time left we requeue
        # the order OPEN for the next tick instead of terminally rejecting
        # (live incident 2026-07-16: a fresh, valid ETH order died
        # "Consensus not reached" because the proposal went out one second
        # after a spurious probe-timeout round zeroed the peer set).
        self._consensus_retries: dict[str, tuple[int, float]] = {}

    # How many consensus attempts an order gets before the failure is
    # terminal, how much order-deadline headroom a retry requires, and the
    # minimum spacing between counted attempts. Spacing matters: the block
    # loop re-picks a requeued order every ~12s tick, but the peer set can
    # only recover on a discovery refresh (60s cadence — and the refresh
    # loop sleeps a full interval before its FIRST pass after boot).
    # Without spacing, all attempts burn in ~36s and the retry mechanism
    # mathematically cannot outlast the very condition it waits out.
    # Requeues inside the spacing window don't consume an attempt.
    _CONSENSUS_RETRY_MAX = _int_env("ORDER_CONSENSUS_RETRY_MAX", 3)
    _CONSENSUS_RETRY_SPACING_S = float(_int_env("ORDER_CONSENSUS_RETRY_SPACING_S", 45))
    _CONSENSUS_RETRY_MIN_DEADLINE_S = 60.0

    def _defer_or_reject_consensus(
        self, order: Order, reason: str, consensus_result: Any = None,
    ) -> None:
        """Requeue the order OPEN for another consensus attempt, or
        terminally reject once attempts/deadline run out.

        The requeued order re-runs the whole pipeline next tick (fresh
        plan, fresh simulation) — same pattern as the perpetual no-plan
        requeue below. ``deadline<=0`` means "no deadline" (see
        ``Order.deadline``) and counts as unlimited headroom.
        """
        # Opportunistic prune so abandoned entries (order expired outside
        # this path) don't accumulate.
        now = time.time()
        if len(self._consensus_retries) > 256:
            self._consensus_retries = {
                k: v for k, v in self._consensus_retries.items()
                if now - v[1] < 7200
            }
        attempts, last_ts = self._consensus_retries.get(order.order_id, (0, 0.0))
        deadline = float(order.deadline or 0)
        deadline_left = (deadline - now) if deadline > 0 else float("inf")

        # Inside the spacing window: requeue without consuming an attempt,
        # so counted attempts stay >= spacing apart and the retry budget
        # spans at least one full discovery-refresh interval.
        if (
            attempts > 0
            and (now - last_ts) < self._CONSENSUS_RETRY_SPACING_S
            and deadline_left > self._CONSENSUS_RETRY_MIN_DEADLINE_S
        ):
            logger.info(
                "Order %s: %s — inside retry spacing window "
                "(%.0fs/%.0fs since attempt %d), requeued OPEN",
                order.order_id, reason, now - last_ts,
                self._CONSENSUS_RETRY_SPACING_S, attempts,
            )
            self.orderbook.update_order(
                order.order_id,
                status=OrderStatus.OPEN,
                error=f"{reason} (retry {attempts}/{self._CONSENSUS_RETRY_MAX} pending)",
            )
            self.order_persistence.sync(order.order_id)
            return

        if attempts < self._CONSENSUS_RETRY_MAX and deadline_left > self._CONSENSUS_RETRY_MIN_DEADLINE_S:
            self._consensus_retries[order.order_id] = (attempts + 1, now)
            logger.warning(
                "Order %s: %s (attempt %d/%d, %s of deadline left) — "
                "requeued OPEN for retry",
                order.order_id, reason, attempts + 1,
                self._CONSENSUS_RETRY_MAX,
                "unlimited" if deadline_left == float("inf") else f"{deadline_left:.0f}s",
            )
            self.orderbook.update_order(
                order.order_id,
                status=OrderStatus.OPEN,
                error=f"{reason} (retry {attempts + 1}/{self._CONSENSUS_RETRY_MAX} pending)",
            )
            self.order_persistence.sync(order.order_id)
            return
        self._consensus_retries.pop(order.order_id, None)
        update_kwargs: dict[str, Any] = {}
        if consensus_result is not None:
            update_kwargs["consensus_result"] = _json_safe(consensus_result)
        self.orderbook.update_order(
            order.order_id,
            status=OrderStatus.REJECTED,
            error=f"{reason} — terminal after {attempts + 1} attempt(s)",
            **update_kwargs,
        )
        self.order_persistence.sync(order.order_id)

    async def process(self, order: Order) -> bool:
        """Process a single order through the full pipeline.

        Returns True if the order was approved and submitted.
        """
        # Look up the app definition
        app = self.app_store.get_app(order.app_id)
        if app is None:
            self.orderbook.update_order(
                order.order_id,
                status=OrderStatus.REJECTED,
                error=f"App not found: {order.app_id}",
            )
            self.order_persistence.sync(order.order_id)
            return False

        # Fee check (skeleton): assign default fee and validate
        if self.fee_enabled and order.fee_amount_wei == 0:
            order.fee_amount_wei = self.default_fee_wei

        # SE-11: Attach manifest to app definition if available
        if app.manifest is None and self.js_engine is not None:
            try:
                manifest = self.js_engine.get_manifest(order.app_id)
                if manifest:
                    app.manifest = manifest
            except Exception:
                pass  # Non-critical: solver can still work without manifest

        # Build market snapshot (synthetic for now)
        snapshot = self._build_snapshot(order, app)

        # Get contract address from deployment (needed by solver for recipient)
        deployment = self.app_store.get_deployment(
            order.app_id, chain_id=getattr(order, "chain_id", None),
        )
        deployed_contract = ""
        if deployment and deployment.contract_address:
            deployed_contract = deployment.contract_address

        # Perpetual pre-flight funds gate. The scoring fork FABRICATES the user's
        # balance, so without this a perpetual the user can no longer fund would
        # pass scoring + quorum + relay every cooldown cycle and only revert at
        # settlement — burning a whole consensus round each time. A cheap LIVE
        # balance+allowance read lets it terminate immediately (or, if the order
        # carries an EIP-2612 permit, cure the allowance gaslessly first).
        if order.perpetual and deployed_contract:
            if not await self._perpetual_funds_check(order, deployed_contract):
                return False

        # Build intent state from order params
        state = IntentState(
            contract_address=deployed_contract,
            chain_id=order.chain_id,
            nonce=order.params.get("user_nonce", 0),
            owner=order.submitted_by,
            raw_params=order.params,
            control={"_intent_function": order.intent_function},
        )
        state.policy_tier = _resolve_effective_policy_tier(order, app)
        if self.v3_flags.typed_contexts_enabled:
            state.typed_context = build_typed_context(app, order.intent_function, state)
            state.context_version = "v3"

        # Generate plan. None = the solver produced no plan: a deliberate no-route,
        # a crash, a >30s timeout, or any unhandled exception (all collapsed to None
        # by PlanGenerator). Don't leave the order silently stranded in ASSIGNED
        # until it EXPIREs with no signal (#225) and — for perpetuals — don't forfeit
        # all remaining executions on one unsolvable cycle (#226).
        plan = await self.plan_generator.generate(app, state, snapshot)
        if plan is None:
            self._handle_no_plan(order)
            return False

        # Record the locked (user-signed) platform fee in plan metadata so it
        # travels in the consensus proposal. The real loss-protection check —
        # fee covers measured gas and lies within the on-chain clamp — runs
        # after simulation below via fee_policy.certify_fee (the relayer cannot
        # refuse a quorum-approved order, so the gate must be upstream).
        fee_in_params = int(order.params.get("platform_fee_wei", 0))
        if fee_in_params > 0:
            plan.metadata["platform_fee_wei"] = fee_in_params

        if (
            self.v3_flags.policy_assessment_enabled
            or self.v3_flags.policy_enforcement_enabled
        ):
            assessment = assess_execution_plan(plan, state.policy_tier)
            self.orderbook.update_order(
                order.order_id,
                plan_assessment=_plan_assessment_to_dict(assessment),
            )
            self.order_persistence.sync(order.order_id)
            if self.v3_flags.policy_enforcement_enabled and not assessment.accepted:
                self.orderbook.update_order(
                    order.order_id,
                    status=OrderStatus.REJECTED,
                    error=(
                        "Policy rejected plan: "
                        f"{assessment.rejection_reason or 'assessment failed'}"
                    ),
                )
                self.order_persistence.sync(order.order_id)
                self.app_store.record_execution(order.app_id, 0.0, success=False)
                return False

        self.orderbook.update_order(
            order.order_id,
            status=OrderStatus.SOLVED,
            plan=_plan_to_dict(plan),
        )
        self.order_persistence.sync(order.order_id)

        # Cross-chain plan compilation: solver provides CrossChainPlan,
        # platform compiles it into MultiLegPlan with bridge calldata + escrow.
        cross_chain_plan_dict = plan.metadata.get("cross_chain_plan")
        if cross_chain_plan_dict and self.cross_chain_compiler is not None:
            try:
                from minotaur_subnet.shared.types import CrossChainPlan
                solver_plan = CrossChainPlan.from_dict(cross_chain_plan_dict)
                compiled = await self.cross_chain_compiler.compile(
                    solver_plan,
                    order_id=order.order_id,
                    user_address=order.submitted_by,
                    contract_address=deployed_contract or "",
                    deadline=int(order.deadline),
                )
                # Replace plan metadata with platform-compiled version
                plan.metadata["multi_leg_plan"] = compiled.multi_leg_plan.to_dict()
                plan.metadata["cross_chain"] = True
                plan.metadata["escrow_params"] = compiled.escrow_params
                plan.metadata["simulation_mocks"] = compiled.simulation_mocks
                plan.metadata["_platform_compiled"] = True
                # Remove solver's raw plan (prevent bypass)
                plan.metadata.pop("cross_chain_plan", None)
                logger.info(
                    "Cross-chain compiled for %s: %d legs, %d bridges",
                    order.order_id,
                    len(compiled.multi_leg_plan.forward_legs),
                    len(compiled.bridge_quotes),
                )
            except Exception as exc:
                logger.warning("Cross-chain compilation failed for %s: %s", order.order_id, exc)
                self.orderbook.update_order(
                    order.order_id, status=OrderStatus.REJECTED, error=str(exc),
                )
                self.order_persistence.sync(order.order_id)
                return False

        # Multi-leg intents: skip normal simulation, use per-leg orchestrator
        multi_leg_dict = plan.metadata.get("multi_leg_plan")
        if multi_leg_dict:
            from minotaur_subnet.shared.types import MultiLegPlan
            multi_leg = MultiLegPlan.from_dict(multi_leg_dict)
            if multi_leg.is_multi_leg():
                logger.info(
                    "Multi-leg order %s: %d forward legs, %d rollback legs",
                    order.order_id, len(multi_leg.forward_legs), len(multi_leg.rollback_legs),
                )
                return await self.multi_leg_orchestrator.process(
                    order, multi_leg, deployed_contract,
                    plan_metadata=plan.metadata,
                )

        # Simulate (real if simulator is set, else mock)
        # Pass contract address for on-chain scoring (SIM-6, SCR-4)
        contract_address = deployed_contract or None
        intent_order_dict = None
        if contract_address:
            # Resolve 4-byte intent selector: prefer explicit hex, else
            # look it up from the plan metadata or the order's intent_function
            _raw_sel = order.params.get("intent_selector") or plan.metadata.get("intent_selector") or ""
            if not _raw_sel or not all(c in '0123456789abcdefABCDEF' for c in _raw_sel.replace("0x", "")):
                # Compute from intent function name using the contract's
                # registered selector convention (keccak of canonical sig).
                # For DexAggregatorApp: swap(address,address,uint256,uint256,address)
                from eth_hash.auto import keccak as _keccak
                _fn = order.intent_function or "swap"
                _KNOWN_SIGS = {
                    "swap": "swap(address,address,uint256,uint256,address)",
                    "execute": "swap(address,address,uint256,uint256,address)",
                    "buy": "buy(address,address,uint256,uint256,address)",
                    "rebalance": "rebalance(address[],uint256[],address)",
                }
                _sig = _KNOWN_SIGS.get(_fn, f"{_fn}()")
                _raw_sel = _keccak(_sig.encode())[:4].hex()
            intent_order_dict = {
                "order_id": order.order_id,
                "app": contract_address,
                "intent_selector": _raw_sel,
                "intent_params": order.params.get("intent_params_hex", ""),
                "submitted_by": order.submitted_by,
                "chain_id": order.chain_id,
                "deadline": int(order.deadline),
                "nonce": order.params.get("user_nonce", 0),
                "perpetual": order.perpetual,
                "max_executions": order.max_executions,
                "cooldown": int(order.cooldown),
                # Carry the native-input flag so the simulator sends
                # msg.value in the scoreIntent call (the contract needs
                # msg.value > 0 to trigger the wrap path).
                "_input_token_is_native": bool(order.params.get("_input_token_is_native")),
                "_input_amount": order.params.get("input_amount", "0"),
            }

        is_cross_chain = plan.metadata.get("cross_chain", False)

        # Defense-in-depth: the API already rejects cross-chain orders when
        # CROSS_CHAIN_ENABLED=0, but a solver could still return a plan
        # marked cross-chain. Refuse to simulate it in that case.
        if is_cross_chain:
            from minotaur_subnet.shared.feature_flags import (
                cross_chain_enabled,
                CROSS_CHAIN_DISABLED_MESSAGE,
            )
            if not cross_chain_enabled():
                self.orderbook.update_order(
                    order.order_id,
                    status=OrderStatus.REJECTED,
                    error=CROSS_CHAIN_DISABLED_MESSAGE,
                )
                self.order_persistence.sync(order.order_id)
                return False

        simulation = await self.simulation_runner.simulate(
            plan, order, contract_address, intent_order_dict,
            is_cross_chain, deployed_contract,
        )

        # Protocol-fee certification — the never-lose-money gate, upstream of
        # the relayer. Re-check the locked, user-signed fee against the gas we
        # just measured: it must cover that gas and lie within the on-chain
        # [min, max] clamp. Runs per processing pass, so one-shot orders are
        # certified once and perpetual orders are re-certified every tick from
        # that tick's own simulation — no perpetual-specific fee code.
        if fee_in_params > 0:
            from minotaur_subnet import fee_policy
            _gas_price = fee_policy.current_gas_price_wei(order.chain_id)
            _ok, _reason = fee_policy.certify_fee(
                order.chain_id, fee_in_params,
                getattr(simulation, "gas_used", 0) or 0, _gas_price,
            )
            if not _ok:
                logger.warning(
                    "[LOOP] Fee certification failed for order %s: %s",
                    order.order_id, _reason,
                )
                self.orderbook.update_order(
                    order.order_id,
                    status=OrderStatus.REJECTED,
                    error=f"Fee certification failed: {_reason}",
                )
                self.order_persistence.sync(order.order_id)
                self.app_store.record_execution(order.app_id, 0.0, success=False)
                return False

        # Score via JS engine. Post relative-cutover the JS ``score`` is a 0/1
        # VALIDITY sentinel (1 valid / 0 invalid), NOT a quality grade. It is
        # kept for the orderbook telemetry field and the consensus/relayer
        # proposal (both unchanged), but it is NO LONGER a quality gate.
        score_result = await self.plan_scorer.score(
            order.app_id, app, plan, simulation, state,
        )
        score = score_result.score if score_result else 0.0

        # On-chain scoreIntent BPS (0..10000) is the real delivered-quality
        # signal and the authoritative accept/reject gate below. ``stat_bps`` is
        # what we record into app stats / order best_score (delivered quality),
        # NOT the saturated JS sentinel. A None on-chain score (contract didn't
        # bless the plan) records 0 BPS.
        on_chain_threshold = app.config.on_chain_threshold  # default 5000 BPS
        oc_score = simulation.on_chain_score
        stat_bps = oc_score if oc_score is not None else 0

        # Track best on-chain score for this order, even if it later rejects.
        current_best = order.best_score or 0
        new_best = max(current_best, stat_bps)

        self.orderbook.update_order(
            order.order_id,
            status=OrderStatus.SCORED,
            score=score,
            best_score=new_best,
            on_chain_score=oc_score,
        )
        self.order_persistence.sync(order.order_id)

        # JS quality-threshold gate RETIRED (relative-cutover): with the JS score
        # now a validity sentinel, a 0..1 ``score_threshold`` comparison is dead
        # (it would pass every valid plan). The on-chain scoreIntent gate below is
        # the SOLE authoritative accept/reject. ``app.config.score_threshold`` is
        # left inert. (self.score_threshold kept on the ctor for back-compat.)

        # On-chain score gate — authoritative accept/reject (SCR-5, SCR-6, VAL-10).
        # Fail-closed (DEFAULT ON fleet-wide): a deployed contract must yield a
        # passing on-chain score. None means scoreIntent returned valid=False (plan
        # breaks an on-chain invariant) or was unreadable — the contract did NOT
        # bless the plan, so don't relay it. Leader + follower share
        # onchain_score_fail_closed() so they gate identically (break-glass:
        # ONCHAIN_SCORE_FAIL_CLOSED in {0,false,no,off} to fail-open fleet-wide).
        if contract_address and oc_score is None and onchain_score_fail_closed():
            if self._try_requeue_perpetual(
                order, "on-chain score unavailable this cycle (fail-closed)",
            ):
                return False
            self.orderbook.update_order(
                order.order_id,
                status=OrderStatus.REJECTED,
                error="On-chain score unavailable — contract returned invalid or unreadable (dual-scoring fail-closed)",
            )
            self.order_persistence.sync(order.order_id)
            self.app_store.record_execution(order.app_id, stat_bps, success=False)
            return False
        if oc_score is not None and oc_score < on_chain_threshold:
            if self._try_requeue_perpetual(
                order,
                f"on-chain score {oc_score} BPS < threshold {on_chain_threshold}",
            ):
                return False
            self.orderbook.update_order(
                order.order_id,
                status=OrderStatus.REJECTED,
                error=(
                    f"On-chain score {oc_score} BPS "
                    f"< threshold {on_chain_threshold}"
                ),
            )
            self.order_persistence.sync(order.order_id)
            self.app_store.record_execution(order.app_id, stat_bps, success=False)
            return False

        # Phase 2: Consensus (with optional peer broadcast)
        consensus_result = None
        if self.consensus is not None:
            from minotaur_subnet.consensus.signatures import hash_plan
            plan_hash = hash_plan(plan)

            # Fast-path defer: with quorum > 1 and ZERO reachable peers the
            # round is mathematically unwinnable — don't broadcast to nobody
            # and then burn the full consensus timeout (that's what killed
            # ord_710c9140 on 2026-07-16). Requeue and let the next tick try
            # with a (hopefully recovered) peer set.
            if (
                self.peer_network is not None
                and int(getattr(self.consensus, "quorum_required", 1) or 1) > 1
                and not self.peer_network.peers
            ):
                self._defer_or_reject_consensus(
                    order, "No reachable validator peers for consensus",
                )
                return False

            # Start peer broadcast concurrently (leader broadcasts to followers)
            broadcast_task = None
            if self.peer_network is not None:
                broadcast_task = asyncio.create_task(
                    self.peer_network.broadcast_proposal(
                        order_id=order.order_id,
                        plan=plan,
                        score=score,
                        plan_hash=plan_hash,
                        order=order,
                        app_id=order.app_id,
                        simulation=simulation,
                    )
                )

            try:
                consensus_result = await self.consensus.propose(
                    order.order_id,
                    plan,
                    score,
                    plan_hash,
                    chain_id=order.chain_id,
                    contract_address=deployed_contract or "",
                )
            except TypeError:
                consensus_result = await self.consensus.propose(
                    order.order_id, plan, score, plan_hash,
                )

            # Cancel broadcast if still running after consensus completes
            if broadcast_task is not None and not broadcast_task.done():
                broadcast_task.cancel()
                try:
                    await broadcast_task
                except asyncio.CancelledError:
                    pass

            if not consensus_result.reached:
                self._defer_or_reject_consensus(
                    order, "Consensus not reached",
                    consensus_result=consensus_result,
                )
                return False

        self._consensus_retries.pop(order.order_id, None)
        self.orderbook.update_order(
            order.order_id,
            status=OrderStatus.APPROVED,
            # Clear any "retry N/M pending" note a prior consensus failure
            # left — a filled order must not carry a stale error forever.
            error=None,
            consensus_result=_json_safe(consensus_result) if consensus_result is not None else None,
        )
        self.order_persistence.sync(order.order_id)

        # Auto-sign for managed wallets that don't have a user signature
        if not order.user_signature and self.wallet_manager is not None:
            try:
                sig = await self._auto_sign_order(order, deployed_contract)
                if sig:
                    order.user_signature = sig
                    self.orderbook.update_order(
                        order.order_id, user_signature=sig,
                    )
            except Exception as exc:
                logger.warning("Auto-sign failed for %s: %s", order.order_id, exc)

        # User-direct-submit path: stop at APPROVED. The frontend will poll
        # /orders/{id}/prepare-direct, build its own TX with msg.value, and
        # submit executeIntent directly from the user's wallet. The relayer
        # can't do this for native ETH/TAO input — only the account holder
        # can attach msg.value.
        if order.params.get("_user_submit"):
            logger.info(
                "Order %s marked for user-direct-submit, stopping at APPROVED",
                order.order_id,
            )
            return True

        # Legacy cross-chain path (substrate-origin, etc.)
        if is_cross_chain:
            return await self.cross_chain_orchestrator.submit(
                order, plan, score, consensus_result, deployed_contract,
            )

        submit_result = await self.relayer.submit_plan(
            order, plan, score, consensus_result,
            contract_address=deployed_contract or None,
        )

        if submit_result.success:
            self.orderbook.update_order(
                order.order_id,
                status=OrderStatus.FILLED,
                tx_hash=submit_result.tx_hash,
                block_number=submit_result.block_number,
                fee_paid=self.fee_enabled and order.fee_amount_wei > 0,
            )
            # Update perpetual order state
            if order.perpetual:
                self.orderbook.update_order(
                    order.order_id,
                    execution_count=order.execution_count + 1,
                    last_filled_at=time.time(),
                )
                # Re-open perpetual orders if under max_executions
                # Note: execution_count was already incremented by update_order above
                if order.execution_count < order.max_executions:
                    # Perpetuals are signed ONCE with the sentinel nonce
                    # (type(uint256).max): the contract neither verifies nor
                    # increments it (AppIntentBase replay protection for a
                    # perpetual is executionCounts + cooldown + maxExecutions),
                    # so there is no per-fill nonce to refresh and the single
                    # user signature stays valid across every fill. Refreshing
                    # it to a concrete value would mutate a signed EIP-712 field
                    # and break signature verification on fill #2.
                    self.orderbook.update_order(
                        order.order_id,
                        status=OrderStatus.OPEN,
                    )
            self.order_persistence.sync(order.order_id)
            # Record the on-chain delivered-quality BPS (not the JS sentinel).
            self._record_settlement_stats(order, stat_bps, submit_result)
            return True

        # Settlement reverted. Distinguish a USER signature fault (#229) — the plan
        # passed scoring + quorum, the user's sig was invalid — from a solver fault.
        # The order is REJECTED either way (it didn't fill), but a user-fault revert
        # does NOT debit the blameless miner's execution stats.
        if _is_user_signature_fault(submit_result.error):
            reject_error = f"User signature rejected at settlement: {submit_result.error}"
        elif _is_user_fund_fault(submit_result.error):
            reject_error = (
                "User cannot fund order (insufficient input-token balance/"
                f"allowance) at settlement: {submit_result.error}"
            )
        else:
            reject_error = f"Relayer submission failed: {submit_result.error}"
        self.orderbook.update_order(
            order.order_id,
            status=OrderStatus.REJECTED,
            error=reject_error,
        )
        self.order_persistence.sync(order.order_id)
        # Record the on-chain delivered-quality BPS (not the JS sentinel).
        self._record_settlement_stats(order, stat_bps, submit_result)
        return False

    def _record_settlement_stats(self, order: Order, score: float, submit_result: Any) -> None:
        """Record execution stats for a settled order (#229 blameless miner).

        ``score`` is the on-chain scoreIntent BPS (0..10000) — the delivered
        quality the app stats now track — not the legacy JS 0..1 score.

        A USER fault at settlement is NOT debited to the solver: the plan passed
        JS scoring, on-chain sim scoring, and the validator quorum, and the revert
        is entirely upstream of the solver — either an invalid order signature OR
        the user not actually holding/approving the input token (the scoring fork
        fabricates the balance, so a balance-less order still scores as doable).
        The order is still REJECTED (handled by the caller); it just isn't counted
        as a solver failure, so an attacker can't spam impossible orders to tank an
        honest miner. Solver-attributable reverts still count.
        """
        if submit_result.success:
            self.app_store.record_execution(order.app_id, score, success=True)
            return
        if _is_user_fault(submit_result.error):
            logger.warning(
                "Order %s settlement reverted on a USER fault (%s) — NOT debiting "
                "the solver (blameless miner, #229)",
                order.order_id, (submit_result.error or "")[:80],
            )
            return
        self.app_store.record_execution(order.app_id, score, success=False)

    def _build_snapshot(self, order: Order, app: AppIntentDefinition):
        """Build a MarketSnapshot for plan generation.

        Returns None -- the solver builds its own market data from RPC.
        Kept as a method for future extensibility (e.g. providing hints).
        """
        return None

    async def _auto_sign_order(
        self,
        order: Order,
        contract_address: str,
    ) -> str | None:
        """Auto-sign an order for managed wallets using the wallet manager.

        Returns hex-encoded EIP-712 signature, or None if signing fails.
        """
        wallet_mgr = self.wallet_manager
        if wallet_mgr is None:
            return None

        # Check if this address belongs to a managed wallet
        try:
            info = await wallet_mgr.get_wallet(order.submitted_by)
            if info is None:
                return None  # Not a managed wallet
        except Exception:
            return None

        # Reuse the relayer encoder so the EIP-712 fields match on-chain
        from minotaur_subnet.relayer.encoder import encode_intent_order
        fields = encode_intent_order(order)
        # fields = (order_id_bytes, app, selector, intent_params, ...)

        sig = await wallet_mgr.sign_eip712_order(
            address=order.submitted_by,
            order_id=fields[0],         # order_id_bytes (keccak of order_id string)
            app=contract_address,
            intent_selector=fields[2],  # bytes4
            intent_params=fields[3],    # ABI-encoded intent params
            submitted_by=order.submitted_by,
            chain_id=order.chain_id,
            deadline=int(order.deadline),
            nonce=order.params.get("user_nonce", 0),
            perpetual=order.perpetual,
            max_executions=order.max_executions,
            cooldown=int(order.cooldown),
            contract_address=contract_address,
        )
        return sig

    async def _perpetual_funds_check(self, order: Any, spender: str) -> bool:
        """Live balance+allowance gate for a perpetual's NEXT fill (#1/#3).

        Returns True to proceed, False if the perpetual was terminated as
        unfundable. Reads the user's real on-chain balance and allowance for the
        input token (and the fee token, if the fee is non-zero). On an allowance
        shortfall where the order carries an EIP-2612 permit, the relayer submits
        it to set the standing allowance gaslessly (#3) and the check re-reads
        rather than terminating. A BALANCE shortfall can't be cured → terminal.

        Fails OPEN on any read error — a transient RPC hiccup must never
        terminate a fundable perpetual; the settlement path stays the backstop.
        Native-input fills (msg.value, no ERC-20 allowance) skip the input leg.
        """
        import asyncio as _asyncio
        from minotaur_subnet.blockchain.token_approval import read_balance_and_allowance

        try:
            from minotaur_subnet.blockchain.chains import get_web3
            w3 = get_web3(order.chain_id)
        except Exception as exc:
            logger.warning(
                "Perpetual %s: no web3 for funds pre-check (fail-open): %s",
                order.order_id, exc,
            )
            return True

        loop = _asyncio.get_running_loop()
        legs: list[tuple[str, int]] = []
        # Spend-token leg — the token the app pulls from the user for THIS intent,
        # identified app-agnostically (shared resolve_spend_token_amount, same
        # convention as order submission). Skipped when the input is native (paid
        # as msg.value) or unidentifiable (unknown → settlement backstop, never a
        # false terminate). Works for any app, not just swaps.
        if not order.params.get("_input_token_is_native"):
            from minotaur_subnet.blockchain.tokens import resolve_spend_token_amount
            spend_token, spend_amount = resolve_spend_token_amount(order.params)
            if spend_token and spend_amount:
                legs.append((spend_token, spend_amount))
        # Fee-token leg — ONLY when the app collects the fee directly from the
        # user (FeeMode.USER: the base _collectPlatformFee pulls WETH via
        # safeTransferFrom on every fill). APP-mode apps (e.g. DexAggregatorApp)
        # instead deduct the fee from the swap output and settle it from their own
        # WETH float — nothing is pulled from the user — so pre-checking WETH there
        # would falsely terminate a fundable perpetual. The fee IS collected in
        # both modes (it covers the relayer's gas); only USER mode collects it
        # from the user's wallet. Read the on-chain feeMode() to decide, and skip
        # the leg (fail-open) on APP mode / unreadable / zero fee / native input.
        # NB WETH (0xC02a…/0x4200…0006) has no EIP-2612 permit, so a USER-mode WETH
        # shortfall can't be cured by a carried permit — the user must approve().
        fee_wei = int(order.params.get("platform_fee_wei", 0) or 0)
        if fee_wei > 0 and not order.params.get("_input_token_is_native"):
            from minotaur_subnet.blockchain.token_approval import fee_mode_is_user
            user_mode = await loop.run_in_executor(None, fee_mode_is_user, w3, spender)
            if user_mode:
                from minotaur_subnet.blockchain.tokens import WRAPPED_NATIVE_TOKEN
                fee_token = WRAPPED_NATIVE_TOKEN.get(order.chain_id)
                if fee_token:
                    legs.append((fee_token, fee_wei))

        for token, required in legs:
            reading = await loop.run_in_executor(
                None, read_balance_and_allowance, w3, token, order.submitted_by, spender,
            )
            if reading is None:
                continue  # read failed → fail open for this leg
            balance, allowance = reading
            if balance < required:
                self._terminate_perpetual_unfunded(
                    order,
                    f"insufficient balance for next fill: {balance} < {required} ({token})",
                )
                return False
            if allowance < required:
                # Try to cure the allowance gaslessly with a carried permit (#3).
                cured = await self._submit_order_permit(order, token, spender)
                if cured:
                    reading = await loop.run_in_executor(
                        None, read_balance_and_allowance, w3, token, order.submitted_by, spender,
                    )
                    if reading is not None and reading[1] >= required:
                        continue
                self._terminate_perpetual_unfunded(
                    order,
                    f"insufficient allowance for next fill: {allowance} < {required} to {spender} ({token})",
                )
                return False
        return True

    async def _submit_order_permit(self, order: Any, token: str, spender: str) -> bool:
        """Submit a user-signed EIP-2612 permit to set a standing allowance (#3).

        External perpetual users sign the permit client-side and carry its fields
        in ``order.params`` (``permit_deadline``/``permit_v``/``permit_r``/
        ``permit_s``, optional ``permit_value`` defaulting to unlimited). The
        relayer submits ``token.permit(...)`` gaslessly for the user — anyone may
        relay a permit, and the one-time gas is dwarfed by the per-fill fee margin
        collected over the perpetual's life. Returns True on a successful submit,
        False when absent/invalid/failed (caller then treats it as unfunded).
        """
        p = order.params
        if not all(k in p for k in ("permit_deadline", "permit_v", "permit_r", "permit_s")):
            return False
        try:
            value = int(p.get("permit_value") or 0) or (2 ** 256 - 1)
            deadline = int(p["permit_deadline"])
            v = int(p["permit_v"])
            r = bytes.fromhex(str(p["permit_r"]).replace("0x", ""))
            s = bytes.fromhex(str(p["permit_s"]).replace("0x", ""))
        except (ValueError, TypeError) as exc:
            logger.warning("Perpetual %s: malformed permit params: %s", order.order_id, exc)
            return False
        try:
            await self.relayer.call_contract_function(
                contract_address=token,
                chain_id=order.chain_id,
                signature="permit(address,address,uint256,uint256,uint8,bytes32,bytes32)",
                abi_types=["address", "address", "uint256", "uint256", "uint8", "bytes32", "bytes32"],
                values=[order.submitted_by, spender, value, deadline, v, r, s],
                gas=120_000,
            )
            logger.info(
                "Perpetual %s: submitted EIP-2612 permit to set allowance on %s",
                order.order_id, token,
            )
            return True
        except Exception as exc:
            logger.warning("Perpetual %s: permit submission failed: %s", order.order_id, exc)
            return False

    def _terminate_perpetual_unfunded(self, order: Any, reason: str) -> None:
        """Terminate a perpetual the user can no longer fund (REJECTED, no debit).

        A funding shortfall is entirely upstream of the solver (the scoring fork
        fabricates balance, so the plan still scored as doable), so like any user
        fund-fault it is blameless — the miner is NOT debited.
        """
        self.orderbook.update_order(
            order.order_id,
            status=OrderStatus.REJECTED,
            error=f"Perpetual terminated — user can no longer fund the next fill ({reason})",
        )
        self.order_persistence.sync(order.order_id)
        logger.info(
            "Perpetual order %s terminated (unfunded, miner not debited): %s",
            order.order_id, reason,
        )

    def _try_requeue_perpetual(self, order: Any, reason: str) -> bool:
        """Requeue a live perpetual OPEN for a later cycle; return True if done.

        A perpetual's trigger condition is simply *"can this execute now?"* —
        answered every cycle by the on-chain score gate. A non-fatal miss this
        cycle (solver produced no plan, on-chain score below threshold, contract
        didn't bless the plan) is the perpetual's normal resting state: a
        limit-order perpetual only clears the gate when the price is favorable,
        and may rest below threshold for most of its life. So a live perpetual —
        still under ``max_executions`` and not past its ``deadline`` — is requeued
        OPEN **without consuming an execution slot and without debiting the miner**
        (the user's price simply not being hit is not a solver failure).
        ``last_filled_at=now`` reuses the cooldown gate to back off one cycle. The
        requeue is unbounded (unlike the consensus retry budget) because resting
        below threshold indefinitely is expected, not a transient fault.

        Returns False and leaves the order untouched for one-shot orders and for
        perpetuals that are exhausted or past deadline, so the caller applies its
        normal terminal handling (REJECTED, with its existing miner-debit policy).
        ``deadline<=0`` means "no deadline" (see ``Order.deadline``).
        """
        now = time.time()
        deadline = float(getattr(order, "deadline", 0) or 0)
        live_perpetual = (
            order.perpetual
            and order.execution_count < order.max_executions
            and (deadline <= 0 or now < deadline)
        )
        if not live_perpetual:
            return False
        self.orderbook.update_order(
            order.order_id,
            status=OrderStatus.OPEN,
            last_filled_at=now,
        )
        self.order_persistence.sync(order.order_id)
        logger.info(
            "Perpetual order %s: %s — requeued OPEN "
            "(execution slot not consumed, miner not debited)",
            order.order_id, reason,
        )
        return True

    def _handle_no_plan(self, order: Any) -> None:
        """Resolve an order whose solver produced no plan (#225/#226).

        Live perpetuals are requeued via ``_try_requeue_perpetual`` — the champion
        solver hot-swaps over the order's lifetime, so a future champion may solve
        it, and the execution slot is not consumed. One-shot orders (and exhausted
        or expired perpetuals) are terminal: ``REJECTED`` with a clear reason +
        ``record_execution(success=False)`` for miner accountability — matching the
        bad-plan path, instead of a silent ASSIGNED→EXPIRED with no signal.
        """
        if self._try_requeue_perpetual(order, "no plan this cycle"):
            return
        self.orderbook.update_order(
            order.order_id,
            status=OrderStatus.REJECTED,
            error="solver produced no plan",
        )
        self.order_persistence.sync(order.order_id)
        self.app_store.record_execution(order.app_id, 0.0, success=False)
        logger.info(
            "Order %s: solver produced no plan — REJECTED + miner debited",
            order.order_id,
        )

