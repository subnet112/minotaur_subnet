"""Background benchmark worker.

Processes submissions that have passed screening and are in BENCHMARKING
status. Loads active intents, builds snapshots, runs the harness, scores
plans via JsExecutionEngine, and ranks replay results for a later round
coordinator to evaluate.

Usage:
    worker = BenchmarkWorker(submission_store, app_store)
    await worker.run_once()          # Process one batch
    await worker.run_loop(interval=30)  # Continuous polling
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import time
from dataclasses import asdict, dataclass, field
from typing import Any

from minotaur_subnet.shared.types import (
    AppIntentDefinition,
    ExecutionPlan,
    IntentState,
    ScoreResult,
    SimulationResult,
    TriggerType,
)
from minotaur_subnet.sdk.intent_solver import MarketSnapshot
from minotaur_subnet.harness.submission_store import (
    SubmissionStatus,
    SubmissionStore,
)
from minotaur_subnet.harness.round_store import RoundStatus, RoundStore
from minotaur_subnet.consensus.round_anchor import ForkPinUnavailable
from minotaur_subnet.harness.orchestrator import (
    BenchmarkConfig,
    BenchmarkResult,
    SolverOrchestrator,
    SolverSession,
    SolverTimeoutError,
    SolverCrashedError,
    run_benchmark,
    REFERENCE_QUOTE_FAILED_SENTINEL,
    BENCHMARK_MIN_SLIPPAGE_BPS,
    build_rpc_url_map,
)
from minotaur_subnet.weight_policy import GENESIS_HOTKEY

logger = logging.getLogger(__name__)

# Genesis submission sentinel values
GENESIS_REPO_URL = "builtin://baseline-swap-solver"
GENESIS_EPOCH = 0
GENESIS_SOLVER_IMAGE = os.environ.get("GENESIS_SOLVER_IMAGE", "").strip()


@dataclass
class _StageScore:
    """Aggregated score for one benchmark stage (synthetic or historical)."""
    avg_score: float
    count: int
    success_count: int


def _result_stage(result: Any) -> str:
    """Extract the stage tag ('synthetic' or 'historical') from a result.

    The stage is embedded in the intent_id format. Historical scenarios
    use 'hist:ord_xxx' in the scenario name; synthetic use the manifest
    scenario name. For backward compat, unknown tags default to 'synthetic'.
    """
    intent_id = getattr(result, "intent_id", "") or ""
    if ":hist:" in intent_id or intent_id.endswith(":hist") or "hist:ord_" in intent_id:
        return "historical"
    return "synthetic"


@dataclass
class BenchmarkScorecard:
    """Per-app and per-scenario scoring breakdown.

    Used by the champion adoption logic to enforce non-regression
    guarantees and per-app quality floors.
    """
    global_score: float = 0.0
    app_scores: dict[str, float] = field(default_factory=dict)
    # Per-app on-chain scoreIntent BPS (one list entry per scenario; None when the
    # sim didn't yield a score). The unfakeable output signal the on-chain-ranked
    # adoption rule (ADOPT_RULE=p2oc) ranks on. Populated only when a real sim runs.
    app_onchain: dict[str, list[int | None]] = field(default_factory=dict)
    scenario_scores: dict[str, float] = field(default_factory=dict)
    failures: int = 0
    total: int = 0
    mock_simulation_count: int = 0  # Number of results that used fabricated simulation

    @property
    def coverage(self) -> float:
        return (self.total - self.failures) / self.total if self.total > 0 else 0.0

    @property
    def mock_simulation_ratio(self) -> float:
        """Fraction of results that relied on mock simulation (0.0 – 1.0)."""
        return self.mock_simulation_count / self.total if self.total > 0 else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "global_score": self.global_score,
            "app_scores": dict(self.app_scores),
            "app_onchain": {k: list(v) for k, v in self.app_onchain.items()},
            "scenario_scores": dict(self.scenario_scores),
            "failures": self.failures,
            "total": self.total,
            "coverage": self.coverage,
            "mock_simulation_count": self.mock_simulation_count,
            "mock_simulation_ratio": self.mock_simulation_ratio,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "BenchmarkScorecard":
        if not data:
            return cls()
        return cls(
            global_score=data.get("global_score", 0.0),
            app_scores=data.get("app_scores", {}),
            app_onchain=data.get("app_onchain", {}),
            scenario_scores=data.get("scenario_scores", {}),
            failures=data.get("failures", 0),
            total=data.get("total", 0),
            mock_simulation_count=data.get("mock_simulation_count", 0),
        )


def _allow_subprocess_benchmark() -> bool:
    """Subprocess benchmarking is permanently disabled.

    All miner submissions run in sandboxed Docker containers with:
    - --network=benchmark-sandbox (iptables-restricted, only Anvil RPCs)
    - --read-only, --cap-drop=ALL, --no-new-privileges
    - Memory + CPU limits
    - No access to host filesystem, secrets, or other services

    The ALLOW_SUBPROCESS_BENCHMARK env var is ignored. Subprocess mode
    was a development shortcut that runs untrusted code directly on the
    host with no isolation — it has been removed.
    """
    return False


# Explicit OFF values for the challenger-vote observability gate. Anything else
# (including unset) keeps it ENABLED, so the empirical fleet-agreement test needs
# ZERO per-validator config — a 3rd-party validator publishes its would-be vote by
# default, exactly like our lead.
_CHALLENGER_QUORUM_OFF_VALUES = frozenset({"0", "false", "no", "off"})


def _challenger_quorum_mode() -> bool:
    """Whether the challenger-vote OBSERVABILITY diagnostics are published. **DEFAULT ON.**

    NOTE (#242): this NEVER affects sampling or verification. The Stage-2 corpus is
    always a single round-seeded SHARED draw, and every follower always casts an
    INDEPENDENT champion-vs-challenger verdict over it (the quorum). This flag ONLY
    toggles observe-only diagnostics — the leader's would-be vote publish and the
    ``/health`` independent_vote view — so operators can watch fleet agreement.

    Defaulted ON in code (publish-only, touches no verdict / weights / adoption) so
    the empirical fleet test needs no per-validator coordination — a bare ``:stable``
    3rd-party publishes its vote like our lead. Emergency override: set
    ``CHALLENGER_QUORUM_MODE`` to one of ``{0, false, no, off}`` to silence.

    The admin ``POST /v1/admin/shadow-vote`` endpoint (which SPAWNS benchmarks on
    demand) is gated SEPARATELY and explicitly in ``api/routes/monitoring.py`` — it
    is NOT enabled by this default, so default-on observability opens no active
    surface.
    """
    import os

    raw = os.environ.get("CHALLENGER_QUORUM_MODE")
    if raw is None:
        return True
    return raw.strip().lower() not in _CHALLENGER_QUORUM_OFF_VALUES


def _consolidate_champion_bench() -> bool:
    """Whether to MEMOIZE the champion benchmark within a round so the two
    champion-run paths (dethrone re-bench in ``_refresh_incumbent_score`` and the
    trustless quorum verdict in ``_independent_adopt_vote``) share ONE result
    instead of each re-running the champion solver. **DEFAULT OFF.**

    Pure compute optimization: a cached result is reused ONLY when round_id +
    champion image + fork block + corpus fingerprint + real-sim mode all match,
    so a hit is provably the SAME deterministic computation — the verdict and the
    persisted score are byte-identical to recomputing. Flag-gated so the saving
    can be enabled + validated separately. Off → both paths recompute (legacy).
    """
    import os

    return os.environ.get("CONSOLIDATE_CHAMPION_BENCH", "").strip().lower() in (
        "1", "true", "yes", "on",
    )


# PROCESS-WIDE champion-benchmark memo. Must be shared, NOT per-worker: the
# dethrone re-bench runs on the EpochManager's worker while the quorum verdict
# (_reactive_benchmark_candidate) constructs a FRESH BenchmarkWorker, so a
# per-instance cache would never share and the consolidation would be a no-op.
# Sharing across instances is safe because the key fully + deterministically
# describes the result (round, image, fork block, real-sim, corpus, scoring-JS).
_CHAMPION_BENCH_CACHE: dict[tuple, list] = {}
_CHAMPION_BENCH_CACHE_MAX = 32  # hard backstop; normally holds only the current round


def _clear_champion_bench_cache() -> None:
    """Reset the process-wide champion-bench memo (test hook / operational reset)."""
    _CHAMPION_BENCH_CACHE.clear()


class BenchmarkWorker:
    """Processes BENCHMARKING submissions by scoring them against active intents.

    Runs as a background task, polling the submission store for work.
    """

    def __init__(
        self,
        submission_store: SubmissionStore,
        app_store: Any = None,  # AppIntentStore, optional for DI
        js_engine: Any = None,  # JsExecutionEngine, optional for DI
        use_docker: bool = True,
        snapshot_builder: Any = None,  # SnapshotBuilder, optional for DI
        epoch_block_number: int | None = None,
        round_store: RoundStore | None = None,
        on_champion_adopted: Any = None,  # deprecated compatibility hook
        genesis_solver_image: str | None = None,  # Docker image for genesis benchmarking
        simulator: Any = None,  # AnvilSimulator / MultiChainSimulator for real simulation
        require_real_sim: bool = False,  # fail-closed: refuse the mock fallback
        pin_resolver: Any = None,  # Callable[[round_id], int|None] -> round-anchored fork block
        validator_identity: str | None = None,  # this validator's stable id (observability label)
    ) -> None:
        self._sub_store = submission_store
        self._app_store = app_store
        self._js_engine = js_engine
        self._use_docker = use_docker
        self._snapshot_builder = snapshot_builder
        self._epoch_block_number = epoch_block_number
        self._round_store = round_store
        self._on_champion_adopted = on_champion_adopted
        self._genesis_solver_image = genesis_solver_image or GENESIS_SOLVER_IMAGE or None
        self._simulator = simulator
        self._require_real_sim = require_real_sim
        # Injected by the API layer (keeps the harness free of API imports):
        # round_id -> the round-anchored benchmark-chain fork block, or None.
        self._pin_resolver = pin_resolver
        # Stable per-validator id (hotkey ss58) — observability label only; the
        # Stage-2 corpus is a single round-seeded SHARED draw for every validator
        # (#242), so it no longer seeds the sample.
        self._validator_identity = validator_identity
        self._warned_env_pin_ignored = False  # one-shot WARN guard (P5 demotion)
        self._running = False
        # app_id -> sha256(js_code)[:16] currently loaded in this worker's
        # engine. Lets _build_score_fn hot-reload a developer's PUT /scoring on
        # the next benchmark run instead of caching the first-seen JS forever
        # (the shared BlockLoop engine already hot-reloads this way; this worker
        # keeps its own engine, so it needs the same hash-diff).
        self._loaded_js_hashes: dict[str, str] = {}

    def _corpus_fingerprint(self, intents: list) -> str:
        """Stable hash of the corpus IDENTITY (ordered scenario labels), using the
        same ``app_id[:scenario_name]`` labelling as the reference-quote pre-pass.
        Guards the champion memo: a different corpus → different fingerprint → no
        reuse. Robust to missing fields (label degrades to app_id)."""
        import hashlib

        labels = []
        for intent, state, _snapshot in intents:
            scenario_name = ""
            try:
                scenario_name = state.control_view().get("_scenario_name", "") or ""
            except Exception:
                scenario_name = ""
            app_id = getattr(intent, "app_id", "") or ""
            labels.append(f"{app_id}:{scenario_name}" if scenario_name else app_id)
        return hashlib.sha256("\n".join(labels).encode()).hexdigest()[:16]

    def _loaded_js_fingerprint(self, intents: list) -> str:
        """Hash of the scoring-JS versions CURRENTLY loaded for the corpus's apps.

        Both champion paths call ``_build_score_fn`` (which hot-reloads JS and
        records ``_loaded_js_hashes``) before the memo, so this captures the exact
        scoring used. Folded into the key so a mid-round ``PUT /scoring`` update
        invalidates the memo — otherwise a result scored with the OLD JS could be
        reused for a verdict expecting the NEW JS (a wrong vote)."""
        import hashlib

        app_ids = sorted({getattr(intent, "app_id", "") or "" for intent, _, _ in intents})
        parts = [f"{a}={self._loaded_js_hashes.get(a, '')}" for a in app_ids]
        return hashlib.sha256("\n".join(parts).encode()).hexdigest()[:16]

    def _reference_quotes_fingerprint(
        self,
        reference_quotes: dict[str, dict[str, str]] | None,
    ) -> str:
        """Stable hash of the reference quote anchor used for a champion run.

        Champion benchmark memoization is only valid when the quote anchor is the
        same. A self-quoted champion run and a champion-reference-quoted run can
        share the same round/image/fork/corpus/JS, but they are different scoring
        computations and must not reuse one another.
        """
        if not reference_quotes:
            return ""

        parts: list[str] = []
        for label in sorted(reference_quotes):
            params = reference_quotes.get(label) or {}
            parts.append(label)
            for key in sorted(params):
                parts.append(f"{key}={params[key]}")
        return hashlib.sha256("\n".join(parts).encode()).hexdigest()[:16]

    async def memo_champion_bench(
        self,
        *,
        round_id: str | None,
        image: str | None,
        fork_block: int | None,
        intents: list,
        require_real_sim: bool,
        reference_quotes: dict[str, dict[str, str]] | None = None,
        run: Any,
    ) -> list[BenchmarkResult]:
        """Run (or reuse) the champion benchmark for this round, via the PROCESS-WIDE
        memo so the dethrone re-bench and the quorum verdict (different worker
        instances) share ONE result.

        ``run`` is the caller's own async benchmark thunk (it owns session setup +
        exception semantics). Returns the cached result ONLY on an exact key match —
        round_id, image, fork_block, real-sim, corpus fingerprint, scoring-JS
        fingerprint, AND reference-quote fingerprint — i.e. the identical
        deterministic computation, so a follower's verdict and persisted score are
        unchanged. Disabled (always recompute) when the flag is off, the key is
        incomplete (no round_id/image), or fork_block is None — a None pin means
        live-head (dev), where reuse across blocks is unsafe.
        """
        if (
            not _consolidate_champion_bench()
            or not round_id or not image or fork_block is None
        ):
            return await run()
        key = (
            round_id, image, int(fork_block), bool(require_real_sim),
            self._corpus_fingerprint(intents),
            self._loaded_js_fingerprint(intents),
            self._reference_quotes_fingerprint(reference_quotes),
        )
        hit = _CHAMPION_BENCH_CACHE.get(key)
        if hit is not None:
            logger.info(
                "[champion-bench] reuse cached champion run round=%s image=%s "
                "(consolidated; skipped a redundant benchmark)", round_id, image,
            )
            return hit
        results = await run()
        if isinstance(results, list):
            # Bound to the current round (drop stale rounds in place), with a hard
            # cap backstop. In-place mutation keeps concurrent readers consistent;
            # concurrent misses just recompute (no saving) — never a wrong result.
            for k in [k for k in _CHAMPION_BENCH_CACHE if k[0] != round_id]:
                _CHAMPION_BENCH_CACHE.pop(k, None)
            if len(_CHAMPION_BENCH_CACHE) >= _CHAMPION_BENCH_CACHE_MAX:
                _CHAMPION_BENCH_CACHE.clear()
            _CHAMPION_BENCH_CACHE[key] = results
        return results

    def set_epoch_block(self, block_number: int) -> None:
        """Set the block number for this epoch's snapshot.

        Called by EpochManager before run_once() to pin snapshots to
        a specific block for deterministic benchmarking.
        """
        self._epoch_block_number = block_number

    def _apply_epoch_block_pin(self) -> None:
        """DEV/TEST-ONLY manual fork pin via BENCHMARK_EPOCH_BLOCK.

        Production pins the fork automatically and per-round via the round-anchored
        derivation (ROUND_ANCHORED_PIN). When that gate is on this env override is
        IGNORED — a stale value must not silently divert a deferred round to an old
        block. Unset/invalid -> no pin (live head). Call-time read so dev can flip
        without restart; threads through run_benchmark -> simulate as fork_block."""
        raw = os.environ.get("BENCHMARK_EPOCH_BLOCK", "").strip()
        if not raw:
            return
        from minotaur_subnet.consensus.round_anchor import round_anchored_pin_enabled
        if round_anchored_pin_enabled():
            if not self._warned_env_pin_ignored:
                logger.warning(
                    "[fork-pin] BENCHMARK_EPOCH_BLOCK=%r ignored — ROUND_ANCHORED_PIN is on "
                    "(round-anchored derivation is authoritative). BENCHMARK_EPOCH_BLOCK is a "
                    "dev/test-only override; unset it in production.", raw,
                )
                self._warned_env_pin_ignored = True
            return
        try:
            block = int(raw)
        except ValueError:
            logger.warning("BENCHMARK_EPOCH_BLOCK=%r is not an int; ignoring", raw)
            return
        if block != self._epoch_block_number:
            self.set_epoch_block(block)
            logger.info("[fork-pin] benchmark pinned to Base block %d (BENCHMARK_EPOCH_BLOCK)",
                        block)

    def _apply_round_anchored_pin(self, round_id: str | None) -> None:
        """Pin the benchmark fork to the round-anchored block.

        When ROUND_ANCHORED_PIN is ON the pin is MANDATORY: ``benchmark_pack_hash``
        seals the *intended* fork block, so benchmarking at any OTHER block (live
        head, or a stale pin carried over from a prior round) produces a DIFFERENT
        score than peers while signing the SAME pack hash — a SILENT cross-host
        divergence (a validator believes it agrees but scored a different state).
        So if the pin cannot be resolved — no resolver/round_id, the resolver
        raised/deferred (``ForkPinUnavailable``), or it returned ``None`` — this
        RAISES ``ForkPinUnavailable`` and the caller DEFERS (retries next tick)
        rather than scoring at the wrong block. When the gate is OFF it stays a
        best-effort no-op (dev / live head; the env value stands).
        """
        from minotaur_subnet.consensus.round_anchor import round_anchored_pin_enabled

        gate_on = round_anchored_pin_enabled()
        if self._pin_resolver is None or not round_id:
            if gate_on:
                raise ForkPinUnavailable(
                    "ROUND_ANCHORED_PIN on but cannot pin "
                    f"(resolver={'set' if self._pin_resolver else 'none'}, "
                    f"round_id={round_id!r})"
                )
            return
        try:
            pin = self._pin_resolver(round_id)
        except ForkPinUnavailable:
            if gate_on:
                raise  # defer LOUD — never silently fall back to live head under the gate
            logger.warning("[fork-pin] round-anchored resolve deferred (gate off, live head)")
            return
        except Exception as exc:  # noqa: BLE001
            if gate_on:
                raise ForkPinUnavailable(
                    f"round-anchored pin resolve failed for {round_id}: {exc}"
                ) from exc
            logger.warning("[fork-pin] round-anchored resolve failed: %s", exc)
            return
        if pin is None:
            if gate_on:
                raise ForkPinUnavailable(
                    f"round-anchored pin unavailable (deferred) for {round_id}"
                )
            return
        if int(pin) != self._epoch_block_number:
            self.set_epoch_block(int(pin))
            logger.info("[fork-pin] benchmark pinned to Base block %d (round-anchored, %s)",
                        int(pin), round_id)

    async def run_loop(self, interval: float = 30.0) -> None:
        """Continuously poll for and process BENCHMARKING submissions.

        Args:
            interval: Seconds between polls when no work is found.
        """
        self._running = True
        logger.info("Benchmark worker started (interval=%ds)", interval)

        while self._running:
            try:
                processed = await self.run_once()
                if processed == 0:
                    await asyncio.sleep(interval)
            except Exception as exc:
                logger.exception("Benchmark worker error: %s", exc)
                await asyncio.sleep(interval)

    def stop(self) -> None:
        """Signal the worker to stop after the current batch."""
        self._running = False

    def _current_replay_round(self) -> Any | None:
        """Return the active replay-ready round when explicit round gating is enabled."""
        if self._round_store is None:
            return None
        current = self._round_store.get_current_round()
        if current is None:
            return None
        if current.status in (RoundStatus.CLOSED, RoundStatus.REPLAYING):
            return current
        return None

    async def run_once(self) -> int:
        """Process all BENCHMARKING submissions in a single pass.

        Returns the number of submissions processed.
        """
        # Startup-race guard: the run_loop is started right after construction, but
        # startup wires the real simulator a bit LATER (ctx.benchmark_worker._simulator).
        # A docker benchmark that runs before the simulator is attached falls to the
        # mock path -> on_chain null -> the genesis scores 0 -> REJECTED and is never
        # retried. Defer until the simulator is wired so the first benchmark is real.
        if self._use_docker and self._simulator is None:
            logger.info("[benchmark] real simulator not yet wired — deferring run_once")
            return 0
        # Deterministic fork-pin: when BENCHMARK_EPOCH_BLOCK is set, pin this round's
        # benchmark simulations to that Base block so on-chain scores are reproducible
        # across validators (the cross-machine determinism keystone). Default unset ->
        # live head (current behavior unchanged). Operators set the SAME value fleet-wide
        # for comparability; an automatic per-round shared block (leader-pinned via the
        # round state) is the production follow-up.
        self._apply_epoch_block_pin()
        replay_round = self._current_replay_round()
        # Round-anchored pin (authoritative over the env fallback above): the round
        # being benchmarked carries / derives a canonical fork block; pin to it so
        # the leader's scores reproduce on followers. None when the gate is off /
        # round not closed / deferred -> env/live-head unchanged.
        _pin_round_id: str | None = None
        if replay_round is not None:
            _pin_round_id = replay_round.round_id
        elif self._round_store is not None:
            _cur = self._round_store.get_current_round()
            if _cur is not None:
                _pin_round_id = _cur.round_id
        try:
            self._apply_round_anchored_pin(_pin_round_id)
        except ForkPinUnavailable as exc:
            logger.warning(
                "[benchmark] DEFERRING run_once — round-anchored pin unavailable: %s. "
                "Refusing to benchmark at live head while the pack hash seals the pin "
                "(would score a different block than peers -> silent cross-host "
                "divergence). Retrying next tick.",
                exc,
            )
            return 0
        benchmarking = self._sub_store.list_by_status(SubmissionStatus.BENCHMARKING)
        if replay_round is not None:
            benchmarking = [
                sub for sub in benchmarking
                if sub.round_id == replay_round.round_id
            ]
        elif self._round_store is not None:
            current_round = self._round_store.get_current_round()
            if current_round is not None:
                # Filter to submissions for the current round — benchmark
                # them eagerly so they're scored before the round closes.
                # Submissions from other rounds are skipped (already scored
                # or will be picked up in their round's evaluation).
                round_subs = [
                    s for s in benchmarking
                    if s.round_id == current_round.round_id
                ]
                if round_subs:
                    benchmarking = round_subs
                else:
                    # No submissions for this round — run genesis/bootstrap
                    processed = await self._maybe_bootstrap_solving_apps_with_champion()
                    if processed:
                        return processed
                    return await self._maybe_run_genesis()

        if not benchmarking:
            if self._round_store is not None and replay_round is not None:
                if self._sub_store.list_by_round(replay_round.round_id):
                    return 0
            processed = await self._maybe_run_genesis()
            if processed:
                return processed
            return await self._maybe_bootstrap_solving_apps_with_champion()

        print(f"[BENCHMARK] Found {len(benchmarking)} submissions to benchmark", flush=True)
        for s in benchmarking:
            print(f"[BENCHMARK]   {s.submission_id}: image_tag={s.image_tag} solver_path={s.solver_path} round={s.round_id}", flush=True)
        logger.info("Found %d submissions to benchmark", len(benchmarking))

        # Load intents to benchmark against
        intents = self._load_benchmark_intents()
        if not intents:
            logger.warning("No active intents for benchmarking")
            for sub in benchmarking:
                self._sub_store.set_benchmark_result(
                    sub.submission_id,
                    score=0.0,
                    details={"error": "no_active_intents"},
                )
            return len(benchmarking)

        # Build scoring function from JS engine (also loads JS into engine)
        score_fn = await self._build_score_fn(intents)

        # Stage 1: enrich intents with synthetic scenarios from manifest
        intents = self._enrich_intents_with_manifests(intents)

        # Stage 2: append historical order scenarios (deterministic from round_id).
        # Uses the current round's ID so all validators sample the same orders.
        if self._round_store is not None:
            current_round = self._round_store.get_current_round()
            if current_round is not None:
                try:
                    historical = self._load_historical_scenarios(current_round.round_id)
                    if historical:
                        intents.extend(historical)
                        logger.info(
                            "Added %d Stage 2 historical scenarios for round %s",
                            len(historical), current_round.round_id,
                        )
                except Exception as exc:
                    logger.warning("Failed to load historical scenarios: %s", exc)

        # Champion quote pre-pass: anchor each scenario's on-chain quote params
        # (CoW quoted_output etc.) to the champion solver so every challenger is
        # graded against the same reference output. Falls back to per-submission
        # self-quoting when no champion is available (still fixes the revert).
        reference_quotes = await self._build_reference_quotes(intents)

        # Benchmark each submission (route by solver_path or image_tag)
        for sub in benchmarking:
            # Skip already-scored submissions (may appear in BENCHMARKING
            # from a previous pass that scored then persisted)
            if sub.benchmark_score is not None and sub.benchmark_score > 0:
                logger.info("Skipping already-scored submission %s (%.3f)", sub.submission_id, sub.benchmark_score)
                continue
            if sub.solver_path is not None:
                if not _allow_subprocess_benchmark():
                    self._sub_store.reject(
                        sub.submission_id,
                        (
                            "Subprocess benchmarking is disabled by policy. "
                            "Use signed git/docker submissions."
                        ),
                    )
                    logger.warning(
                        "Rejected %s: subprocess benchmarking disabled by policy",
                        sub.submission_id,
                    )
                    continue
                # Source submission → subprocess mode (no Docker)
                logger.info(
                    "Benchmarking %s (solver=%s, path=%s)",
                    sub.submission_id,
                    sub.solver_name or "unknown",
                    sub.solver_path,
                )
                try:
                    results = await self._benchmark_solver_path(
                        sub.solver_path, intents, score_fn,
                        reference_quotes=reference_quotes,
                    )
                    avg_score = self._compute_avg_score(results)
                    details = self._results_to_details(results)

                    self._sub_store.set_benchmark_result(
                        sub.submission_id,
                        score=avg_score,
                        details=details,
                    )
                    logger.info(
                        "Submission %s scored %.4f (%d intents)",
                        sub.submission_id, avg_score, len(results),
                    )
                except Exception as exc:
                    logger.exception(
                        "Benchmarking failed for %s: %s",
                        sub.submission_id, exc,
                    )
                    self._sub_store.set_benchmark_result(
                        sub.submission_id,
                        score=0.0,
                        details={"error": str(exc)},
                    )

            elif sub.image_tag is not None:
                # Docker submission → existing Docker-based benchmark
                print(f"[BENCHMARK] Starting Docker benchmark for {sub.submission_id} image={sub.image_tag}", flush=True)
                try:
                    results = await self._benchmark_submission(
                        sub.image_tag, intents, score_fn,
                        reference_quotes=reference_quotes,
                    )
                    print(f"[BENCHMARK] Docker benchmark returned {len(results)} results", flush=True)
                    for r in results[:3]:
                        print(f"[BENCHMARK]   {r.intent_id}: score={r.score} error={r.error} plan={r.plan is not None}", flush=True)
                    avg_score = self._compute_avg_score(results)
                    details = self._results_to_details(results)
                    print(f"[BENCHMARK] avg_score={avg_score:.4f}", flush=True)

                    self._sub_store.set_benchmark_result(
                        sub.submission_id,
                        score=avg_score,
                        details=details,
                    )
                    logger.info(
                        "Submission %s scored %.4f (%d intents)",
                        sub.submission_id, avg_score, len(results),
                    )
                except Exception as exc:
                    import traceback
                    print(f"[BENCHMARK] Docker benchmark FAILED for {sub.submission_id}: {exc}", flush=True)
                    traceback.print_exc()
                    logger.exception(
                        "Benchmarking failed for %s: %s",
                        sub.submission_id, exc,
                    )
                    self._sub_store.set_benchmark_result(
                        sub.submission_id,
                        score=0.0,
                        details={"error": str(exc)},
                    )

            else:
                print(f"[BENCHMARK] No solver_path or image_tag for {sub.submission_id}", flush=True)
                self._sub_store.reject(
                    sub.submission_id,
                    "No solver_path or image_tag available for benchmarking",
                )

        # Transition SOLVING → SOLVED for apps that got a positive score
        self._transition_solving_apps(benchmarking)

        # Assign ranks within the replay batch; champion activation happens later.
        self._rank_scored_submissions(benchmarking)

        return len(benchmarking)

    async def _benchmark_submission(
        self,
        image_tag: str,
        intents: list[tuple[AppIntentDefinition, IntentState, MarketSnapshot]],
        score_fn: Any,
        reference_quotes: dict[str, dict[str, str]] | None = None,
    ) -> list[BenchmarkResult]:
        """Run the benchmark harness against one submission's Docker image."""
        orch = SolverOrchestrator()

        if self._use_docker:
            session = await orch.start_docker(image_tag)
        else:
            # For testing without Docker — find the solver path from image tag
            # This path is only used in tests
            raise ValueError(
                "Subprocess mode requires a solver_path, not an image_tag. "
                "Use use_docker=True for production."
            )

        try:
            results = await run_benchmark(
                session, intents,
                config=BenchmarkConfig(chain_ids=list({s.chain_id for _, s, _ in intents} or {1})),
                score_fn=score_fn,
                simulator=self._simulator,
                fork_block=self._epoch_block_number,
                require_real_sim=self._require_real_sim,
                reference_quotes=reference_quotes,
            )
            return results
        finally:
            await session.shutdown()

    async def _benchmark_solver_path(
        self,
        solver_path: str,
        intents: list[tuple[AppIntentDefinition, IntentState, MarketSnapshot]],
        score_fn: Any,
        reference_quotes: dict[str, dict[str, str]] | None = None,
    ) -> list[BenchmarkResult]:
        """Run the benchmark harness against a local solver file (subprocess mode)."""
        orch = SolverOrchestrator()
        session = await orch.start_subprocess(solver_path)
        try:
            results = await run_benchmark(
                session, intents,
                config=BenchmarkConfig(chain_ids=list({s.chain_id for _, s, _ in intents} or {1})),
                score_fn=score_fn,
                simulator=self._simulator,
                fork_block=self._epoch_block_number,
                require_real_sim=self._require_real_sim,
                reference_quotes=reference_quotes,
            )
            return results
        finally:
            await session.shutdown()

    async def _build_score_fn(
        self,
        intents: list[tuple[AppIntentDefinition, IntentState, MarketSnapshot]],
    ) -> Any:
        """Build an async scoring callback using JsExecutionEngine.

        Loads each intent's JS scoring code into the engine so that
        score_fn(app_id, plan, simulation, state) works for any intent.
        """
        if self._js_engine is not None:
            engine = self._js_engine
        else:
            from minotaur_subnet.engine import JsExecutionEngine
            engine = JsExecutionEngine(timeout_ms=10000)
            self._js_engine = engine  # Save for _enrich_intents_with_manifests()

        # Load (or hot-reload) JS scoring code for each intent. Mirror the
        # BlockLoop's hash-diff reload (blockloop/loop.py): reload when the
        # js_code hash changes so a developer's PUT /scoring is picked up on the
        # next benchmark run WITHOUT an api restart. (Previously this loaded
        # "only if not already loaded", so this worker's engine cached the
        # first-seen JS for the process lifetime.)
        for intent_def, _, _ in intents:
            js_code = intent_def.js_code
            if not js_code or len(js_code.strip()) < 20:
                logger.warning(
                    "App %s has no JS scoring code, skipping",
                    intent_def.app_id,
                )
                continue
            js_hash = hashlib.sha256(js_code.encode()).hexdigest()[:16]
            if self._loaded_js_hashes.get(intent_def.app_id) == js_hash:
                continue  # already loaded at this exact version
            await engine.load_intent(intent_def.app_id, js_code)
            old = self._loaded_js_hashes.get(intent_def.app_id)
            self._loaded_js_hashes[intent_def.app_id] = js_hash
            if old is not None:
                logger.info(
                    "[benchmark] hot-reloaded JS for app %s (hash %s -> %s)",
                    intent_def.app_id, old, js_hash,
                )

        async def score_fn(
            app_id: str,
            plan: ExecutionPlan,
            simulation: SimulationResult,
            state: IntentState,
        ) -> ScoreResult:
            return await engine.score(app_id, plan, simulation, state)

        return score_fn

    def _enrich_intents_with_manifests(
        self,
        intents: list[tuple[AppIntentDefinition, IntentState, MarketSnapshot]],
    ) -> list[tuple[AppIntentDefinition, IntentState, MarketSnapshot]]:
        """Enrich intents with manifest benchmark_scenarios or example_params.

        Priority:
        1. If manifest has benchmark_scenarios, expand one intent per scenario
        2. Else if manifest has intent_functions with example_params, expand
           one intent per function (existing behavior)
        3. Else return intent unchanged
        """
        # Need JS engine for manifest lookup
        engine = self._js_engine
        if engine is None:
            try:
                from minotaur_subnet.engine import JsExecutionEngine
                # Engine should already be created by _build_score_fn()
                # If not available, return intents unchanged
                return intents
            except Exception:
                return intents

        enriched: list[tuple[AppIntentDefinition, IntentState, MarketSnapshot]] = []

        for app_def, state, snapshot in intents:
            manifest = engine.get_manifest(app_def.app_id)
            if manifest is None:
                # No manifest — return intent unchanged
                enriched.append((app_def, state, snapshot))
                continue

            # Check for benchmark_scenarios first
            all_scenarios = manifest.get("benchmark_scenarios", [])
            # Filter scenarios by chain_id — scenarios with a "chains" field
            # only run on matching chains. Scenarios without "chains" run
            # everywhere (backward compat).
            scenarios = [
                s for s in all_scenarios
                if not s.get("chains") or state.chain_id in s["chains"]
            ]
            if scenarios:
                for scenario in scenarios:
                    fn_name = scenario.get("intent_function", "execute")
                    params = scenario.get("params", {})

                    new_raw_params = {**state.raw_params_view(), **params}
                    new_control = {
                        **state.control_view(),
                        "_intent_function": fn_name,
                        "_scenario_name": scenario.get("name", ""),
                        "_stage": "synthetic",
                    }
                    fund = scenario.get("fund")
                    if fund:
                        new_control["_fund"] = fund

                    new_state = IntentState(
                        contract_address=state.contract_address,
                        chain_id=state.chain_id,
                        nonce=state.nonce,
                        owner=state.owner,
                        raw_params=new_raw_params,
                        control=new_control,
                        context_version=state.context_version,
                        policy_tier=state.policy_tier,
                    )
                    enriched.append((app_def, new_state, snapshot))

                logger.info(
                    "App %s: expanded %d benchmark scenarios from manifest",
                    app_def.app_id, len(scenarios),
                )
                continue

            # Fall back to example_params per intent function
            intent_functions = manifest.get("intent_functions", [])
            if not intent_functions:
                enriched.append((app_def, state, snapshot))
                continue

            # Expand: one test intent per manifest function
            for fn_def in intent_functions:
                fn_name = fn_def.get("name", "execute")
                example_params = fn_def.get("example_params", {})

                new_raw_params = {**state.raw_params_view(), **example_params}
                new_control = {
                    **state.control_view(),
                    "_intent_function": fn_name,
                }

                new_state = IntentState(
                    contract_address=state.contract_address,
                    chain_id=state.chain_id,
                    nonce=state.nonce,
                    owner=state.owner,
                    raw_params=new_raw_params,
                    control=new_control,
                    context_version=state.context_version,
                    policy_tier=state.policy_tier,
                )
                enriched.append((app_def, new_state, snapshot))

            logger.info(
                "App %s: expanded %d intent functions from manifest",
                app_def.app_id, len(intent_functions),
            )

        return enriched

    async def _build_snapshot(self, chain_id: int) -> MarketSnapshot:
        """Build a market snapshot for the given chain.

        Uses SnapshotBuilder with the epoch block number if both are available.
        Falls back to build_synthetic_snapshot() if builder is unavailable or
        if the build fails.
        """
        if self._snapshot_builder is not None and self._epoch_block_number is not None:
            try:
                return await self._snapshot_builder.build_chain_snapshot(
                    chain_id=chain_id,
                    block_number=self._epoch_block_number,
                )
            except Exception as exc:
                logger.warning(
                    "SnapshotBuilder failed for chain %d at block %d, "
                    "falling back to synthetic: %s",
                    chain_id, self._epoch_block_number, exc,
                )

        from minotaur_subnet.harness.snapshot import build_synthetic_snapshot
        return build_synthetic_snapshot(chain_id)

    def _load_benchmark_intents(
        self,
        *,
        deployment_statuses: set[Any] | None = None,
    ) -> list[tuple[AppIntentDefinition, IntentState, MarketSnapshot]]:
        """Load active intents from the app store for benchmarking.

        Returns (intent_definition, state, snapshot) tuples.
        """
        if self._app_store is None:
            # Fallback: use synthetic intents for testing/MVP
            from minotaur_subnet.harness.snapshot import build_synthetic_intents
            return build_synthetic_intents()

        intents = []
        for app in self._app_store.list_apps():
            deployment = self._app_store.get_deployment(app.app_id)
            if deployment is None:
                continue
            if not deployment.status.is_operational():
                continue
            if (
                deployment_statuses is not None
                and deployment.status not in deployment_statuses
            ):
                continue

            chain_id = deployment.chain_id or 1
            state = IntentState(
                contract_address=deployment.contract_address or "",
                chain_id=chain_id,
                nonce=0,
                owner="",
            )

            # Use _build_snapshot (async) — but we're in a sync method.
            # Build snapshot synchronously via event loop for compatibility.
            try:
                import asyncio
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    # We're already in an async context — use synthetic fallback
                    # (the async caller should use _build_snapshot directly)
                    from minotaur_subnet.harness.snapshot import build_synthetic_snapshot
                    snapshot = build_synthetic_snapshot(chain_id)
                else:
                    snapshot = loop.run_until_complete(self._build_snapshot(chain_id))
            except Exception:
                from minotaur_subnet.harness.snapshot import build_synthetic_snapshot
                snapshot = build_synthetic_snapshot(chain_id)

            intents.append((app, state, snapshot))

        return intents

    def _load_historical_scenarios(
        self,
        round_id: str,
        n_per_chain: int | None = None,
    ) -> list[tuple[AppIntentDefinition, IntentState, MarketSnapshot]]:
        """Load Stage 2 scenarios from historical order samples.

        Deterministic sampling from round_id ensures all validators
        score against the same set of historical orders without needing
        to broadcast the sample list.

        Returns (intent_def, state, snapshot) tuples tagged with
        state.control["_stage"] == "historical" so the scoring pipeline
        can separate Stage 1 from Stage 2 for composite scoring.
        """
        if self._app_store is None:
            return []

        from minotaur_subnet.harness.order_sampler import (
            STAGE2_CORPUS_SAMPLES,
            sample_historical_orders,
        )

        if n_per_chain is None:
            # Fleet-uniform Stage-2 corpus size — a CODE constant, not the former
            # BENCHMARK_HISTORICAL_SAMPLES env (which let our lead score 10 while bare
            # followers scored 50 → divergent verdicts + a pack-hash blind to the split,
            # since startup.py's pack-hash builder used the same constant default).
            n_per_chain = STAGE2_CORPUS_SAMPLES

        # Stage-2 corpus source. Default: the local order store. Opt-in
        # (BENCHMARK_CHAIN_CORPUS): rebuild it from chain (plan Phase 5b) so a
        # freshly-promoted leader with an empty store still has a corpus. The
        # chain-derived records carry the SAME dict shape, so the deterministic
        # sample below is unchanged. MUST NOT be enabled live until the
        # cross-machine corpus-determinism gate passes.
        chain_records: list[dict[str, Any]] | None = None
        from minotaur_subnet.harness.chain_corpus import chain_corpus_enabled
        if chain_corpus_enabled():
            from minotaur_subnet.harness.chain_corpus import build_chain_corpus
            chain_ids = {
                d.chain_id
                for app in self._app_store.list_apps()
                for d in self._app_store.get_deployments(app.app_id).values()
            } or {8453}
            chain_records = []
            for cid in sorted(chain_ids):
                try:
                    chain_records.extend(build_chain_corpus(
                        self._app_store, self._js_engine, cid,
                        # Pin the corpus cutoff to the SAME block the benchmark forks
                        # at (round-anchored) so the Stage-2 sample matches across
                        # validators. Only the benchmark chain has the scalar pin;
                        # other chains fall back to env/live head.
                        to_block=(self._epoch_block_number if cid == 8453 else None)))
                except Exception as exc:
                    logger.warning("chain corpus build failed for chain %s: %s", cid, exc)

        # Single round-seeded SHARED draw — every validator derives the identical
        # subset (#242), so the champion-vs-challenger verdict is over one common
        # corpus and ratifiable by quorum.
        sampled = sample_historical_orders(
            app_store=self._app_store,
            round_id=round_id,
            n_per_chain=n_per_chain,
            records=chain_records,
        )
        if not sampled:
            return []

        # Group sampled orders by app_id to reuse AppIntentDefinition + snapshot
        apps_by_id = {app.app_id: app for app in self._app_store.list_apps()}
        snapshots_by_chain: dict[int, MarketSnapshot] = {}

        scenarios: list[tuple[AppIntentDefinition, IntentState, MarketSnapshot]] = []
        for order in sampled:
            app_id = order.get("app_id")
            chain_id = order.get("chain_id")
            if not app_id or chain_id is None:
                continue
            app_def = apps_by_id.get(app_id)
            if app_def is None:
                continue
            deployment = self._app_store.get_deployment(app_id)
            contract_address = deployment.contract_address if deployment else ""

            # Snapshot per chain (cached). Use synthetic snapshot here —
            # the solver re-queries live pool state via RPC anyway.
            if chain_id not in snapshots_by_chain:
                from minotaur_subnet.harness.snapshot import build_synthetic_snapshot
                snapshots_by_chain[chain_id] = build_synthetic_snapshot(chain_id)
            snapshot = snapshots_by_chain[chain_id]

            # Build IntentState from the historical order's params
            state = IntentState(
                contract_address=contract_address,
                chain_id=chain_id,
                nonce=0,
                owner="",
                raw_params=dict(order.get("params", {})),
                control={
                    "_stage": "historical",
                    "_scenario_name": f"hist:{order.get('order_id', '?')}",
                    "_intent_function": order.get("intent_function", "swap"),
                    "_original_block_number": order.get("block_number"),
                    "_original_tx_hash": order.get("tx_hash"),
                },
            )
            scenarios.append((app_def, state, snapshot))

        logger.info(
            "Loaded %d historical scenarios for round %s",
            len(scenarios), round_id,
        )
        return scenarios

    def _has_solving_apps(self) -> bool:
        """Check if any deployed apps are operational (SOLVING/SOLVED/ACTIVE)."""
        if self._app_store is None:
            return False
        for app in self._app_store.list_apps():
            deployment = self._app_store.get_deployment(app.app_id)
            if deployment is not None and deployment.status.is_operational():
                return True
        return False

    async def _maybe_run_genesis(self) -> int:
        """Auto-register and benchmark the baseline solver when no champion exists.

        The genesis submission goes through the same replay-scoring pipeline as
        any miner submission: create -> benchmark -> score -> rank.

        Prefers Docker image (genesis_solver_image) over subprocess path
        (baseline_solver_path). Docker mode uses the same sandboxed pipeline
        as miner submissions. Subprocess mode is deprecated.

        Returns the number of submissions processed (0 or 1).
        """
        if self._genesis_solver_image is None:
            return 0

        if self._sub_store.get_champion() is not None:
            return 0

        if not self._has_solving_apps():
            return 0

        # Idempotency: skip if genesis submission already exists
        existing = self._sub_store.get_by_hotkey_epoch(GENESIS_HOTKEY, GENESIS_EPOCH)
        if existing is not None:
            return 0

        logger.info("Genesis: no champion and SOLVING apps exist — bootstrapping baseline solver")

        round_id = None
        if self._round_store is not None:
            current_round = self._round_store.get_current_round()
            if current_round is not None:
                round_id = current_round.round_id

        # Create genesis submission (skip screening, go straight to BENCHMARKING)
        sub = self._sub_store.create(
            repo_url=GENESIS_REPO_URL,
            commit_hash="builtin",
            epoch=GENESIS_EPOCH,
            hotkey=GENESIS_HOTKEY,
            round_id=round_id,
        )
        self._sub_store.set_solver_info(
            sub.submission_id, name="baseline-swap-solver", version="2.0.0",
        )
        self._sub_store.update_status(sub.submission_id, SubmissionStatus.BENCHMARKING)
        logger.info("Genesis submission created: %s", sub.submission_id)

        # Load intents, build scoring, enrich with manifests (same as run_once)
        intents = self._load_benchmark_intents()
        if not intents:
            logger.warning("Genesis: no active intents for benchmarking")
            self._sub_store.set_benchmark_result(
                sub.submission_id, score=0.0, details={"error": "no_active_intents"},
            )
            return 1

        score_fn = await self._build_score_fn(intents)
        intents = self._enrich_intents_with_manifests(intents)

        # Stage 2: historical scenarios (if any order history exists)
        if self._round_store is not None:
            current_round = self._round_store.get_current_round()
            if current_round is not None:
                try:
                    historical = self._load_historical_scenarios(current_round.round_id)
                    if historical:
                        intents.extend(historical)
                except Exception as exc:
                    logger.warning("Failed to load genesis historical scenarios: %s", exc)

        # Genesis benchmark via Docker — same sandboxed pipeline as miner submissions.
        try:
            logger.info("Genesis benchmark via Docker image: %s", self._genesis_solver_image)
            results = await self._benchmark_submission(
                self._genesis_solver_image, intents, score_fn,
            )
            avg_score = self._compute_avg_score(results)
            details = self._results_to_details(results)

            self._sub_store.set_benchmark_result(
                sub.submission_id, score=avg_score, details=details,
            )
            logger.info(
                "Genesis submission scored %.4f (%d intents)", avg_score, len(results),
            )
        except Exception as exc:
            logger.exception("Genesis benchmarking failed: %s", exc)
            self._sub_store.set_benchmark_result(
                sub.submission_id, score=0.0, details={"error": str(exc)},
            )

        # Transition SOLVING apps and rank the replay result (same pipeline as run_once)
        self._transition_solving_apps([sub])
        self._rank_scored_submissions([sub])

        return 1

    def _resolve_incumbent_submission(self) -> Any | None:
        """The current INCUMBENT champion submission — resolved to equal the leader's
        ``bool(self._champion.submission_id)`` so the follower derives the SAME
        ``has_champion``. Resolution order: the ADOPTED champion, else the round-store
        active-champion snapshot, else a SCORED/ADOPTED genesis with a usable score.

        Genesis-as-bar (#242, user decision): the FIRST champion must BEAT genesis, so
        a benchmarked (SCORED, score>0) genesis IS the incumbent here — mirroring the
        leader's ``_maybe_seed_genesis_incumbent`` (which seeds self._champion from the
        same predicate at decision time). KEEP this predicate identical to the leader's
        or has_champion parity breaks. Returns ``None`` only at true bootstrap (no
        adopted/snapshot champion AND no scored genesis yet).
        """
        adopted = self._sub_store.get_champion()
        if adopted is not None:
            return adopted
        if self._round_store is not None:
            try:
                snapshot = self._round_store.get_active_champion()
                sid = getattr(snapshot, "submission_id", None)
                if sid:
                    return self._sub_store.get(sid)
            except Exception:  # pragma: no cover - defensive
                return None
        # Genesis-as-bar: a SCORED/ADOPTED genesis with a usable score is the
        # incumbent (same predicate as EpochManager._maybe_seed_genesis_incumbent).
        genesis = self._sub_store.get_by_hotkey_epoch(GENESIS_HOTKEY, GENESIS_EPOCH)
        if (
            genesis is not None
            and genesis.status in (SubmissionStatus.SCORED, SubmissionStatus.ADOPTED)
            and (genesis.benchmark_score or 0.0) > 0
        ):
            return genesis
        return None

    def _resolve_champion_image(self) -> str | None:
        """Resolve the Docker image tag of the current champion (or genesis).

        Mirrors the champion resolution in
        ``_maybe_bootstrap_solving_apps_with_champion``: prefer an explicit
        champion, fall back to a SCORED/ADOPTED genesis submission, and map a
        genesis hotkey with no image to the configured genesis solver image.
        Returns ``None`` when no usable champion image is available. (This is the
        image to RUN/benchmark — NOT the has_champion incumbent; see
        ``_resolve_incumbent_submission``.)
        """
        champion = self._sub_store.get_champion()
        if champion is None:
            genesis = self._sub_store.get_by_hotkey_epoch(GENESIS_HOTKEY, GENESIS_EPOCH)
            if genesis is not None and genesis.status in (
                SubmissionStatus.SCORED,
                SubmissionStatus.ADOPTED,
            ):
                champion = genesis
        if champion is None:
            return None
        image_tag = champion.image_tag
        if (
            image_tag is None
            and champion.hotkey == GENESIS_HOTKEY
            and self._genesis_solver_image
        ):
            image_tag = self._genesis_solver_image
        return image_tag

    async def run_shadow_vote(self, challenger_image: str) -> dict[str, Any]:
        """Observe-only per-validator shadow adopt-vote (challenger-quorum demo).

        Benchmarks the REAL reference champion — the adopted champion, or the
        official genesis solver when none is adopted (``_resolve_champion_image``,
        the same store-backed resolution scoring uses) — and ``challenger_image``
        on THIS validator's own diverse Stage-2 subset, then applies the shared
        ``evaluate_adoption`` rule. Returns + publishes this validator's vote.

        The reference is resolved from the store, NOT an injectable env, so a
        miner can't point the vote at a weak/own reference to look better.

        NEVER touches the real champion, adoption, or weights — it is a pure
        shadow computation so the fleet can demonstrate the challenger-quorum
        decision (good->adopt / bad->reject by majority). Each validator scores
        its own slice of orders, so disagreement on a regression is the feature.
        """
        from minotaur_subnet.harness.orchestrator import (
            BenchmarkConfig,
            RealSimulationUnavailable,
            SolverOrchestrator,
            require_real_sim_default,
            run_benchmark,
        )
        from minotaur_subnet.epoch.adopt_rule import evaluate_adoption
        from minotaur_subnet.epoch.manager import DETHRONE_MARGIN

        champ_image = self._resolve_champion_image()
        if not champ_image:
            return {"error": "no champion/genesis reference available"}
        if not challenger_image:
            return {"error": "challenger_image required"}

        # Same benchmark set run_once uses: Stage-1 synthetic + Stage-2 diverse
        # (per-validator seed when CHALLENGER_QUORUM_MODE is on).
        intents = self._load_benchmark_intents()
        if not intents:
            return {"error": "no active intents"}
        score_fn = await self._build_score_fn(intents)
        intents = self._enrich_intents_with_manifests(intents)
        round_id = None
        if self._round_store is not None:
            cur = self._round_store.get_current_round()
            if cur is not None:
                round_id = cur.round_id
                try:
                    hist = self._load_historical_scenarios(cur.round_id)
                    if hist:
                        intents.extend(hist)
                except Exception as exc:
                    logger.warning("[shadow-vote] historical load failed: %s", exc)

        _require_real_sim = require_real_sim_default()
        cfg = BenchmarkConfig(chain_ids=list({s.chain_id for _, s, _ in intents} or {1}))

        # Champion-anchored bar: grade BOTH the reference champion and the
        # challenger against the SHADOW champion's OWN quote (one shared floor,
        # ~0.5% slippage), with the champion->challenger self-quote fallback when
        # the champion can't quote a scenario. Without this both would self-quote
        # and tie — the saturation the product owner flagged.
        reference_quotes = await self._build_reference_quotes(intents, image_tag=champ_image)

        async def _bench(image: str) -> list[BenchmarkResult]:
            orch = SolverOrchestrator()
            sess = await orch.start_docker(image)
            try:
                return await run_benchmark(
                    sess, intents, config=cfg, score_fn=score_fn,
                    simulator=self._simulator, require_real_sim=_require_real_sim,
                    fork_block=self._epoch_block_number,
                    reference_quotes=reference_quotes,
                )
            finally:
                await sess.shutdown()

        try:
            champ_results = await _bench(champ_image)
            chal_results = await _bench(challenger_image)
        except RealSimulationUnavailable:
            return {"error": "real simulator unavailable"}

        champ_score = self._compute_avg_score(champ_results)
        chal_score = self._compute_avg_score(chal_results)
        adopt, reason = evaluate_adoption(
            challenger_score=chal_score,
            champion_score=champ_score,
            challenger_scorecard=self._build_scorecard(chal_results).to_dict(),
            champion_scorecard=self._build_scorecard(champ_results).to_dict(),
            dethrone_margin=DETHRONE_MARGIN,
            has_champion=True,
        )
        vote = {
            "candidate_id": challenger_image,
            "role": "shadow",
            "vote": "ADOPT" if adopt else "REJECT",
            "chal_score": round(float(chal_score), 4),
            "champ_score": round(float(champ_score), 4),
            "champion_image": champ_image,
            "validator_id": self._validator_identity,
            "round_id": round_id,
            "reason": reason,
        }
        logger.info(
            "[shadow-vote] validator=%s champ=%s chal=%s vote=%s "
            "champ_score=%.4f chal_score=%.4f: %s",
            self._validator_identity, champ_image, challenger_image,
            vote["vote"], champ_score, chal_score, reason,
        )
        try:
            from minotaur_subnet.api.server_context import ctx
            ctx.last_independent_vote = dict(vote)
        except Exception:  # observe-only — must never break
            pass
        return vote
    async def _build_reference_quotes(
        self,
        intents: list[tuple[AppIntentDefinition, IntentState, MarketSnapshot]],
        *,
        image_tag: str | None = None,
    ) -> dict[str, dict[str, str]]:
        """Quote every scenario with the CHAMPION solver as the reference.

        Runs a short champion pre-pass before benchmarking submissions: start a
        champion Docker session, ask it to quote each scenario, map the result
        via the shared ``map_quote_result_to_params`` helper, and key the output
        by the same per-scenario label ``run_benchmark`` uses (``app_id`` or
        ``app_id:scenario_name``). This anchors the on-chain ``quoted_output``
        (the CoW fee reference) to the champion so every challenger is graded
        against the same reference output — the champion→challenger fallback the
        product owner specified.

        Returns ``{}`` (every scenario self-quotes, which still fixes the
        revert) when no champion image is available, Docker is disabled, or the
        champion session can't be started.

        Per-scenario: when the champion session is up but FAILS to quote a
        specific scenario (raises or returns ``None``), that scenario's entry is
        set to the ``REFERENCE_QUOTE_FAILED_SENTINEL`` marker instead of being
        omitted. ``run_benchmark`` detects the marker and treats the scenario as
        a CHAMPION BLIND-SPOT: every solver SELF-QUOTES it (see the
        ``[champion-blind-spot]`` path in ``orchestrator.run_benchmark``). The
        champion — which can't quote it — scores 0 there, while a challenger that
        CAN quote + execute reveals real capability the champion lacks. The
        marker exists so this is a *surfaced, logged* blind spot, not a silently
        masked self-quote.
        """
        if not self._use_docker:
            return {}
        if image_tag is None:
            image_tag = self._resolve_champion_image()
        if image_tag is None:
            logger.info("Reference-quote pre-pass skipped: no champion image")
            return {}

        from minotaur_subnet.api.services.app_service import (
            map_quote_result_to_params,
            source_quote_param_names,
        )

        reference: dict[str, dict[str, str]] = {}
        failed: set[str] = set()
        orch = SolverOrchestrator()
        try:
            session = await orch.start_docker(image_tag)
        except Exception as exc:
            logger.warning(
                "Reference-quote pre-pass: failed to start champion session "
                "(%s); scenarios will self-quote", exc,
            )
            return {}
        from minotaur_subnet.harness.solver_read_proxy import (
            CHAIN_NAMES,
            build_pin_blocks,
            close_session,
            open_session,
            proxy_rpc_url,
            read_proxy_config,
        )
        _read_proxy = read_proxy_config()
        _proxy_session_id: str | None = None
        try:
            chain_ids = list({s.chain_id for _, s, _ in intents} or {1})
            rpc_map = build_rpc_url_map(chain_ids)
            missing_rpc = [c for c in chain_ids if c not in rpc_map]
            if missing_rpc:
                # Fail loud, not silent: without a live RPC the champion quotes
                # against an INCOMPLETE snapshot (missing pools → false "No route"
                # → false blind spots → corrupt reference quotes that EVERY
                # challenger is then graded against). Refuse rather than build the
                # network's scoring bar on degraded data.
                raise RuntimeError(
                    f"Reference-quote pre-pass: no benchmark RPC for chain(s) "
                    f"{missing_rpc} — refusing to build champion reference quotes "
                    f"on snapshot fallback (incomplete pools). Set "
                    f"BENCHMARK_ANVIL_RPC_* / *_SIM_RPC_URL / *_RPC_URL."
                )
            # Route the champion's reads through the SAME block-pin proxy the
            # challenger benchmark uses (orchestrator.run_benchmark), pinned at the
            # round's fork block. Without this the champion dials the RAW anvil fork
            # — unreachable on the sealed sandbox net (BENCHMARK_ALLOWED_HOSTS only
            # permits the proxy) → "Web3 not connected" → 0 reference quotes → every
            # challenger self-quotes (champion scores 0 everywhere, so the benchmark
            # measures nothing). This was the migration gap: the challenger path
            # moved to the proxy, the champion pre-pass kept the raw-anvil wiring.
            fork_block = self._epoch_block_number
            if _read_proxy is not None and fork_block is not None and rpc_map:
                pin_blocks = build_pin_blocks(_read_proxy, rpc_map, fork_block)
                if pin_blocks:
                    _proxy_session_id = f"refquote-{id(session):x}-{fork_block}"
                    await open_session(_read_proxy, _proxy_session_id, pin_blocks)
                    for cid in list(rpc_map):
                        if cid in _read_proxy.chain_ids and cid in CHAIN_NAMES:
                            rpc_map[cid] = proxy_rpc_url(
                                _read_proxy, _proxy_session_id, cid,
                            )
                    logger.info(
                        "[reference-quote] champion reads routed via block-pin "
                        "proxy session=%s pinned=%s", _proxy_session_id, pin_blocks,
                    )
            await session.initialize({
                "chain_ids": chain_ids,
                "rpc_urls": {str(k): v for k, v in rpc_map.items()},
            })
            for intent, state, snapshot in intents:
                intent_function = state.control_view().get("_intent_function", "swap")
                # Same manifest-driven gate as run_benchmark's enrichment.
                if not source_quote_param_names(intent.manifest, intent_function):
                    continue
                if state.raw_params_view().get("quoted_output") not in (None, ""):
                    continue
                scenario_name = state.control_view().get("_scenario_name", "")
                label = (
                    f"{intent.app_id}:{scenario_name}"
                    if scenario_name else intent.app_id
                )
                try:
                    quote_result = await session.quote(intent, state, snapshot)
                except Exception as exc:
                    # Surface, don't mask: a champion that can't quote a scenario
                    # is a real failure (broken solver, bad scenario, RPC issue).
                    # Mark it as a champion blind-spot so run_benchmark SELF-QUOTES
                    # it per-solver (champion scores 0 there; a challenger that can
                    # quote + execute reveals capability) instead of masking the
                    # champion failure behind a comparable pass.
                    logger.error(
                        "[reference-quote-FAILED] champion quote raised for %s "
                        "(%s); champion blind-spot — solvers SELF-QUOTE this "
                        "scenario, champion scores 0 here", label, exc,
                    )
                    reference[label] = {REFERENCE_QUOTE_FAILED_SENTINEL: "1"}
                    failed.add(label)
                    continue
                if quote_result is None:
                    logger.error(
                        "[reference-quote-FAILED] champion quote returned None "
                        "for %s; champion blind-spot — solvers SELF-QUOTE this "
                        "scenario, champion scores 0 here", label,
                    )
                    reference[label] = {REFERENCE_QUOTE_FAILED_SENTINEL: "1"}
                    failed.add(label)
                    continue
                mapped = map_quote_result_to_params(
                    quote_result, intent.manifest, intent_function,
                    slippage_bps=BENCHMARK_MIN_SLIPPAGE_BPS,  # loose benchmark floor
                )
                if mapped:
                    reference[label] = mapped
            built = len(reference) - len(failed)
            if failed:
                logger.error(
                    "Reference-quote pre-pass: built %d champion reference "
                    "quotes; %d scenario(s) FAILED to quote (champion blind-spots "
                    "— solvers self-quote these, champion scores 0): %s",
                    built, len(failed), sorted(failed),
                )
            else:
                logger.info(
                    "Reference-quote pre-pass: built %d champion reference quotes",
                    built,
                )
        finally:
            await session.shutdown()
            if _proxy_session_id is not None and _read_proxy is not None:
                try:
                    await close_session(_read_proxy, _proxy_session_id)
                except Exception:  # noqa: BLE001 — cleanup must not mask the result
                    pass
        return reference

    async def _maybe_bootstrap_solving_apps_with_champion(self) -> int:
        """Benchmark the current champion against newly deployed solving apps.

        This keeps newly deployed apps from getting stuck in SOLVING once a
        champion already exists, without creating synthetic submissions or
        disturbing current rankings/adoption state.
        """
        if self._app_store is None:
            return 0

        champion = self._sub_store.get_champion()
        if champion is None:
            genesis = self._sub_store.get_by_hotkey_epoch(GENESIS_HOTKEY, GENESIS_EPOCH)
            if genesis is not None and genesis.status in (
                SubmissionStatus.SCORED,
                SubmissionStatus.ADOPTED,
            ):
                champion = genesis
        if champion is None:
            return 0

        from minotaur_subnet.shared.types import AppStatus

        intents = self._load_benchmark_intents(
            deployment_statuses={AppStatus.SOLVING},
        )
        if not intents:
            return 0

        logger.info(
            "Champion bootstrap: benchmarking %s against %d solving intents",
            champion.submission_id,
            len(intents),
        )

        score_fn = await self._build_score_fn(intents)
        intents = self._enrich_intents_with_manifests(intents)

        try:
            image_tag = champion.image_tag
            if image_tag is None and champion.hotkey == GENESIS_HOTKEY and self._genesis_solver_image:
                image_tag = self._genesis_solver_image

            if image_tag is None:
                logger.warning(
                    "Champion bootstrap skipped for %s: no image_tag",
                    champion.submission_id,
                )
                return 0

            results = await self._benchmark_submission(
                image_tag, intents, score_fn,
            )
        except Exception as exc:
            logger.exception(
                "Champion bootstrap failed for %s: %s",
                champion.submission_id,
                exc,
            )
            return 1

        app_best: dict[str, float] = {}
        for result in results:
            bare_app_id = (
                result.intent_id.split(":")[0]
                if ":" in result.intent_id
                else result.intent_id
            )
            if result.score > app_best.get(bare_app_id, 0.0):
                app_best[bare_app_id] = result.score

        transitioned = 0
        for app_id, best_score in app_best.items():
            if best_score <= 0:
                continue
            dep = self._app_store.get_deployment(app_id)
            if dep is not None and dep.status == AppStatus.SOLVING:
                self._app_store.update_deployment_status(
                    app_id, dep.chain_id, AppStatus.SOLVED,
                )
                transitioned += 1
                logger.info(
                    "Champion bootstrap: app %s transitioned SOLVING -> SOLVED (best_score=%.4f)",
                    app_id,
                    best_score,
                )

        return 1 if results or transitioned else 0

    def _compute_avg_score(self, results: list[BenchmarkResult]) -> float:
        """Compute composite score: 40% Stage 1 (synthetic) + 60% Stage 2 (historical).

        If no Stage 2 (historical) results exist (e.g. new app with no order
        history), falls back to pure Stage 1 to avoid penalizing bootstrap.

        Failures and timeouts count as 0 in the denominator — prevents a
        solver that handles 1/10 well from outscoring one that handles 10/10.

        Mock-simulation results are heavily penalized (score zeroed) to
        prevent fabricated passing scores from inflating benchmarks.
        """
        if not results:
            return 0.0

        stage1 = self._compute_stage_score(results, stage_tag="synthetic")
        stage2 = self._compute_stage_score(results, stage_tag="historical")

        # If no historical scenarios ran, use pure Stage 1 (bootstrap case)
        if stage2.count == 0:
            return stage1.avg_score
        # If no synthetic scenarios ran, use pure Stage 2 (unusual, but possible)
        if stage1.count == 0:
            return stage2.avg_score

        return 0.4 * stage1.avg_score + 0.6 * stage2.avg_score

    def _compute_stage_score(
        self,
        results: list[BenchmarkResult],
        stage_tag: str,
    ) -> "_StageScore":
        """Compute average score for a single stage.

        Stage is identified by the _stage tag in each result's intent_id
        (via the state.control["_stage"] field preserved through scoring).
        For backward compat: results without a _stage tag are counted as
        "synthetic".
        """
        from dataclasses import dataclass

        stage_results = [
            r for r in results
            if _result_stage(r) == stage_tag
        ]
        if not stage_results:
            return _StageScore(avg_score=0.0, count=0, success_count=0)

        total = 0.0
        successes = 0
        for r in stage_results:
            if r.score <= 0:
                continue
            if getattr(r, "mock_simulation", False):
                continue
            total += r.score
            successes += 1
        avg = total / len(stage_results)
        return _StageScore(avg_score=avg, count=len(stage_results), success_count=successes)

    def _build_scorecard(self, results: list[BenchmarkResult]) -> BenchmarkScorecard:
        """Build a per-app scorecard from benchmark results.

        app_scores is keyed by the BARE app_id so the adoption gate enforces
        true per-app non-regression. intent_id has the form "<app_id>:<scenario>"
        (app_ids never contain ':'), so the first ':'-segment is the app; the
        full-label per-scenario breakdown is kept separately in scenario_scores.
        """
        # Group results by the bare app_id (strip the scenario suffix).
        by_app: dict[str, list[BenchmarkResult]] = {}
        scenario_scores: dict[str, float] = {}
        failures = 0
        for r in results:
            intent_label = r.intent_id or "unknown"
            app_id = intent_label.split(":")[0]
            by_app.setdefault(app_id, []).append(r)
            # Per-scenario breakdown stays at full-label granularity.
            scenario_scores[intent_label] = r.score
            if r.error is not None or r.plan is None or r.score <= 0:
                failures += 1

        app_scores: dict[str, float] = {}
        app_onchain: dict[str, list[int | None]] = {}
        for app_id, app_results in by_app.items():
            # Per-app average; failed/zero scenarios stay in the denominator
            # (same anti-gaming dilution as _compute_stage_score).
            app_total = sum(r.score for r in app_results if r.score > 0)
            app_scores[app_id] = app_total / len(app_results) if app_results else 0.0
            # Per-app on-chain scoreIntent BPS (one per scenario; None if no sim score).
            app_onchain[app_id] = [getattr(r, "on_chain_score", None) for r in app_results]

        global_score = self._compute_avg_score(results)

        mock_count = sum(1 for r in results if getattr(r, "mock_simulation", False))

        return BenchmarkScorecard(
            global_score=global_score,
            app_scores=app_scores,
            app_onchain=app_onchain,
            scenario_scores=scenario_scores,
            failures=failures,
            total=len(results),
            mock_simulation_count=mock_count,
        )

    def _results_to_details(
        self, results: list[BenchmarkResult],
    ) -> dict[str, Any]:
        """Convert benchmark results to a details dict for storage."""
        scorecard = self._build_scorecard(results)
        return {
            "total_intents": len(results),
            "plans_generated": sum(1 for r in results if r.plan is not None),
            "errors": sum(1 for r in results if r.error is not None),
            "avg_score": scorecard.global_score,
            "scorecard": scorecard.to_dict(),
            "per_intent": [
                {
                    "intent_id": r.intent_id,
                    "score": r.score,
                    "plan_score": r.plan_score,
                    "trigger_score": r.trigger_score,
                    "on_chain_score": getattr(r, "on_chain_score", None),
                    "elapsed_ms": r.elapsed_ms,
                    "error": r.error,
                    "revert_reason": getattr(r, "revert_reason", None),
                    "revert_trace": getattr(r, "revert_trace", None),
                    "has_plan": r.plan is not None,
                    "mock_simulation": getattr(r, "mock_simulation", False),
                }
                for r in results
            ],
        }

    def _transition_solving_apps(self, submissions: list) -> None:
        """Transition SOLVING → SOLVED for apps proven by benchmark results.

        After benchmarking, check per-app scores in the best submission's
        details. If any SOLVING app achieved avg_score > 0, transition it.
        """
        if self._app_store is None:
            return

        from minotaur_subnet.shared.types import AppStatus

        # Collect the best per-app scores across all submissions
        app_best: dict[str, float] = {}
        for sub in submissions:
            refreshed = self._sub_store.get(sub.submission_id)
            if refreshed is None or not refreshed.benchmark_details:
                continue
            per_intent = refreshed.benchmark_details.get("per_intent", [])
            for entry in per_intent:
                aid = entry.get("intent_id", "")
                sc = entry.get("score", 0.0)
                if aid and sc > app_best.get(aid, 0.0):
                    app_best[aid] = sc

        # Transition SOLVING apps that scored > 0
        for app_id, best_sc in app_best.items():
            if best_sc <= 0:
                continue
            # Strip scenario suffix (e.g., "app_xxx:WETH_to_USDC" → "app_xxx")
            bare_app_id = app_id.split(":")[0] if ":" in app_id else app_id
            dep = self._app_store.get_deployment(bare_app_id)
            if dep and dep.status == AppStatus.SOLVING:
                self._app_store.update_deployment_status(
                    bare_app_id, dep.chain_id, AppStatus.SOLVED,
                )
                logger.info(
                    "App %s transitioned SOLVING → SOLVED (best_score=%.4f)",
                    bare_app_id, best_sc,
                )

    def _rank_scored_submissions(
        self,
        submissions: list,
    ) -> list[Any]:
        """Assign benchmark ranks for the current replay-scored batch."""
        scored = []
        for sub in submissions:
            refreshed = self._sub_store.get(sub.submission_id)
            if refreshed and refreshed.status == SubmissionStatus.SCORED:
                scored.append(refreshed)

        if not scored:
            return []

        # Sort by benchmark score descending
        scored.sort(key=lambda s: s.benchmark_score or 0.0, reverse=True)

        # Assign ranks
        for i, sub in enumerate(scored):
            self._sub_store.set_benchmark_result(
                sub.submission_id,
                score=sub.benchmark_score or 0.0,
                rank=i + 1,
                details=sub.benchmark_details,
            )
        return scored
