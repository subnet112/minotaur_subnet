"""Block Loop — per-tick processing loop for the Intent OrderBook.

Each tick (~12s, matching block time):
1. Expire stale orders past their deadline
2. Snapshot OPEN orders from the OrderBook
3. For each order: generate plan -> simulate -> score -> consensus -> relay
4. Return TickResult with summary

The block loop is the core runtime for v2 validators.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any

from minotaur_subnet.orderbook.orderbook import IntentOrderBook, OrderStatus
from minotaur_subnet.relayer.base import RelayerBase, MockRelayer
from minotaur_subnet.store import AppIntentStore
from minotaur_subnet.v3.flags import load_v3_flags

from minotaur_subnet.blockloop.plan_generation import PlanGenerator
from minotaur_subnet.blockloop.simulation import SimulationRunner
from minotaur_subnet.blockloop.scoring import PlanScorer
from minotaur_subnet.blockloop.persistence import OrderPersistence
from minotaur_subnet.blockloop.cross_chain import CrossChainOrchestrator
from minotaur_subnet.blockloop.multi_leg import MultiLegOrchestrator
from minotaur_subnet.blockloop.order_processor import OrderProcessor

logger = logging.getLogger(__name__)


# Default score threshold for approval
_SCORE_THRESHOLD = 0.5


@dataclass
class TickResult:
    """Summary of a single block loop tick."""
    tick_number: int
    timestamp: float
    orders_processed: int = 0
    orders_approved: int = 0
    orders_rejected: int = 0
    orders_expired: int = 0
    elapsed_ms: float = 0.0
    errors: list[str] = field(default_factory=list)


class BlockLoop:
    """Async per-tick processing loop that drains the OrderBook.

    Processes OPEN orders each tick: plan generation -> simulation ->
    JS scoring -> consensus (Phase 2) -> relayer submission.
    Cross-chain orders use two-phase lifecycle: source leg -> BRIDGING -> dest leg.

    Args:
        orderbook: The Intent OrderBook to drain.
        app_store: Persistent store for app definitions and stats.
        js_engine: JS scoring engine (optional, scores mocked if None).
        solver: IntentSolver instance (optional, uses baseline if None).
        relayer: Relayer for submitting approved plans (default: MockRelayer).
        consensus: ConsensusManager (Phase 2, optional).
        simulator: AnvilSimulator or MultiChainSimulator (optional, mock if None).
        tick_interval: Seconds between ticks (default 12.0 = block time).
        score_threshold: Minimum JS score for approval.
        bridge_registry: BridgeRegistry for cross-chain bridge quoting (optional).
        bridge_tracker: BridgeTracker for monitoring in-flight bridge transfers (optional).
    """

    def __init__(
        self,
        orderbook: IntentOrderBook,
        app_store: AppIntentStore,
        js_engine: Any = None,
        solver: Any = None,
        relayer: RelayerBase | None = None,
        consensus: Any = None,
        simulator: Any = None,
        tick_interval: float = 12.0,
        score_threshold: float = _SCORE_THRESHOLD,
        bridge_registry: Any = None,
        bridge_tracker: Any = None,
        wallet_manager: Any = None,
        substrate_relayer: Any = None,
    ) -> None:
        self.orderbook = orderbook
        self.app_store = app_store
        self.js_engine = js_engine
        self.solver = solver
        self.relayer = relayer or MockRelayer()
        self.consensus = consensus
        self.simulator = simulator
        self.peer_network: Any = None
        self.substrate_relayer = substrate_relayer
        self.tick_interval = tick_interval
        self.score_threshold = score_threshold
        self.bridge_registry = bridge_registry
        self.bridge_tracker = bridge_tracker
        self.wallet_manager = wallet_manager
        self.cross_chain_compiler: Any = None
        if bridge_registry is not None:
            from minotaur_subnet.bridge.compiler import CrossChainCompiler
            self.cross_chain_compiler = CrossChainCompiler(bridge_registry)
        self.v3_flags = load_v3_flags()

        # Fee model (skeleton): when enabled, orders must have fee_amount_wei > 0
        self.fee_enabled = os.environ.get("FEE_ENABLED", "").strip().lower() in (
            "1", "true", "yes",
        )
        # Default per-execution fee in wTAO wei (0.01 wTAO = 10^16 wei)
        self.default_fee_wei = int(
            os.environ.get("DEFAULT_FEE_WEI", "10000000000000000")
        )

        self._tick_number = 0
        self._running = False
        self._last_tick_result: TickResult | None = None
        self._reload_interval = 10  # Rescan apps every N ticks (VAL-17/18)
        self._known_js_hashes: dict[str, str] = {}  # app_id -> hash of loaded JS

        # Build pipeline components
        self._plan_generator = PlanGenerator(solver=solver)
        self._simulation_runner = SimulationRunner(
            simulator=simulator,
            bridge_registry=bridge_registry,
        )
        self._plan_scorer = PlanScorer(js_engine=js_engine)
        self._order_persistence = OrderPersistence(
            app_store=app_store,
            orderbook=orderbook,
        )
        self._cross_chain_orchestrator = CrossChainOrchestrator(
            relayer=self.relayer,
            substrate_relayer=substrate_relayer,
            bridge_tracker=bridge_tracker,
            order_persistence=self._order_persistence,
            orderbook=orderbook,
            app_store=app_store,
        )
        self._multi_leg_orchestrator = MultiLegOrchestrator(
            simulator=simulator,
            plan_scorer=self._plan_scorer,
            consensus=consensus,
            peer_network=self.peer_network,
            relayer=self.relayer,
            order_persistence=self._order_persistence,
            orderbook=orderbook,
            app_store=app_store,
        )
        self._multi_leg_orchestrator.bridge_tracker = bridge_tracker
        self._order_processor = self._build_order_processor()

    def _build_order_processor(self) -> OrderProcessor:
        """Create the OrderProcessor with current dependencies."""
        return OrderProcessor(
            plan_generator=self._plan_generator,
            simulation_runner=self._simulation_runner,
            plan_scorer=self._plan_scorer,
            order_persistence=self._order_persistence,
            cross_chain_orchestrator=self._cross_chain_orchestrator,
            multi_leg_orchestrator=self._multi_leg_orchestrator,
            orderbook=self.orderbook,
            app_store=self.app_store,
            relayer=self.relayer,
            consensus=self.consensus,
            peer_network=self.peer_network,
            wallet_manager=self.wallet_manager,
            cross_chain_compiler=self.cross_chain_compiler,
            js_engine=self.js_engine,
            score_threshold=self.score_threshold,
            fee_enabled=self.fee_enabled,
            default_fee_wei=self.default_fee_wei,
            v3_flags=self.v3_flags,
            simulator=self.simulator,
        )

    async def run_loop(self) -> None:
        """Run the block loop forever until stop() is called."""
        self._running = True

        # OB-12: Load persisted OPEN orders on startup so they survive restarts
        loaded = self.load_open_orders_from_store()
        if loaded > 0:
            logger.info("Loaded %d persisted OPEN orders from store", loaded)

        logger.info(
            "BlockLoop started (tick_interval=%.1fs, threshold=%.2f)",
            self.tick_interval,
            self.score_threshold,
        )
        while self._running:
            try:
                result = await self.tick()
                self._last_tick_result = result
                if result.orders_processed > 0:
                    logger.info(
                        "Tick #%d: processed=%d approved=%d rejected=%d expired=%d (%.0fms)",
                        result.tick_number,
                        result.orders_processed,
                        result.orders_approved,
                        result.orders_rejected,
                        result.orders_expired,
                        result.elapsed_ms,
                    )
            except Exception as exc:
                logger.error("BlockLoop tick error: %s", exc, exc_info=True)
            await asyncio.sleep(self.tick_interval)
        logger.info("BlockLoop stopped")

    async def tick(self) -> TickResult:
        """Execute a single tick of the block loop."""
        self._tick_number += 1
        start = time.time()
        result = TickResult(
            tick_number=self._tick_number,
            timestamp=start,
        )
        self._last_tick_result = result

        # 0. Periodic rescan for new/updated apps (VAL-17, VAL-18)
        if self._tick_number % self._reload_interval == 0:
            try:
                await self._reload_apps_and_js()
            except Exception as exc:
                logger.warning("App reload failed: %s", exc)

        # 1. Expire stale orders
        result.orders_expired = self.orderbook.expire_stale()

        # 2. Snapshot open orders
        orders = self.orderbook.snapshot_open()
        if not orders:
            result.elapsed_ms = (time.time() - start) * 1000
            return result

        # 3. Process each order
        for order in orders:
            try:
                approved = await self._order_processor.process(order)
                result.orders_processed += 1
                if approved:
                    result.orders_approved += 1
                else:
                    result.orders_rejected += 1
            except Exception as exc:
                result.orders_processed += 1
                result.orders_rejected += 1
                result.errors.append(f"{order.order_id}: {exc}")
                logger.error(
                    "Error processing order %s: %s",
                    order.order_id, exc, exc_info=True,
                )
                self.orderbook.update_order(
                    order.order_id,
                    status=OrderStatus.REJECTED,
                    error=str(exc),
                )
                self._order_persistence.sync(order.order_id)

        result.elapsed_ms = (time.time() - start) * 1000
        return result

    def set_solver(self, solver: Any) -> None:
        """Hot-swap the solver (for epoch transitions)."""
        old_solver = self.solver
        old_name = getattr(old_solver, 'metadata', lambda: None)()
        new_name = getattr(solver, 'metadata', lambda: None)()
        logger.info(
            "Solver swapped: %s -> %s",
            getattr(old_name, 'name', 'none') if old_name else 'none',
            getattr(new_name, 'name', 'none') if new_name else 'none',
        )
        self.solver = solver

        # Update the plan generator's solver reference
        self._plan_generator.solver = solver

        # Best-effort cleanup for replaced solver runtimes (e.g. Docker sessions).
        if old_solver is not None and old_solver is not solver:
            shutdown = getattr(old_solver, "shutdown", None)
            if callable(shutdown):
                try:
                    maybe_awaitable = shutdown()
                    if inspect.isawaitable(maybe_awaitable):
                        try:
                            asyncio.get_running_loop().create_task(maybe_awaitable)
                        except RuntimeError:
                            asyncio.run(maybe_awaitable)
                except Exception as exc:
                    logger.warning("Previous solver shutdown failed: %s", exc)

    def set_consensus(self, consensus: Any) -> None:
        """Hot-swap the consensus manager."""
        logger.info("Consensus manager updated")
        self.consensus = consensus
        self._multi_leg_orchestrator.consensus = consensus
        self._order_processor.consensus = consensus

    def set_peer_network(self, peer_network: Any) -> None:
        """Set or update the peer network for multi-validator broadcast."""
        logger.info("Peer network updated")
        self.peer_network = peer_network
        self._multi_leg_orchestrator.peer_network = peer_network
        self._order_processor.peer_network = peer_network

    async def on_leader_changed(self, new_leader_id: str) -> None:
        """Handle leadership transition (CON-15, REL-12, OB-12).

        Clears all in-flight consensus proposals and relayer submissions,
        then reloads OPEN orders from the store for reprocessing.
        """
        logger.info("Leader changed to %s -- resetting pipeline", new_leader_id[:10] if new_leader_id else "?")

        # CON-15: Drop all pending consensus proposals
        if self.consensus is not None and hasattr(self.consensus, "clear_all_pending"):
            await self.consensus.clear_all_pending()

        # REL-12: Drop in-flight relayer submissions
        if hasattr(self.relayer, "on_leader_changed"):
            self.relayer.on_leader_changed(new_leader_id)

        # OB-12: Reload OPEN orders from store into the OrderBook
        self.load_open_orders_from_store()

    def load_open_orders_from_store(self) -> int:
        """Load persisted OPEN orders from store into the OrderBook (OB-12).

        Called on leader transition so the new leader can reprocess
        all outstanding orders. Returns the number of orders loaded.
        """
        return self._order_persistence.load_open_orders(self.orderbook)

    async def _reload_apps_and_js(self) -> None:
        """Periodic rescan for new apps and JS updates (VAL-17, VAL-18).

        Checks the app store for active apps and reloads JS code into the
        engine when it changes. Called every ``_reload_interval`` ticks.
        """
        if self.js_engine is None:
            return

        from hashlib import sha256

        for app_def in self.app_store.list_apps():
            deployment = self.app_store.get_deployment(app_def.app_id)
            if deployment is None:
                continue
            if not deployment.status.is_operational():
                continue

            js_code = app_def.js_code
            if not js_code or len(js_code.strip()) < 20:
                continue

            js_hash = sha256(js_code.encode()).hexdigest()[:16]
            old_hash = self._known_js_hashes.get(app_def.app_id)

            if old_hash != js_hash:
                try:
                    await self.js_engine.load_intent(app_def.app_id, js_code)
                    self._known_js_hashes[app_def.app_id] = js_hash
                    if old_hash is not None:
                        logger.info(
                            "Hot-reloaded JS for app %s (hash %s -> %s)",
                            app_def.app_id, old_hash, js_hash,
                        )
                    else:
                        logger.info(
                            "Loaded JS for new app %s (hash %s)",
                            app_def.app_id, js_hash,
                        )
                except Exception as exc:
                    logger.warning(
                        "Failed to load JS for app %s: %s",
                        app_def.app_id, exc,
                    )

    def stop(self) -> None:
        """Signal the block loop to stop after the current tick."""
        self._running = False

    @property
    def running(self) -> bool:
        return self._running

    @property
    def last_tick(self) -> TickResult | None:
        return self._last_tick_result

    def status(self) -> dict[str, Any]:
        """Return current block loop status."""
        lt = self._last_tick_result
        return {
            "running": self._running,
            "tick_number": self._tick_number,
            "tick_interval": self.tick_interval,
            "score_threshold": self.score_threshold,
            "last_tick": {
                "tick_number": lt.tick_number,
                "orders_processed": lt.orders_processed,
                "orders_approved": lt.orders_approved,
                "orders_rejected": lt.orders_rejected,
                "orders_expired": lt.orders_expired,
                "elapsed_ms": lt.elapsed_ms,
                "timestamp": lt.timestamp,
            } if lt else None,
            "orderbook_stats": self.orderbook.stats(),
        }
