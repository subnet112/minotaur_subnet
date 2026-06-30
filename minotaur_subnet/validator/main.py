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

from minotaur_subnet.weight_policy import (
    CHAMPION_MINER_WEIGHT_FLOOR,
    ORDERS_FOR_FULL_EMISSION,
    apply_champion_burn_ramp,
    champion_miner_weight_fraction,
)

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
        # Pass external_ip + external_port EXPLICITLY. bittensor's Axon
        # class has separate ``ip`` (bind address) and ``external_ip``
        # (what gets published to the metagraph) parameters. If
        # external_ip isn't passed, bittensor's auto-detection takes
        # over and queries an outbound IP service (or a cached value),
        # which on hosts that switched IPs returns the OLD address. We
        # observed exactly that on a third-party validator (2026-05-27):
        # serve_axon submitted ip=920929821 (=54.228.70.29, the OLD IP)
        # while the daemon's log said ip=52.17.102.181 (the NEW one).
        # The data field on the extrinsic response confirmed
        # ``external_ip: '54.228.70.29'`` — bittensor's auto-detect
        # silently overrode VALIDATOR_AXON_URL. Use ``resolved_ip`` (the
        # already-DNS-resolved hostname from VALIDATOR_AXON_URL) for
        # both fields so the chain entry matches what the operator
        # actually asked for.
        served = subtensor.serve_axon(
            netuid=netuid,
            axon=bt_module.Axon(
                wallet=wallet,
                ip=axon_ip,
                port=axon_port,
                external_ip=resolved_ip,
                external_port=axon_port,
            ),
        )
        logger.info(
            "Auto-served axon on metagraph (netuid=%d external_ip=%s "
            "external_port=%d ok=%s)",
            netuid, resolved_ip, axon_port, served,
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
        # The champion for weight emission is read from THIS node's CO-LOCATED API
        # (GET /v1/solver/champion) — the single source of truth that ran the
        # benchmark/consensus and adopted. NEVER from chain or the public leader, so a 3rd
        # party can't free-ride on a published answer. Default targets the compose service
        # name; override with CHAMPION_API_URL for a non-standard topology.
        champion_api_url: str = "http://api:8080",
    ) -> None:
        self.store = store
        self.port = port
        self._subtensor_url = subtensor_url
        self.engine = JsExecutionEngine(timeout_ms=10000)
        self.weights = ChampionWeights(
            epoch_seconds=epoch_seconds,
            owner_hotkey=os.environ.get("SUBNET_OWNER_HOTKEY", "")
            or os.environ.get("OWNER_HOTKEY", ""),
        )

        # Champion source for weight emission: THIS node's co-located API's single source
        # of truth (GET /v1/solver/champion), read with a bounded last-known-good memo so a
        # transient API restart never flips a standing champion to 100% burn. The validator
        # holds NO champion state of its own — see docs/architecture/state-consolidation.md.
        from minotaur_subnet.validator.champion_client import ChampionResolver

        self._champion_resolver = ChampionResolver(
            champion_api_url, memo_ttl_seconds=max(2.0 * float(epoch_seconds), 600.0),
        )
        self._champion_source = "init"
        logger.info(
            "Weight emission champion: co-located API %s (GET /v1/solver/champion)",
            champion_api_url,
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
        # FORCE_SOLVER_IMAGE (operator break-glass) wins over GENESIS_SOLVER_IMAGE.
        solver = None
        from minotaur_subnet.harness.runtime_solver import resolve_boot_solver_image
        _boot_image, _boot_forced = resolve_boot_solver_image()
        if _boot_image:
            try:
                from minotaur_subnet.harness.runtime_solver import DockerRuntimeSolver
                import asyncio
                solver = asyncio.get_event_loop().run_until_complete(
                    DockerRuntimeSolver.create(
                        image_ref=_boot_image,
                        chain_ids=chain_ids or [31337],
                        rpc_urls=rpc_urls,
                        bridge_registry=bridge_registry,
                    )
                )
                if _boot_forced:
                    logger.warning(
                        "FORCE_SOLVER_IMAGE override ACTIVE — live solver pinned to %s "
                        "(break-glass; clear FORCE_SOLVER_IMAGE to resume normal resolution)",
                        _boot_image,
                    )
                else:
                    logger.info("Genesis solver initialized via Docker (%s)", _boot_image)
            except Exception as exc:
                logger.warning("Live solver Docker boot unavailable (%s): %s", _boot_image, exc)
        else:
            logger.info("No FORCE_SOLVER_IMAGE / GENESIS_SOLVER_IMAGE — solver unavailable until champion is adopted")

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
        # Live order-volume emission ramp signal (updated each emit; surfaced
        # on /health). Floor share until the first emit measures order volume.
        self._orders_24h: int = 0
        self._miner_weight_fraction: float = CHAMPION_MINER_WEIGHT_FLOOR

        # ── Bittensor integration ────────────────────────────────────────
        self._metagraph_sync = None
        self._weights_emitter = None
        self._bt_wallet = None
        self._is_leader = True  # Default: standalone = always leader
        self._metagraph_task: asyncio.Task | None = None
        self._leader_monitor_task: asyncio.Task | None = None
        # Stored references for the periodic axon-resync loop. Populated
        # in initialize() inside the ``if subtensor_url:`` block alongside
        # the first serve_axon call. The loop uses them to call
        # _auto_serve_axon_on_metagraph() on a timer for operators whose
        # public IP rotates (eg AWS ELB).
        self._bt_subtensor: Any = None
        self._bt_module: Any = None
        self._bt_netuid: int | None = None
        self._validator_axon_url: str = ""
        self._axon_resync_task: asyncio.Task | None = None
        # Last weight-emit attempt — surfaced in /health for self-diagnosis.
        # Updated atomically inside _epoch_loop after every emit attempt
        # (success OR failure). None until the first attempt completes;
        # /health treats that as "never attempted". The error string is
        # truncated to 300 chars before exposure — chain/substrate error
        # messages don't typically carry secrets, but defense in depth.
        #
        # Persisted to disk on every update so a Watchtower restart doesn't
        # wipe the most recent attestation. Pre-fix, the validator-health
        # workflow's "self vs external" classifier would false-positive
        # an external attribution when chain showed a recent set_weights
        # but the in-memory state had just been reset by a container
        # recreate. See _persist_last_emit_state() for the write path.
        self._last_emit_state: dict | None = None
        self._last_emit_state_path = os.environ.get(
            "LAST_EMIT_STATE_PATH", "/data/last_emit.json",
        )
        # last_successful_emit: only advances on a SUCCESSFUL set_weights.
        # The validator-health classifier keys off THIS, not _last_emit_state
        # (the latest attempt) — a transient/rate-limited retry overwrites
        # _last_emit_state with an error and would otherwise mask a perfectly
        # healthy validator that just set weights minutes ago. The real
        # health question is "are enough weight-sets succeeding to keep this
        # validator stable?", which only successes answer.
        self._last_successful_emit_state: dict | None = None
        self._last_successful_emit_state_path = os.environ.get(
            "LAST_SUCCESSFUL_EMIT_STATE_PATH", "/data/last_successful_emit.json",
        )
        self._restore_last_emit_state()

        # ── Internal RPC: per-miner weight queue (from api EpochManager) ──
        # Single-slot, newest-wins, in-process. The api process can POST a
        # per-miner ranking here when a solver round closes; _epoch_loop
        # consumes it on the next tick and emits via the same WeightsEmitter
        # that handles the burn fallback. This is the ONLY supported way
        # for any other process to drive chain weights — there is exactly
        # one set_weights caller per validator host (this daemon's
        # _epoch_loop), eliminating the race condition that would otherwise
        # exist between burn fallback and per-miner ranking.
        #
        # If the slot is empty when _epoch_loop ticks, burn fallback fires.
        # Burn is therefore unconditionally available as a safety net: any
        # failure of the queue path (auth misconfig, api down, network
        # partition, etc.) just leaves the slot empty, and burn covers it.
        self._queued_weights_mapping: dict[str, float] | None = None
        self._queued_weights_source: str | None = None

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

                # Resolve owner_hotkey CHAIN-PRIMARY (env only as fallback).
                # The subnet owner is public on-chain data — authoritative and
                # identical for every validator — so the chain is queried FIRST
                # and overrides the env value when present. Requiring the env was
                # a misconfig hazard that silently broke weight emission for
                # third-party operators on canonical compose (they had no
                # SUBNET_OWNER_HOTKEY set and their daemon emitted {} → silent
                # dead loop every epoch). The env stays only as a fallback for
                # environments where the chain isn't queryable (e.g. a local
                # testnet without the storage set).
                from minotaur_subnet.weight_policy import lookup_subnet_owner_from_chain
                chain_owner = lookup_subnet_owner_from_chain(subtensor, netuid)
                if chain_owner:
                    self.weights.owner_hotkey = chain_owner
                    logger.info(
                        "Resolved subnet %d owner hotkey from chain: %s…",
                        netuid, chain_owner[:16],
                    )
                elif not self.weights.owner_hotkey:
                    logger.warning(
                        "No SUBNET_OWNER_HOTKEY env AND chain lookup failed; "
                        "weight emission will be empty until either resolves",
                    )

                if wallet_name and hotkey_name:
                    try:
                        from minotaur_subnet.shared.bt_wallet import load_hotkey_wallet
                        # Honour BT_WALLET_PATH and log an actionable diagnostic on
                        # failure instead of a bare "wallet load failed" — the most
                        # common cause is a mount the SDK's $HOME/.bittensor default
                        # doesn't see, or one uid 1000 can't read.
                        self._bt_wallet = load_hotkey_wallet(wallet_name, hotkey_name)
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
                        # Pass the URL so a failed emit can rebuild the client —
                        # the emitter holds ONE long-lived Subtensor whose ws goes
                        # stale on an RPC rotation and otherwise needs a restart.
                        subtensor_url=subtensor_url,
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

                    # Stash the bittensor handles so the periodic axon-resync
                    # loop (started later in initialize()) can reuse them
                    # without re-creating the Subtensor client each tick.
                    self._bt_subtensor = subtensor
                    self._bt_module = bt
                    self._bt_netuid = netuid
                    self._validator_axon_url = axon_url

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
                # Seed the local epoch clock from the AUTHORITATIVE chain state.
                # Without this, every container restart silently delays the
                # next set_weights by a full epoch_seconds window — even when
                # our on-chain last_update would already permit an immediate
                # emit. A stale validator that pulls a new image now resumes
                # emitting on the FIRST epoch tick after restart instead of
                # waiting another 20 min cold.
                if state.my_last_update_block is not None and state.my_last_update_block > 0:
                    # Mainnet: 12s/block. Local testnet (anvil-style): 0.25s.
                    # Subtensor URL substring is the same heuristic the
                    # WeightsEmitter uses for its own block_time arg.
                    block_time = 0.25 if (
                        "localhost" in (self._subtensor_url or "")
                        or "127.0.0.1" in (self._subtensor_url or "")
                    ) else 12.0
                    blocks_since = max(0, state.block - state.my_last_update_block)
                    self.weights.seed_epoch_clock_from_last_emit(blocks_since * block_time)
                elif state.my_last_update_block == 0 and state.my_uid is not None:
                    # Registered but never emitted — emit immediately on first
                    # tick. Passing > epoch_seconds guarantees the clock has
                    # "already elapsed" for the next maybe_emit call.
                    self.weights.seed_epoch_clock_from_last_emit(
                        self.weights.epoch_seconds + 1
                    )
                # If my_uid is None (unregistered), leave the clock at its
                # process-start value — we shouldn't be emitting anyway until
                # we're registered.
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

        # Periodic axon re-resync. Re-resolves VALIDATOR_AXON_URL on a
        # timer and calls serve_axon if the resolved IP drifted from
        # what's currently on the metagraph. Required for operators
        # behind dynamic-IP setups (AWS ELB/ALB, Cloudflare proxy,
        # rotating residential IPs, etc.) — without this they have to
        # restart the daemon every time their public IP rotates, which
        # they may not even detect. Gated on having both a bittensor
        # subtensor handle AND a configured VALIDATOR_AXON_URL.
        if self._bt_subtensor is not None and self._validator_axon_url:
            self._axon_resync_task = asyncio.create_task(self._axon_resync_loop())

        # Keep running
        while True:
            await asyncio.sleep(3600)

    def _restore_last_emit_state(self) -> None:
        """Reload persisted emit state from disk so a restart preserves it.

        Restores BOTH the latest-attempt state (``_last_emit_state``) and
        the last-*successful*-emit state (``_last_successful_emit_state``).
        Pre-fix, every Watchtower restart wiped these in-memory fields, so
        the next /health probe returned them null even though chain still
        showed a recent set_weights — the validator-health classifier then
        false-flagged the validator. Persisting the success across restarts
        keeps the "emitting often enough?" signal intact.

        Best-effort: missing/malformed/unreadable files leave the state at
        None and the next emit re-establishes it. Never raises.
        """
        self._last_emit_state = self._read_persisted_state(
            self._last_emit_state_path, "last_emit",
        )
        self._last_successful_emit_state = self._read_persisted_state(
            self._last_successful_emit_state_path, "last_successful_emit",
        )
        # First-upgrade seed: on the very first run of the image that added
        # last_successful_emit there is no persisted success file yet, but a
        # restored last_emit with result="ok" IS a real recent success.
        # Adopt it so the daemon doesn't spend ~one epoch reporting null —
        # which the health classifier would read as a (false) "external"
        # until the next emit lands. After the first success this file
        # exists and the seed never fires again.
        if (
            self._last_successful_emit_state is None
            and self._last_emit_state is not None
            and self._last_emit_state.get("result") == "ok"
        ):
            self._last_successful_emit_state = dict(self._last_emit_state)

    @staticmethod
    def _read_persisted_state(path: str, label: str) -> dict | None:
        try:
            import json as _json
            with open(path, "r") as f:
                restored = _json.load(f)
            if isinstance(restored, dict) and "attempted_at" in restored:
                logger.info(
                    "Restored %s state from %s (attempted_at=%s, result=%s, source=%s)",
                    label, path, restored.get("attempted_at"),
                    restored.get("result"), restored.get("source", "unknown"),
                )
                return restored
        except FileNotFoundError:
            pass
        except Exception as exc:
            logger.warning("Could not restore %s state from %s: %s", label, path, exc)
        return None

    def _persist_last_emit_state(self) -> None:
        """Write emit state to disk so a restart preserves it.

        Persists BOTH ``_last_emit_state`` (latest attempt) and
        ``_last_successful_emit_state`` (last success). Best-effort: a
        persistence failure logs a single warning and never crashes the
        emit path — the chain emit already happened.
        """
        self._write_persisted_state(
            self._last_emit_state, self._last_emit_state_path, "last_emit",
        )
        self._write_persisted_state(
            self._last_successful_emit_state,
            self._last_successful_emit_state_path,
            "last_successful_emit",
        )

    @staticmethod
    def _write_persisted_state(state: dict | None, path: str, label: str) -> None:
        if state is None:
            return
        try:
            import json as _json
            import os as _os
            _os.makedirs(_os.path.dirname(path) or ".", exist_ok=True)
            tmp_path = path + ".tmp"
            with open(tmp_path, "w") as f:
                _json.dump(state, f)
            _os.replace(tmp_path, path)
        except Exception as exc:
            logger.warning(
                "Could not persist %s state to %s: %s",
                label, path, exc,
            )

    async def _axon_resync_loop(self) -> None:
        """Periodically re-resolve VALIDATOR_AXON_URL and re-publish on
        chain if the IP changed.

        Composes with _auto_serve_axon_on_metagraph's existing checks:
        the helper already (a) DNS-resolves the URL, (b) skips serve_axon
        when the metagraph entry already matches, and (c) treats
        Bittensor's per-hotkey serve_axon rate limit (~50 blocks / 10 min
        on finney) as a benign no-op. So this loop just calls it on a
        timer — the helper deduplicates work and won't spam the chain.

        Default cadence 5 min (``AXON_RESYNC_INTERVAL_SECONDS``).
        Minimum 60s — operators tempted to go faster aren't beating the
        chain's rate limit, so it's purely log spam. Setting the env to
        0 (or negative) disables the loop entirely for operators with
        truly static IPs who don't want any background chain reads.
        """
        try:
            interval = int(os.environ.get("AXON_RESYNC_INTERVAL_SECONDS", "300"))
        except ValueError:
            interval = 300
        if interval <= 0:
            logger.info(
                "AXON_RESYNC_INTERVAL_SECONDS<=0; periodic axon resync disabled",
            )
            return
        interval = max(60, interval)
        logger.info(
            "Periodic axon resync loop started (interval=%ds, VALIDATOR_AXON_URL=%s)",
            interval, self._validator_axon_url,
        )
        # MetagraphSync.my_hotkey is the only authoritative copy of the
        # hotkey ss58 after wallet load; read it each tick rather than
        # caching so it survives a (hypothetical) hotkey-rotate restart.
        while True:
            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                raise
            try:
                my_hotkey = getattr(self._metagraph_sync, "my_hotkey", "")
                if not my_hotkey:
                    logger.debug(
                        "Axon resync skipped — metagraph_sync has no my_hotkey yet",
                    )
                    continue
                _auto_serve_axon_on_metagraph(
                    subtensor=self._bt_subtensor,
                    bt_module=self._bt_module,
                    wallet=self._bt_wallet,
                    netuid=self._bt_netuid,
                    my_hotkey=my_hotkey,
                    axon_url=self._validator_axon_url,
                )
            except Exception as exc:
                logger.warning(
                    "Periodic axon resync iteration failed (continuing): %s",
                    exc,
                )

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
                # The SQLite-backed AppIntentStore (PR #157) reads live from the
                # DB on every query — there is no in-memory cache to reload, and
                # the old JSON-era `store._load()` was removed in that migration.
                # `list_apps()` below already reflects apps written in by the
                # app-sync loop, so the rescan picks up new deployments directly.
                # (Calling the removed `_load()` here raised AttributeError every
                # 15s on all validators after the SQLite migration.)
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

    async def _local_champion_hotkey(self) -> str | None:
        """Hotkey of the current champion for the 0.05/0.95 burn ramp; None ⇒ 100% burn.

        Read from THIS node's co-located API (GET /v1/solver/champion) — the single source
        of truth that ran the benchmark/consensus and adopted — with a bounded
        last-known-good memo so a transient API restart never flips a standing champion to
        100% burn. Never read from chain or the public leader (anti-free-ride). See
        docs/architecture/state-consolidation.md."""
        import time

        hotkey, src = await self._champion_resolver.resolve(time.monotonic())
        self._champion_source = src  # 'api' | 'memo' | 'none' — surfaced on /health
        return hotkey

    async def _epoch_loop(self) -> None:
        """Periodically emit weights — single source of chain set_weights calls.

        Every validator on the subnet emits weights independently — that's
        the whole point of Yuma's stake-weighted median. Gating this on
        ``self._is_leader`` (the subnet-team order-consensus leader
        election) confused two different things and silently broke
        weight emission for every non-leader validator on subnet 112,
        including registered third parties. Pre-PR-#69, their dividends
        silently collapsed; for the Bittensor network it looked like
        they'd stopped voting.

        Three input sources feed this loop, evaluated in priority order:

        1. **Queued per-miner ranking** (from api EpochManager via
           ``/internal/weights/queue``). When a solver round closes, the
           api POSTs a per-miner mapping derived from benchmark scores
           and replay performance. This is the richest signal we have —
           when present, it always wins.

        2. **Burn fallback** (``self.weights.maybe_emit(champion_id)``).
           When the queue is empty, fall back to ChampionWeights' simple
           champion-vs-owner allocation. This is the default behavior on
           validators with no champion adopted, and the safety net when
           the queue path fails (api down, auth misconfig, etc.). The
           burn path is unconditionally available — anything that breaks
           the queue just leaves the slot empty, and burn fires.

        Cadence is rate-limited at the chain layer (commit-reveal on
        sn112, ~100 blocks ~= 20 min). The loop ticks every 5s but
        ``maybe_emit`` only returns a non-empty mapping when its
        ``epoch_seconds`` window has elapsed.
        """
        while True:
            await asyncio.sleep(5)
            if not self._weights_emitter:
                continue

            # Priority 1: queued per-miner mapping from api EpochManager.
            queued = self._queued_weights_mapping
            if queued:
                source = self._queued_weights_source or "queued"
                # Consume atomically — if emit fails, we DON'T re-queue.
                # The api will POST a fresh mapping on the next round close.
                self._queued_weights_mapping = None
                self._queued_weights_source = None
                await self._do_emit(queued, source=source)
                continue

            # Priority 2: burn fallback via ChampionWeights. Resolve the champion from
            # the co-located API. CRITICAL distinction: a None hotkey from a DEFINITIVE
            # API read (source 'api') means "no champion adopted" ⇒ burning to owner is
            # the correct bootstrap state. A None from an UNRESOLVED read (source
            # 'none'/'init', or a memo with no hotkey) means "we don't KNOW the champion
            # yet" — e.g. the heavier API not ready right after a watchtower CO-restart, or
            # an outage past the memo TTL. Burning out of IGNORANCE would collapse the
            # standing champion's emission for a full ~1300s commit-reveal window. So SKIP:
            # the validator's PRIOR on-chain weights (already weighting the champion)
            # persist until the API answers, and we retry on the next 5s tick. This
            # restores the old local-file read's "instant at startup, tolerant of API
            # outages" safety without holding any champion state. See
            # docs/architecture/state-consolidation.md.
            self._champion_miner_id = await self._local_champion_hotkey()
            if self._champion_miner_id is None and self._champion_source != "api":
                continue
            epoch_weights = self.weights.maybe_emit(self._champion_miner_id)
            if epoch_weights:
                await self._do_emit(epoch_weights, source="burn_fallback")

    def _orders_last_24h(self) -> int:
        """Count orders that went through Minotaur in the trailing 24h.

        Reads the local order store (the same store the order API writes to).
        Any failure degrades to 0 — i.e. the conservative floor share — so a
        store hiccup never inflates miner emission."""
        store = getattr(self, "store", None)
        if store is None or not hasattr(store, "count_orders_since"):
            return 0
        try:
            return int(store.count_orders_since(time.time() - 86400.0))
        except Exception as exc:
            logger.warning("Could not count 24h orders for emission scaling: %s", exc)
            return 0

    def _scale_emission_by_order_volume(
        self, mapping: dict[str, float]
    ) -> dict[str, float]:
        """Scale the aggregate miner share of an emission mapping by trailing-24h
        order volume, the single chokepoint shared by both the queued (API) and
        burn-fallback paths.

        At 0 orders the miner share stays at the conservative floor (5%, the
        incoming mappings are already built at the floor — this is a no-op); it
        ramps linearly to 100% at ``ORDERS_FOR_FULL_EMISSION`` (5000) orders.
        ``apply_champion_burn_ramp`` is idempotent in the fraction, so re-ramping
        an already-floor-ramped mapping just re-targets the aggregate share while
        preserving the relative split among miners and the owner burn target.

        Pure-burn mappings (no champion → ``{owner: 1.0}``) and mappings with no
        resolvable owner are returned unchanged: there is no miner share to ramp."""
        if not mapping:
            return mapping
        orders = self._orders_last_24h()
        fraction = champion_miner_weight_fraction(orders)
        # Surface the live signal on /health and in logs for operators.
        self._orders_24h = orders
        self._miner_weight_fraction = fraction
        if fraction <= CHAMPION_MINER_WEIGHT_FLOOR + 1e-12:
            return mapping  # at the floor: incoming mapping is already correct
        owner = (self.weights.owner_hotkey or "").strip()
        if not owner or list(mapping.keys()) == [owner]:
            return mapping  # nothing to ramp (no burn target / pure burn)
        scaled = apply_champion_burn_ramp(
            mapping, owner_hotkey=owner, miner_fraction=fraction
        )
        logger.info(
            "Order-volume emission ramp: %d orders/24h → miners %.1f%% "
            "(floor %.0f%%, full at %d orders)",
            orders, fraction * 100, CHAMPION_MINER_WEIGHT_FLOOR * 100,
            ORDERS_FOR_FULL_EMISSION,
        )
        return scaled

    async def _do_emit(self, mapping: dict[str, float], *, source: str) -> None:
        """Actually call ``WeightsEmitter.emit_async`` + record + persist state.

        Shared by the queue path and the burn path. The ``source`` field
        in ``_last_emit_state`` lets the validator-health workflow tell
        which input drove this emit (queued_from_api vs burn_fallback).
        """
        attempt_ts = time.time()
        mapping = self._scale_emission_by_order_volume(mapping)
        uids_attempted = len(mapping)
        try:
            success = await self._weights_emitter.emit_async(mapping)
            self._last_emit_state = {
                "attempted_at": attempt_ts,
                "result": "ok" if success else "error",
                "error": None if success else "emit_async returned False (see daemon logs)",
                "uids_attempted": uids_attempted,
                "source": source,
            }
            # Only a success advances the last-successful marker — this is the
            # signal the health classifier trusts (transient/rate-limited
            # failures leave it untouched so a healthy validator that set
            # weights minutes ago isn't false-flagged by a later failed retry).
            if success:
                self._last_successful_emit_state = dict(self._last_emit_state)
        except Exception as exc:
            # Truncate to 300 chars so a verbose substrate stack trace
            # doesn't make /health huge or risk leaking large blobs.
            self._last_emit_state = {
                "attempted_at": attempt_ts,
                "result": "error",
                "error": str(exc)[:300],
                "uids_attempted": uids_attempted,
                "source": source,
            }
            logger.error("Weight emission failed (source=%s): %s", source, exc)

        self._persist_last_emit_state()

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
        # Internal RPC (signed payload required; same-host api → daemon)
        app.router.add_post("/internal/weights/queue", self._handle_weights_queue)
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
        # Build version: MINOTAUR_IMAGE_SHA (baked by CI/Dockerfile) for
        # published images; falls back to the source checkout's git SHA for
        # from-source / bare-metal operators (e.g. no-Docker validators);
        # "dev" otherwise. See minotaur_subnet/version.py.
        from minotaur_subnet.version import resolve_version
        image_sha = resolve_version()
        # last_emit: surface the most recent set_weights attempt so the
        # operator (and the subnet-team validator-health workflow) can tell
        # whether emission is silently failing inside the daemon. None when
        # we've never attempted (eg. uptime < epoch_seconds OR Bittensor not
        # configured). Updated atomically in _epoch_loop. See
        # self._last_emit_state initialization in __init__ for the schema.
        #
        # weights_emitter_configured + my_uid + my_last_update_block disambiguate
        # the failure modes that ``last_emit: null`` could indicate. The chart:
        #   weights_emitter_configured=false → wallet didn't load at startup
        #     (likely WALLET_NAME/HOTKEY_NAME unset or wallet dir unreadable by
        #     uid 1000). The daemon is in "metagraph-only mode" — it can sign
        #     /identity from VALIDATOR_PRIVATE_KEY, but cannot set_weights.
        #   weights_emitter_configured=true, my_uid=null → hotkey not on
        #     metagraph (not registered).
        #   weights_emitter_configured=true, my_uid=<int>, last_emit=null →
        #     either uptime < epoch_seconds, OR the seeding path silently
        #     failed. Compare my_last_update_block * 12s vs current chain head.
        sync_state = getattr(self._metagraph_sync, "state", None) if self._metagraph_sync is not None else None
        return web.json_response({
            "status": "ok",
            "service": "app-intents-validator",
            "image_sha": image_sha,
            "loaded_intents": len(loaded),
            "uptime_seconds": round(time.time() - self._start_time, 1),
            "block_loop_running": self.block_loop.running,
            "weights_emitter_configured": self._weights_emitter is not None,
            # Champion observability (state-consolidation): where the champion was resolved
            # (api/memo/none) and whether we emit the champion ramp vs 100% burn — so a
            # validator burning a REAL champion can no longer present as healthy (the old
            # blindspot where only last_emit.uids_attempted 1-vs-2 revealed it).
            "champion_source": self._champion_source,
            "champion_hotkey": self._champion_miner_id,
            # champion = weighting a real champion; burn = DEFINITIVE no-champion (source
            # 'api') => owner burn; hold = champion UNRESOLVED (api unreachable) => we SKIP
            # emitting so the prior on-chain weights persist (never a burn out of ignorance).
            "emission_mode": (
                "champion" if self._champion_miner_id
                else "burn" if self._champion_source == "api"
                else "hold"
            ),
            "owner_hotkey_resolved": bool(self.weights.owner_hotkey),
            "my_uid": sync_state.my_uid if sync_state is not None else None,
            "my_last_update_block": sync_state.my_last_update_block if sync_state is not None else None,
            "last_emit": self._last_emit_state,
            "last_successful_emit": self._last_successful_emit_state,
            "orderbook": ob_stats,
            "orders_24h": self._orders_24h,
            "miner_weight_fraction": round(self._miner_weight_fraction, 4),
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

    async def _handle_weights_queue(self, request: web.Request) -> web.Response:
        """Accept a per-miner weight mapping from the api process.

        Single-slot newest-wins: each POST overwrites whatever was queued.
        ``_epoch_loop`` consumes the slot on its next tick and emits via
        the same WeightsEmitter the burn fallback uses.

        Auth: signed payload via ``X-Internal-Timestamp`` +
        ``X-Internal-Signature`` headers (see ``shared.internal_auth``).
        Signer must recover to this validator's own EVM address — the api
        signs with the SAME ``VALIDATOR_PRIVATE_KEY`` env this validator
        loaded, so the operator doesn't need a separate shared secret.

        Response codes:
          - 200: queued (caller can stop retrying)
          - 400: body malformed (not retried)
          - 403: signature missing / invalid / stale (not retried)
          - 503: daemon not in weight-emit mode (no wallet loaded, or
                 internal auth disabled because validator_private_key
                 wasn't configured) — caller should fall back to its
                 own logging, not retry
        """
        from minotaur_subnet.shared.internal_auth import (
            InvalidSignature,
            verify_request,
        )

        # 503 first: if the daemon can't emit, there's no point queueing.
        # Operators see this distinct from 403 so they can diagnose
        # "wallet not loaded" vs "auth misconfigured".
        if self._weights_emitter is None:
            return web.json_response(
                {"queued": False, "reason": "weights_emitter not configured"},
                status=503,
            )
        if not self._validator_id:
            return web.json_response(
                {"queued": False, "reason": "internal auth not configured (VALIDATOR_PRIVATE_KEY unset)"},
                status=503,
            )

        body = await request.read()

        ts_header = request.headers.get("X-Internal-Timestamp", "")
        sig_header = request.headers.get("X-Internal-Signature", "")
        if not ts_header or not sig_header:
            return web.json_response(
                {"queued": False, "reason": "missing auth headers"},
                status=403,
            )
        try:
            ts_int = int(ts_header)
        except ValueError:
            return web.json_response(
                {"queued": False, "reason": "malformed X-Internal-Timestamp"},
                status=403,
            )
        try:
            verify_request(
                method=request.method,
                path=request.path,
                body=body,
                timestamp=ts_int,
                signature_hex=sig_header,
                expected_address=self._validator_id,
            )
        except InvalidSignature as exc:
            # Don't echo the specific reason — defense in depth against
            # a curious attacker probing which check failed.
            logger.warning("internal-auth rejected /internal/weights/queue: %s", exc)
            return web.json_response(
                {"queued": False, "reason": "signature verification failed"},
                status=403,
            )

        # Parse body. Schema:
        #   {"mapping": {"5HOwner...": 1.0, "5Other...": 0.5},
        #    "source":  "epoch_manager",       # optional, for /health attribution
        #    "epoch":   10}                    # optional, debug only
        try:
            import json as _json
            payload = _json.loads(body)
        except Exception as exc:
            return web.json_response(
                {"queued": False, "reason": f"body is not JSON: {exc}"},
                status=400,
            )
        if not isinstance(payload, dict):
            return web.json_response(
                {"queued": False, "reason": "body must be a JSON object"},
                status=400,
            )
        mapping = payload.get("mapping")
        if not isinstance(mapping, dict) or not mapping:
            return web.json_response(
                {"queued": False, "reason": "mapping must be a non-empty object"},
                status=400,
            )
        # Light validation: keys must be strings, values must be numeric.
        # We trust the api's EpochManager to produce sane weights (it
        # builds them from scored submissions) but reject obviously-
        # malformed data before storing.
        try:
            cleaned: dict[str, float] = {
                str(k): float(v) for k, v in mapping.items()
            }
        except (TypeError, ValueError) as exc:
            return web.json_response(
                {"queued": False, "reason": f"mapping values must be numeric: {exc}"},
                status=400,
            )

        source = str(payload.get("source", "queued_from_api"))[:64]

        # Single-slot newest-wins: overwriting an unconsumed queue
        # is the documented behavior, not a race. The api only POSTs
        # when a solver round closes (rare relative to the 5s tick),
        # so overwrites are unusual but harmless.
        self._queued_weights_mapping = cleaned
        self._queued_weights_source = source

        logger.info(
            "queued per-miner mapping from api (source=%s, uids=%d, epoch=%s)",
            source, len(cleaned), payload.get("epoch", "?"),
        )
        return web.json_response({"queued": True, "uids": len(cleaned)})

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
        # Advisory public API base (see api/routes/identity.py) — optional.
        api_url = os.environ.get("API_URL", "").strip() or None
        identity = sign_identity(
            self._consensus.private_key,
            self._metagraph_sync.my_hotkey,
            axon_url,
            api_url=api_url,
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

    # Resolve the app catalog path the SAME way the API does (APP_INTENTS_STORE_PATH env)
    # with the --store-path CLI arg as a fallback, so a node's api + validator never resolve
    # the shared SQLite store differently. SQLite-backed (cross-process safe).
    _app_store_path = os.environ.get("APP_INTENTS_STORE_PATH", "").strip() or args.store_path
    store_path = Path(_app_store_path) if _app_store_path else None
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
            # The order daemon's quorum source IS its own ValidatorRegistry —
            # pass it explicitly (no silent fallback inside
            # from_validator_registry).
            quorum_address=registry_address,
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
        # The champion is resolved from THIS node's co-located API (default http://api:8080);
        # override CHAMPION_API_URL only for a non-standard topology. MUST be the operator's
        # OWN co-located API — never the public leader (anti-free-ride).
        champion_api_url=os.environ.get("CHAMPION_API_URL", "").strip() or "http://api:8080",
    )
    asyncio.run(validator.start())


if __name__ == "__main__":
    main()
