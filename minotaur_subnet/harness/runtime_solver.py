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
import socket
import subprocess
import time
from typing import Any

from minotaur_subnet.harness.orchestrator import (
    SolverCrashedError,
    SolverOrchestrator,
    SolverSession,
    SolverTimeoutError,
)
from minotaur_subnet.sdk.intent_solver import MarketSnapshot, SolverMetadata
from minotaur_subnet.shared.types import AppIntentDefinition, ExecutionPlan, IntentState, QuoteResult

logger = logging.getLogger(__name__)


def forced_solver_image() -> str | None:
    """Operator break-glass override for the live solver image, or None.

    ``FORCE_SOLVER_IMAGE`` lets an operator pin THIS node's live order-processing
    solver to a specific image ref (a ``<repo>@sha256:D`` digest or a ``<repo>:tag``)
    to restore functionality when the active champion's code is broken — without
    waiting on re-adoption or fighting champion resolution. Operator-LOCAL, not
    consensus: the live solver only generates plans on the leader (followers
    re-simulate the plan, not the solver), and weights/benchmarking are untouched.
    Applied on restart (set the env, redeploy). Unset = normal champion/genesis
    resolution.
    """
    return os.environ.get("FORCE_SOLVER_IMAGE", "").strip() or None


def resolve_boot_solver_image() -> tuple[str | None, bool]:
    """The image the live solver boots from, and whether it is a forced override.

    Precedence: ``FORCE_SOLVER_IMAGE`` (break-glass) wins over ``GENESIS_SOLVER_IMAGE``.
    Returns ``(image_ref_or_None, is_forced)``.
    """
    forced = forced_solver_image()
    if forced:
        return forced, True
    return (os.environ.get("GENESIS_SOLVER_IMAGE", "").strip() or None), False


# Marker placed on every live-solver container. Lets us reap orphans from
# prior API restarts (the --rm flag only runs on clean exit, so a SIGKILLed
# API leaves its child container running).
LIVE_SOLVER_LABEL_KEY = "minotaur.role"
LIVE_SOLVER_LABEL_VALUE = "live-solver"
LIVE_SOLVER_LABEL = f"{LIVE_SOLVER_LABEL_KEY}={LIVE_SOLVER_LABEL_VALUE}"

# Per-launcher scope for the orphan reap. Multiple API instances (leader +
# peers) can share one host docker.sock; reaping by role alone makes each
# instance kill the OTHERS' in-use live-solver containers mid-INITIALIZE.
# Tagging each container with the launching instance's hostname and reaping
# only matching ones keeps siblings from stomping each other.
LIVE_SOLVER_LAUNCHER_KEY = "minotaur.launcher"


def _live_solver_launcher_id() -> str:
    """Stable-per-instance launcher id (container hostname)."""
    try:
        return socket.gethostname() or "unknown"
    except Exception:
        return "unknown"


def _reap_orphan_live_solvers() -> int:
    """Kill + remove any live-solver containers left over from prior runs.

    Idempotent; safe to call on every API boot. Returns the number of
    containers removed. Errors are logged, not raised — boot must proceed
    even if the docker daemon is unresponsive.
    """
    try:
        result = subprocess.run(
            [
                "docker", "ps", "-aq",
                "--filter", f"label={LIVE_SOLVER_LABEL}",
                # Only reap OUR OWN orphans — never a sibling instance's
                # in-use live solver (see LIVE_SOLVER_LAUNCHER_KEY).
                "--filter", f"label={LIVE_SOLVER_LAUNCHER_KEY}={_live_solver_launcher_id()}",
            ],
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
        chain_ids: list[int] | None = None,
        rpc_urls: dict[int, str] | None = None,
        bridge_registry: Any = None,
    ) -> None:
        self._session = session
        self._image_ref = image_ref
        self._metadata = metadata
        self._lock = asyncio.Lock()
        self._closed = False
        # Best-effort cleanup on ungraceful shutdown (sys.exit, SIGTERM
        # caught by the Python runtime). SIGKILL will bypass this — the
        # next api boot reaps orphans via _reap_orphan_live_solvers().
        atexit.register(_atexit_reap_on_shutdown)
        # Init args for auto-respawn after a session crash. The orchestrator
        # kills the inner SolverSession on any per-command timeout to
        # preserve stdio protocol sync (a half-read response from a
        # timed-out request would desynchronize subsequent commands). A
        # single slow Alchemy call or a malformed user order would
        # therefore permanently break the live solver — until pre-fix
        # 2026-05-27, the api had no way to recover without a process
        # restart. We now keep the create-args here so the next call
        # after a crash can rebuild the inner session transparently.
        self._init_chain_ids = list(chain_ids or [])
        self._init_rpc_urls = dict(rpc_urls or {})
        self._init_bridge_registry = bridge_registry
        # Respawn observability:
        #   _respawn_count: how many times the session has been rebuilt
        #     since this runtime was constructed. Surfaced in /health so
        #     ops can spot crash-loops.
        #   _last_respawn_at: unix seconds of the most recent respawn.
        #     None if the runtime has never crashed.
        #   _last_crash_error: the str() of whatever exception we caught
        #     just before respawning. Truncated to 300 chars.
        self._respawn_count: int = 0
        self._last_respawn_at: float | None = None
        self._last_crash_error: str | None = None

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
                labels={
                    LIVE_SOLVER_LABEL_KEY: LIVE_SOLVER_LABEL_VALUE,
                    LIVE_SOLVER_LAUNCHER_KEY: _live_solver_launcher_id(),
                },
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

        return cls(
            session=session,
            image_ref=image_ref,
            metadata=meta,
            chain_ids=chain_ids,
            rpc_urls=rpc_urls,
            bridge_registry=bridge_registry,
        )

    async def _respawn_session(self) -> None:
        """Rebuild the inner SolverSession after it crashed.

        Caller must hold ``self._lock``. Best-effort: any failure here
        leaves ``self._session._closed`` True so the next call will try
        again on the next request. We don't sleep/back-off in-line —
        respawning is fast (docker run + initialize, typically < 5s)
        and a tight retry loop on persistent failure is preferable to
        silently masking the problem.

        Mirrors the path in ``create()`` but skips the orphan-reap
        (handled at process boot) and the metadata refresh (cached on
        the runtime; champion image_ref hasn't changed).
        """
        live_network = os.environ.get("LIVE_SOLVER_NETWORK", "").strip()
        saved_production = os.environ.get("MINOTAUR_PRODUCTION")
        os.environ["MINOTAUR_PRODUCTION"] = "1"
        try:
            orchestrator = SolverOrchestrator()
            session = await orchestrator.start_docker(
                self._image_ref,
                live=True,
                network=live_network or None,
                labels={
                    LIVE_SOLVER_LABEL_KEY: LIVE_SOLVER_LABEL_VALUE,
                    LIVE_SOLVER_LAUNCHER_KEY: _live_solver_launcher_id(),
                },
            )
        finally:
            if saved_production is not None:
                os.environ["MINOTAUR_PRODUCTION"] = saved_production
            else:
                os.environ.pop("MINOTAUR_PRODUCTION", None)
        await session.initialize({
            "chain_ids": self._init_chain_ids,
            "rpc_urls": self._init_rpc_urls,
            "bridge_registry": self._init_bridge_registry,
        })
        self._session = session
        self._respawn_count += 1
        self._last_respawn_at = time.time()
        logger.info(
            "Live solver respawned (%s, respawn_count=%d)",
            self._image_ref, self._respawn_count,
        )

    async def _ensure_session_alive(self) -> None:
        """Respawn the inner session if it crashed since the last call.

        Caller must hold ``self._lock`` so two concurrent callers don't
        race on rebuilding. Cheap when the session is alive (one bool
        check); only does work after a crash.
        """
        if self._session is not None and not self._session._closed:
            return
        # The session is dead (either kill-on-timeout or process exit).
        # Rebuild it in-place so this and subsequent calls work without
        # operator intervention.
        try:
            await self._respawn_session()
        except Exception as exc:
            # Record the failure so /health can surface it, then re-raise
            # so the caller's request fails fast rather than hanging.
            self._last_crash_error = f"respawn failed: {str(exc)[:280]}"
            logger.error(
                "Live solver respawn failed (%s): %s",
                self._image_ref, exc,
            )
            raise

    def is_alive(self) -> bool:
        """Whether the underlying session can serve a request right now.

        Returns False when the runtime has been explicitly shut down,
        OR when the inner session has crashed and hasn't been respawned
        yet. Surfaced in api's /health so ops + the validator-health
        workflow can spot a wedged solver without waiting for a quote
        request to fail.

        Note: this is a fast property — it does NOT probe the live
        process by sending a metadata() command. A solver that's alive
        but unresponsive will still return True here; only an explicit
        request will detect that state and trigger respawn.
        """
        return not self._closed and self._session is not None and not self._session._closed

    def respawn_state(self) -> dict[str, Any]:
        """Diagnostic snapshot for /health.

        Returns the respawn count + last respawn timestamp + last crash
        reason. Operators use this to distinguish "solver is healthy" from
        "solver crash-looping" from "solver is wedged and respawn keeps
        failing".
        """
        return {
            "respawn_count": self._respawn_count,
            "last_respawn_at": self._last_respawn_at,
            "last_crash_error": self._last_crash_error,
        }

    async def _call_with_respawn(
        self,
        op_name: str,
        coro_factory,
    ) -> Any:
        """Run a session call, respawning the session once on crash.

        ``coro_factory`` is a zero-arg callable returning a fresh coroutine
        on each invocation — needed because we may need to await the call
        twice (first attempt → crash → respawn → second attempt). Passing
        an already-awaited coroutine wouldn't work for the retry.

        The single-retry policy is deliberate: respawning is comparatively
        cheap, but if the second attempt also crashes the request is
        probably hitting a real bug (malformed input that hangs the solver,
        wedged Anvil, etc.). We surface that error to the caller rather
        than loop forever.
        """
        async with self._lock:
            await self._ensure_session_alive()
            try:
                return await coro_factory()
            except (SolverCrashedError, SolverTimeoutError) as exc:
                # Inner session is now closed (orchestrator killed it to
                # preserve protocol sync). Respawn and retry ONCE so a
                # single slow Alchemy roundtrip or malformed user order
                # doesn't permanently break the live solver.
                self._last_crash_error = f"{op_name}: {str(exc)[:280]}"
                logger.warning(
                    "Live solver %s crashed (%s); respawning + retrying once",
                    op_name, type(exc).__name__,
                )
                await self._respawn_session()
                return await coro_factory()

    async def generate_plan(
        self,
        intent: AppIntentDefinition,
        state: IntentState,
        snapshot: MarketSnapshot | None = None,
    ) -> ExecutionPlan | None:
        """Generate a plan by forwarding the request to the Docker session.

        Resilient to inner-session crashes: a single timeout (eg slow
        Alchemy RPC, or a solver that hangs on malformed params) triggers
        a transparent respawn-and-retry rather than permanently breaking
        the runtime. Operators see the crash via /health.live_solver
        but the next user request still works.
        """
        if self._closed:
            raise RuntimeError("Champion runtime solver is closed")
        if snapshot is None:
            snapshot = MarketSnapshot.empty(chain_id=state.chain_id)

        return await self._call_with_respawn(
            "generate_plan",
            lambda: self._session.generate_plan(intent, state, snapshot),
        )

    async def quote(
        self,
        intent: AppIntentDefinition,
        state: IntentState,
        snapshot: MarketSnapshot | None = None,
    ) -> QuoteResult | None:
        """Generate a quote by forwarding the request to the Docker session.

        See ``generate_plan`` for the resilience contract.
        """
        if self._closed:
            raise RuntimeError("Champion runtime solver is closed")
        if snapshot is None:
            snapshot = MarketSnapshot.empty(chain_id=state.chain_id)

        return await self._call_with_respawn(
            "quote",
            lambda: self._session.quote(intent, state, snapshot),
        )

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
