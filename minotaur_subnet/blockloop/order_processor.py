"""Order processing pipeline for the block loop.

Orchestrates the single-order pipeline: plan generation -> simulation ->
JS scoring -> policy enforcement -> consensus voting -> wallet signing ->
relayer submission -> order persistence.
"""

from __future__ import annotations

import asyncio
import logging
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

        # Generate plan — if no solver is available, skip (order stays open)
        plan = await self.plan_generator.generate(app, state, snapshot)
        if plan is None:
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

        # Score via JS engine
        score_result = await self.plan_scorer.score(
            order.app_id, app, plan, simulation, state,
        )
        score = score_result.score if score_result else 0.0

        # Track best score even if below threshold
        current_best = order.best_score or 0.0
        new_best = max(current_best, score)

        self.orderbook.update_order(
            order.order_id,
            status=OrderStatus.SCORED,
            score=score,
            best_score=new_best,
        )
        self.order_persistence.sync(order.order_id)

        # Check JS score threshold — per-app if configured, else global (SCR-7)
        js_threshold = app.config.score_threshold if app.config else self.score_threshold
        if score < js_threshold:
            self.orderbook.update_order(
                order.order_id,
                status=OrderStatus.REJECTED,
                error=f"Score {score:.4f} below threshold {js_threshold}",
            )
            self.order_persistence.sync(order.order_id)
            self.app_store.record_execution(order.app_id, score, success=False)
            return False

        # On-chain score gate — dual scoring (SCR-5, SCR-6, VAL-10)
        on_chain_threshold = app.config.on_chain_threshold  # default 5000 BPS
        oc_score = simulation.on_chain_score
        if oc_score is not None:
            self.orderbook.update_order(order.order_id, on_chain_score=oc_score)
        # Fail-closed (opt-in): a deployed contract must yield a passing on-chain
        # score. None means scoreIntent returned valid=False (plan breaks an
        # on-chain invariant) or was unreadable — the contract did NOT bless the
        # plan, so don't relay it on the JS score alone. Leader + follower share
        # onchain_score_fail_closed() so they gate identically.
        if contract_address and oc_score is None and onchain_score_fail_closed():
            self.orderbook.update_order(
                order.order_id,
                status=OrderStatus.REJECTED,
                error="On-chain score unavailable — contract returned invalid or unreadable (dual-scoring fail-closed)",
            )
            self.order_persistence.sync(order.order_id)
            self.app_store.record_execution(order.app_id, score, success=False)
            return False
        if oc_score is not None and oc_score < on_chain_threshold:
            self.orderbook.update_order(
                order.order_id,
                status=OrderStatus.REJECTED,
                error=(
                    f"On-chain score {oc_score} BPS "
                    f"< threshold {on_chain_threshold}"
                ),
            )
            self.order_persistence.sync(order.order_id)
            self.app_store.record_execution(order.app_id, score, success=False)
            return False

        # Phase 2: Consensus (with optional peer broadcast)
        consensus_result = None
        if self.consensus is not None:
            from minotaur_subnet.consensus.signatures import hash_plan
            plan_hash = hash_plan(plan)

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
                self.orderbook.update_order(
                    order.order_id,
                    status=OrderStatus.REJECTED,
                    error="Consensus not reached",
                    consensus_result=_json_safe(consensus_result),
                )
                self.order_persistence.sync(order.order_id)
                return False

        self.orderbook.update_order(
            order.order_id,
            status=OrderStatus.APPROVED,
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
                    # Sync on-chain nonce so next execution doesn't revert
                    await self._refresh_perpetual_nonce(order)
                    self.orderbook.update_order(
                        order.order_id,
                        status=OrderStatus.OPEN,
                    )
            self.order_persistence.sync(order.order_id)
        else:
            self.orderbook.update_order(
                order.order_id,
                status=OrderStatus.REJECTED,
                error=f"Relayer submission failed: {submit_result.error}",
            )
            self.order_persistence.sync(order.order_id)

        self.app_store.record_execution(
            order.app_id, score, success=submit_result.success,
        )
        return submit_result.success

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

    async def _refresh_perpetual_nonce(self, order: "Order") -> None:
        """Fetch the user's current on-chain nonce and update the order params.

        After a perpetual order fills, the on-chain nonce advances but the
        off-chain order params still hold the old value.  Without this sync,
        subsequent fills revert with "Invalid nonce".

        Uses the simulator's web3 connection to call ``nonces(address)`` on
        the app contract.  If the fetch fails, a warning is logged but the
        re-open is not blocked.
        """
        try:
            w3 = None
            if self.simulator is not None:
                w3 = getattr(self.simulator, "w3", None)
            if w3 is None:
                return

            contract_address = order.params.get("contract_address")
            if not contract_address:
                # Try to look up from app store deployment
                dep = self.app_store.get_deployment(order.app_id) if self.app_store else None
                if dep:
                    contract_address = getattr(dep, "contract_address", None)
            if not contract_address:
                return

            # Call nonces(address) -- returns uint256
            nonce_selector = "0x7ecebe00"  # keccak256("nonces(address)")[:4]
            padded_addr = order.submitted_by.lower().replace("0x", "").zfill(64)
            call_data = nonce_selector + padded_addr

            result = w3.eth.call({
                "to": w3.to_checksum_address(contract_address),
                "data": call_data,
            })
            new_nonce = int(result.hex(), 16)
            order.params["user_nonce"] = new_nonce
            # Also update the order in the orderbook
            self.orderbook.update_order(order.order_id, params=order.params)
            logger.info(
                "Refreshed on-chain nonce for perpetual order %s (user=%s, nonce=%d)",
                order.order_id, order.submitted_by, new_nonce,
            )
        except Exception as exc:
            logger.warning(
                "Failed to refresh on-chain nonce for perpetual order %s: %s",
                order.order_id, exc,
            )
