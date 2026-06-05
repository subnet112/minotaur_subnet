"""Epoch manager — connects benchmarking harness to the block loop.

Each epoch:
1. BenchmarkWorker processes all BENCHMARKING submissions and produces replay scores
2. EpochManager selects the best champion-eligible scored submission
3. EpochManager loads the champion as a SolverSession
4. BlockLoop.set_solver() hot-swaps to the new champion

The EpochManager is the glue: it detects epoch boundaries, triggers
benchmarking, and wires the winning solver into the live block loop.

Usage:
    manager = EpochManager(
        block_loop=block_loop,
        benchmark_worker=benchmark_worker,
        submission_store=submission_store,
        orchestrator=orchestrator,
    )
    # Called by the validator when an epoch boundary is detected
    await manager.on_epoch_boundary(epoch=42)
"""

from __future__ import annotations

import inspect
import logging
import os
import time
from dataclasses import dataclass
from typing import Any

from minotaur_subnet.harness.submission_store import (
    Submission,
    SubmissionStatus,
    SubmissionStore,
)
from minotaur_subnet.harness.champion_policy import is_submission_champion_eligible
from minotaur_subnet.harness.round_store import (
    ChampionSnapshot,
    RoundState,
    RoundStatus,
    RoundStore,
)
from minotaur_subnet.weight_policy import (
    build_bootstrap_or_champion_weights,
    get_subnet_owner_hotkey,
    is_real_miner_hotkey,
)

logger = logging.getLogger(__name__)

# Champion must beat the incumbent by this margin to be adopted.
# 0.005 == 0.5% (NOT 5% — the prose used to say 5%). The value is left as-is on
# purpose: raising it to a deliberate minimum-detectable-effect margin must wait
# until scores are cross-validator comparable (sealed-round work, design doc P1).
DETHRONE_MARGIN = 0.005


def _onchain_pass(scores: list, floor: int) -> tuple[bool, "int | None", int]:
    """all_pass, min_bps, n_missing — a champion-covered app must clear the floor on
    every scenario (ported from scoring_lab/stages.py)."""
    present = [s for s in scores if s is not None]
    n_missing = sum(1 for s in scores if s is None)
    all_pass = n_missing == 0 and all(s >= floor for s in present)
    return all_pass, (min(present) if present else None), n_missing


def _app_onchain_mean(scores: list) -> "float | None":
    """Mean on-chain scoreIntent BPS over present scenarios for an app — the unfakeable
    output-quality signal, independent of the gas-weighted JS score."""
    present = [s for s in scores if s is not None]
    return (sum(present) / len(present)) if present else None


async def _resolve_image_id_via_docker(image_tag: str) -> str | None:
    """Return the local sha256 image_id for *image_tag*, or None on error.

    Used by hot-swap to verify that the local image still matches the
    sha256 captured at Stage 3 before activating it as the new champion.
    """
    import asyncio as _asyncio
    proc = await _asyncio.create_subprocess_exec(
        "docker", "image", "inspect", "--format", "{{.Id}}", image_tag,
        stdout=_asyncio.subprocess.PIPE,
        stderr=_asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    if proc.returncode != 0:
        return None
    return stdout.decode("utf-8", errors="replace").strip() or None


def _stage3_disabled() -> bool:
    """Whether Stage 3 regression testing is opt-out-disabled.

    Read at call time so operators can flip without restarting. Dev-only
    escape hatch — leaving this on in production would let a champion with
    regressions through the gate.
    """
    return os.environ.get("STAGE3_DISABLED", "0").strip().lower() in (
        "1", "true", "yes", "on",
    )


@dataclass
class ChampionInfo:
    """Metadata about the currently active solver champion."""

    submission_id: str | None = None
    solver_name: str | None = None
    solver_version: str | None = None
    benchmark_score: float = 0.0
    epoch_adopted: int = 0
    image_tag: str | None = None
    hotkey: str | None = None
    adopted_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "submission_id": self.submission_id,
            "solver_name": self.solver_name,
            "solver_version": self.solver_version,
            "benchmark_score": self.benchmark_score,
            "epoch_adopted": self.epoch_adopted,
            "image_tag": self.image_tag,
            "hotkey": self.hotkey,
            "adopted_at": self.adopted_at,
        }


class EpochManager:
    """Manages solver lifecycle across epochs.

    Connects BenchmarkWorker results to BlockLoop solver hot-swapping.
    Tracks the current champion and enforces the dethrone margin.
    """

    def __init__(
        self,
        block_loop: Any = None,
        benchmark_worker: Any = None,
        submission_store: SubmissionStore | None = None,
        orchestrator: Any = None,
        round_store: RoundStore | None = None,
        runtime_builder: Any = None,
        dethrone_margin: float = DETHRONE_MARGIN,
        weights_emitter: Any = None,
        weight_decay: float = 0.6,
        owner_hotkey: str | None = None,
        on_champion_adopted: Any = None,
    ) -> None:
        self._block_loop = block_loop
        self._benchmark_worker = benchmark_worker
        self._sub_store = submission_store
        self._orchestrator = orchestrator
        self._round_store = round_store
        self._runtime_builder = runtime_builder
        self._dethrone_margin = dethrone_margin
        self._weights_emitter = weights_emitter
        self._weight_decay = weight_decay
        self._owner_hotkey = (owner_hotkey or "").strip() or get_subnet_owner_hotkey()
        self._on_champion_adopted = on_champion_adopted

        self._champion = ChampionInfo()
        self._current_session: Any = None  # SolverSession
        self._current_epoch: int = 0
        self._epoch_history: list[dict[str, Any]] = []
        # Last attempt by _emit_weights — surfaced via api's /health
        # so the validator-health workflow can attribute leader-side
        # emissions to the api process. Mirrors the schema used by the
        # validator daemon's _last_emit_state. None until the first
        # attempt completes. Records the queue POST outcome; the
        # validator daemon records the actual chain emit outcome
        # under its own _last_emit_state (source="queued_from_api"
        # when this manager's POST drove the emit).
        self._last_emit_state: dict | None = None

        restored = self._restore_active_champion_submission()
        if restored is not None:
            restored_snapshot = (
                self._round_store.get_active_champion()
                if self._round_store is not None
                else ChampionSnapshot()
            )
            self._champion = ChampionInfo(
                submission_id=restored.submission_id,
                solver_name=restored.solver_name,
                solver_version=restored.solver_version,
                benchmark_score=restored.benchmark_score or 0.0,
                epoch_adopted=(
                    restored_snapshot.activated_epoch
                    if restored_snapshot.submission_id == restored.submission_id
                    else restored.epoch
                ),
                image_tag=restored.image_tag,
                hotkey=restored.hotkey,
                adopted_at=(
                    restored_snapshot.activated_at
                    if restored_snapshot.submission_id == restored.submission_id
                    else restored.updated_at
                ),
            )

    # ── Public API ────────────────────────────────────────────────────────

    async def on_epoch_boundary(self, epoch: int) -> dict[str, Any]:
        """Called when a new epoch starts.

        1. Run benchmarks for all pending submissions
        2. Find the new champion
        3. If champion changed, hot-swap solver in block loop

        Returns a summary dict of what happened.
        """
        logger.info("Epoch boundary: epoch=%d (previous=%d)", epoch, self._current_epoch)
        self._current_epoch = epoch

        current_round = self._prepare_round(epoch)
        result = {
            "epoch": epoch,
            "benchmarked": 0,
            "champion_changed": False,
            "previous_champion": self._champion.to_dict(),
            "new_champion": None,
            "error": None,
            "round_id": current_round.round_id if current_round is not None else None,
            "next_round_id": None,
        }
        scope_round_id = current_round.round_id if current_round is not None else None

        # Step 1: Run benchmarks
        if self._benchmark_worker:
            try:
                await self._benchmark_worker.run_once()
                result["benchmarked"] = self._count_scored(epoch, round_id=scope_round_id)
            except Exception as exc:
                logger.error("Benchmark run failed in epoch %d: %s", epoch, exc)
                result["error"] = str(exc)
                next_round = self._complete_round(
                    current_round,
                    epoch,
                    activated=False,
                    abort_reason=f"benchmark_failed: {exc}",
                )
                if next_round is not None:
                    result["next_round_id"] = next_round.round_id
                self._epoch_history.append(result)
                return result

        # Step 2: Find the best champion-eligible scored submission for this epoch
        new_champion_sub = self._find_champion(epoch, round_id=scope_round_id)

        if new_champion_sub is None:
            logger.info("No champion found for epoch %d, keeping current solver", epoch)
            next_round = self._complete_round(
                current_round,
                epoch,
                activated=False,
                abort_reason="no_champion_candidate",
            )
            if next_round is not None:
                result["next_round_id"] = next_round.round_id
            result["weights_emitted"] = await self._emit_weights(epoch, round_id=scope_round_id)
            self._epoch_history.append(result)
            return result

        # Step 3: Re-score incumbent with current scenarios, then check margin
        await self._refresh_incumbent_score()
        if self._should_adopt(new_champion_sub):
            # Stage 3: regression gate — block adoption if challenger fails
            # scenarios where the incumbent still succeeds
            if not await self._passes_regression_gate(new_champion_sub, scope_round_id):
                logger.warning(
                    "Challenger %s blocked by Stage 3 regression gate",
                    new_champion_sub.submission_id,
                )
                next_round = self._complete_round(
                    current_round,
                    epoch,
                    activated=False,
                    abort_reason="regression_detected",
                )
                if next_round is not None:
                    result["next_round_id"] = next_round.round_id
                self._epoch_history.append(result)
                return result
            try:
                await self._hot_swap(new_champion_sub, epoch, round_id=scope_round_id)
                result["champion_changed"] = True
                result["new_champion"] = self._champion.to_dict()
                next_round = self._complete_round(
                    current_round,
                    epoch,
                    activated=True,
                )
                if next_round is not None:
                    result["next_round_id"] = next_round.round_id
                logger.info(
                    "Champion changed in epoch %d: %s (score=%.4f)",
                    epoch,
                    self._champion.solver_name,
                    self._champion.benchmark_score,
                )
            except Exception as exc:
                logger.error(
                    "Failed to hot-swap champion in epoch %d: %s", epoch, exc
                )
                result["error"] = str(exc)
                next_round = self._complete_round(
                    current_round,
                    epoch,
                    activated=False,
                    abort_reason=f"activation_failed: {exc}",
                )
                if next_round is not None:
                    result["next_round_id"] = next_round.round_id
        else:
            logger.info(
                "Challenger score %.4f does not beat champion %.4f by %.3g%% margin",
                new_champion_sub.benchmark_score or 0,
                self._champion.benchmark_score,
                self._dethrone_margin * 100,
            )
            next_round = self._complete_round(
                current_round,
                epoch,
                activated=False,
                abort_reason="dethrone_margin_not_met",
            )
            if next_round is not None:
                result["next_round_id"] = next_round.round_id

        # Step 4: Emit weights for all scored miners
        result["weights_emitted"] = await self._emit_weights(epoch, round_id=scope_round_id)

        self._epoch_history.append(result)
        return result

    async def evaluate_round(self, round_id: str, *, epoch: int) -> dict[str, Any]:
        """Replay and rank a closed round, producing at most one finalist."""
        if self._round_store is None:
            raise ValueError("round_store is required for explicit round evaluation")
        self._current_epoch = max(self._current_epoch, epoch)

        round_state = self._round_store.get_round(round_id)
        if round_state is None:
            raise KeyError(f"Round not found: {round_id}")
        if round_state.status == RoundStatus.OPEN:
            raise ValueError(f"Round {round_id} is still open")

        result = {
            "round_id": round_id,
            "epoch": epoch,
            "benchmarked": 0,
            "status_before": round_state.status.value,
            "status_after": round_state.status.value,
            "abort_reason": round_state.abort_reason,
            "finalist_submission_id": round_state.finalist_submission_id,
            "finalist_image_id": round_state.finalist_image_id,
            "next_round_id": None,
            "error": None,
        }

        if round_state.status == RoundStatus.CLOSED:
            round_state = self._round_store.set_round_status(
                round_id,
                RoundStatus.REPLAYING,
            )

        if self._benchmark_worker:
            try:
                await self._benchmark_worker.run_once()
                result["benchmarked"] = self._count_scored(epoch, round_id=round_id)
            except Exception as exc:
                result["error"] = str(exc)
                next_round = self._complete_round(
                    round_state,
                    epoch,
                    activated=False,
                    abort_reason=f"benchmark_failed: {exc}",
                )
                result["status_after"] = RoundStatus.ABORTED.value
                result["abort_reason"] = f"benchmark_failed: {exc}"
                if next_round is not None:
                    result["next_round_id"] = next_round.round_id
                return result

        finalist = self._find_champion(epoch, round_id=round_id)
        if finalist is None:
            next_round = self._complete_round(
                round_state,
                epoch,
                activated=False,
                abort_reason="no_champion_candidate",
            )
            result["status_after"] = RoundStatus.ABORTED.value
            result["abort_reason"] = "no_champion_candidate"
            if next_round is not None:
                result["next_round_id"] = next_round.round_id
            return result

        # Re-benchmark the incumbent with current scenarios so the
        # comparison is fair. Without this, a JS scoring update that adds
        # harder scenarios would make the incumbent's stale score (from
        # easier scenarios) impossible to beat.
        await self._refresh_incumbent_score()

        if not self._should_adopt(finalist):
            next_round = self._complete_round(
                round_state,
                epoch,
                activated=False,
                abort_reason="dethrone_margin_not_met",
            )
            result["status_after"] = RoundStatus.ABORTED.value
            result["abort_reason"] = "dethrone_margin_not_met"
            if next_round is not None:
                result["next_round_id"] = next_round.round_id
            return result

        # Stage 3: regression gate — block if challenger fails scenarios
        # that the incumbent still handles correctly at the original block.
        if not await self._passes_regression_gate(finalist, round_id):
            logger.warning(
                "Challenger %s blocked by Stage 3 regression gate (round %s)",
                finalist.submission_id, round_id,
            )
            next_round = self._complete_round(
                round_state,
                epoch,
                activated=False,
                abort_reason="regression_detected",
            )
            result["status_after"] = RoundStatus.ABORTED.value
            result["abort_reason"] = "regression_detected"
            if next_round is not None:
                result["next_round_id"] = next_round.round_id
            return result

        updated = self._round_store.set_round_finalist(
            round_id,
            submission_id=finalist.submission_id,
            image_id=finalist.image_id,
            benchmark_score=finalist.benchmark_score,
        )
        result["status_after"] = updated.status.value
        result["finalist_submission_id"] = updated.finalist_submission_id
        result["finalist_image_id"] = updated.finalist_image_id
        return result

    async def activate_certified_round(self, round_id: str, *, epoch: int) -> dict[str, Any]:
        """Activate a previously certified round finalist."""
        if self._round_store is None:
            raise ValueError("round_store is required for certified activation")

        self._current_epoch = max(self._current_epoch, epoch)
        round_state = self._round_store.get_round(round_id)
        if round_state is None:
            raise KeyError(f"Round not found: {round_id}")
        if round_state.status != RoundStatus.CERTIFIED:
            raise ValueError(
                f"Round {round_id} is {round_state.status.value}; expected certified",
            )
        certificate = round_state.certificate
        if certificate is None or not certificate.candidate_submission_id:
            raise ValueError(f"Round {round_id} has no champion certificate")

        effective_epoch = certificate.effective_epoch or round_state.effective_epoch or epoch
        result = {
            "round_id": round_id,
            "epoch": epoch,
            "effective_epoch": effective_epoch,
            "champion_changed": False,
            "new_champion": None,
            "next_round_id": None,
            "weights_emitted": False,
        }
        if epoch < effective_epoch:
            return result

        if self._sub_store is None:
            raise ValueError("submission_store is required for certified activation")
        submission = self._sub_store.get(certificate.candidate_submission_id)
        if submission is None:
            raise KeyError(
                f"Certified submission not found: {certificate.candidate_submission_id}",
            )

        await self._hot_swap(submission, effective_epoch, round_id=round_id)
        activated = self._round_store.activate_round(
            round_id,
            effective_epoch=effective_epoch,
        )
        next_round = self._round_store.open_next_round(
            opened_epoch=effective_epoch,
            incumbent=self._get_incumbent_snapshot(),
        )

        result["champion_changed"] = True
        result["new_champion"] = self._champion.to_dict()
        result["next_round_id"] = next_round.round_id
        result["weights_emitted"] = await self._emit_weights(
            effective_epoch,
            round_id=round_id,
        )
        result["status_after"] = activated.status.value

        # Notify the relayer to attest on-chain + create GitHub PR.
        # The certificate is passed so the callback can record the validator
        # signatures on BT EVM's ChampionRegistry and include the on-chain
        # tx hash in the PR body for the GitHub Action to verify.
        if self._on_champion_adopted is not None:
            try:
                cb_result = self._on_champion_adopted(
                    submission, round_id, certificate=certificate,
                )
                if inspect.isawaitable(cb_result):
                    await cb_result
            except Exception as exc:
                logger.warning("on_champion_adopted callback failed: %s", exc)

        return result

    def get_champion(self) -> dict[str, Any]:
        """Return metadata about the current champion solver."""
        return self._champion.to_dict()

    def get_epoch_history(self) -> list[dict[str, Any]]:
        """Return history of epoch transitions."""
        return list(self._epoch_history)

    @property
    def current_epoch(self) -> int:
        return self._current_epoch

    @property
    def champion(self) -> ChampionInfo:
        return self._champion

    # ── Internal ──────────────────────────────────────────────────────────

    def _find_champion(self, epoch: int, *, round_id: str | None = None) -> Submission | None:
        """Find the highest-scoring champion-eligible submission for the epoch.

        Prefers current-epoch `SCORED` or `ADOPTED` submissions and falls back
        to recent epochs if none are available for the current one.
        """
        if not self._sub_store:
            return None

        if round_id is not None:
            round_candidates = self._eligible_candidates(self._sub_store.list_by_round(round_id))
            if round_candidates:
                return round_candidates[0]
            # No candidates for this round — if there's no incumbent champion,
            # consider ALL scored submissions. This handles the case where a
            # submission was eagerly benchmarked during an earlier round.
            if not self._champion.submission_id:
                all_scored = self._eligible_candidates(
                    self._sub_store.list_by_status(SubmissionStatus.SCORED)
                )
                if all_scored:
                    logger.info(
                        "No round %s candidates; using best scored submission: %s (%.3f)",
                        round_id, all_scored[0].submission_id, all_scored[0].benchmark_score or 0,
                    )
                    return all_scored[0]
            return None

        epoch_candidates = self._eligible_candidates(self._sub_store.list_by_epoch(epoch))
        if epoch_candidates:
            return epoch_candidates[0]

        # Fall back: recent scored/adopted submissions from nearby epochs
        all_subs = []
        # Check recent epochs (current and previous 5)
        for e in range(max(0, epoch - 5), epoch + 1):
            all_subs.extend(self._sub_store.list_by_epoch(e))

        fallback_candidates = self._eligible_candidates(all_subs)
        if fallback_candidates:
            return fallback_candidates[0]

        return None

    def _eligible_candidates(self, submissions: list[Submission]) -> list[Submission]:
        """Filter and rank champion-eligible scored submissions."""
        eligible: list[Submission] = []
        for submission in submissions:
            if submission.status not in (SubmissionStatus.SCORED, SubmissionStatus.ADOPTED):
                continue
            if submission.benchmark_score is None or submission.benchmark_score <= 0:
                continue
            ok, reason = is_submission_champion_eligible(submission)
            if ok:
                eligible.append(submission)
                continue
            logger.info(
                "Skipping champion candidate %s: %s",
                submission.submission_id,
                reason,
            )
        eligible.sort(key=lambda s: s.benchmark_score or 0.0, reverse=True)
        return eligible

    async def _refresh_incumbent_score(self) -> None:
        """Re-benchmark the current champion with the latest scenarios.

        When the app's JS scoring code is updated (e.g. new benchmark
        scenarios added), the incumbent's stored score becomes stale —
        it was computed under different conditions. Re-benchmarking
        ensures challenger vs incumbent comparisons are fair.

        If the incumbent has no Docker image (genesis/builtin), or the
        benchmark worker is unavailable, the score is left unchanged.
        """
        if not self._champion.submission_id:
            return
        if not self._benchmark_worker:
            return

        # Find the incumbent's submission to get its image_tag
        incumbent_sub = None
        if self._sub_store:
            incumbent_sub = self._sub_store.get(self._champion.submission_id)
        if incumbent_sub is None:
            return

        image_tag = incumbent_sub.image_tag
        if not image_tag:
            # Builtin/genesis — no Docker image to benchmark
            return

        logger.info(
            "Re-benchmarking incumbent %s (%s) with current scenarios",
            self._champion.submission_id, image_tag,
        )

        try:
            # Guard: the benchmark worker must have the real internal
            # methods. Mock workers (from tests) don't, and calling
            # MagicMock methods would corrupt the champion score.
            if not callable(getattr(self._benchmark_worker, "_load_benchmark_intents", None)):
                return

            intents = self._benchmark_worker._load_benchmark_intents()
            if not isinstance(intents, list) or not intents:
                return

            score_fn = await self._benchmark_worker._build_score_fn(intents)
            intents = self._benchmark_worker._enrich_intents_with_manifests(intents)

            # Stage 2: append historical scenarios so incumbent is scored
            # under the same conditions as challengers.
            if self._round_store is not None and callable(
                getattr(self._benchmark_worker, "_load_historical_scenarios", None)
            ):
                try:
                    current_round = self._round_store.get_current_round()
                    if current_round is not None:
                        historical = self._benchmark_worker._load_historical_scenarios(
                            current_round.round_id,
                        )
                        if isinstance(historical, list) and historical:
                            intents.extend(historical)
                except Exception:
                    pass  # fall through with synthetic-only scenarios

            results = await self._benchmark_worker._benchmark_submission(
                image_tag, intents, score_fn,
            )
            if not isinstance(results, list):
                return
            fresh_score = self._benchmark_worker._compute_avg_score(results)

            old_score = self._champion.benchmark_score
            self._champion.benchmark_score = fresh_score

            # Also update the submission store so the score persists
            if self._sub_store and fresh_score != old_score:
                self._sub_store.set_benchmark_result(
                    incumbent_sub.submission_id,
                    score=fresh_score,
                    details=self._benchmark_worker._results_to_details(results),
                )

            logger.info(
                "Incumbent re-scored: %s %.4f → %.4f (%d scenarios)",
                self._champion.submission_id, old_score, fresh_score, len(results),
            )
        except Exception:
            logger.warning(
                "Failed to re-benchmark incumbent %s — using stale score %.4f",
                self._champion.submission_id, self._champion.benchmark_score,
                exc_info=True,
            )

    async def _passes_regression_gate(
        self,
        challenger: Submission,
        round_id: str | None,
    ) -> bool:
        """Stage 3 regression gate. Returns True to allow adoption.

        Truth table (explicit so the audit doesn't have to re-derive it):

          | candidates | archive ok | STAGE3_DISABLED | result | why |
          |------------+------------+-----------------+--------+-----|
          | -          | -          | 1               | True   | explicit skip |
          | 0          | -          | 0               | True   | nothing to regress |
          | >0         | yes        | 0               | T/F    | run the test |
          | >0         | no         | 0               | False  | fail-closed on missing archive |

        "archive ok" means every chain the candidates reference has an
        archive RPC configured. We check all of them up front so we don't
        waste work on one candidate only to fail on the next.
        """
        if _stage3_disabled():
            logger.info("[stage3] disabled by STAGE3_DISABLED env — returning True")
            return True

        if not self._benchmark_worker:
            # No benchmark worker → can't replay, fail-closed
            return False
        if not self._sub_store:
            return False

        incumbent_sub = self._sub_store.get(self._champion.submission_id) if self._champion.submission_id else None
        if incumbent_sub is None or not incumbent_sub.image_tag:
            # No incumbent to compare against → genesis case, skip
            logger.info("[stage3] skipped: no incumbent Docker image available")
            return True

        if not challenger.image_tag:
            logger.info("[stage3] skipped: challenger has no image_tag")
            return True

        # Find regression candidates from challenger's benchmark details
        details = getattr(challenger, "benchmark_details", None) or {}
        results = details.get("results") or []
        candidates: list[dict] = []
        for r in results:
            intent_id = r.get("intent_id", "")
            score = r.get("score", 0)
            error = r.get("error")
            # Only historical scenarios where challenger failed
            if ":hist:" not in intent_id:
                continue
            if score > 0:
                continue
            # Extract order_id from intent_id format "app_xxx:hist:ord_yyy"
            parts = intent_id.split(":hist:")
            if len(parts) != 2:
                continue
            order_id = parts[1]
            if self._app_store is None:
                continue
            order = self._app_store.get_order(order_id) if callable(
                getattr(self._app_store, "get_order", None)
            ) else None
            if order is None:
                continue
            block_number = order.get("block_number")
            chain_id = order.get("chain_id")
            if block_number is None or chain_id is None:
                continue
            candidates.append({
                "order_id": order_id,
                "chain_id": chain_id,
                "block_number": block_number,
                "params": order.get("params", {}),
                "app_id": order.get("app_id"),
            })

        if not candidates:
            logger.info("Stage 3: no regression candidates (no failed historical orders)")
            return True

        max_checks = int(os.environ.get("STAGE3_MAX_REGRESSION_CHECKS", "5"))
        candidates = candidates[:max_checks]

        logger.info(
            "[stage3] checking %d potential regression(s) for challenger %s",
            len(candidates), challenger.submission_id,
        )

        from minotaur_subnet.harness.historical_fork import (
            historical_anvil,
            archive_rpc_available,
            HistoricalForkError,
        )

        # Pre-flight: every chain the candidates reference must have an
        # archive RPC. Checked up front so we fail-closed in one shot
        # rather than mid-loop after partial work. Truth-table row:
        #   enabled + missing-archive + have-candidates → False.
        missing_chains = sorted({
            cand["chain_id"] for cand in candidates
            if not archive_rpc_available(cand["chain_id"])
        })
        if missing_chains:
            logger.warning(
                "[stage3] fail-closed: archive RPC missing for chain(s) %s "
                "(set STAGE3_DISABLED=1 to skip this gate in dev only)",
                missing_chains,
            )
            return False

        # Build a score function once (reused across all candidates)
        from minotaur_subnet.shared.types import IntentState
        from minotaur_subnet.harness.snapshot import build_synthetic_snapshot

        try:
            # Use an empty intent list to initialize the JS engine,
            # then we'll pass per-candidate intents to the scenario runner.
            _proto_intents = self._benchmark_worker._load_benchmark_intents()
            if not _proto_intents:
                logger.info("[stage3] no active intents to build score_fn; skipping")
                return True
            score_fn = await self._benchmark_worker._build_score_fn(_proto_intents)
        except Exception:
            logger.warning("[stage3] failed to build score_fn — failing closed", exc_info=True)
            return False

        # Index app definitions for fast lookup
        apps_by_id = {app.app_id: app for app in (self._app_store.list_apps() if self._app_store else [])}

        regressions_detected = 0
        for cand in candidates:
            chain_id = cand["chain_id"]

            app_def = apps_by_id.get(cand["app_id"])
            if app_def is None:
                logger.debug("Stage 3: app %s not found for order %s, skipping", cand["app_id"], cand["order_id"])
                continue

            deployment = self._app_store.get_deployment(cand["app_id"]) if self._app_store else None
            contract_address = deployment.contract_address if deployment else ""

            state = IntentState(
                contract_address=contract_address,
                chain_id=chain_id,
                nonce=0,
                owner="",
                raw_params=dict(cand["params"] or {}),
                control={
                    "_intent_function": cand.get("intent_function", "swap"),
                    "_scenario_name": f"regression:{cand['order_id']}",
                    "_stage": "regression",
                },
            )
            snapshot = build_synthetic_snapshot(chain_id)

            try:
                async with historical_anvil(chain_id, cand["block_number"]) as fork_rpc:
                    overrides = {chain_id: fork_rpc}
                    # Run challenger first
                    challenger_result = await self._benchmark_worker._benchmark_one_scenario_with_rpc(
                        image_tag=challenger.image_tag,
                        intent=app_def,
                        state=state,
                        snapshot=snapshot,
                        score_fn=score_fn,
                        rpc_overrides=overrides,
                    )
                    # Only run incumbent if challenger failed — saves one
                    # Docker startup per successful candidate
                    if challenger_result.score > 0:
                        logger.info(
                            "Stage 3 PASS: challenger succeeded on %s (score=%.3f)",
                            cand["order_id"], challenger_result.score,
                        )
                        continue

                    incumbent_result = await self._benchmark_worker._benchmark_one_scenario_with_rpc(
                        image_tag=incumbent_sub.image_tag,
                        intent=app_def,
                        state=state,
                        snapshot=snapshot,
                        score_fn=score_fn,
                        rpc_overrides=overrides,
                    )

                    if incumbent_result.score > 0:
                        regressions_detected += 1
                        logger.warning(
                            "Stage 3 REGRESSION: order=%s block=%d — "
                            "incumbent score=%.3f, challenger score=%.3f (FAIL)",
                            cand["order_id"], cand["block_number"],
                            incumbent_result.score, challenger_result.score,
                        )
                    else:
                        logger.info(
                            "Stage 3 NEUTRAL: order=%s — both solvers failed "
                            "(not a regression, likely market drift)",
                            cand["order_id"],
                        )
            except HistoricalForkError as exc:
                logger.warning(
                    "Stage 3 fork failed for order %s: %s — failing closed",
                    cand["order_id"], exc,
                )
                return False
            except Exception:
                logger.warning(
                    "Stage 3 unexpected error for order %s — failing closed",
                    cand["order_id"],
                    exc_info=True,
                )
                return False

        if regressions_detected > 0:
            logger.warning(
                "Stage 3: %d regression(s) detected — adoption blocked",
                regressions_detected,
            )
            return False

        logger.info(
            "Stage 3: %d candidate(s) tested, no regressions",
            len(candidates),
        )
        return True

    def _should_adopt(self, challenger: Submission) -> bool:
        """Check if the challenger should replace the current champion.

        Enforces:
        1. Global minimum score (MIN_CHAMPION_SCORE, default 0.5)
        2. Per-app minimum (PER_APP_MIN_SCORE, default 0.3)
        3. Per-app non-regression: no champion-covered app may be dropped, and
           no app the champion solves may drop more than MAX_APP_REGRESSION (10%)
        4. Global improvement over the champion by the dethrone margin (default 0.5%)
        """
        challenger_score = challenger.benchmark_score or 0
        champion_score = self._champion.benchmark_score or 0
        min_score = float(os.environ.get("MIN_CHAMPION_SCORE", "0.5"))
        per_app_min = float(os.environ.get("PER_APP_MIN_SCORE", "0.3"))
        max_regression = float(os.environ.get("MAX_APP_REGRESSION", "0.10"))

        # 1. Global minimum
        if challenger_score < min_score:
            logger.info(
                "Challenger %s global score %.3f below minimum %.3f",
                challenger.submission_id, challenger_score, min_score,
            )
            return False

        # Same submission — no change needed
        if challenger.submission_id == self._champion.submission_id:
            return False

        # On-chain co-ranked dethrone (opt-in). Default "current" falls through to the
        # JS logic below, byte-for-byte unchanged. ADOPT_RULE=p2oc ranks the dethrone on
        # the unfakeable on-chain OUTPUT surplus instead of the gas-polluted JS score.
        # MUST NOT be enabled live until the cross-machine determinism gate passes.
        if os.environ.get("ADOPT_RULE", "current").strip().lower() == "p2oc":
            return self._should_adopt_onchain(challenger)

        # Extract scorecards from benchmark details
        challenger_scorecard = self._get_scorecard(challenger)
        incumbent_scorecard = self._get_incumbent_scorecard()

        # 2. Per-app minimum — every app must be above floor
        if challenger_scorecard:
            for app_id, app_score in challenger_scorecard.get("app_scores", {}).items():
                if app_score < per_app_min:
                    logger.info(
                        "Challenger %s app %s score %.3f below per-app minimum %.3f",
                        challenger.submission_id, app_id, app_score, per_app_min,
                    )
                    return False

        # No current champion — adopt if above minimums
        if not self._champion.submission_id:
            return True

        # 3. Per-app non-regression. app_scores is keyed by bare app_id (see
        #    BenchmarkWorker._build_scorecard), so this compares true per-app
        #    quality, not per-scenario. A challenger may neither drop an app the
        #    champion covers nor regress > MAX_APP_REGRESSION on any app it solves.
        if challenger_scorecard and incumbent_scorecard:
            inc_apps = incumbent_scorecard.get("app_scores", {})
            ch_apps = challenger_scorecard.get("app_scores", {})
            for app_id, inc_score in inc_apps.items():
                ch_score = ch_apps.get(app_id)
                # (a) Dropping a champion-covered app is a hard regression.
                if ch_score is None:
                    logger.info(
                        "Challenger %s drops app %s that the champion covers",
                        challenger.submission_id, app_id,
                    )
                    return False
                # (b) A non-positive incumbent baseline gives no meaningful drop
                #     threshold; the real per-app floor arrives with the on-chain
                #     gate (design doc P2). Skip only the magnitude check here.
                if inc_score <= 0:
                    continue
                if ch_score < inc_score * (1 - max_regression):
                    logger.info(
                        "Challenger %s regresses on %s: %.3f → %.3f (max drop %.0f%%)",
                        challenger.submission_id, app_id, inc_score, ch_score,
                        max_regression * 100,
                    )
                    return False

        # 4. Global improvement over the champion's actual (freshly
        #    re-benchmarked) score by the dethrone margin. The absolute
        #    MIN_CHAMPION_SCORE floor is already enforced in step 1, so the
        #    baseline must NOT be floored at min_score here — flooring only
        #    over-protects a degraded (sub-floor) champion (design doc, §a #6).
        required = champion_score * (1 + self._dethrone_margin)
        if challenger_score <= champion_score:
            logger.info(
                "Challenger %s score %.3f not better than incumbent %.3f",
                challenger.submission_id, challenger_score, champion_score,
            )
            return False
        if challenger_score < required:
            logger.info(
                "Challenger %s score %.3f doesn't meet dethrone margin (need %.3f)",
                challenger.submission_id, challenger_score, required,
            )
            return False
        return True

    def _should_adopt_onchain(self, challenger: Submission) -> bool:
        """On-chain co-ranked dethrone (ADOPT_RULE=p2oc) — port of the lab's
        P2OcAdoptRule. Ranks the dethrone on the unfakeable on-chain OUTPUT surplus
        (Δ scoreIntent BPS / 10000 > dethrone margin) instead of the gas-polluted JS
        score, so a more-output-but-more-gas challenger (which the JS path rejects) is
        adoptable, while a gas-gaming challenger that delivers less is not. Keeps the
        vetoes: on-chain admission floor (ONCHAIN_FLOOR_BPS), app-coverage drop, and a
        JS no-catastrophic-regression guard (MAX_APP_REGRESSION). The shared preamble in
        _should_adopt (global-min + same-submission) already ran. Requires the
        on-chain plumbing (BenchmarkResult.on_chain_score -> scorecard.app_onchain).
        """
        if not self._champion.submission_id:
            return True  # no champion yet (genesis)

        champ_card = self._get_incumbent_scorecard() or {}
        chal_card = self._get_scorecard(challenger) or {}
        champ_apps = champ_card.get("app_scores", {})
        chal_apps = chal_card.get("app_scores", {})
        champ_oc = champ_card.get("app_onchain", {})
        chal_oc = chal_card.get("app_onchain", {})
        max_regression = float(os.environ.get("MAX_APP_REGRESSION", "0.10"))
        floor_env = os.environ.get("ONCHAIN_FLOOR_BPS", "").strip()
        floor = int(floor_env) if floor_env else None

        oc_surpluses: list[float] = []
        for app, inc in champ_apps.items():
            ch = chal_apps.get(app)
            # (veto 1) on-chain admission floor on the challenger's every scenario
            if floor is not None:
                all_pass, min_bps, n_missing = _onchain_pass(chal_oc.get(app, []), floor)
                if not all_pass:
                    logger.info("p2oc reject %s: on-chain floor fail (min=%s missing=%d)",
                                app, min_bps, n_missing)
                    return False
            # (veto 2) dropping a champion-covered app is a hard regression
            if ch is None:
                logger.info("p2oc reject %s: dropped by challenger", app)
                return False
            # rank input: on-chain output surplus (only when both means are present)
            co = _app_onchain_mean(champ_oc.get(app, []))
            cco = _app_onchain_mean(chal_oc.get(app, []))
            if co is not None and cco is not None:
                oc_surpluses.append(cco - co)
            elif co is not None and cco is None:
                logger.info("p2oc reject %s: challenger produced no on-chain score", app)
                return False
            # (veto 3) JS no-CATASTROPHIC-regression — a gas-blowup safety net only
            if inc > 0 and ch < inc * (1 - max_regression):
                logger.info("p2oc reject %s: JS regress %.3f->%.3f", app, inc, ch)
                return False

        # rank: mean per-app on-chain BPS surplus / 10000 must beat the dethrone margin
        net_bps = sum(oc_surpluses) / len(oc_surpluses) if oc_surpluses else 0.0
        net = net_bps / 10000.0
        if net <= self._dethrone_margin:
            logger.info("p2oc reject %s: net on-chain surplus %+.1f BPS <= margin %.4f",
                        challenger.submission_id, net_bps, self._dethrone_margin)
            return False
        logger.info("p2oc ADOPT %s: net on-chain surplus %+.1f BPS",
                    challenger.submission_id, net_bps)
        return True

    def _get_scorecard(self, submission: Submission) -> dict[str, Any] | None:
        """Extract the scorecard from a submission's benchmark details."""
        details = getattr(submission, "benchmark_details", None)
        if isinstance(details, dict):
            return details.get("scorecard")
        return None

    def _get_incumbent_scorecard(self) -> dict[str, Any] | None:
        """Get the scorecard from the current champion's submission."""
        if not self._champion.submission_id or not self._sub_store:
            return None
        sub = self._sub_store.get(self._champion.submission_id)
        if sub is None:
            return None
        return self._get_scorecard(sub)

    async def _hot_swap(
        self,
        submission: Submission,
        epoch: int,
        *,
        round_id: str | None = None,
    ) -> None:
        """Load the winning submission and swap it into the block loop.

        If a runtime builder is configured, it constructs the live solver object
        for BlockLoop. Otherwise, if orchestrator is available, starts a Docker
        session directly. If neither is configured, updates champion metadata
        only (solver stays the same).
        """
        new_runtime = None

        if self._runtime_builder is not None:
            new_runtime = self._runtime_builder(submission, epoch)
            if inspect.isawaitable(new_runtime):
                new_runtime = await new_runtime

        elif self._orchestrator and submission.image_tag:
            # Before swapping, verify the local image tag resolves to the
            # same sha256 image_id captured at Stage 3. A redeploy, a stale
            # local image, or a tag reassignment on this host would otherwise
            # cause the hot-swap to run different bytecode than what was
            # certified.
            if submission.image_id:
                local_id = await _resolve_image_id_via_docker(submission.image_tag)
                if local_id is None:
                    logger.error(
                        "Hot-swap aborted: cannot inspect local image %s",
                        submission.image_tag,
                    )
                    return
                if local_id.lower() != submission.image_id.lower():
                    logger.error(
                        "Hot-swap aborted: image_id mismatch for %s "
                        "(local=%s certified=%s)",
                        submission.image_tag, local_id, submission.image_id,
                    )
                    return

            # Shut down previous session if any
            if self._current_session is not None:
                try:
                    state_bytes = await self._current_session.serialize_state()
                    await self._current_session.shutdown()
                    logger.info("Previous solver session shut down, state saved")
                except Exception as exc:
                    logger.warning("Error shutting down previous session: %s", exc)
                    state_bytes = None
            else:
                state_bytes = None

            # Start new session
            new_session = await self._orchestrator.start_docker(
                submission.image_tag,
            )
            await new_session.initialize({"epoch": epoch})

            # Restore state from previous session if compatible
            if state_bytes and submission.solver_name == self._champion.solver_name:
                try:
                    await new_session.restore_state(state_bytes)
                    logger.info("Restored state to new solver session")
                except Exception:
                    logger.info("State restore skipped (incompatible solver)")

            self._current_session = new_session
            new_runtime = new_session

        # Update champion info
        adopted_at = time.time()
        self._champion = ChampionInfo(
            submission_id=submission.submission_id,
            solver_name=submission.solver_name,
            solver_version=submission.solver_version,
            benchmark_score=submission.benchmark_score or 0.0,
            epoch_adopted=epoch,
            image_tag=submission.image_tag,
            hotkey=submission.hotkey,
            adopted_at=adopted_at,
        )
        if self._sub_store is not None:
            self._sub_store.adopt(submission.submission_id)
        if self._round_store is not None:
            self._round_store.set_active_champion(
                ChampionSnapshot(
                    submission_id=submission.submission_id,
                    image_id=submission.image_id,
                    solver_name=submission.solver_name,
                    solver_version=submission.solver_version,
                    hotkey=submission.hotkey,
                    activated_round_id=round_id,
                    activated_epoch=epoch,
                    activated_at=adopted_at,
                ),
                sync_open_round=False,
            )

        # Hot-swap in block loop
        if self._block_loop and new_runtime is not None:
            self._block_loop.set_solver(new_runtime)

    def _build_weights_mapping(self, epoch: int, *, round_id: str | None = None) -> dict[str, float]:
        """Build a hotkey→weight mapping for emission policy.

        Before a real miner-backed champion exists, emits 100% to the subnet
        owner hotkey (burn behavior). Once a real miner champion exists, ranks
        scored submissions by benchmark_score descending, applies exponential
        decay (weight_decay^(rank-1)), and normalizes so all weights sum to 1.0.

        Returns:
            Dict mapping hotkey SS58 → normalized weight.
        """
        if not self._sub_store:
            return {}

        if not is_real_miner_hotkey(self._champion.hotkey):
            return build_bootstrap_or_champion_weights(
                self._champion.hotkey,
                owner_hotkey=self._owner_hotkey,
            )

        # Gather champion-eligible submissions from this epoch
        subs = (
            self._sub_store.list_by_round(round_id)
            if round_id is not None
            else self._sub_store.list_by_epoch(epoch)
        )
        scored = [s for s in self._eligible_candidates(subs) if s.hotkey]

        if not scored:
            return {}

        # Sort by score descending
        scored.sort(key=lambda s: s.benchmark_score or 0.0, reverse=True)

        # Apply exponential decay: weight = decay^(rank-1)
        raw_weights: dict[str, float] = {}
        for rank, sub in enumerate(scored):
            weight = self._weight_decay ** rank  # rank 0 = champion
            raw_weights[sub.hotkey] = weight

        # Normalize to sum=1
        total = sum(raw_weights.values())
        if total > 0:
            return {k: v / total for k, v in raw_weights.items()}
        return raw_weights

    async def _emit_weights(self, epoch: int, *, round_id: str | None = None) -> bool:
        """Queue weights for emission by POSTing to the validator daemon.

        The validator daemon (``minotaur_subnet.validator.main``) owns the
        only bt.Wallet on the host that can call ``subtensor.set_weights``.
        Rather than duplicate the wallet here (which would create a race
        condition between two chain set_weights callers competing for the
        same rate-limit slot), we hand it the per-miner mapping via a
        signed HTTP POST and let its ``_epoch_loop`` perform the actual
        emit on its next tick.

        Authentication: signed payload using ``VALIDATOR_PRIVATE_KEY``
        (same key the api signs EIP-712 consensus approvals with). The
        validator daemon derives the expected signer address from the
        SAME env at startup, so no shared secret needs configuring.

        Burn-fallback contract: if this method fails for any reason
        (validator unreachable, auth misconfig, body malformed), the
        validator daemon's burn fallback fires on its next tick and
        emits via ``ChampionWeights.maybe_emit``. The validator never
        depends on this method succeeding — burn is the unconditional
        safety net.

        Non-fatal: logs errors but does not raise.

        Returns:
            True if the validator accepted the queue POST (200 response).
            False on any error or non-200 response.
        """
        import json as _json
        import time as _time

        from minotaur_subnet.shared.internal_auth import sign_request

        attempt_ts = _time.time()

        # Short-circuit on empty mapping — no point making an HTTP call
        # the validator will just reject as a 400.
        try:
            mapping = self._build_weights_mapping(epoch, round_id=round_id)
        except Exception as exc:
            logger.error(
                "Weight mapping build failed for epoch %d (non-fatal): %s",
                epoch, exc,
            )
            self._last_emit_state = {
                "attempted_at": attempt_ts,
                "result": "error",
                "error": f"_build_weights_mapping raised: {str(exc)[:200]}",
                "uids_attempted": 0,
                "source": "epoch_manager",
            }
            return False

        if not mapping:
            logger.info("No valid weights available for emission in epoch %d", epoch)
            self._last_emit_state = {
                "attempted_at": attempt_ts,
                "result": "empty",
                "error": "no scored miners this epoch — nothing to queue",
                "uids_attempted": 0,
                "source": "epoch_manager",
            }
            return False

        private_key = os.environ.get("VALIDATOR_PRIVATE_KEY", "").strip()
        if not private_key:
            # Without a private key, internal-auth signing is impossible.
            # This is the same condition under which the validator
            # daemon refuses to register the queue endpoint (503), so we
            # short-circuit before making a doomed HTTP call.
            self._last_emit_state = {
                "attempted_at": attempt_ts,
                "result": "error",
                "error": "VALIDATOR_PRIVATE_KEY not set — cannot sign internal request",
                "uids_attempted": len(mapping),
                "source": "epoch_manager",
            }
            logger.warning(
                "Skipping queue POST for epoch %d: VALIDATOR_PRIVATE_KEY not set",
                epoch,
            )
            return False

        validator_url = os.environ.get("INTERNAL_VALIDATOR_URL", "http://validator:9100").rstrip("/")
        path = "/internal/weights/queue"
        body = _json.dumps({
            "mapping": mapping,
            "source": "epoch_manager",
            "epoch": epoch,
        }).encode("utf-8")

        ts, sig = sign_request(
            private_key, method="POST", path=path, body=body,
        )
        headers = {
            "Content-Type": "application/json",
            "X-Internal-Timestamp": str(ts),
            "X-Internal-Signature": sig,
        }

        logger.info(
            "Queueing weights for epoch %d: %d hotkeys → %s",
            epoch, len(mapping), validator_url + path,
        )

        try:
            import aiohttp
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10),
            ) as session:
                async with session.post(
                    validator_url + path,
                    data=body,
                    headers=headers,
                ) as resp:
                    resp_text = await resp.text()
                    if resp.status == 200:
                        logger.info(
                            "Validator accepted weight queue for epoch %d",
                            epoch,
                        )
                        self._last_emit_state = {
                            "attempted_at": attempt_ts,
                            "result": "queued",
                            "error": None,
                            "uids_attempted": len(mapping),
                            "source": "epoch_manager",
                        }
                        return True
                    else:
                        logger.warning(
                            "Validator rejected weight queue for epoch %d: %d %s",
                            epoch, resp.status, resp_text[:200],
                        )
                        self._last_emit_state = {
                            "attempted_at": attempt_ts,
                            "result": "error",
                            "error": f"validator returned {resp.status}: {resp_text[:200]}",
                            "uids_attempted": len(mapping),
                            "source": "epoch_manager",
                        }
                        return False
        except Exception as exc:
            logger.error(
                "Weight queue POST failed for epoch %d (non-fatal): %s",
                epoch, exc,
            )
            self._last_emit_state = {
                "attempted_at": attempt_ts,
                "result": "error",
                "error": str(exc)[:300],
                "uids_attempted": len(mapping),
                "source": "epoch_manager",
            }
            return False

    def _count_scored(self, epoch: int, *, round_id: str | None = None) -> int:
        """Count SCORED submissions for this epoch."""
        if not self._sub_store:
            return 0
        subs = (
            self._sub_store.list_by_round(round_id)
            if round_id is not None
            else self._sub_store.list_by_epoch(epoch)
        )
        return sum(1 for s in subs if s.status in (
            SubmissionStatus.SCORED,
            SubmissionStatus.ADOPTED,
        ))

    def _prepare_round(self, epoch: int) -> RoundState | None:
        """Freeze the current submission round for replay benchmarking."""
        if self._round_store is None:
            return None

        current = self._round_store.get_current_round()
        if current is None:
            current = self._round_store.ensure_open_round(
                opened_epoch=epoch,
                incumbent=self._get_incumbent_snapshot(),
            )
        elif current.status in (RoundStatus.ACTIVATED, RoundStatus.ABORTED):
            current = self._round_store.open_next_round(
                opened_epoch=epoch,
                incumbent=self._get_incumbent_snapshot(),
            )
        if current.status == RoundStatus.OPEN:
            current = self._round_store.close_current_round(close_epoch=epoch)
        if current.status == RoundStatus.CLOSED:
            current = self._round_store.set_round_status(
                current.round_id,
                RoundStatus.REPLAYING,
            )
        return current

    def _complete_round(
        self,
        round_state: RoundState | None,
        epoch: int,
        *,
        activated: bool,
        abort_reason: str | None = None,
    ) -> RoundState | None:
        """Complete the processed round and immediately reopen intake."""
        if self._round_store is None or round_state is None:
            return None

        if activated:
            self._round_store.activate_round(
                round_state.round_id,
                effective_epoch=epoch,
            )
        else:
            self._round_store.abort_round(
                round_state.round_id,
                abort_reason or "round_aborted",
            )

        return self._round_store.open_next_round(
            opened_epoch=epoch,
            incumbent=self._get_incumbent_snapshot(),
        )

    def _get_incumbent_snapshot(self) -> ChampionSnapshot | None:
        """Return the best available active champion snapshot for round sync."""
        if self._round_store is not None:
            snapshot = self._round_store.get_active_champion()
            if snapshot.submission_id:
                return snapshot

        if self._sub_store is not None:
            adopted = self._sub_store.get_champion()
            if adopted is not None:
                return ChampionSnapshot(
                    submission_id=adopted.submission_id,
                    image_id=adopted.image_id,
                    solver_name=adopted.solver_name,
                    solver_version=adopted.solver_version,
                    hotkey=adopted.hotkey,
                    activated_round_id=adopted.round_id or None,
                    activated_epoch=adopted.epoch,
                    activated_at=adopted.updated_at,
                )

        if not self._champion.submission_id:
            return None
        return ChampionSnapshot(
            submission_id=self._champion.submission_id,
            solver_name=self._champion.solver_name,
            solver_version=self._champion.solver_version,
            hotkey=self._champion.hotkey,
            activated_epoch=self._champion.epoch_adopted,
            activated_at=self._champion.adopted_at,
        )

    def _restore_active_champion_submission(self) -> Submission | None:
        """Restore the active champion submission from persisted stores."""
        if self._sub_store is None:
            return None

        adopted = self._sub_store.get_champion()
        if adopted is not None:
            return adopted

        if self._round_store is None:
            return None

        snapshot = self._round_store.get_active_champion()
        if not snapshot.submission_id:
            return None
        return self._sub_store.get(snapshot.submission_id)
