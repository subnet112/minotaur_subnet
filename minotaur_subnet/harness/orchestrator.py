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
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from minotaur_subnet.shared.types import (
    AppIntentDefinition,
    ExecutionPlan,
    IntentState,
    ScoreResult,
    SimulationResult,
)
from minotaur_subnet.sdk.intent_solver import MarketSnapshot, SolverMetadata
from minotaur_subnet.harness.solver_read_proxy import (
    CHAIN_NAMES,
    budget_enforced,
    build_pin_blocks,
    close_session,
    generate_plan_recv_timeout,
    open_session,
    proxy_rpc_url,
    read_proxy_config,
    reset_session,
)
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


async def _docker_rm_f(name: str) -> None:
    """Best-effort ``docker rm -f <name>`` that also REAPS its own subprocess.

    SIGKILL of a ``docker run`` CLI does NOT stop the container it is attached
    to, so ``proc.wait()`` on the CLI can hang and the CLI process (each ~6 Go
    runtime threads) leaks. Removing the *container* releases the CLI so it
    exits and can be reaped — turning the "lingering zombie" the comment above
    tolerates into an actual reap. Bounded + swallow-all so it can never block
    or raise on the cleanup path. No-op without a name.
    """
    if not name:
        return
    try:
        rm = await asyncio.create_subprocess_exec(
            "docker", "rm", "-f", name,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(rm.wait(), timeout=_KILL_REAP_TIMEOUT)
    except Exception:  # noqa: BLE001 — cleanup path, never propagate
        pass

# Trailing stderr lines kept per session for crash diagnostics (surfaced in the
# SolverCrashedError when a solver dies / hangs). Bounded so a chatty solver
# can't grow memory without bound.
_STDERR_TAIL_LINES = 50

# Transient RPC / upstream-provider failure signatures. A solver quotes/routes
# against a live provider (e.g. Alchemy) from inside its container; when that
# provider rate-limits, times out, or 5xx's, the solver silently produces NO plan
# for the affected order. The scorer then records that order as a blind spot /
# drop and zeroes it — indistinguishable, today, from the solver being genuinely
# unable to serve the pair. That misattribution is a MINER-FAIRNESS bug: a miner
# is scored down for the provider's hiccup, not its own capability. These
# signatures let us SURFACE + COUNT such failures (see `_classify_rpc_error`).
# OBSERVABILITY ONLY — never feeds scoring, benchmark results, or the pack hash.
_RPC_ERROR_SIGNATURES: tuple[str, ...] = (
    "429", "too many requests", "rate limit", "rate-limit", "ratelimit",
    "exceeded your", "compute unit", "over capacity", "throughput",
    "timeout", "timed out", "etimedout", "esockettimedout",
    "econnreset", "connection reset", "connection refused", "econnrefused",
    "socket hang up", "fetch failed", "network error",
    "bad gateway", "service unavailable", "gateway timeout",
    "alchemy", "provider error", "json-rpc error", "-32005", "-32603",
)


def _classify_rpc_error(text: str | None) -> str | None:
    """Return the first transient-RPC/provider signature found in ``text``, else
    ``None``. Pure + case-insensitive; used only to label + count failures for the
    fairness audit — it never changes any benchmark outcome."""
    if not text:
        return None
    low = text.lower()
    for sig in _RPC_ERROR_SIGNATURES:
        if sig in low:
            return sig
    return None


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


def _revert_trace_budget() -> int:
    """How many reverted cases per run to capture a per-step trace for.

    Re-executing the plan for a trace is pure diagnostics (never touches the
    score or the pack hash), but it is extra work on the scoring path — so it's
    bounded per run and disableable. ``BENCHMARK_REVERT_TRACE_MAX=0`` turns it
    off; default 10.
    """
    raw = os.environ.get("BENCHMARK_REVERT_TRACE_MAX", "10").strip()
    try:
        return max(0, int(raw))
    except ValueError:
        return 10


def _capture_revert_trace(
    simulator: Any, plan: Any, token_balances: dict[str, int] | None,
) -> dict[str, Any] | None:
    """Best-effort per-step interaction trace for a reverted plan. Never raises.

    Mirrors the local-testnet replay path: resolves the per-chain AnvilSimulator
    from a MultiChainSimulator and calls its ``simulate_with_trace``.
    """
    try:
        sim = simulator._get_simulator(plan) if hasattr(simulator, "_get_simulator") else simulator
        runner = getattr(sim, "simulate_with_trace", None)
        if runner is None:
            return None
        trace = runner(plan, token_balances=token_balances or {})
        return trace if isinstance(trace, dict) else None
    except Exception as exc:  # noqa: BLE001 — diagnostics must never break scoring
        logger.debug("revert trace capture failed: %s", exc)
        return None


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
    # RAW delivered output from the LIVE raw-output scorer's metadata.raw_output
    # (consumed by relative_scoring). An EXACT DECIMAL WEI STRING (not a float) so
    # token amounts above 2^53 keep full precision end-to-end. None when the live
    # scorer emits no raw_output (pre-cutover scorer); "0" when the order delivered
    # nothing / fell below min. The per-order signal the relative adoption rule
    # consumes; NEVER feeds the aggregate `score`. (Formerly ``shadow_score`` — the
    # observe-only shadow scorer it was named after is gone.)
    raw_output: str | None = None
    revert_reason: str | None = None  # decoded on-chain revert reason when the real sim reverted
    # Per-step interaction trace ({interactions, total_gas, summary}) captured on
    # a real-sim revert — pure diagnostics for the miner; never feeds the score.
    revert_trace: dict[str, Any] | None = None


@dataclass
class _BenchmarkRuntime:
    """One isolated execution unit for the benchmark scenario pool.

    A solver session plus its dedicated read-proxy session id. K runtimes run
    scenarios concurrently; each is fully isolated (own solver subprocess, own
    proxy budget), so scores stay byte-identical and order-independent. K=1 (a
    single runtime over the existing session) is byte-identical to the legacy
    sequential loop. The simulator is shared and serializes on its own per-fork
    lock — a safe, small serialized tail.
    """
    session: "SolverSession"
    proxy_session_id: str | None = None
    # The per-runtime init_config (with THIS runtime's proxy rpc_urls), so a
    # mid-run respawn re-initializes through the same proxy/budget — never
    # another runtime's.
    init_config: "dict[str, Any] | None" = None


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
        container_name: str = "",
    ) -> None:
        self._proc = proc
        self._label = label
        # Name of the docker container backing this session (Docker mode only).
        # Lets kill() force-remove it so a hung `docker run` CLI reaps instead
        # of leaking its threads. Empty for subprocess mode.
        self._container_name = container_name
        self._start_time = time.monotonic()
        self._closed = False
        # live_mode=True disables the total elapsed-time cap. Per-command
        # timeouts still apply. Used for long-lived runtime solvers that
        # serve quotes/plans for real user orders (DockerRuntimeSolver),
        # where session lifetime is the container's lifetime, not a
        # single benchmark run.
        self._live_mode = live_mode
        # Set by SolverOrchestrator.start_docker/start_subprocess to a 0-arg
        # async closure that relaunches the underlying process with the SAME
        # image/args. Enables ``restart()`` so the benchmark can recover from a
        # per-scenario timeout/crash WITHOUT truncating the rest of the run.
        self._relaunch: Any = None
        # The process is launched with stderr=PIPE. If NOTHING reads it, a chatty
        # solver fills the ~64KB kernel pipe buffer and BLOCKS on its next stderr
        # write — which stalls quoting until the per-command timeout kills it, after
        # which every later scenario sees a dead process ("Solver process is not
        # running"). Drain it continuously in the background, keeping a bounded tail
        # for crash diagnostics.
        self._stderr_tail: deque[str] = deque(maxlen=_STDERR_TAIL_LINES)
        self._stderr_task: Any = None
        # Transient RPC/provider errors (Alchemy rate-limit, timeout, 5xx) seen on
        # this session's stderr or protocol responses. Surfaced + counted for the
        # miner-fairness audit — such a failure makes the solver emit no plan, which
        # the scorer misreads as a blind spot / drop and zeroes the order.
        # OBSERVABILITY ONLY: never read by scoring, results, or the pack hash.
        self._rpc_error_count: int = 0
        self._rpc_error_samples: deque[str] = deque(maxlen=8)
        self._begin_stderr_drain()

    def _begin_stderr_drain(self) -> None:
        """(Re)start the background task draining the current process's stderr.

        Called on construction and after ``restart()`` swaps the process. Idempotent
        — cancels any prior task first. No-op when there is no stderr pipe or no
        running event loop (e.g. a synchronous unit test constructing a session).
        """
        task = self._stderr_task
        if task is not None and not task.done():
            task.cancel()
        self._stderr_task = None
        stream = getattr(self._proc, "stderr", None)
        if stream is None:
            return
        try:
            self._stderr_task = asyncio.ensure_future(self._drain_stderr(stream))
        except RuntimeError:
            self._stderr_task = None

    async def _drain_stderr(self, stream: asyncio.StreamReader) -> None:
        """Continuously read the solver's stderr so its pipe never backs up.

        Takes the stream explicitly (not ``self._proc.stderr``) so a task started
        for the pre-restart process keeps draining IT, not the replacement.
        """
        try:
            while True:
                line = await stream.readline()
                if not line:
                    break
                text = line.decode("utf-8", "replace").rstrip()
                if text:
                    self._note_stderr_line(text)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — draining must never break the session
            logger.debug("[%s] stderr drain ended: %r", self._label, exc)

    def _note_stderr_line(self, text: str) -> None:
        """Record one stderr line: keep the bounded tail, and — for the fairness
        audit — SURFACE + COUNT transient RPC/provider errors that would otherwise
        be buried at DEBUG. A matching line is logged at WARNING because it likely
        caused a silent no-plan (a blind spot / drop that unfairly zeroes an order).
        Observability only — this never touches scoring, results, or the pack hash."""
        self._stderr_tail.append(text)
        sig = _classify_rpc_error(text)
        if sig is None:
            logger.debug("[%s solver-stderr] %s", self._label, text)
            return
        self._rpc_error_count += 1
        self._rpc_error_samples.append(text)
        logger.warning(
            "[%s solver-rpc-error] transient RPC/provider failure (%s) during "
            "benchmark — may silently zero an order (miner-fairness impact): %s",
            self._label, sig, text,
        )

    def _note_protocol_rpc_error(self, error: Any) -> str | None:
        """Classify + count a protocol-level failure (``resp.error``) as a transient
        RPC/provider error. Returns the matched signature (for the caller's log) or
        ``None``. Observability only."""
        sig = _classify_rpc_error(str(error) if error is not None else None)
        if sig is not None:
            self._rpc_error_count += 1
            self._rpc_error_samples.append(str(error))
        return sig

    def rpc_error_report(self) -> tuple[int, list[str]]:
        """``(count, sample lines)`` of transient RPC/provider errors seen on this
        session — for the miner-fairness audit. Never affects any benchmark outcome."""
        return self._rpc_error_count, list(getattr(self, "_rpc_error_samples", ()))

    def _stderr_snapshot(self) -> str:
        """The last captured stderr lines, for surfacing in a crash error."""
        tail = getattr(self, "_stderr_tail", None)
        return " | ".join(tail) if tail else "no stderr captured"

    async def restart(self) -> None:
        """Relaunch the underlying solver process in place (same image/args).

        A per-scenario timeout kills the process (or the solver crashes), which
        previously cascaded: the next scenario hit the dead process and the run
        was truncated — non-deterministically, since *which* scenario is slow
        depends on RPC latency. ``restart()`` lets ``run_benchmark`` score only
        the offending scenario 0 and continue on a fresh process, so the result
        set stays the full corpus and is reproducible across hosts. Reuses the
        same SolverSession object so the caller's lifecycle (``shutdown``) is
        unchanged. Raises if no relaunch closure was wired.
        """
        if self._relaunch is None:
            raise SolverCrashedError("session has no relaunch closure; cannot restart")
        # Force-reap the old process directly (``kill()`` no-ops once _closed is
        # set, so it could leak a zombie on respawn).
        try:
            self._proc.kill()
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(self._proc.wait(), timeout=_KILL_REAP_TIMEOUT)
        except (asyncio.TimeoutError, ProcessLookupError):
            pass
        self._proc = await self._relaunch()
        self._closed = False
        self._start_time = time.monotonic()
        self._begin_stderr_drain()  # drain the NEW process's stderr too
        logger.info("[%s] Process respawned", self._label)

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
            sig = self._note_protocol_rpc_error(resp.error)
            logger.warning(
                "[%s] generate_plan failed for %s%s: %s",
                self._label, intent.app_id,
                f" [transient RPC/provider:{sig}]" if sig else "", resp.error,
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
            sig = self._note_protocol_rpc_error(resp.error)
            logger.warning(
                "[%s] quote failed for %s%s: %s",
                self._label, intent.app_id,
                f" [transient RPC/provider:{sig}]" if sig else "", resp.error,
            )
            return None
        return parse_quote_response(resp)

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
        task = self._stderr_task
        if task is not None and not task.done():
            task.cancel()
        try:
            self._proc.kill()
            # Bounded reap — never block the caller (and the runtime lock it
            # may hold) forever if child-reaping stalls. See _KILL_REAP_TIMEOUT.
            await asyncio.wait_for(self._proc.wait(), timeout=_KILL_REAP_TIMEOUT)
        except ProcessLookupError:
            pass
        except asyncio.TimeoutError:
            # SIGKILL of the `docker run` CLI doesn't stop the attached
            # container, so proc.wait() hangs and the CLI (+ its threads) leaks
            # — thousands accumulate over days and starve the api. Force-remove
            # the container to release the CLI, then retry the (now-unblocked)
            # reap. Only lingers if docker itself is wedged.
            if self._container_name:
                await _docker_rm_f(self._container_name)
                try:
                    await asyncio.wait_for(
                        self._proc.wait(), timeout=_KILL_REAP_TIMEOUT,
                    )
                except (asyncio.TimeoutError, ProcessLookupError):
                    logger.warning(
                        "[%s] proc.wait() still hung after docker rm -f %s; "
                        "abandoning reap",
                        self._label, self._container_name,
                    )
            else:
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
            raise SolverCrashedError(
                f"Solver process is not running (last stderr: {self._stderr_snapshot()})"
            )

        if self._proc.stdin is None or self._proc.stdout is None:
            raise SolverCrashedError("Solver process has no stdin/stdout")

        # Check total benchmark timeout (skipped in live mode — see __init__)
        if not self._live_mode and self.elapsed_total > TOTAL_BENCHMARK_TIMEOUT:
            await self.kill()
            raise SolverTimeoutError(
                f"Total benchmark timeout exceeded ({TOTAL_BENCHMARK_TIMEOUT}s)"
            )

        timeout = TIMEOUTS.get(request.command, 30.0)
        # When the deterministic RPC-read budget is the cutoff, the wall-clock
        # GENERATE_PLAN timeout is no longer the cutoff (it would re-introduce
        # cross-host non-determinism). Loosen it to a runaway backstop. No-op when
        # the budget is off (inert). Other commands keep their wall-clock.
        if request.command == Command.GENERATE_PLAN:
            timeout = generate_plan_recv_timeout(timeout)
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
            stderr_tail = self._stderr_snapshot()
            await self.kill()
            raise SolverTimeoutError(
                f"Command {request.command} timed out after {timeout}s "
                f"(last stderr: {stderr_tail})"
            )

        if not raw_line:
            self._closed = True
            raise SolverCrashedError(
                f"Solver process exited during {request.command} "
                f"(last stderr: {self._stderr_snapshot()})"
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
        # Content-addressed run chokepoint: if handed a <repo>@sha256:D digest
        # ref, pre-pull it so a host that didn't build the image (follower / fresh
        # node / restart) runs the exact certified bytes. Pull-by-digest is
        # self-verifying (the daemon rejects a manifest whose digest != D). This is
        # a SEPARATE subprocess carrying none of the run flags below. A local tag
        # is left untouched (no pull), so legacy behavior is unchanged.
        from minotaur_subnet.harness.image_transport import is_digest_ref
        if is_digest_ref(image):
            try:
                pull = await asyncio.create_subprocess_exec(
                    "docker", "pull", image,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                )
                out, _ = await asyncio.wait_for(pull.communicate(), timeout=600)
                if pull.returncode != 0:
                    logger.warning(
                        "Pre-pull of %s failed (rc=%s): %s — attempting run anyway "
                        "(image may be present locally)",
                        image, pull.returncode,
                        out.decode("utf-8", errors="replace")[:200],
                    )
            except (asyncio.TimeoutError, FileNotFoundError) as exc:
                logger.warning("Pre-pull of %s errored: %s — attempting run anyway", image, exc)

        # Name the container so a hung `docker run` CLI can be force-reaped by
        # kill() (docker rm -f) instead of leaking. Unique per session so
        # concurrent benchmark solvers never collide on the name.
        container_name = f"minotaur-bench-{uuid.uuid4().hex[:12]}"
        cmd = ["docker", "run", "--rm", "-i", "--name", container_name]
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

        async def _relaunch() -> asyncio.subprocess.Process:
            # Clear any container left over from a prior launch (name reuse on
            # restart) so `docker run --name` can't 409 on a leftover.
            await _docker_rm_f(container_name)
            return await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

        proc = await _relaunch()
        label = f"docker:{image.split(':')[0][-12:]}"
        session = SolverSession(
            proc, label=label, live_mode=live, container_name=container_name,
        )
        session._relaunch = _relaunch
        return session

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

        async def _relaunch() -> asyncio.subprocess.Process:
            return await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

        proc = await _relaunch()
        label = f"subprocess:{solver_path.split('/')[-1]}"
        session = SolverSession(proc, label=label)
        session._relaunch = _relaunch
        return session


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


def benchmark_static_quote_enabled() -> bool:
    """Flag: benchmark the STATIC-quote way (``BENCHMARK_STATIC_QUOTE=1``).

    When ON, the benchmark injects a static zero quote (``quotedOutput=0``,
    ``min=0``) instead of calling ``solver.quote()`` or the champion
    reference-quote pre-pass. This is safe because the authoritative score is
    the relative per-order RAW delivered output (no quote anchor), and the dex
    ``scoreIntent`` gates its CoW fee on ``quotedOutput > 0`` — so ``0`` means
    "no anchor, no fee, full output executes", which the raw scorer reads.
    The zero is only there to keep the 12-field on-chain ABI valid (omitting
    the field reverts on decode).

    DEFAULT OFF — the current champion-anchored reference-quote behavior is
    unchanged. This is the instant-revert switch for the sensitive scoring
    path: flip the env, no code change, to re-enable either mode at will.
    """
    import os

    return os.environ.get("BENCHMARK_STATIC_QUOTE", "0").strip().lower() in (
        "1", "true", "yes", "on",
    )


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

    from minotaur_subnet.api.services.app_service import (
        map_quote_result_to_params,
    )

    if benchmark_static_quote_enabled():
        # STATIC-quote mode (flag): skip quoting entirely. Inject a zero quote
        # so the 12-field ABI stays valid; scoreIntent gates its CoW fee on
        # quotedOutput>0 (so 0 = no fee, full output executes), and the
        # relative scorer reads the RAW delivered output — the quote is not in
        # the score. No solver.quote() call, no champion reference needed.
        from minotaur_subnet.shared.types import QuoteResult

        quote_params = map_quote_result_to_params(
            QuoteResult(estimated_output="0"), intent.manifest, intent_function,
            slippage_bps=BENCHMARK_MIN_SLIPPAGE_BPS,
        )
    else:
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


# Per-chain benchmark RPC sources, in priority order: sandbox-specific
# BENCHMARK_ANVIL_RPC_* IPs (reachable on BENCHMARK_DOCKER_NETWORK) first, then
# the standard env vars. Shared by run_benchmark AND the champion reference
# pre-pass so BOTH score against the SAME live chain state.
_BENCHMARK_RPC_SOURCES: dict[int, tuple[str, ...]] = {
    1:     ("BENCHMARK_ANVIL_RPC_ETH", "ANVIL_RPC_URL"),
    31337: ("BENCHMARK_ANVIL_RPC_ETH", "ANVIL_RPC_URL"),
    8453:  ("BENCHMARK_ANVIL_RPC_BASE", "BASE_SIM_RPC_URL", "BASE_RPC_URL"),
    964:   ("BENCHMARK_ANVIL_RPC_BTEVM", "BITTENSOR_EVM_SIM_RPC_URL", "BITTENSOR_EVM_RPC_URL"),
}


def build_rpc_url_map(chain_ids) -> dict[int, str]:
    """Resolve per-chain RPC URLs from the environment for benchmarking.

    Returns ``{chain_id: url}`` only for chains with a resolved RPC. A chain
    ABSENT from the result has NO live RPC — and a solver run without it would
    silently fall back to an incomplete on-chain snapshot (missing pools →
    false "No route" → corrupt scores). Callers MUST treat a missing chain as a
    loud failure, never a silent degradation.
    """
    rpc_map: dict[int, str] = {}
    for cid in chain_ids:
        for env_name in _BENCHMARK_RPC_SOURCES.get(cid, ("ANVIL_RPC_URL",)):
            url = os.environ.get(env_name, "").strip()
            if url:
                rpc_map[cid] = url
                break
    return rpc_map


def _pin_solver_read_block_enabled() -> bool:
    """Whether to pin the SOLVER's read fork to the round's fork_block before
    generate_plan (Phase 0 of the deterministic-budget work).

    CONSENSUS-RELEVANT: changes the block state the solver reads, hence its
    routes/quotes/scores. Must be fleet-uniform — ships OFF so it can soak
    inert on the lead (observe the revert/score effect under the adoption
    freeze) and be flipped fleet-wide together (folded into the pack hash) once
    proven, exactly like ROUND_ANCHORED_PIN. Default OFF.
    """
    return os.environ.get("PIN_SOLVER_READ_BLOCK", "").strip().lower() in (
        "1", "true", "yes", "on",
    )


# Proxy registry cap (rpc_budget_proxy.proxy.MAX_SESSIONS) minus one — the hard
# ceiling on concurrent benchmark runtimes, since each runtime opens one proxy
# session. The practical recommendation is far lower (2-4); see BENCHMARK_CONCURRENCY.
_BENCHMARK_MAX_CONCURRENCY = 63


def _benchmark_concurrency() -> int:
    """Number of isolated solver runtimes to shard the benchmark corpus across.

    The benchmark is network-latency-bound (solver quoting + pinned RPC reads on a
    ~90%-idle CPU), so K runtimes run scenarios concurrently for roughly K x on that
    segment. Default ``1`` = the byte-identical legacy single-runtime path — the
    KILL-SWITCH: set ``BENCHMARK_CONCURRENCY=1`` (or unset) to instantly revert with
    zero code change.

    Per-VALIDATOR, NOT consensus: K is never folded into ``benchmark_pack_hash`` (scores
    are order-independent and written back by input index), so a fleet running mixed K
    computes identical pack hashes and identical scores — no fleet coordination needed.
    Clamped to ``[1, _BENCHMARK_MAX_CONCURRENCY]`` (the proxy registry cap).
    """
    raw = os.environ.get("BENCHMARK_CONCURRENCY", "1").strip()
    try:
        k = int(raw)
    except ValueError:
        return 1
    return max(1, min(k, _BENCHMARK_MAX_CONCURRENCY))


async def _provision_extra_runtime(
    sess: "SolverSession",
    *,
    base_rpc_map: dict[int, str],
    pin_blocks: dict[str, int] | None,
    read_proxy: Any | None,
    fork_block: int | None,
    init_config_base: dict[str, Any],
    intents_len: int,
) -> tuple["_BenchmarkRuntime", str | None]:
    """Provision one ADDITIONAL benchmark runtime (only when BENCHMARK_CONCURRENCY > 1).

    Mirrors the primary session's setup so every runtime reads the SAME pinned state
    with its OWN isolated budget — the determinism requirement: each runtime opens its
    own block-pin proxy session (distinct id, since ``id(sess)`` differs per session,
    SAME ``pin_blocks``/budget), routes its reads through it, then initializes the solver
    and signals benchmark start. The per-session ``init_config`` is stored on the runtime
    so a mid-run respawn re-initializes through the SAME proxy (never another runtime's
    budget). Raises on proxy/init failure; the caller degrades gracefully to fewer
    runtimes and shuts the failed session down.
    """
    proxy_session_id: str | None = None
    rpc_map = dict(base_rpc_map)
    init_config = dict(init_config_base)
    if read_proxy is not None and pin_blocks:
        proxy_session_id = f"bench-{id(sess):x}-{fork_block}"
        rec = await open_session(read_proxy, proxy_session_id, pin_blocks)
        for cid in list(rpc_map):
            if cid in read_proxy.chain_ids and cid in CHAIN_NAMES:
                rpc_map[cid] = proxy_rpc_url(read_proxy, proxy_session_id, cid)
        logger.info(
            "[benchmark] solver reads routed via block-pin proxy session=%s pinned=%s",
            proxy_session_id, rec.get("blocks"),
        )
    if rpc_map:
        init_config["rpc_urls"] = {str(k): v for k, v in rpc_map.items()}
    await sess.initialize(init_config)
    await sess.on_benchmark_start(intents_len)
    return (
        _BenchmarkRuntime(
            session=sess, proxy_session_id=proxy_session_id, init_config=init_config,
        ),
        proxy_session_id,
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
    session_factory: "Callable[[], Awaitable[SolverSession]] | None" = None,
    session_count: int | None = None,
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

    # Initialize — pass RPC URLs so Docker solvers can query pool states
    init_config: dict[str, Any] = {
        "chain_ids": config.chain_ids,
        "timeout_per_plan_ms": config.timeout_per_plan_ms,
    }
    # Resolve live RPC for every chain we're about to benchmark. Without it the
    # solver silently falls back to an incomplete on-chain snapshot (missing
    # pools → false "No route" → corrupt scores) — so when real simulation is
    # required, FAIL LOUD rather than score on degraded data.
    rpc_map = build_rpc_url_map(config.chain_ids)
    # Pre-proxy snapshot — extra runtimes (K>1) re-route this base map through
    # their own proxy sessions (the primary session mutates rpc_map in place below).
    base_rpc_map = dict(rpc_map)
    missing_rpc = [c for c in config.chain_ids if c not in rpc_map]
    if missing_rpc:
        msg = (
            f"No benchmark RPC resolved for chain(s) {missing_rpc} — the solver "
            f"would fall back to an incomplete snapshot (degraded scoring). Set "
            f"BENCHMARK_ANVIL_RPC_* / *_SIM_RPC_URL / *_RPC_URL for these chains."
        )
        if require_real_sim:
            raise RealSimulationUnavailable(msg)
        logger.error("[benchmark] %s", msg)

    # SOLVER_READ_PROXY (split-fork): route the untrusted solver's reads for the
    # routed chain(s) through the block-pin proxy at the round's fork_block — one
    # fast upstream round-trip per call, pinned + deterministic on any archive
    # provider — instead of the Anvil fork (which lazily fetches every cold slot,
    # the timeout + cross-host non-determinism source). Inert unless set.
    _read_proxy = read_proxy_config()
    _proxy_session_id: str | None = None
    pin_blocks: dict[str, int] | None = None
    # Safety net (closes the silent-anvil determinism channel): the proxy IS the
    # deterministic read path. If it's configured but we have no fork_block to pin to,
    # refuse to benchmark via the raw, un-pinned anvil — defer LOUD rather than silently
    # diverge cross-host. Normally fork_block is always set when the proxy is active (the
    # worker resolves the round-anchored pin before benchmarking); this guards any path
    # that reaches run_benchmark without one (and the historical bug where the read-proxy
    # env failed to wire, so reads fell back to the anvil).
    if _read_proxy is not None and rpc_map and fork_block is None:
        raise RealSimulationUnavailable(
            "SOLVER_READ_PROXY is configured (the deterministic read path) but no "
            "fork_block was resolved for this benchmark — refusing to read the raw anvil "
            "(non-deterministic, silent cross-host divergence)."
        )
    if _read_proxy is not None and fork_block is not None and rpc_map:
        pin_blocks = build_pin_blocks(_read_proxy, rpc_map, fork_block)
        # Fail-CLOSED on a non-routed chain — BEFORE opening any session (no leak)
        # AND before the `if pin_blocks:` branch, so an all-unrouted benchmark (no
        # routed chain → empty pin_blocks) ALSO fails loud rather than silently
        # handing the solver raw/dead URLs. Once the proxy is the configured read
        # path, EVERY benchmarked chain MUST be routed through it: the solver runs
        # on the sealed sandbox net where only the proxy is reachable, so any chain
        # left on a raw upstream URL is (a) unreachable → silent mis-score and (b)
        # the exact un-pinned, un-budgeted hole this hardening closes. A Base-only
        # round is unaffected (its one chain is routed); this only fires if a
        # future scenario benchmarks a chain not in SOLVER_READ_PROXY_CHAINS.
        unrouted = [
            cid for cid in rpc_map
            if cid not in _read_proxy.chain_ids or cid not in CHAIN_NAMES
        ]
        if unrouted:
            raise RealSimulationUnavailable(
                f"SOLVER_READ_PROXY_CHAINS={sorted(_read_proxy.chain_ids)} but "
                f"benchmark chain(s) {sorted(unrouted)} are NOT routed through the "
                f"block-pin proxy — the solver (sealed sandbox net) would either "
                f"fail to reach them or bypass the pin/budget. Add them to "
                f"SOLVER_READ_PROXY_CHAINS (fleet-wide) before benchmarking them."
            )
        if pin_blocks:
            _proxy_session_id = f"bench-{id(session):x}-{fork_block}"
            try:
                rec = await open_session(_read_proxy, _proxy_session_id, pin_blocks)
            except Exception as exc:  # noqa: BLE001
                # Fail loud: a silent fallback to the unpinned Anvil fork would
                # reintroduce the very non-determinism this exists to remove.
                raise RealSimulationUnavailable(
                    f"SOLVER_READ_PROXY set but opening the proxy session failed: {exc}"
                ) from exc
            for cid in list(rpc_map):
                if cid in _read_proxy.chain_ids and cid in CHAIN_NAMES:
                    rpc_map[cid] = proxy_rpc_url(_read_proxy, _proxy_session_id, cid)
            logger.info(
                "[benchmark] solver reads routed via block-pin proxy "
                "session=%s pinned=%s",
                _proxy_session_id,
                rec.get("blocks"),
            )

    # Snapshot init_config BEFORE the primary session's proxy rpc_urls are added —
    # extra runtimes (K>1) each route through their OWN proxy session.
    init_config_base = dict(init_config)
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

    # The primary runtime (the caller's session + its proxy). K=1 (default) runs ONLY
    # this one — byte-identical to the legacy sequential loop. K>1 provisions K-1
    # ADDITIONAL isolated runtimes (own solver subprocess + own proxy session + own
    # budget) and shards the corpus across them; scores stay order-independent and are
    # written back by input index, and K is NOT folded into the pack hash, so a fleet on
    # mixed K computes identical scores. See _benchmark_concurrency / BENCHMARK_CONCURRENCY.
    runtimes = [
        _BenchmarkRuntime(
            session=session, proxy_session_id=_proxy_session_id, init_config=init_config,
        )
    ]
    proxy_ids: list[str | None] = [_proxy_session_id]
    spawned_sessions: list[SolverSession] = []
    effective_k = session_count if session_count is not None else _benchmark_concurrency()
    effective_k = max(1, min(effective_k, _BENCHMARK_MAX_CONCURRENCY))
    if effective_k > 1 and session_factory is None:
        logger.warning(
            "[benchmark] BENCHMARK_CONCURRENCY=%d but no session_factory provided; "
            "running a single runtime", effective_k,
        )
    elif effective_k > 1:
        try:
            for _ in range(effective_k - 1):
                extra = await session_factory()
                spawned_sessions.append(extra)
                rt, pid = await _provision_extra_runtime(
                    extra,
                    base_rpc_map=base_rpc_map,
                    pin_blocks=pin_blocks,
                    read_proxy=_read_proxy,
                    fork_block=fork_block,
                    init_config_base=init_config_base,
                    intents_len=len(intents),
                )
                runtimes.append(rt)
                proxy_ids.append(pid)
        except Exception as exc:  # noqa: BLE001 — degrade gracefully, never abort the run
            logger.error(
                "[benchmark] failed to provision an extra runtime (%s); continuing "
                "with %d runtime(s)", exc, len(runtimes),
            )
        if len(runtimes) > 1:
            logger.info(
                "[benchmark] scenario pool: %d concurrent runtimes (BENCHMARK_CONCURRENCY)",
                len(runtimes),
            )

    try:
        results = await _run_scenarios(
            intents,
            runtimes=runtimes,
            simulator=simulator,
            init_config=init_config,
            read_proxy=_read_proxy,
            config=config,
            score_fn=score_fn,
            fork_block=fork_block,
            require_real_sim=require_real_sim,
            reference_quotes=reference_quotes,
            trigger_ground_truth=trigger_ground_truth,
        )

        # Signal benchmark end with final scores (on each runtime's session).
        summary = [
            {"intent_id": r.intent_id, "score": r.score, "elapsed_ms": r.elapsed_ms}
            for r in results
        ]
        for rt in runtimes:
            try:
                await rt.session.on_benchmark_end(summary)
            except (SolverTimeoutError, SolverCrashedError):
                pass
    finally:
        # Close every proxy session (best-effort) and shut down the sessions THIS call
        # spawned (the caller still owns the primary `session`).
        for pid in proxy_ids:
            if pid is not None and _read_proxy is not None:
                try:
                    await close_session(_read_proxy, pid)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("[benchmark] proxy close failed for %s: %s", pid, exc)
        if spawned_sessions:
            await asyncio.gather(
                *(s.shutdown() for s in spawned_sessions), return_exceptions=True,
            )
    # Fairness audit (OBSERVABILITY ONLY — never affects scoring or the pack hash):
    # if any solver session hit transient RPC/provider errors during this run,
    # surface a per-run summary. Such errors make the solver emit no plan for the
    # affected orders, which the scorer records as blind spots / drops and zeroes —
    # misattributing provider flake to a lack of miner capability.
    try:
        _sessions = {
            id(s): s for s in ([session] + list(spawned_sessions)) if s is not None
        }
        _rpc_total = 0
        _rpc_samples: list[str] = []
        for _s in _sessions.values():
            _report = getattr(_s, "rpc_error_report", None)
            if _report is None:
                continue
            _n, _samples = _report()
            _rpc_total += _n
            _rpc_samples.extend(_samples)
        if _rpc_total:
            logger.warning(
                "[benchmark-rpc-health] %s: %d transient RPC/provider error(s) over "
                "%d scenario(s) this run — these silently zero orders and get "
                "misattributed to miner capability (fairness impact). samples: %s",
                getattr(session, "_label", "solver"), _rpc_total, len(intents),
                " | ".join(_rpc_samples[:4]),
            )
    except Exception as exc:  # noqa: BLE001 — audit logging must never break a run
        logger.debug("[benchmark-rpc-health] summary failed: %r", exc)
    return results


async def _process_scenario(
    intent: "AppIntentDefinition",
    state: "IntentState",
    snapshot: "MarketSnapshot",
    *,
    session: SolverSession,
    simulator: Any | None,
    proxy_session_id: str | None,
    read_proxy: Any | None,
    config: "BenchmarkConfig",
    score_fn: ScoreFn | None,
    fork_block: int | None,
    require_real_sim: bool,
    reference_quotes: dict[str, dict[str, str]],
    trigger_ground_truth: dict[str, bool],
    trace_budget: list[int],
) -> tuple[BenchmarkResult, bool]:
    """Run ONE benchmark scenario end-to-end on the given runtime.

    Pure with respect to the scenario: takes its own ``session`` / ``simulator``
    / ``proxy_session_id`` plus read-only config, and returns
    ``(result, need_respawn)``. The ONLY shared mutable it touches is
    ``trace_budget`` (a per-run, diagnostics-only counter that NEVER folds into
    the score or the pack hash), so running scenarios concurrently across
    isolated runtimes cannot change any consensus-relevant output. The body is
    the legacy sequential loop body verbatim, so a single-runtime pool is
    byte-identical to the old loop.
    """
    start = time.monotonic()
    scenario_name = state.control_view().get("_scenario_name", "")
    intent_label = f"{intent.app_id}:{scenario_name}" if scenario_name else intent.app_id
    br = BenchmarkResult(intent_id=intent_label)
    need_respawn = False

    # Champion BLIND SPOT. The reference pre-pass marked this scenario as one the
    # CHAMPION could not quote. We do NOT zero it: each solver SELF-QUOTES, so a
    # challenger that CAN quote + execute reveals a real capability the champion
    # lacks. The champion self-quotes the same way (fails → 0), so its 0 is the
    # floor and any real execution here is unambiguous progress.
    _ref = reference_quotes.get(intent_label)
    if _ref and _ref.get(REFERENCE_QUOTE_FAILED_SENTINEL):
        logger.warning(
            "[champion-blind-spot] %s: champion could not quote; this solver "
            "self-quotes to reveal capability (champion scores 0 here)",
            intent_label,
        )
        _ref = None  # fall through to the self-quote path below

    # Phase 0 — pin the SOLVER's read fork to the round's fork_block BEFORE it
    # quotes/routes, so it reads the SAME state the simulator scores at: cross-host
    # deterministic AND it stops the solver mispricing quotes against a different
    # (drifting, per-host) block. No-op when already pinned. Ships OFF, flips
    # fleet-uniformly.
    if (
        _pin_solver_read_block_enabled()
        and fork_block is not None
        and simulator is not None
        and state is not None
        and getattr(state, "chain_id", None)
    ):
        try:
            pin_fn = getattr(simulator, "pin_read_fork", None)
            if pin_fn is not None:
                pin_fn(state.chain_id, fork_block)
        except Exception as exc:  # noqa: BLE001 - never let a pin failure abort the run
            logger.warning(
                "[pin-read-block] fork pin failed for chain %s @ %s: %s",
                getattr(state, "chain_id", "?"), fork_block, exc,
            )

    try:
        # Keep quote-enrich inside the try: _enrich_state_with_quote swallows its
        # own quote exceptions, but on an already-dead solver the next
        # generate_plan raises SolverCrashedError — which the respawn path
        # recovers from instead of aborting the whole run.
        state = await _enrich_state_with_quote(
            session, intent, state, snapshot, _ref,
        )

        from minotaur_subnet.shared.types import TriggerType

        is_auto = (
            intent.config.trigger_type == TriggerType.AUTO_TRIGGERED
        )

        # For auto-triggered intents, check trigger first
        if is_auto:
            br.trigger_decision = await session.check_trigger(
                intent, state, snapshot,
            )

        # Deterministic per-scenario budget: reset the proxy session's spent
        # budget to 0 so EACH generate_plan starts with a fresh budget B.
        # Best-effort + inert unless a proxy session is active AND the budget is
        # enforced; a failed reset never aborts the run.
        if (
            proxy_session_id is not None
            and read_proxy is not None
            and budget_enforced()
        ):
            await reset_session(read_proxy, proxy_session_id)

        # Generate plan
        plan = await session.generate_plan(intent, state, snapshot)
        br.plan = plan

        # Score the plan if a scoring function is provided
        if plan is not None and score_fn is not None:
            try:
                # Use real Anvil simulation when available, fall back to mock.
                # Mock simulation results MUST NOT be used for champion ranking.
                used_mock = False
                fail_closed_miss = False
                if simulator is not None:
                    try:
                        token_balances = _build_token_balances(state)
                        # Ensure the plan's metadata carries chain_id so the
                        # MultiChainSimulator routes to the correct Anvil fork.
                        if state and state.chain_id and plan:
                            if plan.metadata is None:
                                plan.metadata = {}
                            if "chain_id" not in plan.metadata:
                                plan.metadata["chain_id"] = state.chain_id
                        # Build intent_order so the simulator uses the full
                        # scoreIntent contract path instead of the bare path.
                        # The pinned fork block's timestamp anchors the order
                        # deadline (deterministic across validators); resolved
                        # via the simulator's fork-anchor/header cache. A None
                        # resolution falls back to wall clock (legacy).
                        fork_ts: int | None = None
                        if simulator is not None and state is not None:
                            try:
                                ts_fn = getattr(
                                    simulator, "get_block_timestamp", None,
                                )
                                if ts_fn is not None and fork_block is not None:
                                    fork_ts = ts_fn(state.chain_id, fork_block)
                            except Exception as ts_exc:  # noqa: BLE001
                                logger.warning(
                                    "[benchmark] fork timestamp resolve failed "
                                    "(chain=%s block=%s): %s — order deadline "
                                    "falls back to wall clock",
                                    getattr(state, "chain_id", "?"),
                                    fork_block, ts_exc,
                                )
                        intent_order = _build_benchmark_intent_order(
                            state, plan, getattr(intent, "manifest", None),
                            fork_block=fork_block,
                            fork_timestamp=fork_ts,
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
                            # Fail-closed: a real simulation that REVERTED means
                            # the plan could not execute. Score 0, exactly like a
                            # genuine on-chain revert.
                            logger.warning(
                                "Simulation reverted for %s and "
                                "require_real_sim is set; scoring 0: %s",
                                intent.app_id, sim.error,
                            )
                            br.error = f"real_sim_reverted: {sim.error}"
                            br.revert_reason = getattr(sim, "revert_reason", None)
                            # Diagnostics only: capture a per-step trace. Bounded
                            # per run; never affects the score.
                            if trace_budget[0] > 0:
                                tr = _capture_revert_trace(simulator, plan, token_balances)
                                if tr is not None:
                                    br.revert_trace = tr
                                    trace_budget[0] -= 1
                            fail_closed_miss = True
                    except Exception as sim_exc:
                        if require_real_sim:
                            # Fail-closed: do NOT fabricate a passing mock.
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
                    # Capture the unfakeable on-chain scoreIntent BPS.
                    br.on_chain_score = getattr(sim, "on_chain_score", None)
                    score_result = await score_fn(
                        intent.app_id, plan, sim, state,
                    )
                    br.plan_score = score_result.score
                    br.score_breakdown = score_result.breakdown
                    # The score_fn attaches the RAW delivered output (the LIVE
                    # raw-output scorer's metadata.raw_output) to the returned
                    # ScoreResult; absent (pre-cutover scorer) -> stays None. The
                    # relative adoption rule consumes it; never affects br.score.
                    br.raw_output = getattr(score_result, "raw_output", None)

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
        # This scenario scores 0 (recorded in br.error). The timeout killed the
        # process, so the caller respawns before the next scenario.
        br.error = f"timeout: {exc}"
        need_respawn = True
    except SolverCrashedError as exc:
        br.error = f"crashed: {exc}"
        need_respawn = True
    except Exception as exc:
        br.error = f"error: {exc}"

    br.elapsed_ms = int((time.monotonic() - start) * 1000)
    return br, need_respawn


async def _scenario_pool_worker(
    queue: "asyncio.Queue",
    results: list[BenchmarkResult | None],
    *,
    runtime: _BenchmarkRuntime,
    simulator: Any | None,
    init_config: dict[str, Any],
    intents_len: int,
    run_start: float,
    trace_budget: list[int],
    max_respawns: int,
    read_proxy: Any | None,
    config: "BenchmarkConfig",
    score_fn: ScoreFn | None,
    fork_block: int | None,
    require_real_sim: bool,
    reference_quotes: dict[str, dict[str, str]],
    trigger_ground_truth: dict[str, bool],
) -> None:
    """Drain the shared scenario queue on ONE isolated runtime.

    Owns this runtime's respawn state (its own solver subprocess). Writes each
    result back by its INPUT index, so the results list is in input order
    regardless of completion order — the load-bearing invariant the
    order-independence golden test guards. The run budget (TOTAL_BENCHMARK_TIMEOUT)
    is a shared wall-clock backstop checked per-worker; it rarely trips (the
    per-scenario RPC-read budget is the real cutoff), so its best-effort zero-fill
    is not consensus-critical.
    """
    session = runtime.session
    proxy_session_id = runtime.proxy_session_id
    respawns = 0
    solver_dead = False
    dead_reason = "skipped: solver unrecoverable"

    async def _respawn() -> bool:
        """Restart + re-init this runtime's solver for the next scenario.

        Returns True on success; False (→ solver_dead) when the relaunch closure
        is missing, the respawn budget is exhausted, or relaunch/init throws.
        """
        nonlocal respawns
        if session._relaunch is None or respawns >= max_respawns:
            return False
        try:
            await session.restart()
            await session.initialize(runtime.init_config or init_config)
            await session.on_benchmark_start(intents_len)
            respawns += 1
            return True
        except Exception as exc:
            logger.error(
                "[benchmark] solver respawn failed (%s); remaining scenarios "
                "score 0", exc,
            )
            return False

    while True:
        try:
            idx, intent, state, snapshot = queue.get_nowait()
        except asyncio.QueueEmpty:
            return

        if not solver_dead and (time.monotonic() - run_start) > TOTAL_BENCHMARK_TIMEOUT:
            logger.warning(
                "[benchmark] total run budget (%.0fs) exceeded; scoring remaining "
                "scenarios 0", TOTAL_BENCHMARK_TIMEOUT,
            )
            solver_dead = True
            dead_reason = "skipped: total run budget exceeded"

        if solver_dead:
            # Solver unrecoverable or the run budget is spent. Score this scenario
            # 0 deterministically (by index) rather than truncate.
            scenario_name = state.control_view().get("_scenario_name", "")
            intent_label = (
                f"{intent.app_id}:{scenario_name}" if scenario_name else intent.app_id
            )
            br = BenchmarkResult(intent_id=intent_label)
            br.error = dead_reason
            br.elapsed_ms = 0
            results[idx] = br
            continue

        br, need_respawn = await _process_scenario(
            intent, state, snapshot,
            session=session,
            simulator=simulator,
            proxy_session_id=proxy_session_id,
            read_proxy=read_proxy,
            config=config,
            score_fn=score_fn,
            fork_block=fork_block,
            require_real_sim=require_real_sim,
            reference_quotes=reference_quotes,
            trigger_ground_truth=trigger_ground_truth,
            trace_budget=trace_budget,
        )
        results[idx] = br

        # A timeout/crash left the process dead — respawn so the NEXT scenario
        # this worker pulls runs on a live solver. Only THIS scenario scored 0.
        if need_respawn:
            solver_dead = not await _respawn()


async def _run_scenarios(
    intents: list[tuple["AppIntentDefinition", "IntentState", "MarketSnapshot"]],
    *,
    runtimes: list[_BenchmarkRuntime],
    simulator: Any | None,
    init_config: dict[str, Any],
    read_proxy: Any | None,
    config: "BenchmarkConfig",
    score_fn: ScoreFn | None,
    fork_block: int | None,
    require_real_sim: bool,
    reference_quotes: dict[str, dict[str, str]],
    trigger_ground_truth: dict[str, bool],
) -> list[BenchmarkResult]:
    """Run every scenario across ``len(runtimes)`` isolated runtimes concurrently.

    Each scenario is independent and written back by input index, so the result
    list is byte-identical and order-independent regardless of how many runtimes
    drain the queue or in what order they finish (proven by
    test_benchmark_order_independence + test_benchmark_pool). With a single
    runtime this is byte-identical to the legacy sequential loop.
    """
    results: list[BenchmarkResult | None] = [None] * len(intents)
    queue: "asyncio.Queue" = asyncio.Queue()
    for i, (intent, state, snapshot) in enumerate(intents):
        queue.put_nowait((i, intent, state, snapshot))

    run_start = time.monotonic()
    # Diagnostics-only revert-trace budget, shared across runtimes (a list so the
    # workers decrement one counter). Best-effort: never folded into the score.
    trace_budget = [_revert_trace_budget()]
    # Per-runtime respawn ceiling (matches the legacy single-session bound).
    max_respawns = max(4, len(intents))

    await asyncio.gather(*[
        _scenario_pool_worker(
            queue, results,
            runtime=rt,
            simulator=simulator,
            init_config=init_config,
            intents_len=len(intents),
            run_start=run_start,
            trace_budget=trace_budget,
            max_respawns=max_respawns,
            read_proxy=read_proxy,
            config=config,
            score_fn=score_fn,
            fork_block=fork_block,
            require_real_sim=require_real_sim,
            reference_quotes=reference_quotes,
            trigger_ground_truth=trigger_ground_truth,
        )
        for rt in runtimes
    ])

    if any(br is None for br in results):
        # Defensive: every index is dequeued exactly once + written. If not,
        # fail loud rather than silently return a short/misaligned result set.
        missing = [i for i, br in enumerate(results) if br is None]
        raise RuntimeError(f"benchmark pool left scenarios unscored: {missing}")
    return [br for br in results if br is not None]


class _ManifestShim:
    """Adapt a raw manifest dict to the encoder's ``js_engine.get_manifest`` API
    so the benchmark can reuse the generic manifest-driven encoder without a
    full app store / JS engine in scope."""

    __slots__ = ("_m",)

    def __init__(self, manifest: dict[str, Any] | None):
        self._m = manifest

    def get_manifest(self, _app_id):
        return self._m


# Synthetic benchmark orders live exactly one hour past their anchor —
# the legacy wall-clock window, now measured from the pinned fork block's
# timestamp so every validator builds the byte-identical order.
_BENCHMARK_ORDER_DEADLINE_SECS = 3600


def _benchmark_order_id(
    contract_address: str,
    chain_id: Any,
    scenario_name: str,
    fn_name: str,
    fork_block: int | None,
) -> str:
    """Deterministic synthetic order id for a benchmark scenario.

    Replaces the legacy ``uuid4`` id (per-validator random — a cross-host
    calldata asymmetry, since order_id is keccak'd into the scoreIntent
    calldata's bytes32 id). Derived ONLY from round-stable scenario identity:
    the app contract, chain, scenario name (``hist:<order_id>`` for
    historical replays — unique per order), intent function, and the round's
    fork pin. Identical across validators for the same round inputs AND
    identical for the champion/challenger sims of the same scenario (the
    per-sim fork reset makes CREATE2 proxy reuse a non-issue). Unique across
    orders within a run for the same reason (app_id:scenario_name) is already
    the run-wide join key (intent_id / reference_quotes).

    Format matches the legacy id: ``bench_`` + 16 hex chars.
    """
    import hashlib

    seed = "|".join((
        str(contract_address).lower(),
        str(chain_id),
        str(scenario_name),
        str(fn_name),
        str(fork_block),
    ))
    return "bench_" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]


def _build_benchmark_intent_order(
    state: IntentState,
    plan: ExecutionPlan,
    manifest: dict[str, Any] | None = None,
    *,
    fork_block: int | None = None,
    fork_timestamp: int | None = None,
) -> dict[str, Any] | None:
    """Build an intent_order dict for benchmark simulation.

    This enables the simulator's scoreIntent contract path (proxy deploy,
    token funding, plan execution, transfer capture) instead of the bare
    interaction path which runs each call independently and captures no
    meaningful token transfers.

    Mirrors the intent_order construction in order_processor.py (line ~284).

    Determinism (cross-validator, per fork pin): ``fork_block`` feeds the
    deterministic ``order_id`` and ``fork_timestamp`` (the pinned fork
    block's timestamp) anchors the order ``deadline`` — wall clock is only a
    fallback when no fork anchor resolved (mock/unit paths), preserving the
    legacy behavior there.
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

    # Build ABI-encoded intentParams via the SAME generic, manifest-driven
    # encoder the submit-order endpoint uses (one encoder for every app). The
    # benchmarked intent's manifest drives the field layout.
    fn_name = control.get("_intent_function", "swap")
    intent_params_hex = ""
    try:
        from minotaur_subnet.api.services.app_service import (
            build_intent_params_hex_from_manifest,
        )
        # Use the Anvil-friendly submitted_by already computed above
        # (not the scenario's dummy receiver which may be address(1))
        bench_params = {**params, "receiver": submitted_by}
        hex_result = build_intent_params_hex_from_manifest(
            None, _ManifestShim(manifest), "benchmark", fn_name, bench_params, submitted_by,
        ) if manifest else None
        if hex_result:
            intent_params_hex = hex_result
            print(f"[BENCHMARK] Built intent_params_hex: {len(hex_result)} chars for {submitted_by[:10]}", flush=True)
        else:
            print(f"[BENCHMARK] intentParams encoding returned None (manifest={'present' if manifest else 'MISSING'}). params keys: {list(bench_params.keys())}", flush=True)
    except Exception as exc:
        print(f"[BENCHMARK] _build_benchmark_intent_order encoding FAILED: {exc}", flush=True)

    if not intent_params_hex:
        return None

    # Resolve intent selector
    from eth_hash.auto import keccak as _keccak
    _KNOWN_SIGS = {
        "swap": "swap(address,address,uint256,uint256,address)",
        "execute": "swap(address,address,uint256,uint256,address)",
    }
    sig = _KNOWN_SIGS.get(fn_name, f"{fn_name}()")
    selector = _keccak(sig.encode())[:4].hex()

    # Deterministic order_id — unique per scenario within a run (the CREATE2
    # proxy concern), and IDENTICAL across validators / champion-vs-challenger
    # for the same round inputs (see _benchmark_order_id).
    order_id = _benchmark_order_id(
        contract_address,
        state.chain_id,
        control.get("_scenario_name", ""),
        fn_name,
        fork_block,
    )

    # Deadline anchored to the pinned fork block's timestamp (deterministic
    # across validators); wall clock only when no fork anchor resolved.
    if fork_timestamp is not None:
        deadline = int(fork_timestamp) + _BENCHMARK_ORDER_DEADLINE_SECS
    else:
        deadline = int(time.time()) + _BENCHMARK_ORDER_DEADLINE_SECS

    return {
        "order_id": order_id,
        "app": contract_address,
        "intent_selector": selector,
        "intent_params": intent_params_hex,
        "submitted_by": submitted_by,
        "chain_id": state.chain_id,
        "deadline": deadline,
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
