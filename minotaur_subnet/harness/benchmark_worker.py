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
)
from minotaur_subnet.weight_policy import GENESIS_HOTKEY
from minotaur_subnet.epoch.relative_scoring import (
    evaluate_relative_adoption,
    has_delivered_value_rows,
)

logger = logging.getLogger(__name__)

# Genesis submission sentinel values
GENESIS_REPO_URL = "builtin://baseline-swap-solver"
GENESIS_EPOCH = 0


class ExplicitOrderUnavailable(Exception):
    """An explicit-order bench (veto slice / dissent re-verify / audit) cannot
    faithfully rebuild a requested order on this node — REFUSED semantics.

    The canonical corpus path silently skips unresolvable orders (every
    validator derives the same set, so a skip is fleet-uniform); an EXPLICIT
    list must not — a silently short slice benches less than it was asked to
    and returns a vacuous OK. Callers surface this as REFUSED(reason), which
    the veto protocol counts as ABSTAIN, never a round failure.
    """

    def __init__(self, order_id: str, reason: str):
        self.order_id = order_id
        self.reason = reason
        super().__init__(f"{reason} (order {order_id})")

GENESIS_SOLVER_IMAGE = os.environ.get("GENESIS_SOLVER_IMAGE", "").strip()

# Measurement-version tag for pre-refund metered gas rows (the GasMeter probe,
# anvil_simulator.GAS_METER_RUNTIME_HEX). Written by _results_to_details ONLY
# next to a non-None gas_metered, so a row either carries BOTH gas keys or
# NEITHER. Any future re-mechanism of the measurement bumps this version and
# becomes non-comparable instead of silently mixed. Fleet-uniform CODE
# constant — never env-read.
GAS_BASIS = "scoreintent_prerefund_v1"


def _gas_shadow_field(row: Any, name: str) -> Any:
    """Duck-typed row field access (dict key or attribute), mirroring
    relative_scoring._field: rows here are either fresh BenchmarkResult
    objects or persisted per_intent dicts."""
    if isinstance(row, dict):
        return row.get(name)
    return getattr(row, name, None)


def _gas_shadow_of(row: Any) -> int | None:
    """Metered gas of a row for [gas-shadow] display, or None (unmeasured).

    None-safe eligibility: gas_metered must parse to an int > 0, the row must
    not be a mock simulation, and — for persisted dict rows that carry a
    basis — the basis must be the current GAS_BASIS. Display/log only; the
    adoption-rule equivalent (_gas_of) ships with the stacked clause change.
    """
    if _gas_shadow_field(row, "mock_simulation"):
        return None
    gm = _gas_shadow_field(row, "gas_metered")
    if gm is None:
        return None
    basis = _gas_shadow_field(row, "gas_basis")
    if basis is not None and basis != GAS_BASIS:
        return None
    try:
        gm = int(gm)
    except (TypeError, ValueError):
        return None
    return gm if gm > 0 else None


def log_gas_shadow(champ_rows: Any, chal_rows: Any, ctx: str = "") -> None:
    """[gas-shadow] soak log: per-order + total champ/chal PRE-REFUND metered
    gas with coverage counts, on rows joined by intent_id.

    DISPLAY/LOG ONLY — zero influence on any verdict; this is the soak signal
    for calibrating the (separate, stacked) gas clause's margin. Cheap and
    None-safe: rows without gas keys count as unmeasured, any internal error
    is swallowed so the benchmark path can never break on observability.
    """
    try:
        def _by_iid(rows: Any) -> dict[str, Any]:
            out: dict[str, Any] = {}
            for r in rows or []:
                iid = _gas_shadow_field(r, "intent_id")
                if iid:
                    out[str(iid)] = r
            return out

        champ_by = _by_iid(champ_rows)
        chal_by = _by_iid(chal_rows)
        joined = sorted(set(champ_by) & set(chal_by))
        champ_total = chal_total = 0
        n_pairs = champ_measured = chal_measured = 0
        per_order: list[str] = []
        for iid in joined:
            cg = _gas_shadow_of(champ_by[iid])
            xg = _gas_shadow_of(chal_by[iid])
            champ_measured += cg is not None
            chal_measured += xg is not None
            if cg is not None and xg is not None:
                n_pairs += 1
                champ_total += cg
                chal_total += xg
            per_order.append(
                f"{iid}:{cg if cg is not None else '-'}"
                f"/{xg if xg is not None else '-'}"
            )
        # Durable tee: container logs die with every recreate (three separate
        # forensic gaps on 2026-07-07 alone), so the soak/monitoring signal is
        # ALSO appended as JSONL on the persistent volume. Best-effort — an
        # unwritable path must never break the benchmark path.
        try:
            import json as _json
            from pathlib import Path as _Path

            _dir = _Path(os.environ.get("APP_INTENTS_STORE_PATH", "/data/store.json")).parent
            if _dir.is_dir():
                with open(_dir / "gas_shadow.jsonl", "a") as _f:
                    _f.write(_json.dumps({
                        "ts": time.time(), "ctx": ctx or "-",
                        "orders": len(joined), "champ_measured": champ_measured,
                        "chal_measured": chal_measured, "measured_pairs": n_pairs,
                        "champ_gas_total": champ_total, "chal_gas_total": chal_total,
                        "per_order": per_order,
                    }) + "\n")
        except Exception:  # noqa: BLE001 — observability must never break benching
            pass
        logger.info(
            "[gas-shadow] ctx=%s orders=%d champ_measured=%d chal_measured=%d "
            "measured_pairs=%d champ_gas_total=%d chal_gas_total=%d "
            "per_order(champ/chal)=%s",
            ctx or "-", len(joined), champ_measured, chal_measured,
            n_pairs, champ_total, chal_total,
            ",".join(per_order[:64]) + ("…" if len(per_order) > 64 else ""),
        )
    except Exception:  # noqa: BLE001 - observability must never break the path
        logger.debug("[gas-shadow] logging failed", exc_info=True)


@dataclass
class BenchmarkScorecard:
    """Per-app and per-scenario scoring breakdown.

    Non-scalar diagnostics only. The scalar composite (``global_score``, the
    retired ``0.4*synthetic + 0.6*historical`` blend) was removed: adoption and
    finalist ranking are decided per-order by the relative rule
    (``epoch/relative_scoring``) over ``per_intent[*].raw_output``. Benchmarking is
    a SINGLE flat scenario set (every app's synthetic orders ∪ the round-seeded
    historical draw); there is no longer a synthetic/historical stage split.
    """
    app_scores: dict[str, float] = field(default_factory=dict)
    # Per-app on-chain scoreIntent BPS (one list entry per scenario; None when the
    # sim didn't yield a score). The unfakeable output signal the current adoption
    # rule's on-chain HARD VETO consumes. Populated only when a real sim runs.
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


def _rotation_slate_slots() -> int:
    """Benched-slate width (``SOLVER_ROUND_MAX_SUBMISSIONS``, 0 = no rotation).

    Mirrors ``routes._max_submissions_per_round_total`` — read directly from the
    env here to avoid a harness→api import. When > 0, the slate is selected at
    round CLOSE by LRU rotation (``harness/rotation.py``), so open-round eager
    benching is deferred (see ``run_once``).
    """
    raw = os.environ.get("SOLVER_ROUND_MAX_SUBMISSIONS", "0").strip()
    try:
        return int(raw)
    except ValueError:
        return 0


def _consolidate_champion_bench() -> bool:
    """Whether to MEMOIZE the champion benchmark within a round so the two
    champion-run paths (dethrone re-bench in ``_refresh_incumbent_score`` and the
    trustless quorum verdict in ``_independent_adopt_vote``) share ONE result
    instead of each re-running the champion solver. **DEFAULT ON**
    (``CONSOLIDATE_CHAMPION_BENCH=0`` disables).

    Pure compute optimization: a cached result is reused ONLY when round_id +
    champion image + fork block + corpus fingerprint + real-sim mode all match,
    so a hit is provably the SAME deterministic computation — the verdict and the
    persisted score are byte-identical to recomputing. Shipped default-off for
    separate validation, but the flag was never actually set in production
    (audited on the leader 2026-07-02), so every round paid the redundant
    champion run — now default-on with the env as the kill switch.
    """
    import os

    return os.environ.get("CONSOLIDATE_CHAMPION_BENCH", "1").strip().lower() not in (
        "0", "false", "no", "off",
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
        pin_resolver: Any = None,  # Callable[[round_id], int | dict[int, int] | None] -> round-anchored fork pin(s)
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
        # round_id -> the round-anchored fork pin(s): a {chain_id: block} map
        # (preferred), a bare int for the primary anchor chain (legacy), or None.
        self._pin_resolver = pin_resolver
        # Per-chain round-anchored fork pins ({chain_id: block}). None = legacy
        # scalar-only mode: run_benchmark applies _epoch_block_number to every
        # chain a plan routes to (unchanged Base-only behavior). Set whenever the
        # resolver yields a map, so multi-chain rounds pin each chain at ITS OWN
        # canonical block instead of leaking the Base height onto other forks.
        self._fork_pins: dict[int, int] | None = None
        # Stable per-validator id (hotkey ss58) — observability label only; the
        # Stage-2 corpus is a single round-seeded SHARED draw for every validator
        # (#242), so it no longer seeds the sample.
        self._validator_identity = validator_identity
        self._warned_env_pin_ignored = False  # one-shot WARN guard (P5 demotion)
        self._running = False
        # Serializes run_once against concurrent callers — the background
        # run_loop and the round-close evaluate_round both invoke run_once on
        # this same instance. Without it they race: the per-submission
        # idempotency guard loses a read-modify-write window and every
        # challenger is benchmarked TWICE per round, doubling the
        # _sim_lock-serialized sim work. Lazily bound to the running loop.
        self._run_once_lock: asyncio.Lock | None = None
        # app_id -> sha256(js_code)[:16] currently loaded in this worker's
        # engine. Lets _build_score_fn hot-reload a developer's PUT /scoring on
        # the next benchmark run instead of caching the first-seen JS forever
        # (the shared BlockLoop engine already hot-reloads this way; this worker
        # keeps its own engine, so it needs the same hash-diff).
        self._loaded_js_hashes: dict[str, str] = {}

    def _corpus_fingerprint(self, intents: list) -> str:
        """Stable hash of the corpus IDENTITY (ordered scenario labels), using the
        same ``app_id[:scenario_name]`` labelling as ``run_benchmark``'s per-scenario
        ``intent_label``. Guards the champion memo: a different corpus → different
        fingerprint → no reuse. Robust to missing fields (label degrades to app_id)."""
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

    async def memo_champion_bench(
        self,
        *,
        round_id: str | None,
        image: str | None,
        fork_block: int | None,
        intents: list,
        require_real_sim: bool,
        run: Any,
    ) -> list[BenchmarkResult]:
        """Run (or reuse) the champion benchmark for this round, via the PROCESS-WIDE
        memo so the dethrone re-bench and the quorum verdict (different worker
        instances) share ONE result.

        ``run`` is the caller's own async benchmark thunk (it owns session setup +
        exception semantics). Returns the cached result ONLY on an exact key match —
        round_id, image, fork_block, real-sim, corpus fingerprint, AND scoring-JS
        fingerprint — i.e. the identical deterministic computation, so a follower's
        verdict and persisted score are unchanged. Disabled (always recompute) when
        the flag is off, the key is incomplete (no round_id/image), or fork_block is
        None — a None pin means live-head (dev), where reuse across blocks is unsafe.
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

    def set_fork_pins(self, pins: dict[int, int] | None) -> None:
        """Set the per-chain round-anchored fork pins ({chain_id: block}).

        The primary anchor chain's pin also updates ``_epoch_block_number`` so
        the scalar ``fork_block`` path (Base-only) is unchanged when no other
        chain is pinned. ``None`` / empty clears the map (legacy scalar mode).
        Called by the follower verification path (input parity with the leader),
        mirroring how ``set_epoch_block`` sets the scalar.
        """
        if not pins:
            self._fork_pins = None
            return
        self._fork_pins = dict(pins)
        from minotaur_subnet.consensus.round_anchor import ROUND_ANCHOR_CHAINS
        primary = ROUND_ANCHOR_CHAINS[0]
        if primary in pins:
            self._epoch_block_number = int(pins[primary])

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
        # The resolver may return either a per-chain {chain_id: block} map
        # (multi-chain rounds) or a bare int for the primary anchor chain
        # (legacy Base-only). set_fork_pins handles the map (and updates the
        # scalar for the primary chain); a bare int keeps the scalar-only path.
        if isinstance(pin, dict):
            self.set_fork_pins(pin)
            logger.info("[fork-pin] benchmark pinned per-chain %s (round-anchored, %s)",
                        {c: int(b) for c, b in pin.items()}, round_id)
            return
        self._fork_pins = None
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

    def _recorded_benched_slate(self, round_id: str) -> list[str] | None:
        """The round's RECORDED rotation slate (``RoundState.benched_slate``),
        or ``None`` when unavailable (no round store, round not found, field
        unset on a pre-field / rotation-disabled round). The caller falls back
        to recomputation only on ``None``."""
        if self._round_store is None:
            return None
        try:
            state = self._round_store.get_round(round_id)
        except Exception:  # noqa: BLE001 — belt must never break the pass
            return None
        slate = getattr(state, "benched_slate", None) if state is not None else None
        return list(slate) if slate is not None else None

    def _cap_to_rotation_slate(self, benchmarking: list, round_id: str) -> list:
        """BELT for the rotation slate width: never bench a closed round past
        ``SOLVER_ROUND_MAX_SUBMISSIONS``.

        After rotation runs at close, every non-slate submission is REJECTED —
        so normally at most ``slots`` submissions are BENCHMARKING here and
        this is a no-op. If the close-time reject sweep was truncated (the api
        process killed mid-close — observed live 2026-07-07, round-e29724243-n1:
        12 of 19 overflow rejects landed and the round benched 10 submissions
        on 3 slots), the un-rejected overflow is still BENCHMARKING. Cap the
        pass at the slate width.

        SOURCE OF TRUTH: the round's RECORDED ``benched_slate``
        (``apply_rotation_slate`` writes it at close). Reading the recorded
        slate — instead of RECOMPUTING the rotation here — fixes the
        double-bench race: rotation advances the ledger (``mark_selected``) at
        close, so a belt that recomputed ``select_rotation_slate`` against that
        already-advanced ledger picked a DISJOINT slate and benched a second
        trio (2026-07-08, round-e29724975-n1: rotation selected {A,B,C}, the
        belt recomputed {D,B,C} and D scored too → 4 on 3 slots). Recomputation
        survives ONLY as the fallback for rounds with no recorded slate (closed
        before this field existed, or rotation genuinely never ran).
        """
        from minotaur_subnet.harness.rotation import (
            RotationLedger,
            rotation_ledger_path,
            select_rotation_slate,
        )

        slots = _rotation_slate_slots()
        if slots <= 0 or len(benchmarking) <= slots:
            return benchmarking

        # ── Preferred path: the RECORDED slate (single source of truth) ──────
        recorded = self._recorded_benched_slate(round_id)
        if recorded is not None:
            slate_ids = set(recorded)
            kept = [s for s in benchmarking if s.submission_id in slate_ids]
            dropped = [s for s in benchmarking if s.submission_id not in slate_ids]
            if dropped:
                logger.warning(
                    "[benchmark] round %s: %d BENCHMARKING submission(s) are "
                    "not on the recorded rotation slate %s — benching only the "
                    "slate (close-time reject sweep truncated). kept=%s "
                    "not-benched=%s",
                    round_id, len(dropped), sorted(slate_ids),
                    [s.submission_id for s in kept],
                    [s.submission_id for s in dropped],
                )
            return kept

        # ── Fallback: recompute (no recorded slate — pre-field / rotation
        # never ran). Kept for those rounds only; the recorded path above is
        # race-free.
        # The slate is a per-ROUND budget, not per-pass: select it over every
        # submission of the round that occupies (or already consumed) a slate
        # slot, so a slate member that got benched — SCORED, or benched-and-
        # validity-REJECTED (it has benchmark_details) — keeps holding its slot
        # and the overflow can never trickle in `slots`-sized bites across
        # passes. Excluded: ADOPTED (an old round's incumbent, never a mid-round
        # state) and rejects WITHOUT bench details (rotation/screening rejects —
        # they never occupied a slot; this mirrors rotation's candidate filter).
        def _occupies_slot(s: Any) -> bool:
            st = str(getattr(getattr(s, "status", None), "value", None)
                     or getattr(s, "status", "") or "")
            if st == "adopted":
                return False
            if st == "rejected":
                return bool(getattr(s, "benchmark_details", None))
            return True

        try:
            round_subs = self._sub_store.list_by_round(round_id)
        except Exception:  # noqa: BLE001 — belt must never break the pass
            round_subs = list(benchmarking)
        live = [s for s in round_subs if _occupies_slot(s)]
        if len(live) <= slots:
            return benchmarking
        last_selected = RotationLedger(rotation_ledger_path()).load()
        slate, _ = select_rotation_slate(live, slots, last_selected, round_id)
        slate_ids = {s.submission_id for s in slate}
        kept = [s for s in benchmarking if s.submission_id in slate_ids]
        dropped = [s for s in benchmarking if s.submission_id not in slate_ids]
        if dropped:
            logger.warning(
                "[benchmark] round %s has %d live submissions but the rotation "
                "slate is %d — the close-time reject sweep must have been "
                "truncated (or rotation never ran). Benching only the slate: "
                "kept=%s not-benched=%s (the overflow stays un-benched; this "
                "belt does not reject it).",
                round_id, len(live), slots,
                [s.submission_id for s in kept],
                [s.submission_id for s in dropped],
            )
        return kept

    async def run_once(self) -> int:
        """Process all BENCHMARKING submissions in a single pass.

        Serializes against concurrent callers: the background ``run_loop`` and
        the round-close ``evaluate_round`` both invoke ``run_once`` on the same
        worker. Holding a per-worker lock makes the second caller wait, then
        re-enter on an already-SCORED set and no-op via the per-submission
        idempotency guard — instead of racing and benchmarking every challenger
        twice per round (which doubles the ``_sim_lock``-serialized sim work).

        Returns the number of submissions processed.
        """
        lock = self._run_once_lock
        if lock is None:
            # Lazily create so the lock binds to the running event loop.
            lock = self._run_once_lock = asyncio.Lock()
        async with lock:
            return await self._run_once_impl()

    async def _run_once_impl(self) -> int:
        """Single benchmarking pass. Caller MUST hold ``self._run_once_lock``
        (entered via :meth:`run_once`)."""
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
            # BELT: a CLOSED/REPLAYING round must never bench past the
            # rotation slate, even when the close-time reject sweep was
            # truncated (see _cap_to_rotation_slate).
            benchmarking = self._cap_to_rotation_slate(
                benchmarking, replay_round.round_id,
            )
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
                    if (
                        current_round.status == RoundStatus.OPEN
                        and _rotation_slate_slots() > 0
                    ):
                        # Rotation (SOLVER_ROUND_MAX_SUBMISSIONS = slate width,
                        # selected at close by LRU seniority): the benched slate
                        # isn't known while the round is OPEN, so eager-benching
                        # now would spend serialized sim time on submissions
                        # that may not make the slate — and hand early
                        # submitters exactly the arrival-order head start the
                        # rotation exists to remove. Defer: the slate benches
                        # after close, under the auto-scaled decision window.
                        logger.debug(
                            "[benchmark] deferring %d open-round submission(s) "
                            "until the rotation slate is selected at close",
                            len(round_subs),
                        )
                        return 0
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
                    valid=False,
                    details={"error": "no_active_intents"},
                )
            return len(benchmarking)

        # Build scoring function from JS engine (also loads JS into engine)
        score_fn = await self._build_score_fn(intents)

        # Build the SINGLE flat benchmark scenario set: every app's synthetic
        # scenarios from the manifest ∪ the round-seeded historical order draw.
        # There is no stage split or weighting — the relative rule joins all
        # orders by intent_id regardless of origin.
        intents = self._enrich_intents_with_manifests(intents)

        # Append the round-seeded historical order draw (deterministic from
        # round_id, so every validator samples the identical orders).
        if self._round_store is not None:
            current_round = self._round_store.get_current_round()
            if current_round is not None:
                try:
                    historical = self._load_historical_scenarios(current_round.round_id)
                    if historical:
                        intents.extend(historical)
                        logger.info(
                            "Added %d historical order scenarios for round %s",
                            len(historical), current_round.round_id,
                        )
                except Exception as exc:
                    logger.warning("Failed to load historical scenarios: %s", exc)

        # Benchmark each submission (route by solver_path or image_tag)
        for sub in benchmarking:
            # FRESH-STATUS GUARD: ``benchmarking`` is a snapshot; rotation at
            # round close terminally rejects the slate overflow "regardless of
            # benchmark progress" (and purges its private token), and a restart
            # can re-queue an orphaned round mid-pass. Re-fetch and skip any
            # submission that is no longer BENCHMARKING — benching a rejected
            # sub wastes ~1-2 min of serialized sim time on a structurally dead
            # entry (the store's no-resurrection guard would refuse the SCORED
            # flip anyway; this saves the compute).
            fresh = self._sub_store.get(sub.submission_id)
            if fresh is None or fresh.status != SubmissionStatus.BENCHMARKING:
                logger.info(
                    "Skipping %s: status changed to %s since queue snapshot "
                    "(rotation/restart) — not benching",
                    sub.submission_id,
                    getattr(getattr(fresh, "status", None), "value", "gone"),
                )
                continue
            # Skip already-benchmarked submissions (may appear in BENCHMARKING
            # from a previous pass that benched then persisted). "Already
            # benchmarked" = it delivered value on >= 1 order (the validity gate).
            if sub.benchmark_details and has_delivered_value_rows(
                sub.benchmark_details.get("per_intent")
            ):
                logger.info("Skipping already-benchmarked submission %s", sub.submission_id)
                continue
            if sub.solver_path is not None:
                if not _allow_subprocess_benchmark():
                    self._sub_store.reject(
                        sub.submission_id,
                        (
                            "Subprocess benchmarking is disabled by policy. "
                            "Use signed git/docker submissions."
                        ),
                        outcome_code="benchmark_failed",
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
                    )
                    details = self._results_to_details(results)
                    valid = has_delivered_value_rows(details["per_intent"])

                    self._sub_store.set_benchmark_result(
                        sub.submission_id,
                        valid=valid,
                        details=details,
                    )
                    logger.info(
                        "Submission %s benchmarked over %d orders -> %s",
                        sub.submission_id, len(results),
                        "SCORED" if valid else "REJECTED (no order delivered value)",
                    )
                except Exception as exc:
                    logger.exception(
                        "Benchmarking failed for %s: %s",
                        sub.submission_id, exc,
                    )
                    self._sub_store.set_benchmark_result(
                        sub.submission_id,
                        valid=False,
                        details={"error": str(exc)},
                    )

            elif sub.image_tag is not None:
                # Docker submission → existing Docker-based benchmark
                print(f"[BENCHMARK] Starting Docker benchmark for {sub.submission_id} image={sub.image_tag}", flush=True)
                try:
                    results = await self._benchmark_submission(
                        sub.image_tag, intents, score_fn,
                    )
                    print(f"[BENCHMARK] Docker benchmark returned {len(results)} results", flush=True)
                    for r in results[:3]:
                        print(f"[BENCHMARK]   {r.intent_id}: score={r.score} error={r.error} plan={r.plan is not None}", flush=True)
                    details = self._results_to_details(results)
                    valid = has_delivered_value_rows(details["per_intent"])
                    print(f"[BENCHMARK] valid={valid} orders={len(results)}", flush=True)

                    self._sub_store.set_benchmark_result(
                        sub.submission_id,
                        valid=valid,
                        details=details,
                    )
                    logger.info(
                        "Submission %s benchmarked over %d orders -> %s",
                        sub.submission_id, len(results),
                        "SCORED" if valid else "REJECTED (no order delivered value)",
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
                        valid=False,
                        details={"error": str(exc)},
                    )

            else:
                print(f"[BENCHMARK] No solver_path or image_tag for {sub.submission_id}", flush=True)
                self._sub_store.reject(
                    sub.submission_id,
                    "No solver_path or image_tag available for benchmarking",
                    outcome_code="benchmark_failed",
                )

        # Transition SOLVING → SOLVED for apps that got a positive score
        self._transition_solving_apps(benchmarking)

        # Assign ranks within the replay batch; champion activation happens later.
        self._rank_scored_submissions(benchmarking)

        return len(benchmarking)

    async def _score_one_image(self, image_tag: str, *, context: str = "bench") -> dict[str, Any]:
        """Benchmark ONE image through the EXACT challenger scoring path and return
        its scorecard. SHARED by the incumbent re-score (_refresh_incumbent_score)
        and the diagnostic endpoint, so the incumbent and challengers are scored by
        IDENTICAL code — eliminating the incumbent self-quote inflation.

        Mirrors ``run_once``'s per-submission setup: applies the SAME epoch/round
        fork-pin a real challenger gets (the incumbent re-score historically did NOT
        apply the round-anchored pin), builds the same flat intents corpus (synthetic
        ∪ the round's historical order draw), then runs the same
        ``_benchmark_submission`` / ``_results_to_details``.
        Nothing is persisted or made adoption-eligible here — the caller decides what
        to do with the returned ``details`` (whose ``per_intent[*].raw_output`` rows
        are what the relative rule consumes).

        Raises ForkPinUnavailable when the round pin is unsealed (caller defers),
        and RuntimeError when the simulator isn't wired / no active intents.
        """
        if self._use_docker and self._simulator is None:
            raise RuntimeError("real simulator not yet wired — cannot score image")
        # Same deterministic fork-pin a real challenger gets this round.
        self._apply_epoch_block_pin()
        _pin_round_id: str | None = None
        if self._round_store is not None:
            _cur = self._round_store.get_current_round()
            if _cur is not None:
                _pin_round_id = _cur.round_id
        self._apply_round_anchored_pin(_pin_round_id)  # raises ForkPinUnavailable if unsealed

        intents = self._load_benchmark_intents()
        if not intents:
            raise RuntimeError("no active intents for benchmarking")
        score_fn = await self._build_score_fn(intents)
        intents = self._enrich_intents_with_manifests(intents)
        if self._round_store is not None:
            _cur = self._round_store.get_current_round()
            if _cur is not None:
                try:
                    historical = self._load_historical_scenarios(_cur.round_id)
                    if historical:
                        intents.extend(historical)
                except Exception as exc:
                    logger.warning("[%s] historical load failed: %s", context, exc)
        logger.info("[%s] scoring image %s via challenger path (%d intents)", context, image_tag, len(intents))
        results = await self._benchmark_submission(image_tag, intents, score_fn)
        details = self._results_to_details(results)
        # DISPLAY/logging only — the authoritative payload is `details`
        # (per_intent raw_output rows). Adoption/ranking never read this count.
        delivered_value_count = sum(
            1 for row in details["per_intent"]
            if row.get("raw_output") not in (None, "", "0")
        )
        logger.info(
            "[%s] image %s benchmarked: %d/%d orders delivered value",
            context, image_tag, delivered_value_count, len(results),
        )
        return {
            "image": image_tag,
            "delivered_value_count": delivered_value_count,
            "intent_count": len(results),
            "details": details,
            "pin_round_id": _pin_round_id,
        }

    async def score_image_diagnostic(self, image_tag: str) -> dict[str, Any]:
        """Endpoint wrapper: score an arbitrary image via the challenger path
        (no submission, round, or adoption). Thin alias over ``_score_one_image``."""
        return await self._score_one_image(image_tag, context="diagnostic")

    async def _benchmark_submission(
        self,
        image_tag: str,
        intents: list[tuple[AppIntentDefinition, IntentState, MarketSnapshot]],
        score_fn: Any,
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
                fork_blocks=self._fork_pins,
                require_real_sim=self._require_real_sim,
                session_factory=lambda: orch.start_docker(image_tag),
            )
            return results
        finally:
            await session.shutdown()

    async def _benchmark_solver_path(
        self,
        solver_path: str,
        intents: list[tuple[AppIntentDefinition, IntentState, MarketSnapshot]],
        score_fn: Any,
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
                fork_blocks=self._fork_pins,
                require_real_sim=self._require_real_sim,
                session_factory=lambda: orch.start_subprocess(solver_path),
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
            result = await engine.score(app_id, plan, simulation, state)
            # Relative per-order scoring source: the RAW delivered output now comes
            # from the LIVE scorer's own result metadata (metadata.raw_output), set
            # by the raw-output scorer an operator PUTs into the LIVE js_code slot at
            # cutover (PUT /apps/{id}/scoring). (Previously this was a SEPARATE shadow
            # slot dual-loaded under "<app_id>:shadow"; that slot is gone — we read
            # the live slot directly.) The engine clamps `score` to [0,1], so the
            # authoritative unclamped value is metadata.raw_output: an EXACT DECIMAL
            # WEI STRING (BigInt -> .toString()) stored VERBATIM — no float(), which
            # would reintroduce IEEE-754 precision loss above 2^53. The orchestrator
            # copies result.raw_output onto BenchmarkResult.raw_output and
            # _results_to_details into per_intent[*].raw_output; the relative rule
            # parses it with int(). None/"" when the live scorer emits no raw_output
            # (e.g. the pre-cutover quote-anchored scorer) -> no per-order signal.
            raw = (result.metadata or {}).get("raw_output")
            result.raw_output = (
                str(raw) if (raw is not None and str(raw) != "") else None
            )
            return result

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

    def _benchmark_deployments_for_app(self, app_id: str) -> list[Any]:
        """The deployment(s) of ``app_id`` to benchmark this round.

        Default: the single primary (order-ready-preferred) deployment —
        ``get_deployment`` — so Base-only behavior is byte-identical. When
        ``BENCHMARK_ALL_DEPLOYMENT_CHAINS`` is armed: EVERY per-chain deployment,
        so an app deployed on Base AND Ethereum gets each chain's scenarios
        benchmarked (and each chain can independently transition SOLVING→SOLVED).

        The old chain-less ``get_deployment`` is exactly why a second-chain
        deployment could never be promoted: it prefers the already-order-ready
        chain, so the SOLVING one on another chain was never seen by
        ``_transition_solving_apps``.
        """
        from minotaur_subnet.consensus.round_anchor import (
            benchmark_all_deployment_chains_enabled,
        )
        if benchmark_all_deployment_chains_enabled():
            return list(self._app_store.get_deployments(app_id).values())
        dep = self._app_store.get_deployment(app_id)
        return [dep] if dep is not None else []

    def _load_benchmark_intents(
        self,
        *,
        deployment_statuses: set[Any] | None = None,
    ) -> list[tuple[AppIntentDefinition, IntentState, MarketSnapshot]]:
        """Load active intents from the app store for benchmarking.

        Returns (intent_definition, state, snapshot) tuples — one per
        (app, benchmarked-deployment). With ``BENCHMARK_ALL_DEPLOYMENT_CHAINS``
        off this is one per app (the primary deployment, unchanged); on, it is
        one per operational per-chain deployment.
        """
        if self._app_store is None:
            # Fallback: use synthetic intents for testing/MVP
            from minotaur_subnet.harness.snapshot import build_synthetic_intents
            return build_synthetic_intents()

        intents = []
        for app in self._app_store.list_apps():
            for deployment in self._benchmark_deployments_for_app(app.app_id):
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
        """Load the round's historical-order scenarios (the round-seeded draw).

        Deterministic sampling from round_id ensures all validators score against
        the same set of historical orders without broadcasting the sample list.

        Returns (intent_def, state, snapshot) tuples. These join the synthetic
        scenarios as ONE flat benchmark set — there is no stage tag or split; the
        per-order relative rule keys on each order's ``hist:`` scenario name.
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

        # Canonical corpus path: unresolvable orders are SKIPPED (fleet-uniform —
        # every validator derives the same set, so a skip is symmetric).
        apps_by_id = {app.app_id: app for app in self._app_store.list_apps()}
        snapshots_by_chain: dict[int, MarketSnapshot] = {}

        scenarios: list[tuple[AppIntentDefinition, IntentState, MarketSnapshot]] = []
        for order in sampled:
            scenario = self._order_to_scenario(order, apps_by_id, snapshots_by_chain)
            if scenario is not None:
                scenarios.append(scenario)

        logger.info(
            "Loaded %d historical scenarios for round %s",
            len(scenarios), round_id,
        )
        return scenarios

    def _order_to_scenario(
        self,
        order: dict,
        apps_by_id: dict,
        snapshots_by_chain: dict[int, MarketSnapshot],
    ) -> tuple[AppIntentDefinition, IntentState, MarketSnapshot] | None:
        """Build ONE (app_def, IntentState, snapshot) replay tuple from an order
        record — the shared builder for the canonical historical draw AND the
        explicit-order benches (veto slices / re-verify / audits). Returns None
        when the order cannot be rebuilt here (missing app_id/chain/app record);
        callers decide skip-vs-refuse."""
        app_id = order.get("app_id")
        chain_id = order.get("chain_id")
        if not app_id or chain_id is None:
            return None
        app_def = apps_by_id.get(app_id)
        if app_def is None:
            return None
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
                # hist: prefix is the per-order JOIN id the relative rule keys
                # on, NOT a stage marker — benchmarking is one flat set now.
                "_scenario_name": f"hist:{order.get('order_id', '?')}",
                "_intent_function": order.get("intent_function", "swap"),
                "_original_block_number": order.get("block_number"),
                "_original_tx_hash": order.get("tx_hash"),
            },
        )
        return (app_def, state, snapshot)

    def build_explicit_scenarios(
        self,
        orders: list[dict],
    ) -> list[tuple[AppIntentDefinition, IntentState, MarketSnapshot]]:
        """STRICT scenario building for an EXPLICIT order list (veto slices,
        dissent re-verification, audits — distributed-veto Phase 0).

        Unlike the canonical draw, any unresolvable order raises
        :class:`ExplicitOrderUnavailable` (REFUSED semantics): a silently short
        slice would bench less than assigned and return a vacuous OK, which is
        exactly the failure the veto protocol's REFUSED/abstain path exists to
        make loud."""
        if self._app_store is None:
            raise ExplicitOrderUnavailable("<all>", "no app store on this node")
        apps_by_id = {app.app_id: app for app in self._app_store.list_apps()}
        snapshots_by_chain: dict[int, MarketSnapshot] = {}

        scenarios: list[tuple[AppIntentDefinition, IntentState, MarketSnapshot]] = []
        for order in orders:
            order_id = str(order.get("order_id", "?"))
            app_id = order.get("app_id")
            chain_id = order.get("chain_id")
            if not app_id or chain_id is None:
                raise ExplicitOrderUnavailable(order_id, "order lacks app_id/chain_id")
            if app_id not in apps_by_id:
                raise ExplicitOrderUnavailable(order_id, f"missing_app:{app_id}")
            try:
                scenario = self._order_to_scenario(
                    order, apps_by_id, snapshots_by_chain,
                )
            except ExplicitOrderUnavailable:
                raise
            except Exception as exc:  # e.g. params=None → dict(None) TypeError
                raise ExplicitOrderUnavailable(order_id, f"unbuildable: {exc}")
            if scenario is None:
                raise ExplicitOrderUnavailable(order_id, "unbuildable order record")
            scenarios.append(scenario)
        return scenarios

    async def benchmark_explicit_orders(
        self,
        image_tag: str,
        orders: list[dict],
    ) -> list[BenchmarkResult]:
        """Bench ONE image over an EXPLICIT order list at this worker's pins —
        the distributed-veto primitive (follower slice bench; leader dissent
        re-verify; audits). Strict: raises :class:`ExplicitOrderUnavailable`
        when any order cannot be faithfully rebuilt (callers answer REFUSED).

        Pins are the caller's responsibility: wire-layer callers must have
        applied the ASSIGNMENT's fork pins to this worker before calling —
        never a live-head fallback (an unpinned bench produces unconfirmable
        claims and honest-validator strikes)."""
        if not orders:
            raise ExplicitOrderUnavailable("<all>", "empty order list")
        if self._use_docker and self._simulator is None:
            # Fail-closed regardless of the per-validator BENCHMARK_REQUIRE_
            # REAL_SIM env: a mock-sim slice bench fabricates rows → false veto
            # claims → discarded veto + an honest-validator strike. REFUSED is
            # the correct answer when no real simulator is wired.
            raise ExplicitOrderUnavailable("<all>", "no real simulator wired")
        scenarios = self.build_explicit_scenarios(orders)
        score_fn = await self._build_score_fn(scenarios)
        return await self._benchmark_submission(image_tag, scenarios, score_fn)

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
                sub.submission_id, valid=False, details={"error": "no_active_intents"},
            )
            return 1

        score_fn = await self._build_score_fn(intents)
        intents = self._enrich_intents_with_manifests(intents)

        # Append the historical order draw (if any order history exists) — same
        # flat set as a real challenger, no stage split.
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
            details = self._results_to_details(results)
            valid = has_delivered_value_rows(details["per_intent"])

            self._sub_store.set_benchmark_result(
                sub.submission_id, valid=valid, details=details,
            )
            logger.info(
                "Genesis submission benchmarked over %d orders -> %s",
                len(results), "SCORED" if valid else "REJECTED (no order delivered value)",
            )
        except Exception as exc:
            logger.exception("Genesis benchmarking failed: %s", exc)
            self._sub_store.set_benchmark_result(
                sub.submission_id, valid=False, details={"error": str(exc)},
            )

        # Transition SOLVING apps and rank the replay result (same pipeline as run_once)
        self._transition_solving_apps([sub])
        self._rank_scored_submissions([sub])

        return 1

    def _blind_spot_bar_kwargs(self, incumbent: Any | None) -> dict[str, Any]:
        """Blind-spot REPEAT bar kwargs for this worker's relative verdicts
        (shadow vote, benchmark ranking) — round-store-sourced via
        ``bar_kwargs_from_record``, matched to ``incumbent``; ``{}`` (inert)
        on absence/mismatch/failure. Keeps every verdict site grading with the
        SAME rule inputs as the leader's authoritative decision."""
        try:
            from minotaur_subnet.epoch.relative_scoring import bar_kwargs_from_record

            if self._round_store is None or incumbent is None:
                return {}
            return bar_kwargs_from_record(
                self._round_store.get_champion_adoption_bar(),
                getattr(incumbent, "submission_id", None),
                time.time(),
            )
        except Exception:
            return {}

    def _resolve_incumbent_submission(self) -> Any | None:
        """The current INCUMBENT champion submission — resolved to equal the leader's
        ``bool(self._champion.submission_id)`` so the follower derives the SAME
        ``has_champion``. Resolution order: the ADOPTED champion, else the round-store
        active-champion snapshot, else a SCORED/ADOPTED genesis with a usable score.

        Genesis-as-bar (#242, user decision): the FIRST champion must BEAT genesis, so
        a benchmarked genesis that DELIVERED VALUE on >= 1 order IS the incumbent here
        — mirroring the leader's ``_maybe_seed_genesis_incumbent`` (which seeds
        self._champion from the same predicate at decision time). KEEP this predicate
        identical to the leader's or has_champion parity breaks. Returns ``None`` only
        at true bootstrap (no adopted/snapshot champion AND no valid genesis yet).
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
        # Genesis-as-bar: a SCORED/ADOPTED genesis that DELIVERED VALUE is the
        # incumbent (same predicate as EpochManager._maybe_seed_genesis_incumbent —
        # both call has_delivered_value_rows on the genesis per_intent rows).
        genesis = self._sub_store.get_by_hotkey_epoch(GENESIS_HOTKEY, GENESIS_EPOCH)
        if (
            genesis is not None
            and genesis.status in (SubmissionStatus.SCORED, SubmissionStatus.ADOPTED)
            and has_delivered_value_rows((genesis.benchmark_details or {}).get("per_intent"))
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
        on the round's shared flat benchmark set (synthetic ∪ the round-seeded
        historical order draw), then applies the AUTHORITATIVE relative per-order rule
        (:func:`evaluate_relative_adoption`) — the IDENTICAL rule the leader + every
        follower run. Returns + publishes this validator's vote.

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

        champ_image = self._resolve_champion_image()
        if not champ_image:
            return {"error": "no champion/genesis reference available"}
        if not challenger_image:
            return {"error": "challenger_image required"}

        # Same flat benchmark set run_once uses: synthetic ∪ the round-seeded
        # historical order draw (one shared draw for every validator).
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

        async def _bench(image: str) -> list[BenchmarkResult]:
            orch = SolverOrchestrator()
            sess = await orch.start_docker(image)
            try:
                return await run_benchmark(
                    sess, intents, config=cfg, score_fn=score_fn,
                    simulator=self._simulator, require_real_sim=_require_real_sim,
                    fork_block=self._epoch_block_number,
                    session_factory=lambda: orch.start_docker(image),
                )
            finally:
                await sess.shutdown()

        try:
            champ_results = await _bench(champ_image)
            chal_results = await _bench(challenger_image)
        except RealSimulationUnavailable:
            return {"error": "real simulator unavailable"}

        # The AUTHORITATIVE verdict is the per-order relative rule over the RAW
        # delivered output (raw_output), IDENTICAL to the leader + followers —
        # including the blind-spot bar kwargs (inert while the record doesn't
        # match the adopted incumbent, e.g. a genesis reference).
        verdict = evaluate_relative_adoption(
            champ_results, chal_results,
            **self._blind_spot_bar_kwargs(self._resolve_incumbent_submission()),
        )
        # [gas-shadow] soak observability — champion vs challenger metered
        # gas, display/log only, zero influence on the vote above.
        log_gas_shadow(
            champ_results, chal_results,
            ctx=f"shadow-vote:{challenger_image}",
        )
        adopt = bool(verdict["adopt"])
        reason = verdict["reason"]
        vote = {
            "candidate_id": challenger_image,
            "role": "shadow",
            "vote": "ADOPT" if adopt else "REJECT",
            "n_wins": verdict["n_wins"],
            "n_regressions": verdict["n_regressions"],
            "n_blind_spots": verdict["n_blind_spots"],
            "n_matched": verdict["n_matched"],
            "scenarios_compared": verdict["scenarios_compared"],
            "champion_image": champ_image,
            "validator_id": self._validator_identity,
            "round_id": round_id,
            "reason": reason,
        }
        logger.info(
            "[shadow-vote] validator=%s champ=%s chal=%s vote=%s wins=%d "
            "regressions=%d compared=%d: %s",
            self._validator_identity, champ_image, challenger_image,
            vote["vote"], verdict["n_wins"], verdict["n_regressions"],
            verdict["scenarios_compared"], reason,
        )
        try:
            from minotaur_subnet.api.server_context import ctx
            # Own slot (NOT last_independent_vote): a diagnostic probe must not
            # clobber the real fleet-tally field — e.g. a shadow REJECT (this
            # path is deliberately un-threaded with the factor tie-break)
            # overwriting a live follower ADOPT during a factor-tie dethrone
            # would read as fake dissent in the tally.
            ctx.last_shadow_vote = dict(vote)
        except Exception:  # observe-only — must never break
            pass
        return vote
    def _refquote_checkpoint_path(self) -> Path | None:
        """The on-disk checkpoint file, colocated with the submission store
        (which already defaults onto the /data volume — #430), or None when the
        store is memory-only (tests/dev)."""
        p = getattr(self._sub_store, "_persist_path", None)
        return p.with_name(_REFQUOTE_CHECKPOINT_FILENAME) if p is not None else None

    def _refquote_checkpoint_key(
        self, intents: list, image: str, fork_block: int, round_id: str,
    ) -> str:
        """Fully-deterministic identity of one pre-pass result — same components
        as the champion-bench memo key, flattened for JSON storage."""
        return "|".join((
            round_id, image, str(int(fork_block)),
            str(self._corpus_fingerprint(intents)),
            str(self._loaded_js_fingerprint(intents)),
        ))

    def _refquote_disk_load(self, key: str) -> dict[str, dict[str, str]] | None:
        path = self._refquote_checkpoint_path()
        if path is None:
            return None
        try:
            entries = json.loads(path.read_text()).get("entries", [])
        except (OSError, ValueError):
            return None
        for e in entries:
            if isinstance(e, dict) and e.get("key") == key:
                quotes = e.get("quotes")
                return quotes if isinstance(quotes, dict) else None
        return None

    def _refquote_disk_save(self, key: str, quotes: dict) -> None:
        """Best-effort atomic append-and-trim; a failed save only costs a
        recompute after the next restart, never correctness."""
        path = self._refquote_checkpoint_path()
        if path is None:
            return
        try:
            try:
                entries = json.loads(path.read_text()).get("entries", [])
            except (OSError, ValueError):
                entries = []
            entries = [e for e in entries if isinstance(e, dict) and e.get("key") != key]
            entries.append({"key": key, "quotes": quotes})
            entries = entries[-_REFQUOTE_CHECKPOINT_KEEP:]
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(json.dumps({"version": 1, "entries": entries}))
            tmp.replace(path)
        except OSError as exc:
            logger.warning("[reference-quote] checkpoint save failed: %s", exc)

    async def _get_or_build_reference_quotes(
        self,
        intents: list[tuple[AppIntentDefinition, IntentState, MarketSnapshot]],
        *,
        image_tag: str | None = None,
    ) -> dict[str, dict[str, str]]:
        """Memoized + /data-checkpointed :meth:`_build_reference_quotes`.

        The pre-pass is fully determined by (round, champion image, fork block,
        corpus, scoring-JS) — the same key discipline as ``memo_champion_bench``
        — so reuse is the identical computation, and an api restart mid-round
        picks the round back up with the SAME reference set instead of paying a
        fresh champion session (~30 quotes) per run_once pass.

        Falls through to a plain build when: checkpointing is disabled, any key
        component is unavailable (no round / no pin / no champion — dev paths),
        or the build produced an EMPTY result (champion session failed to start:
        transient, must retry next pass, never freeze for the round).

        STATIC-quote mode (the DEFAULT; opt out with
        ``BENCHMARK_STATIC_QUOTE=0``): the pre-pass is skipped entirely — the
        enrichment injects a static zero quote instead of anchoring on the
        champion's, so computing champion reference quotes (~30
        champion-session quotes/round) would be wasted work. Returns {}.
        Every benchmark path MUST obtain reference quotes through THIS getter,
        never `_build_reference_quotes` directly, or it silently benches under
        a different quote regime than the fleet's scoring definition.
        """
        if benchmark_static_quote_enabled():
            return {}

        round_id = None
        if self._round_store is not None:
            current = self._round_store.get_current_round()
            round_id = getattr(current, "round_id", None)
        image = image_tag
        if image is None and self._use_docker:
            image = self._resolve_champion_image()
        fork_block = self._epoch_block_number
        if (
            not _refquote_checkpoint_enabled()
            or not round_id or not image or fork_block is None
        ):
            return await self._build_reference_quotes(intents, image_tag=image_tag)

        key = self._refquote_checkpoint_key(intents, image, fork_block, round_id)
        hit = _REFERENCE_QUOTES_CACHE.get(key)
        if hit is not None:
            logger.info(
                "[reference-quote] reuse memoized pre-pass round=%s (skipped a "
                "champion session)", round_id,
            )
            return hit
        disk = self._refquote_disk_load(key)
        if disk is not None:
            logger.info(
                "[reference-quote] resumed pre-pass from /data checkpoint "
                "round=%s (survived a restart)", round_id,
            )
            _REFERENCE_QUOTES_CACHE[key] = disk
            return disk

        quotes = await self._build_reference_quotes(intents, image_tag=image)
        if quotes:  # {} = transient champion-session failure: retry next pass
            if len(_REFERENCE_QUOTES_CACHE) >= _REFERENCE_QUOTES_CACHE_MAX:
                _REFERENCE_QUOTES_CACHE.clear()
            _REFERENCE_QUOTES_CACHE[key] = quotes
            self._refquote_disk_save(key, quotes)
        return quotes

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
                # Per-chain pins (multi-chain round) so the champion quotes each
                # chain at ITS OWN block — same map the challenger benchmark uses,
                # so both are graded against identical pinned state. A missing
                # per-chain pin raises ValueError (build_pin_blocks); surface it
                # rather than pin one chain at another's block.
                pin_blocks = build_pin_blocks(
                    _read_proxy, rpc_map,
                    self._fork_pins if self._fork_pins else fork_block,
                )
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

        # Best score per (app_id, chain_id) so each per-chain SOLVING deployment
        # promotes independently (mirrors _transition_solving_apps).
        app_chain_best: dict[tuple[str, int | None], float] = {}
        for result in results:
            bare_app_id = (
                result.intent_id.split(":")[0]
                if ":" in result.intent_id
                else result.intent_id
            )
            key = (bare_app_id, getattr(result, "chain_id", None))
            if result.score > app_chain_best.get(key, 0.0):
                app_chain_best[key] = result.score

        transitioned = 0
        for (app_id, cid), best_score in app_chain_best.items():
            if best_score <= 0:
                continue
            dep = self._app_store.get_deployment(
                app_id, chain_id=cid if cid is not None else None,
            )
            if dep is not None and dep.status == AppStatus.SOLVING:
                self._app_store.update_deployment_status(
                    app_id, dep.chain_id, AppStatus.SOLVED,
                )
                transitioned += 1
                logger.info(
                    "Champion bootstrap: app %s (chain %s) transitioned SOLVING -> "
                    "SOLVED (best_score=%.4f)",
                    app_id, dep.chain_id, best_score,
                )

        return 1 if results or transitioned else 0

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
            # (anti-gaming dilution: a solver that handles 1/10 well doesn't
            # outscore one that handles 10/10). DIAGNOSTIC only — not an adoption input.
            app_total = sum(r.score for r in app_results if r.score > 0)
            app_scores[app_id] = app_total / len(app_results) if app_results else 0.0
            # Per-app on-chain scoreIntent BPS (one per scenario; None if no sim score).
            app_onchain[app_id] = [getattr(r, "on_chain_score", None) for r in app_results]

        mock_count = sum(1 for r in results if getattr(r, "mock_simulation", False))

        return BenchmarkScorecard(
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
        per_intent: list[dict[str, Any]] = []
        for r in results:
            row: dict[str, Any] = {
                "intent_id": r.intent_id,
                "score": r.score,
                "plan_score": r.plan_score,
                "trigger_score": r.trigger_score,
                "on_chain_score": getattr(r, "on_chain_score", None),
                # RAW delivered output from the LIVE raw-output scorer's
                # metadata.raw_output (see _build_score_fn); an EXACT DECIMAL WEI
                # STRING, or None when the live scorer emits no raw_output. This
                # is the per-order signal the relative adoption rule consumes.
                # Never feeds the legacy aggregate `score`. (Readers also accept
                # the legacy ``shadow_score`` key for rows persisted before the
                # rename — see relative_scoring._raw_output.)
                "raw_output": getattr(r, "raw_output", None),
                "elapsed_ms": r.elapsed_ms,
                "error": r.error,
                "revert_reason": getattr(r, "revert_reason", None),
                "revert_trace": getattr(r, "revert_trace", None),
                "has_plan": r.plan is not None,
                "mock_simulation": getattr(r, "mock_simulation", False),
                # Chain the scenario ran on — lets the per-chain SOLVING→SOLVED
                # transition promote the correct deployment when an app is
                # deployed on more than one chain. None on legacy rows / single
                # -chain apps (transition falls back to the primary deployment).
                "chain_id": getattr(r, "chain_id", None),
            }
            # PRE-REFUND metered gas (GasMeter probe) + its measurement-basis
            # tag — ADDITIVE keys, present ONLY when the probe measured this
            # row (never on mock/reverted/unmeasured rows: the write gate in
            # the bench loop already forced those to None). Measurement/soak
            # plumbing only — nothing reads these for any verdict yet.
            gm = getattr(r, "gas_metered", None)
            if gm is not None:
                row["gas_metered"] = gm
                row["gas_basis"] = GAS_BASIS
            per_intent.append(row)
        return {
            "total_intents": len(results),
            "plans_generated": sum(1 for r in results if r.plan is not None),
            "errors": sum(1 for r in results if r.error is not None),
            "scorecard": scorecard.to_dict(),
            "per_intent": per_intent,
        }

    def _transition_solving_apps(self, submissions: list) -> None:
        """Transition SOLVING → SOLVED for deployments proven by benchmark results.

        After benchmarking, check per-scenario scores in the submissions'
        details. A deployment transitions when ANY scenario for its (app, chain)
        scored > 0. Keyed per (app_id, chain_id) — NOT per app — so an app
        deployed on multiple chains promotes each chain independently as its
        scenarios pass (the chain-less lookup used to promote only the primary,
        order-ready deployment and leave a second chain stuck SOLVING forever).
        """
        if self._app_store is None:
            return

        from minotaur_subnet.shared.types import AppStatus

        # Best score per (app_id, chain_id). chain_id None (legacy/single-chain
        # rows) folds into the app's primary deployment via get_deployment below.
        app_chain_best: dict[tuple[str, int | None], float] = {}
        for sub in submissions:
            refreshed = self._sub_store.get(sub.submission_id)
            if refreshed is None or not refreshed.benchmark_details:
                continue
            per_intent = refreshed.benchmark_details.get("per_intent", [])
            for entry in per_intent:
                aid = entry.get("intent_id", "")
                if not aid:
                    continue
                bare_app_id = aid.split(":")[0] if ":" in aid else aid
                cid = entry.get("chain_id")
                sc = entry.get("score", 0.0)
                key = (bare_app_id, cid)
                if sc > app_chain_best.get(key, 0.0):
                    app_chain_best[key] = sc

        # Transition each proven (app, chain) SOLVING deployment.
        for (bare_app_id, cid), best_sc in app_chain_best.items():
            if best_sc <= 0:
                continue
            # cid None (legacy rows) → the app's primary deployment; else the
            # specific per-chain deployment.
            dep = self._app_store.get_deployment(
                bare_app_id, chain_id=cid if cid is not None else None,
            )
            if dep and dep.status == AppStatus.SOLVING:
                self._app_store.update_deployment_status(
                    bare_app_id, dep.chain_id, AppStatus.SOLVED,
                )
                logger.info(
                    "App %s (chain %s) transitioned SOLVING → SOLVED (best_score=%.4f)",
                    bare_app_id, dep.chain_id, best_sc,
                )

    def _rank_scored_submissions(
        self,
        submissions: list,
    ) -> list[Any]:
        """Assign DISPLAY benchmark ranks for the current replay-scored batch.

        Ranks by relative NET-BETTER vs the current champion (the same per-order
        raw_output signal finalist selection + adoption use), NOT a scalar score.
        Ties break on the content-addressed (image_id, submission_id) so the rank is
        host-deterministic. DISPLAY only — never gates adoption.
        """
        scored = []
        for sub in submissions:
            refreshed = self._sub_store.get(sub.submission_id)
            if refreshed and refreshed.status == SubmissionStatus.SCORED:
                scored.append(refreshed)

        if not scored:
            return []

        incumbent = self._resolve_incumbent_submission()
        champ_rows = (
            (incumbent.benchmark_details or {}).get("per_intent")
            if incumbent is not None else None
        ) or []

        bar_kwargs = self._blind_spot_bar_kwargs(incumbent)

        def _net_better(sub) -> int:
            rows = (sub.benchmark_details or {}).get("per_intent") or []
            # Bar kwargs so an armed repeat doesn't rank a photocopy-cover
            # ahead of a genuine win (disarmed: no-op).
            v = evaluate_relative_adoption(champ_rows, rows, **bar_kwargs)
            return v["n_wins"] + v["n_blind_spots"] - v["n_regressions"] - v["n_dropped"]

        # Highest net-better first; content-addressed tie-break (host-deterministic).
        scored.sort(key=lambda s: (
            -_net_better(s),
            str(s.image_id or ""),
            str(s.submission_id or ""),
        ))

        for i, sub in enumerate(scored):
            self._sub_store.set_benchmark_rank(sub.submission_id, i + 1)
            # [gas-shadow] soak observability — incumbent (stored rows) vs
            # this challenger's fresh rows; display/log only, never feeds
            # the rank above or any verdict.
            log_gas_shadow(
                champ_rows,
                (sub.benchmark_details or {}).get("per_intent") or [],
                ctx=f"rank{i + 1}:{sub.submission_id}",
            )
        return scored
