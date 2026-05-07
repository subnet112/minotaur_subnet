"""Runtime solver adapter backed by an isolated Docker solver session.

This adapter allows BlockLoop to use a screened champion image directly for
plan generation without importing untrusted Python into the host process.

SECURITY: Live champion containers process real user orders (balances, wallet
addresses, trade parameters). They MUST run with --network=none to prevent a
malicious champion from exfiltrating user data to an external endpoint. Network
access is only needed during benchmarking (for Anvil RPC queries); live
execution receives all necessary state via the harness protocol over stdin.
"""

from __future__ import annotations

import asyncio
import atexit
import logging
import os
import subprocess
import time
from typing import Any

from minotaur_subnet.harness.orchestrator import SolverOrchestrator, SolverSession
from minotaur_subnet.sdk.intent_solver import MarketSnapshot, SolverMetadata
from minotaur_subnet.shared.types import AppIntentDefinition, ExecutionPlan, IntentState, QuoteResult

logger = logging.getLogger(__name__)

# Marker placed on every live-solver container. Lets us reap orphans from
# prior API restarts (the --rm flag only runs on clean exit, so a SIGKILLed
# API leaves its child container running).
LIVE_SOLVER_LABEL_KEY = "minotaur.role"
LIVE_SOLVER_LABEL_VALUE = "live-solver"
LIVE_SOLVER_LABEL = f"{LIVE_SOLVER_LABEL_KEY}={LIVE_SOLVER_LABEL_VALUE}"


def _reap_orphan_live_solvers() -> int:
    """Kill + remove any live-solver containers left over from prior runs.

    Idempotent; safe to call on every API boot. Returns the number of
    containers removed. Errors are logged, not raised — boot must proceed
    even if the docker daemon is unresponsive.
    """
    try:
        result = subprocess.run(
            ["docker", "ps", "-aq", "--filter", f"label={LIVE_SOLVER_LABEL}"],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        logger.warning("Orphan live-solver reap: docker ps failed: %s", exc)
        return 0
    ids = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if not ids:
        return 0
    logger.info("Reaping %d orphan live-solver container(s): %s", len(ids), ids)
    try:
        subprocess.run(
            ["docker", "rm", "-f", *ids],
            capture_output=True, text=True, timeout=30, check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        logger.warning("Orphan live-solver reap: docker rm failed: %s", exc)
        return 0
    return len(ids)


def _atexit_reap_on_shutdown() -> None:
    """atexit hook — reap live-solver containers on interpreter exit.

    Deliberately not tied to a specific DockerRuntimeSolver instance, so
    even if the instance has been garbage-collected before shutdown the
    containers still get cleaned up.
    """
    try:
        _reap_orphan_live_solvers()
    except Exception:
        # atexit hooks must not raise.
        pass


class DockerRuntimeSolver:
    """Adapter that exposes an IntentSolver-like interface over Docker IPC."""

    def __init__(
        self,
        *,
        session: SolverSession,
        image_ref: str,
        metadata: SolverMetadata,
    ) -> None:
        self._session = session
        self._image_ref = image_ref
        self._metadata = metadata
        self._lock = asyncio.Lock()
        self._closed = False
        # Per-chain cache for supported_tokens so the token list endpoint
        # doesn't round-trip through the Docker pipe on every frontend
        # refresh. TTL matches the solver-side cache (5 min).
        self._token_cache: dict[int, tuple[float, list[dict[str, Any]]]] = {}
        self._token_cache_ttl = 300.0
        # Best-effort cleanup on ungraceful shutdown (sys.exit, SIGTERM
        # caught by the Python runtime). SIGKILL will bypass this — the
        # next api boot reaps orphans via _reap_orphan_live_solvers().
        atexit.register(_atexit_reap_on_shutdown)

    @classmethod
    async def create(
        cls,
        *,
        image_ref: str,
        chain_ids: list[int],
        rpc_urls: dict[int, str],
        bridge_registry: Any = None,
    ) -> "DockerRuntimeSolver":
        """Create and initialize a Docker-backed runtime solver.

        NETWORK POLICY: The reference BaselineSwapSolver needs RPC access
        for live pool discovery (it has no prebuilt snapshot at quote time),
        so the live champion must be reachable to the configured RPC URLs.
        If BENCHMARK_DOCKER_NETWORK is set, the container is attached to
        that network (same sandbox the benchmark worker uses); otherwise
        it falls back to --network=none and will only work for solvers
        that operate purely from snapshots passed over stdin.

        SECURITY TRADE-OFF: a malicious champion on a network-connected
        sandbox could attempt to exfiltrate user data. In production that
        network MUST be a Docker `--internal` network with iptables rules
        limiting egress to the Anvil/Alchemy/Base RPC endpoints only. See
        platform/production/README.md for firewall setup.
        """
        # Reap any orphan live-solver containers left by a prior API process
        # that died before docker run --rm could clean up.
        _reap_orphan_live_solvers()

        # The live champion needs RPC access to the URLs configured for
        # production (e.g. Alchemy Base). The benchmark-sandbox network is
        # internal-only so cannot reach external hosts. LIVE_SOLVER_NETWORK
        # lets ops attach the runtime container to a different Docker network
        # (typically the API's own network, which has internet egress).
        # If unset, falls back to --network=none (only works for solvers
        # that operate purely from snapshots passed over stdin).
        #
        # We pass the network via the ``network=`` param — earlier versions
        # mutated BENCHMARK_DOCKER_NETWORK in-process, which made the
        # orchestrator's env-picking logic mistakenly treat the live
        # container as a benchmark sandbox and bake unreachable
        # BENCHMARK_ANVIL_RPC_* IPs into its environment.
        live_network = os.environ.get("LIVE_SOLVER_NETWORK", "").strip()
        saved_production = os.environ.get("MINOTAUR_PRODUCTION")
        os.environ["MINOTAUR_PRODUCTION"] = "1"
        try:
            orchestrator = SolverOrchestrator()
            # live=True disables the total session timeout (600s) AND tells
            # the orchestrator to skip BENCHMARK_ANVIL_RPC_* env-var
            # overrides — the live container runs on the production
            # network where those sandbox IPs are unreachable.
            session = await orchestrator.start_docker(
                image_ref,
                live=True,
                network=live_network or None,
                labels={LIVE_SOLVER_LABEL_KEY: LIVE_SOLVER_LABEL_VALUE},
            )
        finally:
            if saved_production is not None:
                os.environ["MINOTAUR_PRODUCTION"] = saved_production
            else:
                os.environ.pop("MINOTAUR_PRODUCTION", None)
        _net = live_network or "none"
        logger.info(
            "Live champion container started (network=%s): %s",
            _net, image_ref,
        )
        init_cfg: dict[str, Any] = {
            "chain_ids": chain_ids,
            "rpc_urls": rpc_urls,
            "bridge_registry": bridge_registry,
        }
        await session.initialize(init_cfg)

        try:
            meta = await session.metadata()
        except Exception as exc:
            logger.warning("Could not fetch champion metadata (%s): %s", image_ref, exc)
            meta = SolverMetadata(
                name=f"champion-{image_ref[:12]}",
                version="unknown",
                author="unknown",
            )

        return cls(session=session, image_ref=image_ref, metadata=meta)

    async def generate_plan(
        self,
        intent: AppIntentDefinition,
        state: IntentState,
        snapshot: MarketSnapshot | None = None,
    ) -> ExecutionPlan | None:
        """Generate a plan by forwarding the request to the Docker session."""
        if self._closed:
            raise RuntimeError("Champion runtime solver is closed")
        if snapshot is None:
            snapshot = MarketSnapshot.empty(chain_id=state.chain_id)

        async with self._lock:
            return await self._session.generate_plan(intent, state, snapshot)

    async def quote(
        self,
        intent: AppIntentDefinition,
        state: IntentState,
        snapshot: MarketSnapshot | None = None,
    ) -> QuoteResult | None:
        """Generate a quote by forwarding the request to the Docker session."""
        if self._closed:
            raise RuntimeError("Champion runtime solver is closed")
        if snapshot is None:
            snapshot = MarketSnapshot.empty(chain_id=state.chain_id)

        async with self._lock:
            return await self._session.quote(intent, state, snapshot)

    async def supported_tokens(self, chain_id: int) -> list[dict[str, Any]]:
        """Forward token discovery to the solver session with a small TTL cache.

        Frontend hits `/v1/chains/{chain_id}/tokens` on every wallet tab
        open, so caching here avoids waking the solver for every refresh.
        An empty list (no discovery yet) is NOT cached — we want the first
        successful discovery to displace it.
        """
        if self._closed:
            raise RuntimeError("Champion runtime solver is closed")

        now = time.time()
        cached = self._token_cache.get(chain_id)
        if cached and cached[1] and now - cached[0] < self._token_cache_ttl:
            return cached[1]

        async with self._lock:
            tokens = await self._session.supported_tokens(chain_id)
        if tokens:
            self._token_cache[chain_id] = (now, tokens)
        return tokens

    def metadata(self) -> SolverMetadata:
        """Return cached metadata for BlockLoop logging."""
        return self._metadata

    async def shutdown(self) -> None:
        """Close the underlying Docker session."""
        if self._closed:
            return
        self._closed = True
        async with self._lock:
            await self._session.shutdown()
        logger.info("Champion runtime stopped (%s)", self._image_ref)
