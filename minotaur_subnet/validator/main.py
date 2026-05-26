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
# build_mock_simulation + build_bootstrap_or_champion_weights imports removed
# 2026-05-25: their only callers in this module (_handle_submit,
# _handle_app_details, etc.) were deleted in the validator-surface cleanup.

# Extracted modules
from minotaur_subnet.validator.weight_policy import ChampionWeights
from minotaur_subnet.validator.scoring_engine import ScoringEngine
from minotaur_subnet.validator.proposal_handler import ProposalHandler

import os

logger = logging.getLogger("minotaur_subnet.validator")


# ── /consensus/proposal rate limiter (audit H1) ─────────────────────────
# Per-IP token bucket. A flooded follower can stall the consensus loop and
# starve the JS sandbox; we cap inbound proposal traffic before the body
# is even parsed so a malicious/buggy leader cannot trivially saturate us.
# Burst 30, refill 1/s — comfortably above real-traffic peak (~3/12s tick).
_PROPOSAL_RATE: dict[str, tuple[float, float]] = {}  # ip -> (last_refill_ts, tokens)
_PROPOSAL_RATE_LOCK = asyncio.Lock()
_PROPOSAL_RATE_CAPACITY = 30        # burst capacity (proposals)
_PROPOSAL_RATE_REFILL_PER_SEC = 1.0  # steady-state refill rate


@web.middleware
async def _proposal_rate_limit(request, handler):
    if request.path == "/consensus/proposal":
        ip = request.headers.get("X-Real-IP") or request.remote or "unknown"
        async with _PROPOSAL_RATE_LOCK:
            now = time.monotonic()
            last_refill, tokens = _PROPOSAL_RATE.get(
                ip, (now, float(_PROPOSAL_RATE_CAPACITY))
            )
            tokens = min(
                float(_PROPOSAL_RATE_CAPACITY),
                tokens + (now - last_refill) * _PROPOSAL_RATE_REFILL_PER_SEC,
            )
            if tokens < 1.0:
                _PROPOSAL_RATE[ip] = (now, tokens)
                logger.warning(
                    "Rate-limited /consensus/proposal from %s (tokens=%.2f)",
                    ip, tokens,
                )
                return web.json_response(
                    {"error": "rate_limited", "ip": ip}, status=429,
                )
            tokens -= 1.0
            _PROPOSAL_RATE[ip] = (now, tokens)
    return await handler(request)


def _auto_serve_axon_on_metagraph(
    *,
    subtensor: Any,
    bt_module: Any,
    wallet: Any,
    netuid: int,
    my_hotkey: str,
    axon_url: str,
) -> None:
    """Publish ``axon_url`` on the subnet metagraph via ``serve_axon``.

    Idempotent + rate-limit aware:

    * Resolves the hostname to a numeric IP and checks the metagraph
      entry for ``my_hotkey``. If the existing entry already matches
      ``ip:port``, skips the on-chain call entirely. Otherwise the chain
      rate-limits ``serve_axon`` per hotkey (~50 blocks / 10 min on
      finney) and every restart inside that window throws ``Custom
      error: 12 (ServingRateLimitExceeded)`` even when the desired entry
      is already in place.

    * If ``serve_axon`` is invoked and the chain replies with the rate-
      limit error anyway (e.g. another process re-served within the
      window), logs INFO and returns — the previous entry is still in
      effect, no action is needed.

    * Any other failure is caught and downgraded to a warning so startup
      proceeds.

    Designed as a module-level helper so it can be unit-tested with mock
    ``subtensor`` / ``bt_module`` objects without spinning up the full
    validator.
    """
    from urllib.parse import urlparse
    import socket

    parsed = urlparse(axon_url)
    axon_ip = parsed.hostname or ""
    axon_port = parsed.port or 9100
    if not axon_ip:
        logger.warning(
            "VALIDATOR_AXON_URL %r has no hostname; skipping serve_axon",
            axon_url,
        )
        return

    # Resolve DNS once so the idempotency comparison against the chain's
    # numeric ip storage actually matches.
    try:
        resolved_ip = socket.gethostbyname(axon_ip)
    except OSError as exc:
        logger.warning(
            "Could not resolve VALIDATOR_AXON_URL host %r: %s — "
            "falling through to serve_axon and letting the chain decide",
            axon_ip, exc,
        )
        resolved_ip = axon_ip

    # Idempotency pre-check: read the metagraph and skip serve_axon
    # entirely when the published entry already matches.
    try:
        metagraph = subtensor.metagraph(netuid)
        hotkeys = list(getattr(metagraph, "hotkeys", []) or [])
        axons = list(getattr(metagraph, "axons", []) or [])
        for uid, hk in enumerate(hotkeys):
            if hk != my_hotkey:
                continue
            if uid >= len(axons):
                break
            existing = axons[uid]
            existing_ip = str(getattr(existing, "ip", "") or "")
            existing_port = int(getattr(existing, "port", 0) or 0)
            if existing_ip == resolved_ip and existing_port == axon_port:
                logger.info(
                    "Axon entry on metagraph already matches "
                    "VALIDATOR_AXON_URL (ip=%s port=%d) — skipping "
                    "serve_axon to avoid the on-chain rate limit",
                    resolved_ip, axon_port,
                )
                return
            logger.info(
                "Metagraph axon for this hotkey is %s:%d, want %s:%d — "
                "calling serve_axon to update",
                existing_ip, existing_port, resolved_ip, axon_port,
            )
            break
        else:
            logger.info(
                "Hotkey not yet on metagraph for netuid=%d — calling "
                "serve_axon for the first time", netuid,
            )
    except Exception as exc:
        logger.debug(
            "Metagraph idempotency check failed (%s); falling through "
            "to serve_axon", exc,
        )

    try:
        served = subtensor.serve_axon(
            netuid=netuid,
            axon=bt_module.Axon(wallet=wallet, ip=axon_ip, port=axon_port),
        )
        logger.info(
            "Auto-served axon on metagraph (netuid=%d ip=%s port=%d ok=%s)",
            netuid, axon_ip, axon_port, served,
        )
    except Exception as exc:
        msg = str(exc)
        if "Custom error: 12" in msg or "ServingRateLimitExceeded" in msg:
            logger.info(
                "serve_axon rate-limited by chain (ServingRateLimitExceeded "
                "/ Custom error: 12). The previous axon entry is still in "
                "effect — no action needed. The next call will succeed "
                "after the rate-limit window (~50 blocks on finney).",
            )
            return
        logger.warning(
            "Auto-serve axon failed (continuing startup): %s. "
            "Other validators won't find you on the metagraph until a "
            "successful serve_axon — re-check VALIDATOR_AXON_URL, "
            "coldkey TAO balance, and subtensor reachability.",
            exc,
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
        protocol_config: "ProtocolConfig | None" = None,
        chain_id: int = 31337,
        contract_address: str = "0x" + "00" * 20,
        # Follower app catalog sync (optional)
        leader_api_url: str = "",
        app_sync_poll_interval: float = 60.0,
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

                    # Auto-publish the axon URL on the metagraph. Bittensor
                    # convention is for the validator daemon to call
                    # ``serve_axon`` on startup so other validators' peer-
                    # discovery loops can find us via ``metagraph.axons``.
                    # Without this, the metagraph row stays at ``0.0.0.0:0``
                    # and the discovery loop logs "no metagraph peers with
                    # axon URLs" indefinitely. Gated on VALIDATOR_AXON_URL
                    # being set — operators that prefer to publish out-of-
                    # band can leave it unset and serve manually.
                    axon_url = os.environ.get("VALIDATOR_AXON_URL", "").strip()
                    if axon_url:
                        _auto_serve_axon_on_metagraph(
                            subtensor=subtensor,
                            bt_module=bt,
                            wallet=self._bt_wallet,
                            netuid=netuid,
                            my_hotkey=resolved_hotkey,
                            axon_url=axon_url,
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

            from minotaur_subnet.consensus.peer_network import ValidatorPeerNetwork
            from minotaur_subnet.consensus import ConsensusManager

            # ConsensusManager.validators reads through to
            # protocol_config.peers, so newly discovered peers are picked up
            # automatically without restart on the next refresh tick.
            self._consensus = ConsensusManager(
                validator_id=self._validator_id,
                private_key=validator_private_key,
                protocol_config=protocol_config,
                chain_id=chain_id,
                contract_address=contract_address,
            )
            self.block_loop.set_consensus(self._consensus)

            self._peer_network = ValidatorPeerNetwork(
                validator_id=self._validator_id,
                private_key=validator_private_key,
                consensus=self._consensus,
                protocol_config=protocol_config,
            )
            self.block_loop.set_peer_network(self._peer_network)

            logger.info(
                "Consensus enabled (id=%s, peer-mode=discovered, quorum=%d bps)",
                self._validator_id[:10], protocol_config.quorum_bps,
            )

            # Wire peer discovery: when a metagraph exists, tell
            # ProtocolConfig how to fetch the current metagraph peer list.
            # The refresh loop calls this each tick and probes /identity on
            # each axon.
            if self._metagraph_sync is not None:
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

        # ── Follower app catalog sync ────────────────────────────────────
        # Pulls AppIntentDefinition + DeploymentResult from the leader's
        # API so this validator can re-score proposals. Required for any
        # validator that doesn't receive create_app / deploy_app calls
        # directly (every third-party validator). Leaders should leave
        # LEADER_API_URL unset.
        self._app_sync = None
        if leader_api_url:
            from minotaur_subnet.validator.app_sync import ValidatorAppCatalogSync
            self._app_sync = ValidatorAppCatalogSync(
                store=self.store,
                leader_url=leader_api_url,
                poll_interval=app_sync_poll_interval,
            )
            logger.warning(
                "SECURITY NOTICE: App catalog sync enabled (leader=%s). "
                "JS scoring code is fetched from the leader and trusted "
                "as-is — there is no on-chain hash anchor. A compromised "
                "leader could push malicious JS to followers. Tracked "
                "follow-up: AppRegistry JS hash anchoring.",
                leader_api_url,
            )

    async def start(self) -> None:
        """Load active intents, start block loop, and start HTTP server."""
        if self._consensus is not None:
            logger.info(
                "Validator starting as ORDER CONSENSUS PEER — "
                "will re-simulate and re-score proposals from leader"
            )
        # Pull the leader's catalog before loading intents so the first
        # _load_active_intents pass sees the JS. If the leader is
        # unreachable, sync.start() logs and the background loop retries.
        if self._app_sync is not None:
            await self._app_sync.start()
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
        """Validator daemon HTTP surface.

        Three load-bearing routes drive the consensus loop:
          - GET  /health             Docker healthcheck
          - GET  /identity           peer cross-attestation (consensus.peer_discovery)
          - POST /consensus/proposal leader → follower proposal handoff (consensus.peer_network)

        Five ops-debug routes are kept for operator inspection — no automated
        callers reach them, but they're useful for ``curl``-from-inside-container
        debugging of emission, blockloop progress, and consensus identity:
          - GET /weights
          - GET /weights/history
          - GET /blockloop/status
          - GET /consensus/info
          - GET /leader

        Eight pre-OrderBook / duplicate routes were removed 2026-05-25 audit
        cleanup: /intents/{available,*/submit,*/details,*/scores}, /reload,
        /orders, /orders/submit, /apps/*/quote. Each had 0 cross-codebase
        callers; the api at port 8080 carries the equivalent live endpoints.
        """
        # client_max_size=64 KiB caps inbound bodies (audit H1). Real
        # consensus proposals are ~1-5 KB; aiohttp default of 1 MiB lets
        # a leader flood us with megabytes per request before any handler
        # logic runs.
        app = web.Application(
            middlewares=[_proposal_rate_limit],
            client_max_size=64 * 1024,
        )
        # Load-bearing
        app.router.add_get("/health", self._handle_health)
        app.router.add_get("/identity", self._handle_identity)
        app.router.add_post("/consensus/proposal", self._handle_consensus_proposal)
        # Ops-debug
        app.router.add_get("/weights", self._handle_weights)
        app.router.add_get("/weights/history", self._handle_weights_history)
        app.router.add_get("/blockloop/status", self._handle_blockloop_status)
        app.router.add_get("/consensus/info", self._handle_consensus_info)
        app.router.add_get("/leader", self._handle_leader)
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

    async def _handle_weights(self, request: web.Request) -> web.Response:
        return web.json_response({
            "champion": self._champion_miner_id,
            "weights": self.weights.get_weights(self._champion_miner_id),
        })

    async def _handle_weights_history(self, request: web.Request) -> web.Response:
        return web.json_response({"history": self.weights.get_history()})

    async def _handle_blockloop_status(self, request: web.Request) -> web.Response:
        return web.json_response(self.block_loop.status())

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
    # Operator-friendly env check — runs before argparse so a missing
    # .env file produces a clear "you forgot cp .env.example .env"
    # message instead of the deeper "ValidatorRegistry address not
    # provided" crash loop that confused multiple new operators in May.
    from minotaur_subnet.shared.env_check import (
        REQUIRED_REGISTRY_ENV,
        check_required_env_or_exit,
    )
    check_required_env_or_exit(
        REQUIRED_REGISTRY_ENV,
        process_name="validator daemon",
    )

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
        "--validator-registry-address", type=str, default=None,
        help="ValidatorRegistry contract address (source of canonical quorumBps). "
             "Falls back to VALIDATOR_REGISTRY_ADDRESS env.",
    )
    parser.add_argument(
        "--leader-api-url", type=str, default=None,
        help="Leader API base URL to sync the app catalog from (e.g. "
             "https://api.minotaursubnet.com). Required for follower "
             "validators that don't receive create_app / deploy_app calls "
             "directly. Falls back to LEADER_API_URL env.",
    )
    parser.add_argument(
        "--app-sync-interval", type=float, default=60.0,
        help="Seconds between app catalog sync ticks (default 60).",
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
        # Read consensus state from the live upstream chain — never from
        # the local Anvil fork. Anvil snapshots upstream state at the
        # fork point and never re-fetches; an on-chain updateValidators
        # is invisible to the fork until it's recycled. See
        # consensus.protocol_config.consensus_chain_rpc_url for the
        # per-chain env resolution.
        from minotaur_subnet.consensus.protocol_config import (
            consensus_chain_rpc_url,
        )
        consensus_rpc = consensus_chain_rpc_url(chain_id)
        protocol_config = ProtocolConfig.from_validator_registry(
            rpc_url=consensus_rpc,
            registry_address=registry_address,
        )

    leader_api_url = (
        args.leader_api_url
        if args.leader_api_url is not None
        else os.environ.get("LEADER_API_URL", "")
    ).strip()

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
        protocol_config=protocol_config,
        chain_id=chain_id,
        contract_address=contract_address,
        leader_api_url=leader_api_url,
        app_sync_poll_interval=args.app_sync_interval,
    )
    asyncio.run(validator.start())


if __name__ == "__main__":
    main()
