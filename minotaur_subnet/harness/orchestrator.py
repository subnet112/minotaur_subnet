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
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from minotaur_subnet.chains import registry
from minotaur_subnet.shared.types import (
    AppIntentDefinition,
    ExecutionPlan,
    Interaction,
    IntentState,
    ScoreResult,
    SimulationResult,
)
from minotaur_subnet.sdk.intent_solver import MarketSnapshot, SolverMetadata
from minotaur_subnet.simulator.anvil_simulator import TRANSIENT_SIM_ERROR_PREFIX
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
    "-32070", "gateway request timeout",
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


# Fraction of TOTAL_BENCHMARK_TIMEOUT after which transient-RPC retries stop
# being issued. Retries cost wall clock against the SAME run budget as the
# first attempts, so an unbounded retry policy would convert a fairness fix
# into a worse failure: the tail of the corpus zero-filled by
# "skipped: total run budget exceeded". Past this mark the run spends what is
# left finishing first attempts, which every order still needs.
_RPC_RETRY_DEADLINE_FRACTION = 0.75


def _rpc_retry_max() -> int:
    """EXTRA attempts a scenario gets after a TRANSIENT RPC/provider failure.

    A provider rate-limit / timeout / 5xx makes the solver emit no plan for the
    affected order (see ``_RPC_ERROR_SIGNATURES``). The relative adoption rule
    then records that order as ``dropped`` — and a single drop is a HARD VETO
    no amount of wins can offset — so the miner is rejected for the provider's
    hiccup rather than its own capability. Re-running the scenario turns that
    misattribution into a retry.

    CONSENSUS-RELEVANT: a retried scenario can produce a plan where the first
    attempt produced none, which changes per_intent raw_output and hence the
    adoption verdict. Ships OFF (``0``) so it can soak inert, and MUST be
    flipped fleet-uniformly (develop->main promotion + env on every validator)
    exactly like PIN_SOLVER_READ_BLOCK — a split value means leader and
    follower can score the same image differently. Capped at 5 so a
    misconfigured value cannot spend the whole run budget on one order.
    """
    raw = os.environ.get("BENCHMARK_RPC_RETRY_MAX", "0").strip()
    try:
        return max(0, min(int(raw), 5))
    except ValueError:
        return 0


def _rpc_retry_run_budget(n_intents: int) -> int:
    """Run-wide ceiling on transient-RPC retries, shared by every runtime.

    The per-scenario cap alone bounds ONE order; this bounds the RUN, so a
    provider outage degrades to "scored like today" instead of burning the
    whole budget re-running everything. Default scales with corpus size
    (a quarter of it, floor 8) — comfortably above the ~10-15% transient
    failure rate seen live, well below a full re-run.
    """
    raw = os.environ.get("BENCHMARK_RPC_RETRY_RUN_MAX", "").strip()
    if raw:
        try:
            return max(0, int(raw))
        except ValueError:
            pass
    return max(8, n_intents // 4)


# Errors that are DETERMINISTIC outcomes of the plan itself, never provider
# flake — retrying them re-derives the same verdict and only spends budget.
# ``real_sim_reverted`` is the important one: a reverted scoreIntent is a real
# result (the plan cannot execute) and its revert text can incidentally contain
# a transient-looking substring.
_RPC_RETRY_EXCLUDED_PREFIXES: tuple[str, ...] = (
    "real_sim_reverted:",
)


def _is_retryable_rpc_failure(br: "BenchmarkResult") -> str | None:
    """Return the transient signature when ``br`` is a provider-caused MISS.

    All three must hold, so a retry can only ever rescue a row the miner did
    not lose on merit:

    1. the row DELIVERED NOTHING (no ``raw_output``) — a scored row is never
       re-run, so retries cannot change a result the solver actually produced;
    2. its error carries a transient RPC/provider signature; and
    3. it is not a deterministic plan outcome (a real revert).
    """
    raw = br.raw_output
    if raw is not None and str(raw) != "":
        return None
    err = br.error
    if not err:
        return None
    for prefix in _RPC_RETRY_EXCLUDED_PREFIXES:
        if err.startswith(prefix):
            return None
    return _classify_rpc_error(err)


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


# Anvil's first funded account — the benchmark's stand-in receiver when a
# scenario declares none.
_ANVIL_DEFAULT_ACCOUNT = "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"


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
    # PRE-REFUND metered scoreIntent gas from the benchmark-only GasMeter probe
    # (anvil_simulator.GAS_METER_RUNTIME_HEX; basis "scoreintent_prerefund_v1").
    # Set ONLY for a real (non-mock), successful simulation whose probe
    # produced a positive value; None everywhere else (mock rows, reverted
    # sims, probe failures). MEASUREMENT ONLY — never feeds ``score`` or any
    # verdict; the gas clause is a separate, stacked change.
    gas_metered: int | None = None
    # PHASE 0, OBSERVE-ONLY: what a cross-chain plan actually delivered on the
    # DESTINATION chain, as an exact decimal wei string (same precision
    # discipline as raw_output). None for every single-chain row and whenever
    # the destination leg could not be measured. MEASUREMENT ONLY — never
    # feeds ``score`` or any verdict, exactly like gas_metered; the scoring
    # rule that consumes it is a separate, later change, gated on this number
    # first proving identical across leader and follower on the same pins.
    destination_delivered: str | None = None
    # "simulated" (amount observed leaving the source fork — trustworthy) or
    # "declared" (the plan's own number — solver-reported, weaker). Phase-0
    # analysis must be able to separate the two before either moves a score.
    destination_amount_source: str | None = None
    revert_reason: str | None = None  # decoded on-chain revert reason when the real sim reverted
    # Per-step interaction trace ({interactions, total_gas, summary}) captured on
    # a real-sim revert — pure diagnostics for the miner; never feeds the score.
    revert_trace: dict[str, Any] | None = None
    # Chain the scenario's deployment lives on (``state.chain_id``). Lets the
    # SOLVING→SOLVED transition promote the correct per-chain deployment when an
    # app is deployed on more than one chain (BENCHMARK_ALL_DEPLOYMENT_CHAINS);
    # the ``intent_id`` label alone (app_id:scenario) does not identify the chain.
    chain_id: int | None = None
    # How many EXTRA attempts this scenario needed because a previous attempt
    # failed with a TRANSIENT RPC/provider signature (see _RPC_ERROR_SIGNATURES).
    # 0 on every row when the retry is disarmed, so disarmed runs stay
    # byte-identical. OBSERVABILITY: it records that the harness absorbed a
    # provider hiccup on the miner's behalf, never a quality signal about the
    # solver.
    rpc_retries: int = 0


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
            # Absent ⇔ the solver vendored a pre-marker SDK whose runner does
            # not inject this. Read field-by-field (never SolverMetadata(**r))
            # so a NEWER solver reporting keys this validator does not know
            # about is ignored rather than raising — that is what lets the
            # marker roll out across a fleet that promotes unevenly.
            sdk_version=r.get("sdk_version"),
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

_TRUTHY_ENV = {"1", "true", "yes", "on"}


def _require_internal_live_net() -> bool:
    """Whether to HARD-FAIL a live champion whose network is not ``--internal``.

    Opt-in for a safe rollout: default is WARN-only (surfaces a mis-scoped live
    net without breaking a node mid-flight). Set ``LIVE_SOLVER_REQUIRE_INTERNAL=1``
    to fail closed — do this once the live champion has a keyless RPC path (the
    read proxy on the live-solver internal net) so enforcement doesn't sever the
    champion's only RPC route.
    """
    return os.environ.get("LIVE_SOLVER_REQUIRE_INTERNAL", "").strip().lower() in _TRUTHY_ENV


async def _docker_network_is_internal(name: str) -> bool | None:
    """``True``/``False`` if the named Docker network is ``--internal``.

    ``None`` when it can't be determined (docker error, socket-proxy denies
    inspect, or the net is absent) — callers treat ``None`` as "unknown", not
    "safe", but must not hard-fail on it (inspect can be denied on a healthy
    node behind the socket-proxy).
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", "network", "inspect", name, "--format", "{{.Internal}}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
        if proc.returncode != 0:
            return None
        return out.decode("utf-8", "replace").strip() == "true"
    except (asyncio.TimeoutError, FileNotFoundError, OSError):
        return None


async def _docker_network_exists(name: str) -> bool | None:
    """``True`` if the named Docker network exists, ``False`` if it DEFINITELY
    doesn't, ``None`` if it can't be determined.

    ``docker network inspect`` exits non-zero AND prints "not found" (or similar)
    to stderr when the net is absent; any other non-zero (socket-proxy 403, CLI
    error) is "unknown", not "absent" — callers must not treat ``None`` as
    missing. Distinguishing these lets ``start_docker`` refuse to launch a live
    champion onto a definitely-absent net (a doomed container) while never
    hard-failing on an inspect it merely couldn't run.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", "network", "inspect", name, "--format", "{{.Name}}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await asyncio.wait_for(proc.communicate(), timeout=15)
        if proc.returncode == 0:
            return True
        msg = (err or b"").decode("utf-8", "replace").lower()
        if "not found" in msg or "no such network" in msg:
            return False
        return None  # some other failure — can't conclude "absent"
    except (asyncio.TimeoutError, FileNotFoundError, OSError):
        return None


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

        # LIVE champions process REAL user orders (wallet addrs, balances, trade
        # params) and — unlike benchmark/screening on the sealed sandbox —
        # historically attach to a non-internal net (LIVE_SOLVER_NETWORK), giving
        # an adopted-but-hostile champion a route to the internet, the relayer,
        # IMDS, and the docker-socket-proxy. Require the live net to be a Docker
        # `--internal` bridge (no external gateway). WARN-only by default so this
        # merges without breaking a running node; LIVE_SOLVER_REQUIRE_INTERNAL=1
        # fails closed (flip it once the champion's RPC goes through the keyless
        # proxy on an internal net — see _require_internal_live_net).
        if live and bench_network:
            # (a) EXISTENCE: never launch a live champion onto a net that
            # DEFINITELY doesn't exist — the container would fail to start and
            # take order processing down with it (2026-07-22 incident: the live
            # net was renamed but nothing had created it). Fail LOUD instead of
            # launching a doomed container; on a fresh boot this surfaces as an
            # api start failure that update.sh health-gates + rolls back. A
            # "can't determine" (None, e.g. socket-proxy denies inspect) is NOT
            # treated as absent — we proceed and let docker run report any error.
            exists = await _docker_network_exists(bench_network)
            if exists is False:
                raise RuntimeError(
                    "refusing to start live champion: network %r does not exist "
                    "(would launch a doomed container). If enabling the keyless "
                    "RPC proxy, set only LIVE_SOLVER_RPC_VIA_PROXY=1 (the api "
                    "creates the net); do NOT rename LIVE_SOLVER_NETWORK by hand."
                    % bench_network
                )
            # (b) INTERNAL: a live net that exists but has an external gateway lets
            # a hostile champion reach the internet/relayer/IMDS/docker-socket.
            # WARN by default; LIVE_SOLVER_REQUIRE_INTERNAL=1 fails closed.
            internal = await _docker_network_is_internal(bench_network)
            if internal is not True:
                msg = (
                    "LIVE champion network %r is not a Docker --internal net "
                    "(Internal=%s) — a hostile champion could reach the internet, "
                    "relayer, IMDS, or docker-socket-proxy. Move it to an "
                    "--internal net with a keyless RPC broker."
                    % (bench_network, internal)
                )
                if internal is False and _require_internal_live_net():
                    raise RuntimeError("refusing to start live champion: " + msg)
                logger.warning(msg)

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
        # Forward each wired chain's solver RPC into the container under its boot env
        # name (first entry of the registry's boot_rpc_envs ladder). Priority per
        # chain: caller override, then the sandbox benchmark-anvil IP (only on the
        # sealed benchmark net), then the plain live RPC. Chains that share a boot
        # env name collapse to one container var; an override on EITHER applies.
        # (Ethereum 1 used to share ANVIL_RPC_URL with local Anvil 31337; it now
        # gets its own ETHEREUM_RPC_URL var, while 31337 keeps ANVIL_RPC_URL.)
        _by_env: dict[str, list[int]] = {}
        for _cid in registry.wired_chain_ids():
            _s = registry.spec(_cid)
            if _s is not None and _s.boot_rpc_envs:
                _by_env.setdefault(_s.boot_rpc_envs[0], []).append(_cid)
        for _env_name, _cids in _by_env.items():
            override = next((_overrides[c] for c in _cids if _overrides.get(c)), "")
            sandbox = ""
            if _use_sandbox:
                for c in _cids:
                    _bs = registry.spec(c)
                    if _bs is not None and _bs.benchmark_rpc_envs:
                        sandbox = os.environ.get(_bs.benchmark_rpc_envs[0], "").strip()
                        if sandbox:
                            break
            value = override or sandbox or os.environ.get(_env_name, "").strip()
            if value:
                cmd.extend(["-e", f"{_env_name}={value}"])

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




def _enrich_state_with_quote(
    intent: AppIntentDefinition,
    state: IntentState,
) -> IntentState:
    """Populate a swap scenario's source:"quote" params with the static zero quote.

    Synthetic benchmark scenarios never run a quote, so their on-chain
    intentParams omit the CoW ``quoted_output`` field — the deployed
    12-field DexAggregator scoreIntent then reverts on decode. Inject a
    static ZERO quote (``quotedOutput=0``, ``min=0``) via the shared
    ``map_quote_result_to_params`` helper: scoreIntent gates its CoW fee on
    ``quotedOutput > 0``, so 0 = no anchor, no fee, full output executes,
    and the authoritative relative scorer reads the RAW delivered output —
    the quote is not in the score (#543). The zero exists only to keep the
    12-field ABI valid.

    Returns the original ``state`` unchanged when: it isn't a quote-sourced
    intent, or the params already carry ``quoted_output`` (real/historical
    orders — their stored quote values are replayed verbatim).
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
    from minotaur_subnet.shared.types import QuoteResult

    # Static zero quote — the benchmark's scoring definition (#543). No
    # solver.quote() call, no champion reference pre-pass: scoreIntent gates
    # its CoW fee on quotedOutput>0 (so 0 = no fee, full output executes) and
    # the relative scorer reads the RAW delivered output.
    quote_params = map_quote_result_to_params(
        QuoteResult(estimated_output="0"), intent.manifest, intent_function,
        slippage_bps=BENCHMARK_MIN_SLIPPAGE_BPS,
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


def build_rpc_url_map(chain_ids) -> dict[int, str]:
    """Resolve per-chain benchmark RPC URLs from the environment.

    Per-chain priority order (sandbox-specific ``BENCHMARK_ANVIL_RPC_*`` IPs
    first, then the standard env vars) lives in the chain registry
    (``registry.benchmark_rpc``). Shared by run_benchmark AND the champion
    reference pre-pass so BOTH score against the SAME live chain state.

    Returns ``{chain_id: url}`` only for chains with a resolved RPC. A chain
    ABSENT from the result has NO live RPC — and a solver run without it would
    silently fall back to an incomplete on-chain snapshot (missing pools →
    false "No route" → corrupt scores). Callers MUST treat a missing chain as a
    loud failure, never a silent degradation.
    """
    rpc_map: dict[int, str] = {}
    for cid in chain_ids:
        url = registry.benchmark_rpc(cid)
        if not url and not registry.is_supported(cid):
            # Unknown chain: last-resort local anvil (legacy default).
            url = os.environ.get("ANVIL_RPC_URL", "").strip()
        if url:
            rpc_map[cid] = url
    return rpc_map


def _pin_solver_read_block_enabled() -> bool:
    """Whether to pin the SOLVER's read fork to the round's fork_block before
    generate_plan (Phase 0 of the deterministic-budget work).

    CONSENSUS-RELEVANT: changes the block state the solver reads, hence its
    routes/quotes/scores. Must be fleet-uniform — ships OFF so it can soak
    inert on the lead (observe the revert/score effect under the adoption
    freeze) and be flipped fleet-wide together (folded into the pack hash) once
    proven, exactly like ROUND_ANCHORED_PIN. DEFAULT ON (2026-07-08, soaked
    inert on the lead under the adoption freeze). Folds into the pack hash, so
    it MUST be flipped fleet-uniformly (develop->main promotion) — a split
    value surfaces as PACK_HASH_MISMATCH. Emergency override: set to one of
    {0,false,no,off} to disable fleet-wide via compose without a code revert.
    """
    return os.environ.get("PIN_SOLVER_READ_BLOCK", "").strip().lower() not in (
        "0", "false", "no", "off",
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
        # Salt with the PID: post-split the api (incumbent re-bench) and the
        # benchmark worker share ONE block-pin proxy, and id() is only unique
        # within a process — two heaps can allocate a session at the same address
        # for the same fork_block, colliding the session id (open_session would
        # replace the peer's session + reset its budget). PID makes it per-process.
        proxy_session_id = f"bench-{os.getpid()}-{id(sess):x}-{fork_block}"
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
    fork_blocks: dict[int, int] | None = None,
    require_real_sim: bool = False,
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
        fork_blocks: Optional ``{chain_id: block}`` map for multi-chain rounds —
            each scenario is pinned (solver read fork AND simulator fork) at ITS
            OWN chain's canonical block, instead of the single scalar ``fork_block``
            applied to whatever chain a plan routes to. ``None`` (default) keeps
            the scalar path (Base-only), byte-identical to before. When present,
            ``fork_block`` should still be set to the primary chain's block (used
            as the fallback for any chain absent from the map).
        require_real_sim: Fail-closed switch (default ``False``). When ``True``,
            the benchmark refuses to substitute the fabricated mock for a real
            simulation: if no simulator is injected it raises
            ``RealSimulationUnavailable``; if a real ``simulate()`` throws OR
            returns a reverted (``success=False``) result, that scenario is
            scored 0 — never laundered into a ~min*1.05 mock pass nor a
            lenient-app-scorer pass on a plan that could not execute. Default
            keeps today's silent mock fallback.

    Returns:
        List of BenchmarkResult, one per intent.
    """
    if config is None:
        config = BenchmarkConfig()
    if trigger_ground_truth is None:
        trigger_ground_truth = {}

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
    from minotaur_subnet.simulator.cross_chain_bench import (
        bridge_capability_descriptor,
    )

    init_config: dict[str, Any] = {
        "chain_ids": config.chain_ids,
        "timeout_per_plan_ms": config.timeout_per_plan_ms,
        # Which assets a bridge can carry ACROSS which chains, and at what fee —
        # the scored path had no answer to that question at all, so a solver
        # could only get cross-chain right by memorising four addresses.
        #
        # A plain dict, not a BridgeRegistry: the solver protocol is JSON
        # (harness/protocol.py), and a registry object serialises to
        # "<BridgeRegistry object at 0x…>" — a TRUTHY STRING that defeats a
        # solver's `is not None` guard and then raises on attribute access.
        #
        # And deterministic, not live: the real registry prices routes over
        # HTTP, which must never enter a scored path or two validators would
        # disagree on the same plan. See bridge_capability_descriptor.
        "bridge_capability": bridge_capability_descriptor(),
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
        # Per-chain pins when a map is supplied (multi-chain round); else the
        # scalar. A missing per-chain pin (determinism hole) surfaces from
        # build_pin_blocks as ValueError — translate to the fail-loud defer.
        try:
            pin_blocks = build_pin_blocks(
                _read_proxy, rpc_map, fork_blocks if fork_blocks else fork_block,
            )
        except ValueError as exc:
            raise RealSimulationUnavailable(str(exc)) from exc
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
            # PID-salted: the split shares one proxy across api + worker; id() is
            # process-local, so PID prevents a cross-process session-id collision.
            _proxy_session_id = f"bench-{os.getpid()}-{id(session):x}-{fork_block}"
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
            fork_blocks=fork_blocks,
            require_real_sim=require_real_sim,
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
        # How much of that flake the retry actually absorbed this run. Rows
        # that still carry a transient signature were NOT rescued (budget
        # spent, or it failed every attempt) and are the residual fairness
        # cost; `_rescued` is what the miner would otherwise have been
        # hard-vetoed for.
        _retried = sum(int(getattr(r, "rpc_retries", 0) or 0) for r in results)
        _rescued = sum(
            1 for r in results
            if int(getattr(r, "rpc_retries", 0) or 0) > 0
            and _is_retryable_rpc_failure(r) is None
        )
        if _rpc_total or _retried:
            logger.warning(
                "[benchmark-rpc-health] %s: %d transient RPC/provider error(s) over "
                "%d scenario(s) this run — these silently zero orders and get "
                "misattributed to miner capability (fairness impact). "
                "retries=%d rescued_orders=%d. samples: %s",
                getattr(session, "_label", "solver"), _rpc_total, len(intents),
                _retried, _rescued,
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
    fork_blocks: dict[int, int] | None = None,
    require_real_sim: bool,
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
    br = BenchmarkResult(
        intent_id=intent_label, chain_id=getattr(state, "chain_id", None),
    )
    need_respawn = False

    # Effective fork block for THIS scenario's chain: the per-chain pin when a
    # map is supplied (multi-chain round), else the scalar (Base-only). Every
    # fork pin below (solver read pin + simulator fork) uses this so each chain
    # is scored at ITS OWN canonical block, not whichever chain the scalar named.
    _chain_id = getattr(state, "chain_id", None)
    fork_block = (
        fork_blocks.get(_chain_id, fork_block)
        if fork_blocks and _chain_id is not None
        else fork_block
    )

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
        state = _enrich_state_with_quote(intent, state)

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
                        # Bridge calls can't execute on a fork — mock them so a
                        # cross-chain plan is MEASURED rather than fail-closed
                        # to 0. No-op (same object) for single-chain plans.
                        sim_plan = _mock_bridge_for_benchmark(plan, state)
                        # The scenario's chain (state.chain_id) is authoritative
                        # for anvil selection — it also resolved contract_address
                        # and fork_block above — so a plan mis-stamped with
                        # another chain can't run this contract on the wrong fork.
                        # Only a MultiChainSimulator consumes chain_id.
                        _chain_kwargs = (
                            {"chain_id": getattr(state, "chain_id", None)}
                            if hasattr(simulator, "_get_simulator")
                            else {}
                        )
                        sim = await simulator.simulate(
                            sim_plan,
                            contract_address=state.contract_address if state else None,
                            intent_order=intent_order,
                            token_balances=token_balances,
                            fork_block=fork_block,
                            **_chain_kwargs,
                            # BENCHMARK-ONLY: run the GasMeter probe so rows
                            # carry pre-refund metered gas. This is THE only
                            # call site that sets it — the live rail (order
                            # processing / fee certification) never does, so
                            # its direct-send path and receipt gas_used stay
                            # byte-identical.
                            meter_gas=True,
                        )
                        print(f"[BENCHMARK] Simulation: success={sim.success} transfers={len(sim.token_transfers)} gas={sim.gas_used} error={sim.error}", flush=True)
                        if require_real_sim and not sim.success:
                            # TRANSIENT PROVIDER FAULT vs REAL REVERT — opposite
                            # facts about the miner, and they were being
                            # collapsed. A `-32070 Gateway request timeout`
                            # inside the scoreIntent sim used to arrive here
                            # wearing the generic "scoreIntent simulation
                            # reverted" error, so it was recorded as
                            # `real_sim_reverted` (the plan's own failure) and
                            # graded a DROPPED order — a hard adoption veto for
                            # our outage, not the miner's plan. The simulator
                            # now tags it; keep the two apart from here on.
                            _transient = str(sim.error or "").startswith(
                                TRANSIENT_SIM_ERROR_PREFIX,
                            )
                            if _transient:
                                logger.warning(
                                    "Simulation UNAVAILABLE for %s (transient "
                                    "provider fault, NOT a plan revert): %s",
                                    intent.app_id, sim.error,
                                )
                                br.error = f"real_sim_unavailable: {sim.error}"
                            else:
                                logger.warning(
                                    "Simulation reverted for %s and "
                                    "require_real_sim is set; scoring 0: %s",
                                    intent.app_id, sim.error,
                                )
                                br.error = f"real_sim_reverted: {sim.error}"
                                br.revert_reason = getattr(sim, "revert_reason", None)
                            # Diagnostics only: capture a per-step trace. Bounded
                            # per run; never affects the score.
                            if trace_budget[0] > 0 and not _transient:
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
                # DESTINATION MEASUREMENT — deliberately OUTSIDE the
                # fail-closed guard below.
                #
                # It used to sit inside it, so the most common cross-chain
                # outcome was also the one we recorded nothing for: a plan
                # whose scoreIntent reverts fails closed, and 154 of the 172
                # cross-chain rows benched in the first live day were exactly
                # that. The row stored no delivered amount and no reason, so
                # the miner-facing `cross_chain_delivery` block could never
                # populate — the feature meant to explain a zero was silent for
                # the failure that actually happens.
                #
                # A fail-closed scoreIntent says nothing about the destination
                # legs: this is a SEPARATE simulate_cross_chain run over the
                # plan's own legs, so it is exactly as meaningful there.
                #
                # SCORING IS UNCHANGED, structurally: `score_fn` is only called
                # inside the guard, so a fail-closed row never reaches the app's
                # scorer, and the values are attached to `sim` only on that
                # path. This branch writes the ROW (diagnosis) and never the
                # scorer's view. Still gated on `not used_mock` — a fabricated
                # mock sim has no real fork to observe.
                _delivery_diag = None
                if not used_mock:
                    (
                        br.destination_delivered,
                        br.destination_amount_source,
                        _delivery_diag,
                    ) = await _measure_destination_delivery(
                        simulator, plan, state, token_balances, fork_block,
                    )
                    # Only the stable CODE is persisted. The full diagnosis
                    # (recipients, amounts) would bloat every row for detail the
                    # miner can get on demand from the dry run — and
                    # submission-store bloat has frozen the event loop before
                    # (#569).
                    br.destination_delivery_reason = (
                        (_delivery_diag or {}).get("code")
                    )

                if not fail_closed_miss:
                    br.mock_simulation = used_mock
                    # Capture the unfakeable on-chain scoreIntent BPS.
                    br.on_chain_score = getattr(sim, "on_chain_score", None)
                    # PRE-REFUND metered gas (GasMeter probe). WRITE gate:
                    # real sim + success + positive value only — mock rows
                    # and failed probes stay None; reverted sims never reach
                    # here (fail_closed_miss). Display/soak only.
                    try:
                        _gm = int(getattr(sim, "gas_metered", None) or 0)
                    except (TypeError, ValueError):
                        _gm = 0
                    br.gas_metered = (
                        _gm if (not used_mock and sim.success and _gm > 0)
                        else None
                    )
                    # Hand the measurement (taken above) to the app's scorer:
                    # the SAME values persisted on the row ride the sim into
                    # context.simulation (engine/context.py), so the app JS can
                    # price destination delivery itself. One computation feeds
                    # both the stored artifact and the scorer — they can never
                    # disagree. Scorer-visible ONLY here, never on the
                    # fail-closed path.
                    if not used_mock:
                        sim.destination_delivered = br.destination_delivered
                        sim.destination_amount_source = (
                            br.destination_amount_source
                        )
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
    rpc_retry_max: int = 0,
    retry_budget: list[int] | None = None,
    read_proxy: Any | None,
    config: "BenchmarkConfig",
    score_fn: ScoreFn | None,
    fork_block: int | None,
    fork_blocks: dict[int, int] | None = None,
    require_real_sim: bool,
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
    if retry_budget is None:
        retry_budget = [0]
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
            br = BenchmarkResult(
                intent_id=intent_label, chain_id=getattr(state, "chain_id", None),
            )
            br.error = dead_reason
            br.elapsed_ms = 0
            results[idx] = br
            continue

        async def _attempt():
            return await _process_scenario(
                intent, state, snapshot,
                session=session,
                simulator=simulator,
                proxy_session_id=proxy_session_id,
                read_proxy=read_proxy,
                config=config,
                score_fn=score_fn,
                fork_block=fork_block,
                fork_blocks=fork_blocks,
                require_real_sim=require_real_sim,
                trigger_ground_truth=trigger_ground_truth,
                trace_budget=trace_budget,
            )

        br, need_respawn = await _attempt()

        # TRANSIENT-RPC RETRY (disarmed unless BENCHMARK_RPC_RETRY_MAX > 0).
        #
        # When a provider rate-limits / times out / 5xx's, the solver emits no
        # plan for the affected order. The relative rule records that as a
        # `dropped` order, which is a HARD VETO on adoption — so a provider
        # hiccup, not the miner's capability, decides the round. Re-running the
        # scenario is the narrowest fix: it only ever re-runs a row that
        # delivered NOTHING and failed with a transient signature, so it cannot
        # change a result the solver actually produced.
        #
        # Bounded three ways — per scenario (`_rpc_retry_max`), per run
        # (`retry_budget`, shared across runtimes), and by wall clock
        # (`_RPC_RETRY_DEADLINE_FRACTION`) — because retries spend the SAME
        # budget as first attempts; an unbounded policy would zero-fill the
        # tail of the corpus and make miners worse off than doing nothing.
        attempts = 0
        while (
            rpc_retry_max > 0
            and attempts < rpc_retry_max
            and retry_budget[0] > 0
            and not solver_dead
        ):
            sig = _is_retryable_rpc_failure(br)
            if sig is None:
                break
            if (
                time.monotonic() - run_start
            ) > TOTAL_BENCHMARK_TIMEOUT * _RPC_RETRY_DEADLINE_FRACTION:
                logger.warning(
                    "[benchmark-rpc-retry] %s: transient provider failure (%s) "
                    "NOT retried — past %.0f%% of the run budget; the miner "
                    "still eats this drop",
                    br.intent_id, sig, _RPC_RETRY_DEADLINE_FRACTION * 100,
                )
                break
            # A timeout/crash killed the process; it must be live to retry.
            if need_respawn:
                if not await _respawn():
                    solver_dead = True
                    break
                need_respawn = False
            attempts += 1
            retry_budget[0] -= 1
            logger.warning(
                "[benchmark-rpc-retry] %s: retry %d/%d after transient "
                "provider failure (%s): %s",
                br.intent_id, attempts, rpc_retry_max, sig, br.error,
            )
            br, need_respawn = await _attempt()

        # Carried on whichever attempt is FINAL, so the row says how many
        # provider hiccups the harness absorbed for this order.
        br.rpc_retries = attempts
        if attempts and _is_retryable_rpc_failure(br) is None:
            logger.info(
                "[benchmark-rpc-retry] %s: recovered after %d retry(ies)",
                br.intent_id, attempts,
            )
        results[idx] = br

        # A timeout/crash left the process dead — respawn so the NEXT scenario
        # this worker pulls runs on a live solver. Only THIS scenario scored 0.
        if need_respawn and not solver_dead:
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
    fork_blocks: dict[int, int] | None = None,
    require_real_sim: bool,
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
    # Transient-RPC retry allowance, SHARED across runtimes (a list so every
    # worker decrements one counter) — bounds the RUN, not just one scenario.
    rpc_retry_max = _rpc_retry_max()
    retry_budget = [_rpc_retry_run_budget(len(intents)) if rpc_retry_max else 0]

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
            rpc_retry_max=rpc_retry_max,
            retry_budget=retry_budget,
            read_proxy=read_proxy,
            config=config,
            score_fn=score_fn,
            fork_block=fork_block,
            fork_blocks=fork_blocks,
            require_real_sim=require_real_sim,
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
    the run-wide join key (intent_id).

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

    # Resolve the intent selector from the APP'S OWN manifest — the same
    # manifest that drove the encoder immediately above, and the same
    # manifest-driven path order_processor uses since #1152.
    #
    # This was a literal {swap, execute} -> canonical-signature map, i.e. one
    # app's ABI in the path every benchmarked app goes through, and it left the
    # benchmark DISAGREEING with the order path: #1152 removed the map from
    # order_processor, so the same intent_function resolved to two different
    # selectors depending on which path you came through.
    #
    # Verified against the LIVE DexAggregator manifest before writing this:
    # swap -> d5bcb9b5 both ways, an exact drop-in for 3386 of 3387 corpus rows.
    # The one row that changes carries intent_function="execute", which the map
    # aliased to the SWAP signature while the app declares no such intent — core
    # guessing on the app's behalf is precisely what is being removed here, and
    # order_processor already stopped doing it.
    from minotaur_subnet.v3.manifest import selector_from_legacy_manifest

    selector = selector_from_legacy_manifest(manifest, fn_name)

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


def _source_leg_interactions(
    plan: ExecutionPlan, state: IntentState | None,
) -> list[Interaction]:
    """The SOURCE-side interactions of a legs-shaped cross-chain plan.

    ``normalize_to_legs`` flattens ``metadata["cross_chain_plan"].legs`` into
    one interaction list and records, per leg, its ``chain_id``, ``type``
    (source / bridge / destination) and ``interaction_indices`` into that list.
    We keep the legs that execute on the chain being scored and drop the
    destination legs — those belong to another fork, and crediting them here
    would score work on the wrong chain.

    Selection is deliberately conservative: a leg is kept when its ``chain_id``
    matches the scored chain, or — when the leg declares no chain — when it is
    not a destination leg. Anything ambiguous is dropped, which returns this to
    exactly today's behaviour (an empty list, hence score 0) rather than
    guessing. Returns ``[]`` for any plan that is not multi-leg.
    """
    from minotaur_subnet.simulator.cross_chain_bench import normalize_to_legs

    normalized = normalize_to_legs(plan)
    if normalized is None:
        return []
    legs = (normalized.metadata or {}).get("legs") or []
    flat = list(normalized.interactions)
    scored_chain = getattr(state, "chain_id", None)

    picked: list[Interaction] = []
    for leg in legs:
        leg_chain = leg.get("chain_id")
        if leg_chain is not None and scored_chain is not None:
            try:
                keep = int(leg_chain) == int(scored_chain)
            except (TypeError, ValueError):
                keep = False
        else:
            keep = leg.get("type") != "destination"
        if not keep:
            continue
        for idx in leg.get("interaction_indices") or []:
            if 0 <= int(idx) < len(flat):
                picked.append(flat[int(idx)])
    return picked


def _mock_bridge_for_benchmark(
    plan: ExecutionPlan, state: IntentState | None,
) -> ExecutionPlan:
    """Return the plan to SIMULATE, with bridge calls mocked out.

    Bridge protocol contracts cannot execute on an Anvil fork — there is no
    relayer to fill an Across deposit and no attestation service to mint a
    CCTP burn — so a plan carrying real bridge calldata reverts, and
    ``require_real_sim`` fail-closes it to score 0. That is indistinguishable
    from "the solver produced garbage", which means a miner who correctly
    answers a cross-chain scenario is scored exactly like one who didn't
    answer at all. The validator's own re-simulation already mocks these
    calls (validator/scoring_engine.py) and the live multi-leg path does too
    (blockloop/multi_leg.py); the benchmark was the one scoring path that
    didn't, so the incentive gradient for cross-chain work was flat-to-
    negative.

    DETERMINISM (this is a consensus-relevant scoring path):
      - ``mock_bridge_interactions`` is a pure selector-match rewrite with no
        I/O. It is emphatically NOT the CrossChainCompiler, which fetches
        LIVE bridge quotes over HTTP — that must never touch the benchmark,
        or two validators scoring the same plan would disagree.
      - The rewrite only fires for plans that DECLARE cross-chain intent. A
        single-chain plan returns the identical object, so every existing
        champion score stays bit-identical (design §8 compat trap 2).

    ROLLOUT: this changes scoring for cross-chain plans, so — like the
    analyzability gate and deadwood floor — it must reach the whole fleet in
    one :stable promotion before any solver emits such a plan. It is inert
    until one does.
    """
    from minotaur_subnet.simulator.cross_chain_bench import declares_cross_chain

    meta = plan.metadata or {}
    if not declares_cross_chain(meta):
        return plan

    from minotaur_subnet.shared.types import mock_bridge_interactions

    # WHERE THE PLAN'S WORK ACTUALLY LIVES.
    #
    # A solver's ``cross_chain_plan`` carries its interactions inside LEGS and
    # leaves the top level EMPTY. The destination measurement was taught this
    # (it calls normalize_to_legs first); this scored path never was, so it
    # handed the simulator a plan with zero interactions — scoreIntent then
    # reverts "(empty revert)" and the row scores 0 NO MATTER HOW GOOD THE PLAN
    # IS. Measured on the leader 2026-08-18: the one submission that
    # demonstrably delivered on the destination chain (499750000000000000, the
    # exact 5bps-haircut amount) still scored 0.0 with ``interactions: []``.
    # Replayed on a throwaway fork: this function returned the plan UNCHANGED
    # with 0 interactions, while the same plan normalized carried 1 executable
    # source-chain interaction (45836 gas).
    #
    # Only the SOURCE side is taken. Destination legs belong to another fork —
    # running them here would credit work on the wrong chain — and the bridge
    # leg's deposit is exactly what the mocking below exists to make executable.
    #
    # STRICTLY ADDITIVE: this fires only when the top level is EMPTY, which is
    # the case that scores 0 today. A plan that already carries top-level
    # interactions keeps byte-identical inputs, so nothing that scores now moves.
    interactions = plan.interactions
    if not interactions:
        interactions = _source_leg_interactions(plan, state)

    params = (
        state.raw_params_view()
        if state is not None and hasattr(state, "raw_params_view")
        else {}
    )
    try:
        amount = int(params.get("input_amount", 0) or 0)
    except (ValueError, TypeError):
        amount = 0
    mocked = mock_bridge_interactions(
        interactions,
        token_address=params.get("input_token", "") or "",
        amount=amount,
    )
    if mocked == plan.interactions:
        # Nothing changed AND nothing was recovered from the legs: a
        # destination-only leg, or a plan with no executable source side.
        # Same object out, exactly as before.
        return plan

    logger.info(
        "[benchmark] cross-chain plan: scoring %d source-side interaction(s), "
        "%d bridge call(s) mocked%s",
        len(mocked),
        sum(1 for a, b in zip(mocked, interactions) if a != b),
        " (recovered from legs — top level was empty)" if not plan.interactions else "",
    )
    return ExecutionPlan(
        intent_id=plan.intent_id,
        interactions=mocked,
        deadline=plan.deadline,
        nonce=plan.nonce,
        metadata=plan.metadata,
    )


async def _measure_destination_delivery(
    simulator: Any,
    plan: ExecutionPlan,
    state: IntentState | None,
    token_balances: dict[str, int] | None,
    fork_block: Any,
) -> tuple[str | None, str | None]:
    """PHASE 0 (observe-only): run the destination leg and report delivery.

    The scored simulation stays exactly what it was — single-chain, with
    bridge calls mocked — so this cannot move a score. It runs ALONGSIDE it
    purely to answer the question the scoring rule will eventually need:
    *how much did this plan actually deliver on the far chain?* Today nothing
    answers it, which is why a correct cross-chain plan and a useless one
    score the same.

    Determinism is the whole point of the exercise, so the bridged amount
    comes from the fixed-fee benchmark model applied to what the source leg
    was observed to move — never a live bridge quote (differs between
    validators) and never the solver's declared output (self-reported).

    Returns ``(delivered_wei_str | None, amount_source | None, diagnosis |
    None)``. ``diagnosis`` explains a ZERO — see :func:`_delivery_diagnosis`.
    It is ``None`` whenever delivery was credited, so its presence IS the
    "this delivered nothing, and here is why" signal. Never raises: an
    observation failing must not fail a benchmark row.

    FLEET UNIFORMITY: the credited quantity is an input to ``raw_output`` and
    therefore to the adoption verdict, so a leader filtering by token while
    followers still sum blind would have them disagree on the same plan. This
    is inert only while nothing emits cross-chain — which is true today and
    stops being true the moment cross-chain demand is re-seeded. Promote to
    :stable fleet-wide BEFORE seeding, never after.
    """
    from minotaur_subnet.simulator.cross_chain_bench import (
        intent_requests_cross_chain,
        is_cross_chain_plan,
        normalize_to_legs,
    )

    if not is_cross_chain_plan(plan):
        # THE COMMON FAILURE, and until 2026-08-17 the silent one. Measured on
        # the leader over 51 rounds: of 578 benched cross-chain rows, 482 (83%)
        # were a plan that never declared cross-chain at all — the solver simply
        # did not route the order — and 78 more had no plan. Only 18 declared
        # one. Those 18 got a reason; the 482 got the bare
        # "scoreIntent reverted: (empty revert)" that any broken plan produces,
        # so the single most common way to score zero on cross-chain demand was
        # also the one carrying no cross-chain signal.
        #
        # It is a distinct state and needs a distinct code: nothing_delivered
        # means "your destination legs moved nothing", which would be a LIE here
        # — there are no destination legs to blame. Nothing is measured (there
        # is no journey to run), so this costs one dict lookup and cannot move a
        # score: delivered stays None exactly as before.
        params = (
            state.raw_params_view()
            if state is not None and hasattr(state, "raw_params_view")
            else {}
        )
        if intent_requests_cross_chain(params, getattr(state, "chain_id", None)):
            return None, None, {
                "code": "no_cross_chain_plan",
                "requested_chain": str(params.get("dest_chain_id")),
            }
        return None, None, None
    if simulator is None or not hasattr(simulator, "simulate_cross_chain"):
        return None, None, None

    # Normalize HERE, not only inside the simulator: the delivered-amount
    # extraction below walks metadata["legs"], which only the LEGACY shape
    # carries. The modern shapes — the solver's cross_chain_plan, the very
    # one miners emit — were normalized onto a COPY inside
    # simulate_cross_chain, so dest_ids stayed empty and the measurement
    # returned null on a perfectly delivered journey (soak finding,
    # 2026-07-28). Idempotent for legacy plans; None means cross-chain was
    # declared but there is no multi-leg journey to measure.
    normalized = normalize_to_legs(plan)
    if normalized is None:
        return None, None, None
    plan = normalized

    try:
        result = await simulator.simulate_cross_chain(
            plan,
            deterministic_bridge=True,
            contract_address=state.contract_address if state else None,
            token_balances=token_balances,
            fork_block=fork_block,
        )
    except Exception as exc:  # noqa: BLE001
        logger.info("[benchmark] destination-leg observation failed: %s", exc)
        return None, None, None

    estimate = getattr(result, "bridge_estimate", None) or {}
    amount_source = estimate.get("amount_source")

    legs_meta = (plan.metadata or {}).get("legs") or []
    leg_results = getattr(result, "leg_results", None) or {}
    dest_ids = [
        leg["leg_id"] for leg in legs_meta if leg.get("type") == "destination"
    ]
    if not dest_ids:
        return None, amount_source, None

    params = (
        state.raw_params_view()
        if state is not None and hasattr(state, "raw_params_view")
        else {}
    )
    recipients = _delivery_recipients(state, plan)

    # WHICH token counts as delivery — the asset the INTENT asked for, taken
    # from the request params, never from the plan's own metadata.
    #
    # Summing every transfer to the receiver regardless of token does not
    # measure delivery, it measures arrival, and the two come apart exactly
    # where it matters. A cross-chain intent is "spend WETH on chain A, deliver
    # USDC on chain B"; a plan that bridges the WETH and simply forwards it,
    # skipping the destination swap, lands a raw amount ~1e12 larger than the
    # honest USDC answer purely because WETH has 18 decimals and USDC has 6.
    # That number becomes ``metadata.raw_output``, which feeds the per-order
    # relative comparison — so the plan that DIDN'T do the work wins the order
    # outright and the plan that did is recorded as a regression. The incentive
    # inverts. (The app's own single-chain scorer never had this hole: it
    # already filters ``tokenAddr === tokenOut``, and so does the sibling
    # cross-chain QUOTE path at cross_chain_quote.py — this scored path was the
    # only one summing blind.)
    #
    # Params, not plan metadata, because the plan is solver-authored: filtering
    # on a solver-declared ``token_out`` would restore the same vector in one
    # move — declare the cheap token, dump the cheap token, get credited for it.
    # ``output_token`` is already the DESTINATION-chain address (the CAIP-10
    # intake derives dest_chain_id from it), so it needs no remapping.
    expected_token = str(params.get("output_token") or "").lower()
    if not expected_token:
        # Fail closed, exactly like the no-destination-leg case above: an
        # unmeasurable journey reports nothing and earns nothing. Crediting an
        # unfiltered sum here would be the very mis-credit this guards against.
        logger.info(
            "[benchmark] destination delivery not measurable: intent declares "
            "no output_token to credit against",
        )
        return None, amount_source, {"code": "no_output_token"}

    # Four buckets, because a zero has more than one cause and they need
    # OPPOSITE fixes. Collapsing them (as a bare sum does) is what made every
    # cross-chain zero look identical and unactionable.
    delivered = 0                 # right token, credited recipient
    wrong_token_to_recipient = 0  # credited recipient, but not what was asked
    right_token_elsewhere = 0     # what was asked, delivered somewhere we don't count
    for leg_id in dest_ids:
        for t in (leg_results.get(leg_id) or {}).get("token_transfers", []):
            try:
                amount = int(t.get("amount") or 0)
            except (ValueError, TypeError):
                continue
            right_token = str(t.get("token", "")).lower() == expected_token
            credited_to = str(t.get("to", "")).lower() in recipients
            if right_token and credited_to:
                delivered += amount
            elif right_token:
                right_token_elsewhere += amount
            elif credited_to:
                wrong_token_to_recipient += amount

    diagnosis = None
    if not delivered:
        diagnosis = _delivery_diagnosis(
            expected_token, recipients,
            wrong_token_to_recipient, right_token_elsewhere,
        )
        logger.info(
            "[benchmark] destination legs credited 0 of %s — %s",
            expected_token, diagnosis["code"],
        )

    return str(delivered), amount_source, diagnosis


def _delivery_diagnosis(
    expected_token: str,
    recipients: set[str],
    wrong_token_to_recipient: int,
    right_token_elsewhere: int,
) -> dict[str, Any]:
    """Why did a cross-chain plan deliver nothing? Answered in a stable code.

    A zero delivery is the single most common cross-chain outcome and, until
    now, the least actionable thing the platform could tell a miner: the row
    said ``0`` whether they shipped the wrong asset, shipped to an address the
    contest does not count, or never built a destination leg at all. Those need
    three different fixes, and a miner had no way to tell which they had.

    The code vocabulary is CLOSED and the values are content-addressed off the
    measurement, never free text — this rides a persisted benchmark row that
    leader and follower compare, so a wording difference between two validator
    builds must never read as a data difference. Add codes, never reword them.

      ``wrong_recipient``   the requested token WAS delivered, just not to an
                            address that counts. Nearest miss there is: fix the
                            destination leg's recipient.
      ``wrong_token``       something reached a counted recipient, but not what
                            the intent asked for — the signature of bridging
                            and skipping the destination swap.
      ``nothing_delivered`` the destination legs moved nothing at all to
                            anyone. Usually an empty or reverting leg.
      ``no_output_token``   the intent declared no output token, so there was
                            nothing to measure against (set upstream).

    Two codes are set by the CALLER rather than here, because they are decided
    before there is any journey to measure — ``no_output_token`` above, and
    ``no_cross_chain_plan`` (the order asked for delivery on another chain and
    the plan is not cross-chain at all: 83% of live cross-chain rows).

    ``wrong_recipient`` outranks ``wrong_token`` when both are present: it is
    the closer miss and the cheaper fix, so it is the more useful thing to say.
    """
    if right_token_elsewhere:
        code = "wrong_recipient"
    elif wrong_token_to_recipient:
        code = "wrong_token"
    else:
        code = "nothing_delivered"
    return {
        "code": code,
        "requested_token": expected_token,
        # Sorted so two validators emit byte-identical diagnoses for the same
        # observation — this is a set, and set order is not stable.
        "credited_recipients": sorted(recipients),
        "delivered_to_others": str(right_token_elsewhere),
        "other_tokens_delivered": str(wrong_token_to_recipient),
    }


def _delivery_recipients(
    plan_state: IntentState | None, plan: ExecutionPlan,
) -> set[str]:
    """Addresses whose incoming transfers count as destination delivery.

    Crediting ONLY ``params['receiver']`` is why the reference solver measured
    zero on every case. A benchmark IntentState is built with ``owner=""``
    (benchmark_worker.py), quote cases carry no ``receiver``, and the solver's
    own default is ``receiver_default = state.contract_address or state.owner``
    — so the solver addresses the destination leg at the APP CONTRACT while the
    platform was watching only the anvil default account. Neither side is wrong
    on its own; they were answering different questions.

    The app contract is a legitimate delivery target, not a workaround: under
    the V2 escrow model the destination funds are SUPPOSED to land in the app
    (``escrowDeposit`` gates on ``balanceOf(address(this))``, and ``escrowRefund``
    returns from there), which is exactly why the cross-chain compiler resolves
    the dest recipient to the App on the DESTINATION chain and fails closed when
    that chain has no order-ready deployment. The app's own single-chain scorer
    has always counted both: ``toAddr === receiver || toAddr === appAddr``.

    Deliberately the DESTINATION chain's app address, never the source chain's.
    They differ, and a transfer to the source-chain address on the destination
    fork reaches an account with no code there — stranded funds. Crediting that
    would be a mis-credit, so an unresolvable destination address simply is not
    added (the measurement then reports what it can see, and a plan delivering
    only there reads 0 — correctly).

    Strictly a SUPERSET of the previous single-address rule, so this can only
    ever raise a measured delivery, never lower one.
    """
    params = (
        plan_state.raw_params_view()
        if plan_state is not None and hasattr(plan_state, "raw_params_view")
        else {}
    )
    control = (
        plan_state.control_view()
        if plan_state is not None and hasattr(plan_state, "control_view")
        else {}
    )

    out: set[str] = set()
    receiver = str(params.get("receiver") or "").lower()
    if receiver:
        out.add(receiver)
    else:
        # Preserve the historical default so single-receiver cases are
        # unchanged: the pre-funded Anvil account is who the benchmark's
        # scoreIntent path submits as.
        out.add(_ANVIL_DEFAULT_ACCOUNT.lower())

    dst_chain = (plan.metadata or {}).get("dst_chain_id")
    app_addresses = control.get("_app_addresses") or {}
    if dst_chain is not None and isinstance(app_addresses, Mapping):
        try:
            key = int(dst_chain)
        except (TypeError, ValueError):
            key = None
        if key is not None:
            # Callers may key by int or str depending on where the map came
            # from (app store vs a JSON round-trip).
            dest_app = app_addresses.get(key) or app_addresses.get(str(key))
            if dest_app:
                out.add(str(dest_app).lower())

    return out


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
