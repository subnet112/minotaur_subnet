"""
App Intents Validator -- standalone HTTP service.

Loads deployed App Intent JS scoring code, accepts miner plan submissions,
scores them via JsExecutionEngine, and tracks per-miner weights across epochs.

Start the validator:
    python -m minotaur_subnet.validator.main --port 9100 --epoch-seconds 60
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from pathlib import Path
from typing import Any

from aiohttp import web

# Ensure repo root is importable
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from minotaur_subnet.consensus.protocol_config import ProtocolConfig
from minotaur_subnet.engine import JsExecutionEngine
from minotaur_subnet.store import AppIntentStore
from minotaur_subnet.blockloop.loop import BlockLoop
from minotaur_subnet.shared.simulation import build_mock_simulation
from minotaur_subnet.orderbook import IntentOrderBook
from minotaur_subnet.shared.types import (
    AppStatus,
    AppIntentDefinition,
    DeploymentResult,
    ExecutionPlan,
    Interaction,
    IntentState,
    SignedApproval,
)
from minotaur_subnet.weight_policy import build_bootstrap_or_champion_weights

# Extracted modules
from minotaur_subnet.validator.weight_policy import ChampionWeights
from minotaur_subnet.validator.scoring_engine import ScoringEngine
from minotaur_subnet.validator.proposal_handler import ProposalHandler

import os

logger = logging.getLogger("minotaur_subnet.validator")


def _build_intent_state_from_params(
    app_def: AppIntentDefinition,
    deployment: DeploymentResult,
    params: dict[str, Any],
) -> IntentState:
    """Build an IntentState from app definition and order params."""
    return IntentState(
        contract_address=deployment.contract_address or "",
        chain_id=deployment.chain_id,
        nonce=0,
        owner=app_def.deployer or "",
        raw_params=params,
        control={"_intent_function": params.get("intent_function", "execute")},
    )


# ═══════════════════════════════════════════════════════════════════════════════
#                        APP INTENTS VALIDATOR
# ═══════════════════════════════════════════════════════════════════════════════


class AppIntentsValidator:
    """Validator service for the App Intents platform.

    - Reads the store for active (deployed) apps
    - Loads their MVP scoring JS into JsExecutionEngine
    - Exposes HTTP endpoints for miners to discover and submit plans
    - Tracks per-miner weights across epochs
    """

    def __init__(
        self,
        store: AppIntentStore,
        port: int = 9100,
        epoch_seconds: int = 60,
        tick_interval: float = 12.0,
        # Bittensor integration (optional)
        subtensor_url: str | None = None,
        netuid: int = 112,
        wallet_name: str | None = None,
        hotkey_name: str | None = None,
        validator_hotkey_ss58: str | None = None,
        # Consensus (optional)
        validator_private_key: str = "",
        validator_peers: list[str] | None = None,
        protocol_config: "ProtocolConfig | None" = None,
        chain_id: int = 31337,
        contract_address: str = "0x" + "00" * 20,
    ) -> None:
        self.store = store
        self.port = port
        self.engine = JsExecutionEngine(timeout_ms=10000)
        self.weights = ChampionWeights(
            epoch_seconds=epoch_seconds,
            owner_hotkey=os.environ.get("SUBNET_OWNER_HOTKEY", "")
            or os.environ.get("OWNER_HOTKEY", ""),
        )
        self.orderbook = IntentOrderBook()

        # Shared bridge registry for quote/solve paths.
        bridge_registry = None
        try:
            from minotaur_subnet.bridge import BridgeRegistry
            from minotaur_subnet.bridge.mock import MockBridgeAdapter
            from minotaur_subnet.bridge.hyperlane import HyperlaneAdapter

            bridge_registry = BridgeRegistry()
            bridge_registry.register(MockBridgeAdapter())
            bridge_registry.register(HyperlaneAdapter())
            logger.info("BridgeRegistry initialized (mock + hyperlane adapters)")
        except Exception as exc:
            logger.warning("BridgeRegistry unavailable: %s", exc)

        # Connect to Anvil for real simulation if available (multi-chain)
        simulator = None
        anvil_url = os.environ.get("ANVIL_RPC_URL")
        base_url = os.environ.get("BASE_RPC_URL")
        base_sim_url = os.environ.get("BASE_SIM_RPC_URL") or base_url
        sim_rpc_urls: dict[int, str] = {}
        if anvil_url:
            sim_rpc_urls[31337] = anvil_url
            sim_rpc_urls[1] = anvil_url
        if base_sim_url:
            sim_rpc_urls[8453] = base_sim_url

        # Upstream RPCs (same endpoints anvil containers fork from) so
        # AnvilSimulator can advance the fork to current upstream head
        # before each simulation. Without this, anvil_reset is a no-op
        # and sims run against stale fork-time state.
        upstream_rpc_urls: dict[int, str] = {}
        eth_upstream = (os.environ.get("ETH_UPSTREAM_RPC_URL") or "").strip()
        if eth_upstream:
            upstream_rpc_urls[1] = eth_upstream
        base_upstream = (os.environ.get("BASE_UPSTREAM_RPC_URL") or "").strip()
        if base_upstream:
            upstream_rpc_urls[8453] = base_upstream
        btevm_upstream = (os.environ.get("BITTENSOR_EVM_UPSTREAM_RPC_URL") or "").strip()
        if btevm_upstream:
            upstream_rpc_urls[964] = btevm_upstream

        if sim_rpc_urls:
            try:
                from minotaur_subnet.simulator.anvil_simulator import MultiChainSimulator
                simulator = MultiChainSimulator(
                    sim_rpc_urls,
                    upstream_rpc_urls=upstream_rpc_urls,
                )
                logger.info(
                    "MultiChainSimulator initialized (chains=%s, upstreams=%s)",
                    list(sim_rpc_urls.keys()),
                    [c for c in sim_rpc_urls if c in upstream_rpc_urls],
                )
            except Exception as exc:
                logger.warning("MultiChainSimulator init failed: %s", exc)

        # Baseline solver for live validator quotes / plan generation.
        solver = None
        rpc_urls: dict[int, str] = {}
        chain_ids: list[int] = []
        if anvil_url:
            rpc_urls[31337] = anvil_url
            rpc_urls[1] = anvil_url
            chain_ids.extend([1, 31337])
        if base_url:
            rpc_urls[8453] = base_url
            chain_ids.append(8453)
        solver = None
        _genesis_image = os.environ.get("GENESIS_SOLVER_IMAGE", "").strip()
        if _genesis_image:
            try:
                from minotaur_subnet.harness.runtime_solver import DockerRuntimeSolver
                import asyncio
                solver = asyncio.get_event_loop().run_until_complete(
                    DockerRuntimeSolver.create(
                        image_ref=_genesis_image,
                        chain_ids=chain_ids or [31337],
                        rpc_urls=rpc_urls,
                        bridge_registry=bridge_registry,
                    )
                )
                logger.info("Genesis solver initialized via Docker (%s)", _genesis_image)
            except Exception as exc:
                logger.warning("Genesis Docker solver unavailable: %s", exc)
        else:
            logger.info("No GENESIS_SOLVER_IMAGE — solver unavailable until champion is adopted")

        self.block_loop = BlockLoop(
            orderbook=self.orderbook,
            app_store=store,
            js_engine=self.engine,
            solver=solver,
            simulator=simulator,
            tick_interval=tick_interval,
        )
        self._start_time = time.time()
        self._app: web.Application | None = None
        self._epoch_task: asyncio.Task | None = None
        self._block_loop_task: asyncio.Task | None = None
        # Champion miner tracking (set by git-based submission pipeline)
        self._champion_miner_id: str | None = None

        # ── Bittensor integration ────────────────────────────────────────
        self._metagraph_sync = None
        self._weights_emitter = None
        self._bt_wallet = None
        self._is_leader = True  # Default: standalone = always leader
        self._metagraph_task: asyncio.Task | None = None
        self._leader_monitor_task: asyncio.Task | None = None

        if subtensor_url:
            try:
                import bittensor as bt

                subtensor = bt.Subtensor(network=subtensor_url)
                resolved_hotkey = (validator_hotkey_ss58 or "").strip()

                # Restore logging — bittensor clears handlers, sets root to
                # WARNING, and sets all existing loggers to CRITICAL.
                logging.basicConfig(
                    level=logging.INFO,
                    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
                    force=True,
                )
                for name in list(logging.Logger.manager.loggerDict):
                    if name.startswith("minotaur_subnet"):
                        logging.getLogger(name).setLevel(logging.NOTSET)

                if wallet_name and hotkey_name:
                    try:
                        self._bt_wallet = bt.Wallet(name=wallet_name, hotkey=hotkey_name)
                        resolved_hotkey = self._bt_wallet.hotkey.ss58_address
                    except Exception as exc:
                        if not resolved_hotkey:
                            raise
                        logger.warning(
                            "Failed to load wallet %s/%s; using explicit hotkey %s for metagraph-only mode: %s",
                            wallet_name,
                            hotkey_name,
                            resolved_hotkey[:16],
                            exc,
                        )

                if not resolved_hotkey:
                    raise RuntimeError(
                        "No validator hotkey available; set VALIDATOR_HOTKEY_SS58 or provide a wallet"
                    )

                from minotaur_subnet.validator.metagraph_sync import MetagraphSync
                self._metagraph_sync = MetagraphSync(
                    subtensor_url=subtensor_url,
                    netuid=netuid,
                    my_hotkey=resolved_hotkey,
                    poll_interval=60.0,
                )

                if self._bt_wallet is not None:
                    from minotaur_subnet.validator.weights_emitter import WeightsEmitter

                    self._weights_emitter = WeightsEmitter(
                        wallet=self._bt_wallet,
                        subtensor=subtensor,
                        netuid=netuid,
                        block_time=0.25 if "localhost" in subtensor_url or "127.0.0.1" in subtensor_url else 12.0,
                    )

                logger.info(
                    "Bittensor integration enabled (netuid=%d, hotkey=%s, wallet_loaded=%s)",
                    netuid,
                    resolved_hotkey[:16],
                    self._bt_wallet is not None,
                )
            except Exception as exc:
                logger.error("Bittensor init failed: %s (continuing standalone)", exc)
                self._metagraph_sync = None
                self._weights_emitter = None

        # ── Consensus + Peer Network ─────────────────────────────────────
        self._consensus = None
        self._peer_network = None
        self._validator_id = ""
        self.protocol_config = protocol_config

        if validator_private_key:
            if protocol_config is None:
                raise RuntimeError(
                    "validator_private_key set but no protocol_config supplied; "
                    "consensus cannot start without a quorum source"
                )

            from minotaur_subnet.consensus.eip712 import address_from_key
            self._validator_id = address_from_key(validator_private_key)

            from minotaur_subnet.consensus.peer_network import (
                ValidatorPeerNetwork,
                PeerEndpoint,
                parse_peers_env,
            )

            # Peer set comes from ProtocolConfig discovery by default. The
            # optional validator_peers arg pins a manual list (escape hatch
            # for local testnet / pre-discovery setups).
            pinned_peers: list[PeerEndpoint] | None = None
            if validator_peers:
                pinned_peers = parse_peers_env(",".join(validator_peers))

            from minotaur_subnet.consensus import ConsensusManager
            self._consensus = ConsensusManager(
                validator_id=self._validator_id,
                private_key=validator_private_key,
                protocol_config=protocol_config,
                # When pinned_peers is None, ConsensusManager.validators reads
                # through to protocol_config.peers automatically.
                validators=(
                    [self._validator_id] + [p.validator_id for p in pinned_peers]
                    if pinned_peers is not None else None
                ),
                chain_id=chain_id,
                contract_address=contract_address,
            )
            self.block_loop.set_consensus(self._consensus)

            self._peer_network = ValidatorPeerNetwork(
                validator_id=self._validator_id,
                private_key=validator_private_key,
                consensus=self._consensus,
                peers=pinned_peers,
                protocol_config=protocol_config,
            )
            self.block_loop.set_peer_network(self._peer_network)

            mode = "pinned" if pinned_peers is not None else "discovered"
            logger.info(
                "Consensus enabled (id=%s, peer-mode=%s, quorum=%d bps)",
                self._validator_id[:10], mode, protocol_config.quorum_bps,
            )

            # Wire peer discovery: if we have a metagraph and we're in
            # discovery mode (no pinned peers), tell ProtocolConfig how to
            # fetch the current metagraph peer list. The refresh loop calls
            # this each tick and probes /identity on each axon.
            if pinned_peers is None and self._metagraph_sync is not None:
                protocol_config.my_evm_address = self._validator_id
                protocol_config.metagraph_provider = self._metagraph_peers_for_discovery

        # ── Extracted sub-components ─────────────────────────────────────
        self._scoring_engine = ScoringEngine(
            js_engine=self.engine,
            store=self.store,
            simulator=simulator,
            consensus=self._consensus,
            peer_network=self._peer_network,
            validator_id=self._validator_id,
        )
        self._proposal_handler = ProposalHandler(
            scoring_engine=self._scoring_engine,
            consensus=self._consensus,
            score_threshold=self.block_loop.score_threshold,
        )

    async def start(self) -> None:
        """Load active intents, start block loop, and start HTTP server."""
        if self._consensus is not None:
            logger.info(
                "Validator starting as ORDER CONSENSUS PEER — "
                "will re-simulate and re-score proposals from leader"
            )
        await self._load_active_intents()
        self._load_orders_from_store()

        # Initial metagraph sync (determines leader/follower role)
        force_leader = os.environ.get("FORCE_LEADER", "").strip() in ("1", "true", "yes")
        if self._metagraph_sync is not None:
            try:
                state = await self._metagraph_sync.sync_once()
                self._is_leader = self._metagraph_sync.is_leader
                logger.info(
                    "Initial metagraph sync: role=%s, block=%d, validators=%d",
                    state.my_role, state.block, len(state.validators),
                )
            except Exception as exc:
                logger.error("Initial metagraph sync failed: %s (assuming leader)", exc)
                self._is_leader = True

        if force_leader and not self._is_leader:
            logger.info("FORCE_LEADER override: promoting to leader despite metagraph election")
            self._is_leader = True

        # Start peer network session
        if self._peer_network is not None:
            await self._peer_network.start()

        self._app = self._build_app()
        runner = web.AppRunner(self._app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", self.port)
        await site.start()
        logger.info("Validator listening on :%d", self.port)

        # Background tasks
        self._epoch_task = asyncio.create_task(self._epoch_loop())
        self._rescan_task = asyncio.create_task(self._rescan_loop())
        if self.protocol_config is not None:
            self._protocol_refresh_task = asyncio.create_task(
                self.protocol_config.refresh_loop()
            )
            logger.info(
                "ProtocolConfig refresh task started (interval=%ds)",
                self.protocol_config.refresh_interval_seconds,
            )

        # Only start block loop if we're the leader (or standalone)
        if self._is_leader:
            self._block_loop_task = asyncio.create_task(self.block_loop.run_loop())
            logger.info("BlockLoop started (tick_interval=%.1fs)", self.block_loop.tick_interval)
        else:
            logger.info("Follower mode — BlockLoop not started")

        # Metagraph sync loop
        if self._metagraph_sync is not None:
            self._metagraph_task = asyncio.create_task(self._metagraph_sync.sync_loop())
            self._leader_monitor_task = asyncio.create_task(self._leader_monitor_loop())

        # Keep running
        while True:
            await asyncio.sleep(3600)

    def _load_orders_from_store(self) -> None:
        """Load persisted orders from the store into the in-memory OrderBook."""
        orders = self.store.list_orders()
        loaded = 0
        for order_dict in orders:
            status = order_dict.get("status", "")
            if status in ("open", "solved", "scored"):
                try:
                    self.orderbook.submit(
                        app_id=order_dict["app_id"],
                        intent_function=order_dict.get("intent_function", "execute"),
                        params=order_dict.get("params", {}),
                        submitted_by=order_dict.get("submitted_by", ""),
                        chain_id=order_dict.get("chain_id", 1),
                        deadline=order_dict.get("deadline", 0),
                        perpetual=order_dict.get("perpetual", False),
                        max_executions=order_dict.get("max_executions", 1),
                        cooldown=order_dict.get("cooldown", 0),
                    )
                    loaded += 1
                except Exception as exc:
                    logger.warning("Failed to load order %s: %s", order_dict.get("order_id"), exc)
        logger.info("Loaded %d open orders from store", loaded)

    async def _load_active_intents(self) -> None:
        """Load scoring JS for all operational apps."""
        apps = self.store.list_apps()
        loaded = 0
        for app_def in apps:
            deployment = self.store.get_deployment(app_def.app_id)
            if deployment and deployment.status.is_operational():
                scoring_js = app_def.js_code
                if not scoring_js:
                    logger.warning("No JS code for %s, skipping", app_def.app_id)
                    continue
                try:
                    await self.engine.load_intent(app_def.app_id, scoring_js)
                    loaded += 1
                    logger.info("Loaded intent: %s (%s)", app_def.app_id, app_def.name)
                except Exception as exc:
                    logger.error("Failed to load %s: %s", app_def.app_id, exc)
        logger.info("Loaded %d active intents", loaded)

    async def _rescan_loop(self) -> None:
        """Periodically rescan the store for newly deployed apps."""
        while True:
            await asyncio.sleep(15)
            try:
                self.store._load()  # re-read from disk
                loaded_ids = set(self.engine.list_loaded_intents())
                for app_def in self.store.list_apps():
                    if app_def.app_id in loaded_ids:
                        continue
                    deployment = self.store.get_deployment(app_def.app_id)
                    if deployment and deployment.status.is_operational():
                        scoring_js = app_def.js_code
                        if not scoring_js:
                            continue
                        await self.engine.load_intent(app_def.app_id, scoring_js)
                        logger.info("Hot-loaded new intent: %s (%s)", app_def.app_id, app_def.name)
            except Exception as exc:
                logger.error("Rescan error: %s", exc)

    async def _epoch_loop(self) -> None:
        """Periodically emit weights for the current champion solver."""
        while True:
            await asyncio.sleep(5)
            epoch_weights = self.weights.maybe_emit(self._champion_miner_id)
            if epoch_weights and self._weights_emitter and self._is_leader:
                try:
                    await self._weights_emitter.emit_async(epoch_weights)
                except Exception as exc:
                    logger.error("Weight emission failed: %s", exc)

    async def _leader_monitor_loop(self) -> None:
        """Watch for leader changes and transition between leader/follower."""
        force_leader = os.environ.get("FORCE_LEADER", "").strip() in ("1", "true", "yes")
        while True:
            await self._metagraph_sync.leader_changed.wait()
            self._metagraph_sync.leader_changed.clear()

            was_leader = self._is_leader
            new_is_leader = self._metagraph_sync.is_leader

            if force_leader and not new_is_leader:
                logger.info("FORCE_LEADER: ignoring demotion to follower")
                new_is_leader = True

            self._is_leader = new_is_leader

            if self._is_leader and not was_leader:
                await self._become_leader()
            elif not self._is_leader and was_leader:
                await self._become_follower()

            # Peer set refresh is owned by ProtocolConfig.refresh_loop, which
            # combines metagraph axon URLs with the on-chain ValidatorRegistry
            # and verifies each peer's /identity attestation. The old
            # keccak(hotkey)-derived EVM-address path that lived here never
            # actually matched the validators' real signing addresses; the
            # discovery loop replaces it.

    async def _become_leader(self) -> None:
        """Transition to leader role: start the block loop."""
        logger.info("Becoming LEADER — starting BlockLoop")
        self._load_orders_from_store()
        if not self.block_loop.running:
            self._block_loop_task = asyncio.create_task(self.block_loop.run_loop())

    async def _become_follower(self) -> None:
        """Transition to follower role: stop the block loop."""
        logger.info("Becoming FOLLOWER — stopping BlockLoop")
        self.block_loop.stop()

    # ── HTTP endpoints ───────────────────────────────────────────────────

    def _build_app(self) -> web.Application:
        app = web.Application()
        app.router.add_get("/health", self._handle_health)
        app.router.add_get("/intents/available", self._handle_available)
        app.router.add_post("/intents/{app_id}/submit", self._handle_submit)
        app.router.add_post("/reload", self._handle_reload)
        app.router.add_get("/weights", self._handle_weights)
        app.router.add_get("/weights/history", self._handle_weights_history)
        app.router.add_get("/blockloop/status", self._handle_blockloop_status)
        app.router.add_post("/orders/submit", self._handle_order_submit)
        app.router.add_get("/orders", self._handle_orders_list)
        app.router.add_get("/intents/{app_id}/details", self._handle_app_details)
        app.router.add_get("/intents/{app_id}/scores", self._handle_app_scores)
        # Quoting: dry-run the solver without creating an order
        app.router.add_post("/apps/{app_id}/quote", self._handle_quote)
        # Consensus endpoints
        app.router.add_post("/consensus/proposal", self._handle_consensus_proposal)
        app.router.add_get("/consensus/info", self._handle_consensus_info)
        app.router.add_get("/leader", self._handle_leader)
        # Self-attested identity for peer discovery
        app.router.add_get("/identity", self._handle_identity)
        return app

    async def _handle_health(self, request: web.Request) -> web.Response:
        loaded = self.engine.list_loaded_intents()
        ob_stats = self.orderbook.stats()
        return web.json_response({
            "status": "ok",
            "service": "app-intents-validator",
            "loaded_intents": len(loaded),
            "uptime_seconds": round(time.time() - self._start_time, 1),
            "block_loop_running": self.block_loop.running,
            "orderbook": ob_stats,
        })

    async def _handle_reload(self, request: web.Request) -> web.Response:
        """Trigger an immediate rescan for new deployed intents."""
        self.store._load()
        before = len(self.engine.list_loaded_intents())
        await self._load_active_intents()
        after = len(self.engine.list_loaded_intents())
        return web.json_response({
            "reloaded": True,
            "loaded_before": before,
            "loaded_after": after,
        })

    async def _handle_available(self, request: web.Request) -> web.Response:
        """Return active intents for miners to discover. No JS code exposed."""
        loaded_ids = self.engine.list_loaded_intents()
        intents = []
        for app_id in loaded_ids:
            app_def = self.store.get_app(app_id)
            if app_def is None:
                continue
            intents.append({
                "app_id": app_def.app_id,
                "name": app_def.name,
                "intent_type": app_def.intent_type,
                "description": app_def.description,
                "config": {
                    "supported_chains": app_def.config.supported_chains,
                    "trigger_type": app_def.config.trigger_type.value,
                    "max_gas": app_def.config.max_gas,
                },
            })
        return web.json_response({"intents": intents})

    async def _handle_submit(self, request: web.Request) -> web.Response:
        """Accept a miner plan submission and score it."""
        app_id = request.match_info["app_id"]

        # Validate intent is loaded
        if app_id not in self.engine.list_loaded_intents():
            return web.json_response(
                {"error": f"Intent not loaded: {app_id}"}, status=404
            )

        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON body"}, status=400)

        miner_id = body.get("miner_id", "unknown")
        plan_data = body.get("plan")
        params = body.get("params", {})

        if not plan_data:
            return web.json_response({"error": "plan is required"}, status=400)

        # Reconstruct ExecutionPlan from submitted data
        try:
            interactions = [
                Interaction(
                    target=ix.get("target", ""),
                    value=ix.get("value", "0"),
                    call_data=ix.get("call_data", ""),
                    chain_id=ix.get("chain_id", 1),
                )
                for ix in plan_data.get("interactions", [])
            ]
            plan = ExecutionPlan(
                intent_id=plan_data.get("intent_id", app_id),
                interactions=interactions,
                deadline=plan_data.get("deadline", 0),
                nonce=plan_data.get("nonce", 0),
                metadata=plan_data.get("metadata", {}),
            )
        except Exception as exc:
            return web.json_response(
                {"error": f"Invalid plan format: {exc}"}, status=400
            )

        # Build state and mock simulation
        app_def = self.store.get_app(app_id)
        deployment = self.store.get_deployment(app_id)
        if app_def is None or deployment is None:
            return web.json_response({"error": "App not found"}, status=404)

        state = _build_intent_state_from_params(app_def, deployment, params)
        simulation = build_mock_simulation(plan, params)

        # Score via JS engine
        try:
            score = await self.engine.score(app_id, plan, simulation, state)
        except Exception as exc:
            logger.error("Scoring error for %s: %s", app_id, exc)
            return web.json_response(
                {"error": "Scoring failed"}, status=500
            )

        # Record in store stats
        self.store.record_execution(
            app_id, score.score, success=score.valid and score.score >= 0.5
        )

        return web.json_response({
            "score": score.score,
            "valid": score.valid,
            "reason": score.reason,
            "breakdown": score.breakdown,
            "metadata": score.metadata,
        })

    async def _handle_weights(self, request: web.Request) -> web.Response:
        return web.json_response({
            "champion": self._champion_miner_id,
            "weights": self.weights.get_weights(self._champion_miner_id),
        })

    async def _handle_weights_history(self, request: web.Request) -> web.Response:
        return web.json_response({"history": self.weights.get_history()})

    async def _handle_blockloop_status(self, request: web.Request) -> web.Response:
        return web.json_response(self.block_loop.status())

    async def _handle_order_submit(self, request: web.Request) -> web.Response:
        """Submit an order directly to the validator's OrderBook."""
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON"}, status=400)

        try:
            order = self.orderbook.submit(
                app_id=body["app_id"],
                intent_function=body.get("intent_function", "execute"),
                params=body.get("params", {}),
                submitted_by=body.get("submitted_by", ""),
                chain_id=body.get("chain_id", 1),
                deadline=body.get("deadline", 0),
                perpetual=body.get("perpetual", False),
                max_executions=body.get("max_executions", 1),
                cooldown=body.get("cooldown", 0),
            )
            logger.info("Order submitted: %s for %s", order.order_id, order.app_id)
            return web.json_response(order.to_dict())
        except (KeyError, ValueError) as exc:
            return web.json_response({"error": str(exc)}, status=400)

    async def _handle_orders_list(self, request: web.Request) -> web.Response:
        """List orders in the validator's OrderBook."""
        app_id = request.query.get("app_id")
        status = request.query.get("status")
        orders = self.orderbook.list_orders(
            app_id=app_id or None,
            status=status or None,
        )
        return web.json_response({
            "orders": [o.to_dict() for o in orders],
            "count": len(orders),
        })

    async def _handle_app_details(self, request: web.Request) -> web.Response:
        """Return full app context for strategy generation (no JS code)."""
        app_id = request.match_info["app_id"]
        app_def = self.store.get_app(app_id)
        if app_def is None:
            return web.json_response({"error": f"App not found: {app_id}"}, status=404)

        deployment = self.store.get_deployment(app_id)

        # Get manifest from JS engine if loaded
        manifest = None
        if app_id in self.engine.list_loaded_intents():
            manifest = self.engine.get_manifest(app_id)

        result: dict[str, Any] = {
            "app_id": app_def.app_id,
            "name": app_def.name,
            "description": app_def.description,
            "intent_type": app_def.intent_type,
            "supported_chains": app_def.config.supported_chains,
            "config": {
                "trigger_type": app_def.config.trigger_type.value,
                "max_gas": app_def.config.max_gas,
                "score_threshold": app_def.config.score_threshold,
            },
            "solidity_code": app_def.solidity_code,
            "manifest": manifest,
            "contract_address": deployment.contract_address if deployment else None,
        }

        return web.json_response(result)

    async def _handle_app_scores(self, request: web.Request) -> web.Response:
        """Return execution stats for an app."""
        app_id = request.match_info["app_id"]
        app_def = self.store.get_app(app_id)
        if app_def is None:
            return web.json_response({"error": f"App not found: {app_id}"}, status=404)

        stats = self.store.get_stats(app_id)
        return web.json_response(stats)

    async def _handle_quote(self, request: web.Request) -> web.Response:
        """Compute a quote via the solver's quote() method — no simulation needed.

        POST /apps/{app_id}/quote
        Body: { params: {...}, chain_id: 1, slippage_bps: 50 }

        Returns estimated_output, suggested_min_output, gas_estimate.
        No order is created. No signature required. No simulation.
        """
        app_id = request.match_info["app_id"]

        app_def = self.store.get_app(app_id)
        if app_def is None:
            return web.json_response({"error": f"App not found: {app_id}"}, status=404)

        # Need a solver
        if self.block_loop.solver is None:
            return web.json_response(
                {"error": "No solver available — submit one via the git-based submission pipeline"},
                status=503,
            )

        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON body"}, status=400)

        params = body.get("params", {})
        chain_id = int(body.get("chain_id", 1))
        slippage_bps = max(0, min(int(body.get("slippage_bps", 50)), 10000))

        state = IntentState(
            contract_address="",
            chain_id=chain_id,
            nonce=0,
            owner="",
            raw_params=params,
        )

        # Solver builds its own data from RPC; no snapshot needed
        try:
            quote_result = self.block_loop.solver.quote(app_def, state)
        except NotImplementedError:
            return web.json_response(
                {"error": "Solver does not support quoting"}, status=501,
            )
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=400)
        except Exception as exc:
            return web.json_response(
                {"error": f"Solver quote failed: {exc}"}, status=500,
            )

        estimated_output = quote_result.estimated_output

        # Apply slippage
        suggested_min_output = "0"
        try:
            est_int = int(estimated_output)
            if est_int > 0:
                suggested_min_output = str(est_int * (10000 - slippage_bps) // 10000)
        except (ValueError, TypeError):
            pass

        # Build computed_params from manifest's quote-sourced param definitions
        intent_function = body.get("intent_function", "execute")
        quote_values = {
            "estimated_output": estimated_output,
            "suggested_min_output": suggested_min_output,
        }
        computed_params: dict[str, str] = dict(quote_result.computed_params)
        if hasattr(self.engine, "get_manifest"):
            try:
                manifest = self.engine.get_manifest(app_id)
                if manifest and "intent_functions" in manifest:
                    for fn_def in manifest["intent_functions"]:
                        if fn_def.get("name") == intent_function:
                            for pname, pdef in fn_def.get("params", {}).items():
                                if pdef.get("source") == "quote":
                                    qf = pdef.get("quote_field", "")
                                    if qf and qf in quote_values:
                                        computed_params[pname] = quote_values[qf]
                            break
            except Exception:
                pass

        return web.json_response({
            "app_id": app_id,
            "estimated_output": estimated_output,
            "suggested_min_output": suggested_min_output,
            "slippage_bps": slippage_bps,
            "route_summary": quote_result.route_summary,
            "gas_estimate": quote_result.gas_estimate,
            "valid_for_seconds": 30,
            "chain_id": chain_id,
            "computed_params": computed_params,
        })


    # ── Consensus HTTP endpoints ────────────────────────────────────────

    async def _handle_consensus_proposal(self, request: web.Request) -> web.Response:
        """Receive a proposal from the leader, re-score, sign, and return approval.

        Delegates to ProposalHandler for all verification, scoring, and signing.
        """
        return await self._proposal_handler.handle_proposal(request)

    async def _handle_consensus_info(self, request: web.Request) -> web.Response:
        """Return consensus identity and configuration."""
        info: dict[str, Any] = {
            "consensus_enabled": self._consensus is not None,
            "validator_id": self._validator_id or None,
        }
        if self._consensus is not None:
            info["quorum_bps"] = self._consensus.quorum_bps
            info["quorum_required"] = self._consensus.quorum_required
            info["validators"] = self._consensus.validators
        if self._peer_network is not None:
            info["peers"] = [
                {"validator_id": p.validator_id, "url": p.url}
                for p in self._peer_network.peers
            ]
        return web.json_response(info)

    async def _metagraph_peers_for_discovery(self):
        """Adapter: convert MetagraphSync's current state into the
        MetagraphPeer dataclass that peer_discovery expects.

        Returns an empty list when no metagraph sync has happened yet —
        ProtocolConfig.refresh_loop is fault-tolerant and will retry.
        """
        from minotaur_subnet.consensus.peer_discovery import MetagraphPeer
        if self._metagraph_sync is None or self._metagraph_sync.state is None:
            return []
        out = []
        for v in self._metagraph_sync.state.validators:
            if v.axon_url and v.hotkey:
                out.append(MetagraphPeer(hotkey=v.hotkey, axon_url=v.axon_url))
        return out

    async def _handle_identity(self, request: web.Request) -> web.Response:
        """Self-attested identity payload for peer discovery.

        Returns a fresh EIP-712 signature binding (evm_address, hotkey,
        axon_url) so other validators can verify this is the correct
        binding before adding us to their peer list.
        """
        if self._consensus is None:
            return web.json_response(
                {"error": "Consensus not enabled — no signing key"},
                status=503,
            )
        if self._metagraph_sync is None or not self._metagraph_sync.my_hotkey:
            return web.json_response(
                {"error": "No bittensor hotkey configured"},
                status=503,
            )
        axon_url = os.environ.get("VALIDATOR_AXON_URL", "").strip()
        if not axon_url:
            return web.json_response(
                {"error": "VALIDATOR_AXON_URL not configured"},
                status=503,
            )

        from minotaur_subnet.consensus.identity import sign_identity
        identity = sign_identity(
            self._consensus.private_key,
            self._metagraph_sync.my_hotkey,
            axon_url,
        )
        return web.json_response(identity.to_dict())

    async def _handle_leader(self, request: web.Request) -> web.Response:
        """Return leader status and metagraph info."""
        result: dict[str, Any] = {
            "leader": self._is_leader,
        }

        if self._metagraph_sync is not None and self._metagraph_sync.state is not None:
            state = self._metagraph_sync.state
            result["mode"] = "bittensor"
            result["my_uid"] = state.my_uid
            result["my_role"] = state.my_role
            result["block"] = state.block
            result["validator_count"] = len(state.validators)
            if state.leader:
                result["leader_hotkey"] = state.leader.hotkey
                result["leader_stake"] = state.leader.stake
        else:
            result["mode"] = "standalone"

        return web.json_response(result)


# ═══════════════════════════════════════════════════════════════════════════════
#                            ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════════════


def main() -> None:
    parser = argparse.ArgumentParser(description="App Intents Validator")
    parser.add_argument("--port", type=int, default=9100, help="Listen port")
    parser.add_argument(
        "--epoch-seconds", type=int, default=60, help="Epoch duration in seconds"
    )
    parser.add_argument(
        "--store-path", type=str, default=None, help="Path to store.json"
    )
    parser.add_argument(
        "--tick-interval", type=float, default=12.0,
        help="Block loop tick interval in seconds (default 12.0)",
    )
    # Bittensor integration
    parser.add_argument(
        "--subtensor-url", type=str, default=None,
        help="Subtensor WebSocket URL (e.g. ws://localhost:9944)",
    )
    parser.add_argument("--netuid", type=int, default=112, help="Subnet netuid")
    parser.add_argument("--wallet-name", type=str, default=None, help="BT wallet name")
    parser.add_argument("--hotkey-name", type=str, default=None, help="BT hotkey name")
    # Consensus
    parser.add_argument(
        "--validator-key", type=str, default="",
        help="Validator EVM private key (hex) for consensus signing",
    )
    parser.add_argument(
        "--validator-peers", type=str, nargs="*", default=None,
        help="Peer validators in addr@url format",
    )
    parser.add_argument(
        "--validator-registry-address", type=str, default=None,
        help="ValidatorRegistry contract address (source of canonical quorumBps). "
             "Falls back to VALIDATOR_REGISTRY_ADDRESS env.",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    # ── Security warnings for dangerous overrides ───────────────────────
    _resim_env = os.environ.get("FOLLOWER_PROPOSAL_RESIMULATE", "").strip().lower()
    if _resim_env in ("0", "false", "no", "off"):
        logger.warning(
            "SECURITY WARNING: FOLLOWER_PROPOSAL_RESIMULATE is explicitly disabled. "
            "Followers will trust leader simulation data without independent "
            "verification. This is UNSAFE for production — a compromised leader "
            "could fabricate simulation results."
        )

    _sig_env = os.environ.get("CONSENSUS_REQUIRE_SIGNED_PROPOSALS", "").strip().lower()
    if _sig_env in ("0", "false", "no", "off"):
        logger.warning(
            "SECURITY WARNING: CONSENSUS_REQUIRE_SIGNED_PROPOSALS is disabled. "
            "The /consensus/proposal endpoint will accept unsigned proposals "
            "from any HTTP client. This is UNSAFE for production."
        )

    # Also check environment variables as fallback
    subtensor_url = args.subtensor_url or os.environ.get("SUBTENSOR_URL")
    netuid = args.netuid if args.netuid != 112 else int(os.environ.get("NETUID", "112"))
    wallet_name = args.wallet_name or os.environ.get("WALLET_NAME")
    hotkey_name = args.hotkey_name or os.environ.get("HOTKEY_NAME")
    validator_hotkey_ss58 = os.environ.get("VALIDATOR_HOTKEY_SS58", "").strip()
    validator_key = args.validator_key or os.environ.get("VALIDATOR_PRIVATE_KEY", "")

    validator_peers = args.validator_peers
    if validator_peers is None:
        peers_env = os.environ.get("VALIDATOR_PEERS", "")
        if peers_env:
            validator_peers = peers_env.split(",")

    store_path = Path(args.store_path) if args.store_path else None
    store = AppIntentStore(store_path=store_path)

    contract_address = os.environ.get("SWAP_APP_ADDRESS", "") or os.environ.get("APP_INTENT_BASE_31337", "")
    if not contract_address:
        contract_address = "0x" + "00" * 20
    chain_id = int(os.environ.get("CHAIN_ID", "31337"))

    # ── Load canonical quorum from ValidatorRegistry ───────────────────
    # Only loaded when consensus is enabled (validator_key is set). Solo /
    # standalone validators don't need it.
    protocol_config = None
    if validator_key:
        registry_address = (
            args.validator_registry_address
            or os.environ.get("VALIDATOR_REGISTRY_ADDRESS", "").strip()
            or os.environ.get(f"VALIDATOR_REGISTRY_{chain_id}", "").strip()
        )
        if not registry_address:
            raise SystemExit(
                "Consensus enabled but no ValidatorRegistry address provided. "
                f"Set --validator-registry-address, VALIDATOR_REGISTRY_ADDRESS, "
                f"or VALIDATOR_REGISTRY_{chain_id}."
            )
        anvil_rpc = (
            os.environ.get("ANVIL_RPC_URL")
            or os.environ.get("BASE_RPC_URL")
            or "http://localhost:8545"
        )
        protocol_config = ProtocolConfig.from_validator_registry(
            rpc_url=anvil_rpc,
            registry_address=registry_address,
        )

    validator = AppIntentsValidator(
        store=store,
        port=args.port,
        epoch_seconds=args.epoch_seconds,
        tick_interval=args.tick_interval,
        subtensor_url=subtensor_url,
        netuid=netuid,
        wallet_name=wallet_name,
        hotkey_name=hotkey_name,
        validator_hotkey_ss58=validator_hotkey_ss58,
        validator_private_key=validator_key,
        validator_peers=validator_peers,
        protocol_config=protocol_config,
        chain_id=chain_id,
        contract_address=contract_address,
    )
    asyncio.run(validator.start())


if __name__ == "__main__":
    main()
