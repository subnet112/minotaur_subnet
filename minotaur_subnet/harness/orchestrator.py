"""Host-side benchmark orchestrator.

Manages solver Docker containers, sends commands via the JSON-over-stdin/stdout
protocol, collects execution plans, and scores them. This is the validator-side
counterpart to the in-container runner.

Two modes of operation:
1. Docker mode (production): Runs solver in an isolated Docker container
2. Subprocess mode (dev/test): Runs solver as a local Python subprocess

Both modes use the same protocol — the orchestrator doesn't care whether
the other end is a container or a local process.

Usage:
    orchestrator = SolverOrchestrator()

    # Start a solver process (Docker or subprocess)
    session = await orchestrator.start_docker("solver-image:latest", snapshot_dir="/tmp/snap")

    # Run the benchmarking lifecycle
    await session.initialize({"chain_ids": [1]})
    meta = await session.metadata()
    await session.on_benchmark_start(len(intents))
    for intent, state, snapshot in intents:
        plan = await session.generate_plan(intent, state, snapshot)
    await session.on_benchmark_end(results)
    state_bytes = await session.serialize_state()
    await session.shutdown()
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any

from minotaur_subnet.shared.types import (
    AppIntentDefinition,
    ExecutionPlan,
    IntentState,
    ScoreResult,
    SimulationResult,
)
from minotaur_subnet.sdk.intent_solver import MarketSnapshot, SolverMetadata
from minotaur_subnet.harness.protocol import (
    Command,
    HarnessRequest,
    HarnessResponse,
    TIMEOUTS,
    TOTAL_BENCHMARK_TIMEOUT,
    make_initialize_request,
    make_generate_plan_request,
    make_check_trigger_request,
    make_benchmark_start_request,
    make_benchmark_end_request,
    make_serialize_state_request,
    make_restore_state_request,
    make_metadata_request,
    make_shutdown_request,
    make_quote_request,
    make_supported_tokens_request,
    parse_plan_response,
    parse_quote_response,
)

logger = logging.getLogger(__name__)

# Upper bound on how long ``kill()`` waits to reap the killed process.
# After SIGKILL the process is gone regardless; we only wait to clean up
# the zombie. In a container whose asyncio child-watcher can stall (we have
# observed unreaped zombie children piling up under a long-lived api PID 1),
# an UNBOUNDED ``proc.wait()`` here never returns — and because ``kill()``
# runs while the DockerRuntimeSolver holds its per-runtime ``asyncio.Lock``
# (every quote/plan serializes on it), a single stalled reap deadlocks the
# entire live-solver path: every subsequent quote hangs forever while the
# event loop otherwise stays healthy. Bounding the wait guarantees the lock
# is always released; the worst case is a lingering zombie, not an outage.
_KILL_REAP_TIMEOUT = 5.0


class SolverTimeoutError(Exception):
    """A solver command exceeded its timeout."""


class SolverCrashedError(Exception):
    """The solver process exited unexpectedly."""


class SolverProtocolError(Exception):
    """The solver returned an invalid response."""


class RealSimulationUnavailable(RuntimeError):
    """A real Anvil simulation was required but unavailable.

    Raised by ``run_benchmark`` when ``require_real_sim`` is set and no
    simulator was injected. Fail-closed: refuse to benchmark on the fabricated
    mock, which reports a ~min*1.05 success and could be gamed into a passing
    score. The benchmark worker loop logs this and retries; it never crashes
    the process (``run_loop`` catches Exception)."""


def require_real_sim_default() -> bool:
    """Whether the benchmark must use a REAL simulator (fail closed on no-sim / mock).
    Default ON for prod/consensus so a champion can't be adopted on fabricated scores;
    OFF only under LOCAL_TESTNET=1 (testnet configs may run with no Anvil simulator).
    Consensus-relevant: must be uniform across validators."""
    if os.environ.get("LOCAL_TESTNET", "").strip() == "1":
        v = os.environ.get("BENCHMARK_REQUIRE_REAL_SIM", "").strip().lower()
        return v in ("1", "true", "yes", "on")  # testnet defaults OFF
    v = os.environ.get("BENCHMARK_REQUIRE_REAL_SIM", "").strip().lower()
    if v == "":
        v = "1"  # prod/consensus defaults ON (empty env -> ON, not just absent env)
    return v in ("1", "true", "yes", "on")


# ═══════════════════════════════════════════════════════════════════════════════
#                          SOLVER SESSION
# ═══════════════════════════════════════════════════════════════════════════════


@dataclass
class BenchmarkResult:
    """Result of benchmarking a single intent."""
    intent_id: str
    plan: ExecutionPlan | None = None
    trigger_decision: bool | None = None
    score: float = 0.0
    trigger_score: float | None = None
    plan_score: float | None = None
    score_breakdown: dict[str, float] = field(default_factory=dict)
    elapsed_ms: int = 0
    error: str | None = None
    mock_simulation: bool = False  # True when scored with fabricated simulation data
    on_chain_score: int | None = None  # scoreIntent BPS (0-10000) from the simulation


# Type alias for the scoring callback
ScoreFn = Any  # Callable[[str, ExecutionPlan, SimulationResult, IntentState], Awaitable[ScoreResult]]


class SolverSession:
    """A live connection to a solver process (container or subprocess).

    Wraps an asyncio subprocess and provides typed methods for each
    protocol command. Handles timeouts, error parsing, and cleanup.
    """

    def __init__(
        self,
        proc: asyncio.subprocess.Process,
        label: str = "solver",
        *,
        live_mode: bool = False,
    ) -> None:
        self._proc = proc
        self._label = label
        self._start_time = time.monotonic()
        self._closed = False
        # live_mode=True disables the total elapsed-time cap. Per-command
        # timeouts still apply. Used for long-lived runtime solvers that
        # serve quotes/plans for real user orders (DockerRuntimeSolver),
        # where session lifetime is the container's lifetime, not a
        # single benchmark run.
        self._live_mode = live_mode

    async def initialize(self, config: dict[str, Any]) -> None:
        """Send initialize command."""
        resp = await self._send(make_initialize_request(config))
        if not resp.success:
            raise RuntimeError(f"Solver init failed: {resp.error}")

    async def metadata(self) -> SolverMetadata:
        """Get solver metadata."""
        resp = await self._send(make_metadata_request())
        if not resp.success:
            raise RuntimeError(f"Metadata failed: {resp.error}")
        r = resp.result
        return SolverMetadata(
            name=r.get("name", "unknown"),
            version=r.get("version", "0.0.0"),
            author=r.get("author", "unknown"),
            description=r.get("description", ""),
            supported_chains=r.get("supported_chains", [1]),
            supported_intent_types=r.get("supported_intent_types", ["swap"]),
        )

    async def generate_plan(
        self,
        intent: AppIntentDefinition,
        state: IntentState,
        snapshot: MarketSnapshot,
    ) -> ExecutionPlan | None:
        """Send generate_plan and parse the returned ExecutionPlan."""
        resp = await self._send(
            make_generate_plan_request(intent, state, snapshot)
        )
        if not resp.success:
            logger.warning(
                "[%s] generate_plan failed for %s: %s",
                self._label, intent.app_id, resp.error,
            )
            return None
        return parse_plan_response(resp)

    async def quote(
        self,
        intent: AppIntentDefinition,
        state: IntentState,
        snapshot: MarketSnapshot,
    ) -> "QuoteResult | None":
        """Send quote and parse the returned QuoteResult."""
        resp = await self._send(
            make_quote_request(intent, state, snapshot)
        )
        if not resp.success:
            logger.warning(
                "[%s] quote failed for %s: %s",
                self._label, intent.app_id, resp.error,
            )
            return None
        return parse_quote_response(resp)

    async def supported_tokens(self, chain_id: int) -> list[dict[str, Any]]:
        """Send supported_tokens and return the list of discovered tokens."""
        resp = await self._send(make_supported_tokens_request(chain_id))
        if not resp.success:
            logger.warning(
                "[%s] supported_tokens failed for chain %d: %s",
                self._label, chain_id, resp.error,
            )
            return []
        result = resp.result
        if not isinstance(result, list):
            return []
        return result

    async def check_trigger(
        self,
        intent: AppIntentDefinition,
        state: IntentState,
        snapshot: MarketSnapshot,
    ) -> bool:
        """Send check_trigger and return the boolean result."""
        resp = await self._send(
            make_check_trigger_request(intent, state, snapshot)
        )
        if not resp.success:
            logger.warning(
                "[%s] check_trigger failed for %s: %s",
                self._label, intent.app_id, resp.error,
            )
            return False
        return bool(resp.result)

    async def on_benchmark_start(self, intent_count: int) -> None:
        """Signal the start of a benchmark batch."""
        resp = await self._send(make_benchmark_start_request(intent_count))
        if not resp.success:
            logger.warning(
                "[%s] on_benchmark_start failed: %s", self._label, resp.error,
            )

    async def on_benchmark_end(
        self, results: list[dict[str, Any]],
    ) -> None:
        """Signal the end of a benchmark batch with results."""
        resp = await self._send(make_benchmark_end_request(results))
        if not resp.success:
            logger.warning(
                "[%s] on_benchmark_end failed: %s", self._label, resp.error,
            )

    async def serialize_state(self) -> bytes:
        """Get serialized solver state."""
        resp = await self._send(make_serialize_state_request())
        if not resp.success:
            logger.warning(
                "[%s] serialize_state failed: %s", self._label, resp.error,
            )
            return b""
        if not resp.result:
            return b""
        return base64.b64decode(resp.result)

    async def restore_state(self, state_bytes: bytes) -> None:
        """Restore previously serialized state."""
        state_b64 = base64.b64encode(state_bytes).decode("ascii")
        resp = await self._send(make_restore_state_request(state_b64))
        if not resp.success:
            logger.warning(
                "[%s] restore_state failed: %s", self._label, resp.error,
            )

    async def shutdown(self) -> None:
        """Gracefully shut down the solver."""
        if self._closed:
            return
        try:
            await self._send(make_shutdown_request())
        except (SolverTimeoutError, SolverCrashedError):
            pass
        await self.kill()

    async def kill(self) -> None:
        """Force-kill the solver process."""
        if self._closed:
            return
        self._closed = True
        try:
            self._proc.kill()
            # Bounded reap — never block the caller (and the runtime lock it
            # may hold) forever if child-reaping stalls. See _KILL_REAP_TIMEOUT.
            await asyncio.wait_for(self._proc.wait(), timeout=_KILL_REAP_TIMEOUT)
        except ProcessLookupError:
            pass
        except asyncio.TimeoutError:
            logger.warning(
                "[%s] proc.wait() did not return %ss after SIGKILL; "
                "abandoning reap (zombie may linger, but the lock is freed)",
                self._label, _KILL_REAP_TIMEOUT,
            )
        logger.info("[%s] Process terminated", self._label)

    @property
    def elapsed_total(self) -> float:
        """Total elapsed time since session start, in seconds."""
        return time.monotonic() - self._start_time

    # ── Internal communication ────────────────────────────────────────────

    async def _send(self, request: HarnessRequest) -> HarnessResponse:
        """Send a request and wait for the response, with timeout."""
        if self._closed:
            raise SolverCrashedError("Solver process is not running")

        if self._proc.stdin is None or self._proc.stdout is None:
            raise SolverCrashedError("Solver process has no stdin/stdout")

        # Check total benchmark timeout (skipped in live mode — see __init__)
        if not self._live_mode and self.elapsed_total > TOTAL_BENCHMARK_TIMEOUT:
            await self.kill()
            raise SolverTimeoutError(
                f"Total benchmark timeout exceeded ({TOTAL_BENCHMARK_TIMEOUT}s)"
            )

        timeout = TIMEOUTS.get(request.command, 30.0)
        msg = request.to_json() + "\n"

        try:
            self._proc.stdin.write(msg.encode("utf-8"))
            await self._proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as exc:
            self._closed = True
            raise SolverCrashedError(f"Solver stdin broken: {exc}") from exc

        try:
            raw_line = await asyncio.wait_for(
                self._proc.stdout.readline(),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            await self.kill()
            raise SolverTimeoutError(
                f"Command {request.command} timed out after {timeout}s"
            )

        if not raw_line:
            self._closed = True
            raise SolverCrashedError(
                f"Solver process exited during {request.command}"
            )

        line = raw_line.decode("utf-8", errors="replace").strip()

        try:
            return HarnessResponse.from_json(line)
        except (json.JSONDecodeError, KeyError) as exc:
            raise SolverProtocolError(
                f"Invalid response from solver: {exc}. Raw: {line[:200]}"
            ) from exc


# ═══════════════════════════════════════════════════════════════════════════════
#                          ORCHESTRATOR
# ═══════════════════════════════════════════════════════════════════════════════


# Docker container security configuration
DOCKER_SECURITY_OPTS = [
    "--network=none",
    "--read-only",
    "--cap-drop=ALL",
    "--security-opt=no-new-privileges:true",
    "--memory=4g",
    "--memory-swap=4g",
    "--cpus=2.0",
    "--pids-limit=256",
    "--tmpfs=/tmp:size=512m",
    # Prevent Python from writing .pyc files to the read-only filesystem.
    # Without this, dynamic imports (e.g., strategy auto-discovery) fail
    # silently when __pycache__ can't be created.
    "-e", "PYTHONDONTWRITEBYTECODE=1",
]

# SECURITY: When BENCHMARK_DOCKER_NETWORK is set, solver containers gain access
# to the entire Docker network. This is a security risk because malicious solvers
# could reach internal services (API, validator, relayer) and exfiltrate data or
# interfere with consensus. Ideally a dedicated network with firewall rules should
# be used so solvers can ONLY reach the Anvil RPC endpoint. The
# BENCHMARK_ALLOWED_HOSTS variable documents which hosts are intended to be
# reachable (default: anvil, anvil-base — the local testnet Anvil hostnames).
BENCHMARK_ALLOWED_HOSTS = os.environ.get(
    "BENCHMARK_ALLOWED_HOSTS", "anvil,anvil-base"
).strip()

# Environment variable names that MUST NOT be forwarded to solver containers.
# These could leak validator secrets, private keys, API credentials, or wallet
# data to untrusted miner code.
_SENSITIVE_ENV_PREFIXES = (
    "PRIVATE_KEY", "SECRET", "API_KEY", "WALLET", "HMAC",
    "SUBMISSION_PROVENANCE", "BT_", "BITTENSOR", "MNEMONIC",
    "PASSWORD", "TOKEN", "AUTH", "CREDENTIAL",
)


class SolverOrchestrator:
    """Manages solver sessions for benchmarking.

    Supports two backends:
    - Docker: Production mode, full isolation
    - Subprocess: Dev/test mode, runs solver locally
    """

    async def start_docker(
        self,
        image: str,
        snapshot_dir: str | None = None,
        state_dir: str | None = None,
        extra_args: list[str] | None = None,
        rpc_overrides: dict[int, str] | None = None,
        live: bool = False,
        labels: dict[str, str] | None = None,
        network: str | None = None,
    ) -> SolverSession:
        """Start a solver in a Docker container.

        Args:
            image: Docker image name (e.g., "solver-abc123:latest").
            snapshot_dir: Host path to mount as /data/snapshot (read-only).
            state_dir: Host path to mount as /data/state (read-write).
            extra_args: Additional docker run arguments.
            rpc_overrides: Optional {chain_id: rpc_url} overrides for Stage 3
                regression replay — points the solver at a historical fork.
            labels: Optional docker `--label k=v` pairs. The runtime solver
                uses ``minotaur.role=live-solver`` so orphan containers from
                prior API restarts can be reaped on boot.

        Returns:
            A SolverSession connected to the container.
        """
        cmd = ["docker", "run", "--rm", "-i"]
        if labels:
            for k, v in labels.items():
                cmd.extend(["--label", f"{k}={v}"])

        # During benchmarking, solvers may need RPC access to query
        # on-chain state (pool liquidity, prices). If BENCHMARK_DOCKER_NETWORK
        # is set, attach to that network instead of --network=none.
        #
        # SECURITY RISK: This gives the solver access to the entire Docker
        # network. A dedicated network with iptables rules restricting access
        # to only Anvil RPC endpoints should be used in production. See
        # BENCHMARK_ALLOWED_HOSTS for the intended whitelist.
        # ``network`` (an explicit parameter) wins over BENCHMARK_DOCKER_NETWORK
        # so callers like the live-champion bootstrap don't have to mutate
        # the process environment to pick a different network.
        bench_network = (network or "").strip() or os.environ.get(
            "BENCHMARK_DOCKER_NETWORK", "",
        ).strip()
        security_opts = list(DOCKER_SECURITY_OPTS)
        if bench_network:
            # Use the dedicated benchmark network. In production this MUST be
            # a Docker --internal network with iptables rules restricting
            # access to only the Anvil RPC ports (8545-8547). The network
            # has no external gateway, so solver containers cannot reach the
            # internet, host, or other Docker networks. See:
            # platform/production/README.md for firewall setup instructions.
            security_opts = [
                opt for opt in security_opts if not opt.startswith("--network")
            ]
            security_opts.append(f"--network={bench_network}")
            logger.info(
                "Solver container on benchmark network '%s' "
                "(allowed hosts: %s)",
                bench_network, BENCHMARK_ALLOWED_HOSTS,
            )
        cmd.extend(security_opts)

        if snapshot_dir:
            cmd.extend(["-v", f"{snapshot_dir}:/data/snapshot:ro"])
        if state_dir:
            cmd.extend(["-v", f"{state_dir}:/data/state:rw"])

        # SECURITY: Only pass whitelisted environment variables to solver
        # containers. Never forward API keys, private keys, wallet secrets,
        # or other sensitive host environment variables.
        #
        # When BENCHMARK_DOCKER_NETWORK is set (sandboxed network), prefer
        # BENCHMARK_ANVIL_RPC_* env vars (which use IPs reachable on the
        # sandbox network) over the default Docker-hostname-based URLs.
        #
        # For LIVE champion containers (live=True) we explicitly DON'T use
        # the BENCHMARK_ANVIL_RPC_* IPs even if BENCHMARK_DOCKER_NETWORK is
        # set: live containers run on a different (production) network where
        # those sandbox-subnet IPs are unreachable. They must use the
        # production env vars (BASE_RPC_URL etc.) instead.
        _use_sandbox = bool(bench_network) and not live
        _overrides = rpc_overrides or {}
        # ETH (chain 1 / Anvil 31337)
        anvil_rpc = _overrides.get(1) or _overrides.get(31337) or (
            os.environ.get("BENCHMARK_ANVIL_RPC_ETH", "").strip()
            if _use_sandbox else ""
        ) or os.environ.get("ANVIL_RPC_URL", "").strip()
        if anvil_rpc:
            cmd.extend(["-e", f"ANVIL_RPC_URL={anvil_rpc}"])
        # Base (chain 8453)
        base_rpc = _overrides.get(8453) or (
            os.environ.get("BENCHMARK_ANVIL_RPC_BASE", "").strip()
            if _use_sandbox else ""
        ) or os.environ.get("BASE_RPC_URL", "").strip()
        if base_rpc:
            cmd.extend(["-e", f"BASE_RPC_URL={base_rpc}"])
        # BT EVM (chain 964)
        btevm_rpc = _overrides.get(964) or (
            os.environ.get("BENCHMARK_ANVIL_RPC_BTEVM", "").strip()
            if _use_sandbox else ""
        ) or os.environ.get("BITTENSOR_EVM_RPC_URL", "").strip()
        if btevm_rpc:
            cmd.extend(["-e", f"BITTENSOR_EVM_RPC_URL={btevm_rpc}"])

        if extra_args:
            cmd.extend(extra_args)

        # Just specify the image — the base image's ENTRYPOINT/CMD
        # already runs the harness runner with the solver path.
        cmd.append(image)

        logger.info("Starting Docker solver: %s", " ".join(cmd))

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        label = f"docker:{image.split(':')[0][-12:]}"
        return SolverSession(proc, label=label, live_mode=live)

    async def start_subprocess(
        self,
        solver_path: str,
    ) -> SolverSession:
        """Start a solver as a local Python subprocess (dev/test mode).

        SECURITY RISK: No Docker isolation — the solver runs with full host
        access (filesystem, network, secrets). A malicious solver can
        compromise the validator. Never use in production; use Docker mode
        with signed git submissions instead.

        Args:
            solver_path: Path to the solver.py file.

        Returns:
            A SolverSession connected to the subprocess.
        """
        import sys

        cmd = [
            sys.executable, "-m", "minotaur_subnet.harness.runner",
            solver_path,
        ]

        logger.info("Starting subprocess solver: %s", solver_path)

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        label = f"subprocess:{solver_path.split('/')[-1]}"
        return SolverSession(proc, label=label)


# ═══════════════════════════════════════════════════════════════════════════════
#                          BENCHMARK RUNNER
# ═══════════════════════════════════════════════════════════════════════════════


@dataclass
class BenchmarkConfig:
    """Configuration for a benchmark run."""
    chain_ids: list[int] = field(default_factory=lambda: [1])
    timeout_per_plan_ms: int = 30000
    auto_trigger_weight: float = 0.4
    plan_quality_weight: float = 0.6


# Sentinel a reference-quote pre-pass writes into ``reference_quotes[label]``
# when the CHAMPION solver FAILED to quote that scenario. ``run_benchmark``
# detects it and surfaces an explicit error + scores 0 instead of silently
# self-quoting (which would fabricate a non-comparable pass and mask the
# champion-reference failure). Cannot collide with real mapped quote params —
# those are keyed by manifest param names, never this dunder key.
REFERENCE_QUOTE_FAILED_SENTINEL = "__reference_quote_failed__"

# Slippage applied to a BENCHMARK scenario's quote-derived min_output_amount.
# Deliberately generous (50%): scoring is anchored on quotedOutput (the full
# quote), NOT on min, so the min is purely the execution slippage guard here.
# A loose floor lets any solver that produces a real swap EXECUTE and be graded
# continuously on output-vs-quote, instead of reverting (and scoring 0) the
# moment it lands a few % under a tight floor. This does NOT re-introduce the
# old on-chain saturation — that bug existed only because the score was anchored
# on min; decoupled, a loose min is safe. Live user orders keep their own tight
# slippage tolerance (this constant is benchmark-only).
BENCHMARK_MIN_SLIPPAGE_BPS = 5000


async def _enrich_state_with_quote(
    session: "SolverSession",
    intent: AppIntentDefinition,
    state: IntentState,
    snapshot: MarketSnapshot,
    reference_params: dict[str, str] | None,
) -> IntentState:
    """Populate a swap scenario's source:"quote" params from a real quote.

    Synthetic benchmark scenarios never run a quote, so their on-chain
    intentParams omit the CoW ``quoted_output`` field — the deployed
    12-field DexAggregator scoreIntent then reverts. This mirrors the live
    get_quote path: obtain a quote (a provided reference, else the solver's
    own), map it via the shared ``map_quote_result_to_params`` helper, and
    rebuild the IntentState with those quote VALUES winning over the
    scenario's params.

    Returns the original ``state`` unchanged when: it isn't a swap intent,
    the params already carry ``quoted_output``, no manifest/quote params
    apply, or quoting fails (the scenario then scores 0 as it does today —
    never a crash).
    """
    raw = state.raw_params_view()
    intent_function = state.control_view().get("_intent_function", "swap")

    # Manifest-driven gate (NOT intent.intent_type — that field is empty for
    # the live DexAggregator app, so keying on it makes this a no-op on prod).
    # Enrich iff the manifest declares source:"quote" params for this function
    # that aren't already populated. Real/historical orders carry their quote
    # values, so they're skipped; synthetic scenarios don't, so they're filled.
    from minotaur_subnet.api.services.app_service import source_quote_param_names
    quote_param_names = source_quote_param_names(
        getattr(intent, "manifest", None), intent_function,
    )
    if not quote_param_names:
        return state  # nothing sourced from a quote → leave as-is
    # `quoted_output` is the canonical "was this quoted" marker: synthetic
    # scenarios never carry it (they only set static input/output/min_output),
    # while real/historical orders always do. (min_output_amount can't be the
    # marker — scenarios set it statically.)
    if raw.get("quoted_output") not in (None, ""):
        return state  # already quoted (real/historical order) — leave as-is

    quote_params = reference_params
    if not quote_params:
        # Fallback: self-quote via the solver session, mapped through the
        # one shared helper so there is no second quote implementation.
        try:
            quote_result = await session.quote(intent, state, snapshot)
        except Exception as exc:  # noqa: BLE001 — defensive, never crash a run
            logger.error(
                "[quote-FAILED] self-quote raised for %s (%s); scenario keeps "
                "the legacy (un-quoted) layout and will revert/score 0 — a real "
                "failure, not a silent pass", intent.app_id, exc,
            )
            return state
        if quote_result is None:
            logger.error(
                "[quote-FAILED] self-quote returned None for %s; scenario keeps "
                "the legacy (un-quoted) layout and will revert/score 0 — a real "
                "failure, not a silent pass", intent.app_id,
            )
            return state
        from minotaur_subnet.api.services.app_service import (
            map_quote_result_to_params,
        )
        quote_params = map_quote_result_to_params(
            quote_result, intent.manifest, intent_function,
            slippage_bps=BENCHMARK_MIN_SLIPPAGE_BPS,  # loose benchmark floor
        )

    if not quote_params:
        return state

    # Quote VALUES win over the scenario's params for ALL source:"quote" fields,
    # INCLUDING min_output_amount. Scoring is anchored on quotedOutput (the full
    # quote), so the min is purely the execution slippage guard — we let it track
    # the quote (quote × (1 − benchmark slippage)) rather than pinning the
    # scenario's stale STATIC floor. A static floor goes stale: a min set when
    # WETH was ~$2000 sits ABOVE a $1777 market and reverts EVERY solver with
    # "Too little received" (the WETH→USDC bug). A quote-relative loose floor
    # never goes stale and never spuriously reverts, while quoted_output keeps
    # the score (and CoW fee) honest.
    new_raw = {**raw, **quote_params}
    return IntentState(
        contract_address=state.contract_address,
        chain_id=state.chain_id,
        nonce=state.nonce,
        owner=state.owner,
        raw_params=new_raw,
        control=state.control_view(),
        context_version=state.context_version,
        policy_tier=state.policy_tier,
    )


async def run_benchmark(
    session: SolverSession,
    intents: list[tuple[AppIntentDefinition, IntentState, MarketSnapshot]],
    config: BenchmarkConfig | None = None,
    trigger_ground_truth: dict[str, bool] | None = None,
    score_fn: ScoreFn | None = None,
    simulator: Any | None = None,
    fork_block: int | None = None,
    require_real_sim: bool = False,
    reference_quotes: dict[str, dict[str, str]] | None = None,
) -> list[BenchmarkResult]:
    """Run a complete benchmark against a solver session.

    Executes the full lifecycle: init → metadata → benchmark_start →
    (generate_plan / check_trigger per intent) → score → benchmark_end.

    Args:
        session: An active SolverSession.
        intents: List of (intent, state, snapshot) tuples to benchmark.
        config: Benchmark configuration. Defaults to standard config.
        trigger_ground_truth: For auto-triggered intents, the correct
            trigger decision keyed by intent app_id. Used for scoring
            trigger accuracy.
        score_fn: Optional async callback to score plans. Signature:
            async (app_id, plan, simulation, state) -> ScoreResult.
            If None, plans are not scored (score stays 0.0).
        fork_block: Optional historical block to pin the Anvil fork to for
            every simulation in this run (forwarded to ``simulator.simulate``,
            which resets the fork to that block). ``None`` (default) leaves the
            fork at upstream head — the existing live-head behavior. This is
            the keystone that makes a benchmark round reproducible across
            validators: all of them re-simulate at the same pinned block.
        require_real_sim: Fail-closed switch (default ``False``). When ``True``,
            the benchmark refuses to substitute the fabricated mock for a real
            simulation: if no simulator is injected it raises
            ``RealSimulationUnavailable``; if a real ``simulate()`` throws OR
            returns a reverted (``success=False``) result, that scenario is
            scored 0 — never laundered into a ~min*1.05 mock pass nor a
            lenient-app-scorer pass on a plan that could not execute. Default
            keeps today's silent mock fallback.
        reference_quotes: Optional pre-computed quote params keyed by a stable
            per-scenario key (the intent's intent_id label). Each value is the
            ``map_quote_result_to_params`` output for that scenario (the CoW
            ``quoted_output`` and friends). Synthetic benchmark scenarios carry
            no quote, so without this the on-chain encoder emits the legacy
            layout and the deployed CoW scoreIntent reverts. When a scenario's
            swap params lack ``quoted_output``, the reference quote is applied;
            absent a reference, the solver session is asked for its own quote
            as a fallback. ``None``/``{}`` = always self-quote (still fixes the
            revert; the reference path just makes the quote champion-anchored).

    Returns:
        List of BenchmarkResult, one per intent.
    """
    if config is None:
        config = BenchmarkConfig()
    if trigger_ground_truth is None:
        trigger_ground_truth = {}
    if reference_quotes is None:
        reference_quotes = {}

    # Fail-closed: when a real simulation is required but none was injected,
    # refuse to run rather than score every scenario on the fabricated mock
    # (which reports ~min*1.05 success and can be gamed). The worker loop logs
    # this and retries each tick; it does not crash the process.
    if require_real_sim and simulator is None:
        raise RealSimulationUnavailable(
            "require_real_sim is set but no simulator was injected — refusing "
            "to benchmark on fabricated mock simulation data."
        )

    results: list[BenchmarkResult] = []

    # Initialize — pass RPC URLs so Docker solvers can query pool states
    init_config: dict[str, Any] = {
        "chain_ids": config.chain_ids,
        "timeout_per_plan_ms": config.timeout_per_plan_ms,
    }
    # Build per-chain RPC URL map. Prefer sandbox-specific IPs (BENCHMARK_ANVIL_RPC_*)
    # when a benchmark network is configured, falling back to the standard env vars.
    _bench_net = os.environ.get("BENCHMARK_DOCKER_NETWORK", "").strip()
    rpc_map: dict[int, str] = {}
    _rpc_sources = {
        1:     ("BENCHMARK_ANVIL_RPC_ETH", "ANVIL_RPC_URL"),
        31337: ("BENCHMARK_ANVIL_RPC_ETH", "ANVIL_RPC_URL"),
        8453:  ("BENCHMARK_ANVIL_RPC_BASE", "BASE_SIM_RPC_URL", "BASE_RPC_URL"),
        964:   ("BENCHMARK_ANVIL_RPC_BTEVM", "BITTENSOR_EVM_SIM_RPC_URL", "BITTENSOR_EVM_RPC_URL"),
    }
    for cid in config.chain_ids:
        sources = _rpc_sources.get(cid, ("ANVIL_RPC_URL",))
        for env_name in sources:
            url = os.environ.get(env_name, "").strip()
            if url:
                rpc_map[cid] = url
                break
    if rpc_map:
        init_config["rpc_urls"] = {str(k): v for k, v in rpc_map.items()}
    await session.initialize(init_config)

    # Get metadata for logging
    try:
        meta = await session.metadata()
        logger.info(
            "Benchmarking solver: %s v%s by %s",
            meta.name, meta.version, meta.author,
        )
    except Exception as exc:
        logger.warning("Could not get metadata: %s", exc)

    # Signal benchmark start
    await session.on_benchmark_start(len(intents))

    # Process each intent
    for intent, state, snapshot in intents:
        start = time.monotonic()
        scenario_name = state.control_view().get("_scenario_name", "")
        intent_label = f"{intent.app_id}:{scenario_name}" if scenario_name else intent.app_id
        br = BenchmarkResult(intent_id=intent_label)

        # Champion BLIND SPOT. The reference pre-pass marked this scenario as one
        # the CHAMPION could not quote (surfaced/logged there — the anti-masking
        # guarantee). We do NOT zero it: instead each solver SELF-QUOTES, so a
        # challenger that CAN quote + execute this order reveals a real capability
        # the champion lacks. The self-quote still requires a real on-chain
        # execution to score, so it can't fabricate capability — and the champion
        # self-quotes the same way (it fails → scores 0), so the champion's 0 is
        # the floor and any real execution here is unambiguous progress. On an
        # order the champion can't process at all, any progress is good progress.
        # (Under-quoting can inflate the MAGNITUDE of that progress, not its
        # existence; capping the per-blind-spot contribution is a future guard for
        # when champion ADOPTION is live, not for observe-only progress reveal.)
        _ref = reference_quotes.get(intent_label)
        if _ref and _ref.get(REFERENCE_QUOTE_FAILED_SENTINEL):
            logger.warning(
                "[champion-blind-spot] %s: champion could not quote; this solver "
                "self-quotes to reveal capability (champion scores 0 here)",
                intent_label,
            )
            _ref = None  # fall through to the self-quote path below

        # Quote-at-benchmark: synthetic scenarios never ran a quote, so their
        # on-chain intentParams omit the CoW `quoted_output` field and the
        # deployed scoreIntent reverts. Populate the source:"quote" params from
        # a REAL quote — exactly like the live get_quote path — so downstream
        # _build_benchmark_intent_order emits the full (CoW) layout.
        state = await _enrich_state_with_quote(
            session, intent, state, snapshot, _ref,
        )

        try:
            from minotaur_subnet.shared.types import TriggerType

            is_auto = (
                intent.config.trigger_type == TriggerType.AUTO_TRIGGERED
            )

            # For auto-triggered intents, check trigger first
            if is_auto:
                br.trigger_decision = await session.check_trigger(
                    intent, state, snapshot,
                )

            # Generate plan
            plan = await session.generate_plan(intent, state, snapshot)
            br.plan = plan

            # Score the plan if a scoring function is provided
            if plan is not None and score_fn is not None:
                try:
                    # Use real Anvil simulation when available, fall back to mock.
                    # Mock simulation results MUST NOT be used for champion ranking
                    # — they fabricate passing scores (~5% above minimum) and can
                    # be exploited to inflate benchmark results.
                    used_mock = False
                    fail_closed_miss = False
                    if simulator is not None:
                        try:
                            token_balances = _build_token_balances(state)
                            # Ensure the plan's metadata carries chain_id so the
                            # MultiChainSimulator routes to the correct Anvil fork.
                            # Without this, it defaults to chain 31337 (Ethereum).
                            if state and state.chain_id and plan:
                                if plan.metadata is None:
                                    plan.metadata = {}
                                if "chain_id" not in plan.metadata:
                                    plan.metadata["chain_id"] = state.chain_id
                            # Build intent_order so the simulator uses the
                            # full scoreIntent contract path (proxy deploy,
                            # token funding, plan execution, transfer capture)
                            # instead of the bare interaction path.
                            intent_order = _build_benchmark_intent_order(
                                state, plan,
                            ) if state and state.contract_address else None
                            sim = await simulator.simulate(
                                plan,
                                contract_address=state.contract_address if state else None,
                                intent_order=intent_order,
                                token_balances=token_balances,
                                fork_block=fork_block,
                            )
                            print(f"[BENCHMARK] Simulation: success={sim.success} transfers={len(sim.token_transfers)} gas={sim.gas_used} error={sim.error}", flush=True)
                            if require_real_sim and not sim.success:
                                # Fail-closed: a real simulation that REVERTED
                                # (success=False) means the plan could not
                                # execute. Don't hand it to the scorer — a lenient
                                # app JS scorer doesn't hard-gate on success and
                                # could still pass it. Score 0, exactly like a
                                # genuine on-chain revert.
                                logger.warning(
                                    "Simulation reverted for %s and "
                                    "require_real_sim is set; scoring 0: %s",
                                    intent.app_id, sim.error,
                                )
                                br.error = f"real_sim_reverted: {sim.error}"
                                fail_closed_miss = True
                        except Exception as sim_exc:
                            if require_real_sim:
                                # Fail-closed: do NOT fabricate a passing mock.
                                # Leave the scenario at score 0 (the same outcome
                                # as an on-chain revert) so a flaky Anvil can't be
                                # laundered into a ~min*1.05 passing score.
                                logger.warning(
                                    "Anvil simulation failed for %s and "
                                    "require_real_sim is set; scoring 0 (no mock "
                                    "fallback): %s",
                                    intent.app_id, sim_exc,
                                )
                                br.error = f"real_sim_unavailable: {sim_exc}"
                                fail_closed_miss = True
                            else:
                                logger.warning(
                                    "Anvil simulation failed for %s, falling back to mock: %s",
                                    intent.app_id, sim_exc,
                                )
                                sim = _build_benchmark_simulation(plan, state)
                                used_mock = True
                    else:
                        sim = _build_benchmark_simulation(plan, state)
                        used_mock = True
                    if not fail_closed_miss:
                        br.mock_simulation = used_mock
                        # Capture the unfakeable on-chain scoreIntent BPS (was dropped
                        # here). Used by the opt-in on-chain-ranked adoption rule.
                        br.on_chain_score = getattr(sim, "on_chain_score", None)
                        score_result = await score_fn(
                            intent.app_id, plan, sim, state,
                        )
                        br.plan_score = score_result.score
                        br.score_breakdown = score_result.breakdown

                        # Compute composite score for auto-triggered intents
                        if is_auto and br.trigger_decision is not None:
                            gt = trigger_ground_truth.get(intent.app_id)
                            if gt is not None:
                                trigger_correct = (br.trigger_decision == gt)
                                br.trigger_score = 1.0 if trigger_correct else 0.0
                                br.score = (
                                    config.auto_trigger_weight * br.trigger_score
                                    + config.plan_quality_weight * score_result.score
                                )
                            else:
                                br.score = score_result.score
                        else:
                            br.score = score_result.score

                except Exception as exc:
                    logger.warning(
                        "Scoring failed for %s: %s", intent.app_id, exc,
                    )
                    br.error = f"scoring_error: {exc}"

        except SolverTimeoutError as exc:
            br.error = f"timeout: {exc}"
        except SolverCrashedError as exc:
            br.error = f"crashed: {exc}"
            # If the solver crashed, we can't continue
            br.elapsed_ms = int((time.monotonic() - start) * 1000)
            results.append(br)
            break
        except Exception as exc:
            br.error = f"error: {exc}"

        br.elapsed_ms = int((time.monotonic() - start) * 1000)
        results.append(br)

    # Signal benchmark end with final scores
    summary = [
        {"intent_id": r.intent_id, "score": r.score, "elapsed_ms": r.elapsed_ms}
        for r in results
    ]
    try:
        await session.on_benchmark_end(summary)
    except (SolverTimeoutError, SolverCrashedError):
        pass

    return results


def _build_benchmark_intent_order(
    state: IntentState,
    plan: ExecutionPlan,
) -> dict[str, Any] | None:
    """Build an intent_order dict for benchmark simulation.

    This enables the simulator's scoreIntent contract path (proxy deploy,
    token funding, plan execution, transfer capture) instead of the bare
    interaction path which runs each call independently and captures no
    meaningful token transfers.

    Mirrors the intent_order construction in order_processor.py (line ~284).
    """
    contract_address = state.contract_address
    if not contract_address:
        return None

    params = state.raw_params_view() if hasattr(state, "raw_params_view") else {}
    control = state.control_view() if hasattr(state, "control_view") else {}

    # Use Anvil default account instead of dummy address(1)
    _ANVIL_DEFAULT_ACCOUNT = "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"
    submitted_by = params.get("receiver") or _ANVIL_DEFAULT_ACCOUNT
    if submitted_by == "0x0000000000000000000000000000000000000001":
        submitted_by = _ANVIL_DEFAULT_ACCOUNT

    # Build ABI-encoded intentParams (same as submit-order endpoint)
    intent_params_hex = ""
    try:
        from minotaur_subnet.api.services.order_service import build_swap_intent_params_hex
        # Use the Anvil-friendly submitted_by already computed above
        # (not the scenario's dummy receiver which may be address(1))
        bench_params = {**params, "receiver": submitted_by}
        hex_result = build_swap_intent_params_hex(bench_params, submitted_by)
        if hex_result:
            intent_params_hex = hex_result
            print(f"[BENCHMARK] Built intent_params_hex: {len(hex_result)} chars for {submitted_by[:10]}", flush=True)
        else:
            print(f"[BENCHMARK] build_swap_intent_params_hex returned None. params keys: {list(bench_params.keys())}", flush=True)
    except Exception as exc:
        print(f"[BENCHMARK] _build_benchmark_intent_order encoding FAILED: {exc}", flush=True)

    if not intent_params_hex:
        return None

    # Resolve intent selector
    from eth_hash.auto import keccak as _keccak
    fn_name = control.get("_intent_function", "swap")
    _KNOWN_SIGS = {
        "swap": "swap(address,address,uint256,uint256,address)",
        "execute": "swap(address,address,uint256,uint256,address)",
    }
    sig = _KNOWN_SIGS.get(fn_name, f"{fn_name}()")
    selector = _keccak(sig.encode())[:4].hex()

    # Unique order_id per scenario to avoid CREATE2 proxy collision.
    import uuid

    return {
        "order_id": f"bench_{uuid.uuid4().hex[:16]}",
        "app": contract_address,
        "intent_selector": selector,
        "intent_params": intent_params_hex,
        "submitted_by": submitted_by,
        "chain_id": state.chain_id,
        "deadline": int(time.time()) + 3600,
        "nonce": 0,
        "perpetual": False,
        "max_executions": 1,
        "cooldown": 0,
        "params": params,  # For _input_token_is_native etc.
    }


def _build_token_balances(state: IntentState | None) -> dict[str, int] | None:
    """Extract token balances to pre-fund the simulator executor from state.

    Checks for an explicit ``_fund`` dict first (declared by the app developer
    in their manifest's ``benchmark_scenarios[].fund``), then falls back to the
    ``input_token`` / ``input_amount`` convention for simple swap-like intents.

    Without this fallback, Stage-2 historical replays always score 0 because
    the original order's ``submitted_by`` address has no token balance on the
    anvil fork, so the contract's ``safeTransferFrom(submitted_by, proxy, ...)``
    reverts before the swap can execute. Historical orders don't carry a
    ``_fund`` field (only manifest scenarios do), so we must synthesize from
    the order's own params.
    """
    if state is None:
        return None
    control = state.control_view() if hasattr(state, "control_view") else {}

    # 1. Explicit fund map from manifest scenario (authoritative when present).
    fund = control.get("_fund")
    if fund and isinstance(fund, dict):
        balances: dict[str, int] = {}
        for token_addr, amount in fund.items():
            try:
                balances[token_addr] = int(amount)
            except (ValueError, TypeError):
                continue
        if balances:
            return balances

    # 2. Fallback: pre-fund submitted_by with input_amount of input_token.
    # Necessary for Stage-2 historical replays — the original submitted_by
    # address has no balance on the fork, so the scoreIntent path would
    # revert in safeTransferFrom otherwise.
    params = state.raw_params_view() if hasattr(state, "raw_params_view") else {}
    input_token = params.get("input_token")
    input_amount_raw = params.get("input_amount")
    if input_token and input_amount_raw is not None:
        try:
            amt = int(input_amount_raw)
            if amt > 0:
                return {input_token: amt}
        except (ValueError, TypeError):
            pass

    return None


def _build_benchmark_simulation(
    plan: ExecutionPlan, state: IntentState | None = None,
) -> SimulationResult:
    """Build a mock SimulationResult for benchmark scoring.

    WARNING: Mock simulation results MUST NOT be used for champion ranking.
    This function fabricates passing results (~5% above minimum output),
    which can be exploited to inflate benchmark scores.  Results scored
    with this mock are flagged via ``BenchmarkResult.mock_simulation = True``
    and are heavily penalized during ranking.

    In a full production setup, plans would be simulated against a forked
    chain. For the MVP, we construct a plausible result from the plan
    metadata and state, synthesizing token transfers so JS scoring can
    evaluate plan quality.
    """
    from minotaur_subnet.shared.types import TokenTransfer

    gas_per_interaction = 80_000
    gas_used = 21_000 + len(plan.interactions) * gas_per_interaction

    # Synthesize token transfers from plan metadata for swap-type intents
    transfers: list[TokenTransfer] = []
    meta = plan.metadata or {}
    extra = _state_params(state)
    output_token = meta.get("output_token") or extra.get("output_token", "")
    min_output = meta.get("min_output_amount") or extra.get("min_output_amount", "")
    receiver = (
        getattr(getattr(state, "typed_context", None), "receiver", "")
        or extra.get("receiver")
        or (state.contract_address if state else "")
    )

    if output_token and min_output and receiver:
        # Simulate output delivery: solver achieves ~5% above minimum
        try:
            amount = str(int(int(min_output) * 1.05))
        except (ValueError, TypeError):
            amount = str(min_output)
        transfers.append(TokenTransfer(
            token=output_token,
            from_addr="0x" + "00" * 20,  # pool/router
            to_addr=receiver,
            amount=amount,
        ))

    return SimulationResult(
        success=True,
        gas_used=gas_used,
        token_transfers=transfers,
        state_changes=[],
    )


def _state_params(state: IntentState | None) -> dict[str, Any]:
    if state is None:
        return {}
    typed = getattr(state, "typed_context", None)
    if typed is not None:
        raw = getattr(typed, "raw_params", None)
        if isinstance(raw, dict):
            return raw
    return state.raw_params_view()
