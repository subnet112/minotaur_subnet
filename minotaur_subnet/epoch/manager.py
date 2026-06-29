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
    GENESIS_EPOCH,
    GENESIS_HOTKEY,
    apply_champion_burn_ramp,
    build_bootstrap_or_champion_weights,
    get_subnet_owner_hotkey,
    is_real_miner_hotkey,
)

logger = logging.getLogger(__name__)

# Champion must beat the incumbent by this margin to be adopted. THE SINGLE
# SOURCE of the dethrone margin: every consumer imports DETHRONE_MARGIN from here
# (the manager passes it to adopt_rule; champion_consensus, benchmark_worker, and
# scoring_lab import it directly), so changing it here moves the bar everywhere —
# leader and followers stay on an identical rule.
# 0.01 == 1%. Champion and challenger are scored on the SAME round pack at the SAME
# pinned fork block, so per-pack difficulty is COMMON-MODE and cancels in the
# challenger-vs-champion comparison — the cross-round champion-score drift (~0.68–0.76)
# is pack difficulty, NOT comparison noise. The same-pack run-to-run comparison noise
# is ~0 (pinned fork #333; measured delta=0.0000 on the FIX-1 reference-vs-self shadow).
# So this margin is not guarding run-to-run noise (the earlier "5% above ~1% noise"
# rationale conflated absolute drift with comparison noise); it's a thin guard against
# per-pack SAMPLING (a challenger better on this pack's order mix but not overall),
# already damped by the ~62-scenario packs. Lowered so genuinely-close challengers
# (e.g. +3.9%, which 5% rejected) can win; the per-app non-regression vetoes still
# block solvers that are worse on any app. (History: 0.005 → 0.05 → 0.01.)
DETHRONE_MARGIN = 0.01


def _adoption_disabled() -> bool:
    """Safety gate: when ``DISABLE_CHAMPION_ADOPTION`` is set, submissions are
    scored normally (benchmark + scorecard + feedback report all run) but NO
    challenger is ever adopted as champion — the champion solver and the on-chain
    emission target stay put.

    Lets us run the real scoring pipeline on a live validator (e.g. to exercise
    the miner feedback report) without a test submission accidentally winning the
    champion slot and redirecting emissions. Default off (normal adoption). Read
    at call time so it can be flipped without a restart.
    """
    return os.environ.get("DISABLE_CHAMPION_ADOPTION", "").strip().lower() in (
        "1", "true", "yes", "on",
    )


def _follower_weight_adopt_enabled() -> bool:
    """DEFAULT ON: a FOLLOWER that INDEPENDENTLY re-benchmarked + verified a
    quorum-certified champion self-adopts it locally so its weight emitter emits the
    champion share instead of 100% burn-to-owner. Default ON because third-party
    validators won't set env vars themselves — shipping the code IS the enablement.
    Disable per-node with ``FOLLOWER_CHAMPION_WEIGHT_ADOPT`` in {0,false,no,off}. Read at
    call time so it can be toggled without a restart.

    Safety does NOT rest on this flag: a follower only ever weights a champion it
    ITSELF verified this round (``round_state.self_verified``, never blind-sign/builtin)
    that is a real-miner hotkey, and the leader is never affected (definite-leader
    guard). See ``activate_certified_round``.
    """
    raw = os.environ.get("FOLLOWER_CHAMPION_WEIGHT_ADOPT")
    if raw is None:
        return True
    return raw.strip().lower() not in ("0", "false", "no", "off")


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
        app_store: Any = None,
        orchestrator: Any = None,
        round_store: RoundStore | None = None,
        runtime_builder: Any = None,
        dethrone_margin: float = DETHRONE_MARGIN,
        weights_emitter: Any = None,
        weight_decay: float = 0.6,
        owner_hotkey: str | None = None,
        on_champion_adopted: Any = None,
        on_champion_rejected: Any = None,
        on_champion_finalist: Any = None,
        vote_recorder: Any = None,
    ) -> None:
        self._block_loop = block_loop
        self._benchmark_worker = benchmark_worker
        self._sub_store = submission_store
        # App/order store injected for app/order lookups (optional; may be None).
        self._app_store = app_store
        self._orchestrator = orchestrator
        self._round_store = round_store
        self._runtime_builder = runtime_builder
        self._dethrone_margin = dethrone_margin
        self._weights_emitter = weights_emitter
        self._weight_decay = weight_decay
        self._owner_hotkey = (owner_hotkey or "").strip() or get_subnet_owner_hotkey()
        # Chain-primary owner resolution: a wired chain source (MetagraphSync with
        # resolve_subnet_owner()) takes precedence over the env/constructor owner.
        self._owner_chain_source: Any = None
        self._resolved_owner: str = ""
        self._on_champion_adopted = on_champion_adopted
        self._on_champion_rejected = on_champion_rejected
        self._on_champion_finalist = on_champion_finalist
        # Leader gate for PR-mirroring side effects: only the configured leader
        # posts the reject report onto the miner's PR. None → ungated (tests /
        # not wired). Set via ``set_leader_check`` in startup.
        self._is_leader: Any = None
        # CHALLENGER_QUORUM_MODE observability: optional callback(dict) that publishes
        # this leader's would-be adopt vote for the fleet shadow tally. No decision effect.
        self._vote_recorder = vote_recorder

        self._champion = ChampionInfo()
        # Set True by _refresh_incumbent_score when an incumbent EXISTS but could
        # NOT be freshly re-benchmarked this round (unresolvable image / bad results
        # / benchmark error incl. RealSimulationUnavailable). _should_adopt ABSTAINS
        # when set, so the leader never decides adoption on a STALE champion bar —
        # mirroring the follower's conservative REJECT (fleet parity).
        self._incumbent_refresh_failed = False
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
        # Champion submission recovered at boot (or None). Used by
        # ensure_live_solver_matches_champion() to relaunch the live ORDER solver
        # onto the adopted champion after a restart — the restore below only
        # rebuilds champion METADATA, not the running solver.
        self._restored_champion_submission = restored
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
            reject_reason = getattr(self, "_last_adopt_reason", None) or "did not beat the champion"
            logger.info(
                "Challenger %s not adopted (relative per-order rule): %s",
                getattr(new_champion_sub, "submission_id", "?"), reject_reason,
            )
            self._notify_champion_rejected(new_champion_sub, reject_reason)
            next_round = self._complete_round(
                current_round,
                epoch,
                activated=False,
                abort_reason=reject_reason,
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

        # PROTOCOL: only the leader evaluates a closed round. evaluate_round transitions
        # the round (replay → certify/abort), and on a NON-leader — e.g. a third-party
        # validator that runs the coordinator (ENABLE_SOLVER_ROUND_COORDINATOR defaults
        # ON) but has no benchmark worker — _find_champion sees no scores, so it aborts
        # the round LOCALLY ("no_champion_candidate"). That diverges from the leader and
        # makes the whole fleet reject the leader's certification: every round dies
        # ROUND_WRONG_STATE → fleet-abort, the quorum never forms, and no champion can be
        # dethroned at quorum>1. Non-leaders instead FOLLOW the leader's synced outcome
        # (close/certify/abort via the /internal/* round sync) and re-benchmark reactively
        # to vote on the leader's proposal — so a non-leader must NOT mutate round status
        # here. _is_leader unset (local testnet / single-node / tests) → treat as leader,
        # preserving original behavior.
        _leader_check = getattr(self, "_is_leader", None)
        if _leader_check is not None and not _leader_check():
            return result  # status unchanged — defer to the leader's round sync

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

        # DISPLAY-ONLY: persist each competitor's SAME-PIN relative counts vs the
        # just-refreshed champion (champion@this-round-pin). The API report/round
        # response then READ these stored counts instead of recomputing them
        # cross-fork against the champion's latest (different-pin) record. Fully
        # best-effort — it must never affect the authoritative verdict below.
        self._persist_round_relative_counts(round_id)

        # Record the leader's would-be vote (observability), then proceed on the
        # PURE verdict. The DISABLE_CHAMPION_ADOPTION freeze is enforced at the
        # COMMIT boundary (activate_certified_round), NOT here — so under the freeze
        # the round still broadcasts + collects a would-be quorum (observe-only)
        # before the commit is blocked, letting the fleet's cross-host agreement be
        # measured without ever adopting.
        self._record_would_be_vote(finalist)
        if not self._meets_adoption_criteria(finalist):
            # Relative-rule reject reason (e.g. "reject: N regression(s)/drop(s)" /
            # "reject: no win (challenger only matched the champion)"), not the
            # obsolete saturated "dethrone_margin_not_met".
            reject_reason = getattr(self, "_last_adopt_reason", None) or "did not beat the champion"
            next_round = self._complete_round(
                round_state,
                epoch,
                activated=False,
                abort_reason=reject_reason,
            )
            # Mirror the reject onto the challenger's PR (comment + close + GC).
            self._notify_champion_rejected(finalist, reject_reason)
            result["status_after"] = RoundStatus.ABORTED.value
            result["abort_reason"] = reject_reason
            if next_round is not None:
                result["next_round_id"] = next_round.round_id
            return result

        # Private and public finalists follow the SAME adoption path. A private
        # finalist was certified by the quorum against its image digest (followers
        # pull-by-digest; they never need the private source), and the relayer lands
        # it on canonical main via publish_private_champion_when_certified. No
        # special-casing here — the dispatch on is_private happens at finalization.
        updated = self._round_store.set_round_finalist(
            round_id,
            submission_id=finalist.submission_id,
            image_id=finalist.image_id,
            benchmark_score=finalist.benchmark_score,
        )
        result["status_after"] = updated.status.value
        result["finalist_submission_id"] = updated.finalist_submission_id
        result["finalist_image_id"] = updated.finalist_image_id
        # Mirror the WIN onto the finalist's PR (full report, no close — the PR
        # stays open for the cert-gated merge).
        self._notify_champion_finalist(finalist, "selected as finalist")
        return result

    async def activate_certified_round(
        self, round_id: str, *, epoch: int, leader_champion_changed: bool | None = None,
    ) -> dict[str, Any]:
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
        # COMMIT-BOUNDARY FREEZE GATE (relocated from _should_adopt): the round was
        # certified by quorum — the FULL consensus ran observe-only (broadcast →
        # peers independently re-benchmarked + voted + signed) — but
        # DISABLE_CHAMPION_ADOPTION blocks the actual commit. Advance WITHOUT
        # changing the champion: no hot-swap, no weight emit, no on-chain attest.
        # The toggle now disables the ADOPTION ACTION, not the pipeline. Defense in
        # depth: _hot_swap also refuses under the freeze and the merge callback is
        # unwired, so this is one of three independent guards on the commit.
        if _adoption_disabled():
            logger.warning(
                "[no-adopt] round %s certified by quorum but DISABLE_CHAMPION_ADOPTION "
                "is set — NOT activating (no hot-swap / weights / on-chain attest); "
                "champion unchanged.", round_id,
            )
            next_round = self._complete_round(
                round_state, epoch, activated=False, abort_reason="adoption_frozen",
            )
            result["abort_reason"] = "adoption_frozen"
            if next_round is not None:
                result["next_round_id"] = next_round.round_id
            return result

        if epoch < effective_epoch:
            return result

        if self._sub_store is None:
            raise ValueError("submission_store is required for certified activation")
        submission = self._sub_store.get(certificate.candidate_submission_id)
        if submission is None:
            raise KeyError(
                f"Certified submission not found: {certificate.candidate_submission_id}",
            )

        # PROVENANCE GATE (runs BEFORE the hot-swap): notify the relayer to attest
        # the certificate on-chain (BT EVM ChampionRegistry, recording the validator
        # signatures + tx hash for the GitHub Action) and squash-merge the miner's
        # signed fork PR. The callback returns True only when BOTH the attestation
        # and the merge succeed. We capture that result up front to gate the adoption
        # before any champion change takes effect. With no merge callback wired (e.g.
        # a testnet without a solver repo), merge_ok stays True and the gate no-ops.
        merge_ok = True
        # Finalization (on-chain attest + squash-merge the miner's PR) is the
        # LEADER's job: it alone holds the solver-repo PAT and is the single
        # on-chain writer. A FOLLOWER must NOT re-attest or re-merge — it has no
        # PAT (the callback would fail → merge_ok False → it would wrongly REFUSE
        # to adopt and never earn the champion's weights) and duplicate writers
        # would race. The follower already INDEPENDENTLY verified this certificate
        # at certify time (every approval checked against the on-chain
        # ValidatorRegistry + EIP-712 in _certify_solver_round_state), so it adopts
        # the quorum-verified winner directly. Leadership is dynamic → check at call
        # time. _is_leader unset (local testnet / single-node / tests) → treat as
        # leader, preserving the original behavior.
        _leader_check = getattr(self, "_is_leader", None)
        _is_follower = _leader_check is not None and not _leader_check()
        if self._on_champion_adopted is not None and not _is_follower:
            try:
                cb_result = self._on_champion_adopted(
                    submission, round_id, certificate=certificate,
                )
                if inspect.isawaitable(cb_result):
                    cb_result = await cb_result
                merge_ok = bool(cb_result)
            except Exception as exc:
                logger.warning("on_champion_adopted callback failed: %s", exc)
                merge_ok = False
        elif _is_follower:
            logger.info(
                "[merge-gate] round %s: follower adopts quorum-certified champion %s "
                "on the verified certificate (leader owns attest + PR merge).",
                round_id, certificate.candidate_submission_id,
            )

        # A failed attest/merge ABORTS the adoption — UNCONDITIONALLY, by design (no
        # opt-out env var): no hot-swap, no weight emit, champion unchanged. A
        # challenger whose source can't be merged to main + attested on-chain (e.g. a
        # drifted PR head, or a missing on-chain proof) MUST NOT earn weights — its
        # provenance can't be established. The fleet still RUNS the certified image
        # digest at runtime, but a champion that can't be recorded is not adopted.
        if not merge_ok:
            logger.error(
                "[merge-gate] round %s certified, but on-chain attest + PR merge did "
                "NOT both succeed for %s — REFUSING to adopt (no hot-swap / weights); "
                "champion unchanged.",
                round_id, certificate.candidate_submission_id,
            )
            # Mirror the failure onto the miner's PR via the SAME reject-feedback
            # path used for benchmark rejections (_notify_champion_rejected →
            # on_champion_rejected_pr → PR comment): tell the miner WHY their win
            # couldn't be finalized so they can fix it (typically: reset the PR head
            # back to the certified commit). Leader-gated + best-effort internally.
            self._notify_champion_rejected(
                submission,
                "adoption blocked — this submission won the round, but the champion "
                "could not be finalized: its on-chain attestation and/or the "
                "squash-merge of this PR did not both succeed. The most common cause "
                "is the PR head being pushed PAST the certified commit, so the quorum "
                "certificate no longer binds the head SHA (do not push to the branch "
                "after submitting). The round was aborted and the champion is "
                "unchanged; re-submit with the PR head pinned to the certified commit.",
            )
            next_round = self._complete_round(
                round_state, epoch, activated=False, abort_reason="merge_failed",
            )
            result["abort_reason"] = "merge_failed"
            if next_round is not None:
                result["next_round_id"] = next_round.round_id
            return result

        # ── Follower champion-weight gate ────────────────────────────────────
        # The leader adopts unconditionally (it ran the merge callback / is the single
        # on-chain writer). A NON-leader only adopts + weights a champion it
        # INDEPENDENTLY re-benchmarked (round_state.self_verified) — gated by
        # FOLLOWER_CHAMPION_WEIGHT_ADOPT (DEFAULT ON; disable per-node with an
        # off-value). Safety rests on self_verified + real-hotkey, NOT on the flag.
        # Fails CLOSED: any node that is not a DEFINITE leader and isn't a gated,
        # self-verified follower advances WITHOUT changing the champion (stays
        # burn-to-owner). This also closes the ambiguous-leadership fall-through
        # (merge callback unwired + the leader-check defaulting to leader when the
        # metagraph is uninitialized), which would otherwise adopt unconditionally.
        _definite_leader = (
            _leader_check is None  # local testnet / single-node / tests
            or (self._on_champion_adopted is not None and not _is_follower)
        )
        if not _definite_leader:
            # Candidate-bound: the follower must have verified THE candidate being
            # certified (not merely "some candidate this round") — closes the
            # propose-A / certify-B gap at quorum>1.
            _self_verified = (
                getattr(round_state, "self_verified_submission_id", None) is not None
                and round_state.self_verified_submission_id
                == certificate.candidate_submission_id
            )
            _real = is_real_miner_hotkey(getattr(submission, "hotkey", "") or "")
            # The leader signals whether IT finalized (champion_changed). On an EXPLICIT
            # False (leader merge_failed / refused to finalize), the follower must NOT
            # weight a champion the leader rejected (no on-chain provenance) — refuse +
            # advance. None = absent field (old leader): preserve legacy adopt logic so a
            # new follower against an old leader is never stranded (mixed-version bridge).
            _leader_refused = leader_champion_changed is False
            if _leader_refused or not (_follower_weight_adopt_enabled() and _self_verified and _real):
                _reason = "leader_merge_failed" if _leader_refused else "follower_adopt_gated"
                logger.info(
                    "[follower-adopt] round %s: NOT self-adopting champion %s "
                    "(reason=%s opt_in=%s self_verified=%s real_hotkey=%s leader_changed=%s) "
                    "— champion/weights unchanged (burn-to-owner).",
                    round_id, certificate.candidate_submission_id, _reason,
                    _follower_weight_adopt_enabled(), _self_verified, _real,
                    leader_champion_changed,
                )
                # Rollback: if a PRIOR self-adopt left a real champion-of-record and we are
                # now NOT adopting (flag off / not verified / leader refused), durably
                # revert so the daemon burns rather than weighting a stale/rejected champion.
                self._reset_self_adopted_champion_to_burn()
                next_round = self._complete_round(
                    round_state, epoch, activated=False, abort_reason=_reason,
                )
                result["abort_reason"] = _reason
                if next_round is not None:
                    result["next_round_id"] = next_round.round_id
                return result
            logger.info(
                "[follower-adopt] round %s: self-adopting verified champion %s "
                "(FOLLOWER_CHAMPION_WEIGHT_ADOPT on) — emitting champion weights.",
                round_id, certificate.candidate_submission_id,
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
        return result

    def _reset_self_adopted_champion_to_burn(self) -> None:
        """Follower rollback: DURABLY revert a prior self-adopt back to 100% burn-to-owner.

        ⚠️ MUST only be called from the ``not _definite_leader`` branch of
        activate_certified_round — it has NO internal leader guard, so a stray caller
        could un-adopt a LEADER's legitimate champion. The champion has THREE resurrection
        sources that must all be closed or the next API touch / boot / same-call
        open_next_round silently brings it back: (1) round_store._active_champion (the
        daemon reads this), (2) the submission store's ADOPTED champion (the reconcile +
        boot + _get_incumbent_snapshot fallback re-derive from it), (3) the in-memory
        self._champion (the same-call _get_incumbent_snapshot / weights fallback). Clear
        all three. Best-effort; never raises into activation."""
        try:
            # (1) round-store active champion — immediate effect (daemon reads this).
            if self._round_store is not None:
                current = self._round_store.get_active_champion()
                if current is not None and is_real_miner_hotkey(getattr(current, "hotkey", "") or ""):
                    self._round_store.set_active_champion(ChampionSnapshot(), sync_open_round=False)
            # (2) DURABLE: un-adopt in the submission store so the route-driven reconcile,
            # boot restore, and the same-call open_next_round fallback cannot resurrect it.
            # Flip ADOPTED→SCORED — the exact pre-adopt state adopt() restores a displaced
            # champion to; @_write_locked + _persist => survives restart + visible to the daemon.
            if self._sub_store is not None:
                champ = self._sub_store.get_champion()
                if champ is not None and is_real_miner_hotkey(getattr(champ, "hotkey", "") or ""):
                    self._sub_store.update_status(champ.submission_id, SubmissionStatus.SCORED)
            # (3) in-memory ChampionInfo — closes the same-call _get_incumbent_snapshot /
            # _build_weights_mapping fallback that would otherwise re-seed the next round.
            self._champion = ChampionInfo()
            logger.info(
                "[follower-adopt] reverted self-adopted champion to burn-to-owner "
                "(round-store + submission-store + in-memory) — opted out / leader refused / "
                "no longer verified.",
            )
        except Exception as exc:  # best-effort — must never break activation
            # A swallowed failure here resurrects the champion (the original bug) — log LOUD.
            logger.error(
                "[follower-adopt] revert-to-burn FAILED — champion may resurrect: %s", exc,
            )

    def set_leader_check(self, is_leader: Any) -> None:
        """Wire the leader predicate (callable → bool). When set, only the leader
        mirrors the reject decision onto the PR."""
        self._is_leader = is_leader

    def _notify_champion_rejected(self, submission: Any, reason: str) -> None:
        """Best-effort fire the reject callback (PR comment + image GC; the PR is
        left OPEN — only a merge closes a PR). Sync —
        called from the round-evaluation path; the callback itself is sync GitHub
        API. No-op without a callback / a PR-based submission.

        Leader-gated: ``evaluate_round`` runs on every validator, so without this
        gate every node with a solver-repo token would post its own (possibly
        divergent) report. Only the configured leader mirrors it."""
        if self._on_champion_rejected is None:
            return
        # Defensive read: the reaper path can construct an EpochManager via __new__
        # (bypassing __init__ where _is_leader is set), so use getattr to avoid an
        # AttributeError that the reaper would swallow and silently skip the reject.
        _is_leader = getattr(self, "_is_leader", None)
        if _is_leader is not None and not _is_leader():
            return  # followers don't post — the leader is the single source
        if not getattr(submission, "pr_number", None):
            return
        # Pass champion context so the callback can render the full scored report
        # on the PR (your score vs the champion per case). Only forward kwargs the
        # callback accepts — mock/legacy callbacks take just (submission, reason).
        kwargs: dict[str, Any] = {}
        try:
            params = inspect.signature(self._on_champion_rejected).parameters
        except (TypeError, ValueError):
            params = {}
        if "champion_score" in params:
            kwargs["champion_score"] = self._champion.benchmark_score
        if "dethrone_margin" in params:
            kwargs["dethrone_margin"] = self._dethrone_margin
        # Private submissions: the report must post to the miner's PRIVATE repo,
        # which needs the per-submission token. Fetch it from the (same-process)
        # store; None for public submissions → the callback falls back to canonical.
        if "repo_token" in params:
            try:
                kwargs["repo_token"] = self._sub_store.get_repo_token(
                    getattr(submission, "submission_id", "") or "",
                )
            except Exception:  # noqa: BLE001
                pass
        # Fresh read so the report carries the relative counts persisted above
        # (the object handed in predates that merge → otherwise no relative block,
        # and the report falls back to the bare "see status endpoint" note).
        try:
            _sid = getattr(submission, "submission_id", "") or ""
            _store = getattr(self, "_sub_store", None)
            if _sid and _store is not None:
                _fresh = _store.get(_sid)
                # Only adopt a fresh object that is genuinely THIS submission — a
                # mock/loose store can return a non-matching object; keep the
                # original then (the reaper path passes a Mock store).
                if _fresh is not None and getattr(_fresh, "submission_id", None) == _sid:
                    submission = _fresh
        except Exception:
            pass
        try:
            self._on_champion_rejected(submission, reason, **kwargs)
        except Exception as exc:
            logger.warning("on_champion_rejected callback failed: %s", exc)

    def _notify_champion_finalist(self, submission: Any, reason: str) -> None:
        """WIN mirror of ``_notify_champion_rejected``: best-effort fire the
        finalist callback (PR comment only — posts the full scored report). NEVER
        closes the PR; the winner's PR must stay open for the cert-gated merge.

        Same leader gate as the reject path: ``evaluate_round`` runs on every
        validator, so without the gate every node with a solver-repo token would
        post its own (possibly divergent) report. Only the configured leader
        mirrors it. No-op without a callback / a PR-based submission."""
        if self._on_champion_finalist is None:
            return
        # Defensive read: the reaper path can construct an EpochManager via __new__
        # (bypassing __init__ where _is_leader is set), so use getattr to avoid an
        # AttributeError that the reaper would swallow and silently skip the win.
        _is_leader = getattr(self, "_is_leader", None)
        if _is_leader is not None and not _is_leader():
            return  # followers don't post — the leader is the single source
        if not getattr(submission, "pr_number", None):
            return
        # Pass champion context so the callback can render the full scored report
        # on the PR (your score vs the champion per case). Only forward kwargs the
        # callback accepts — mock/legacy callbacks take just (submission, reason).
        kwargs: dict[str, Any] = {}
        try:
            params = inspect.signature(self._on_champion_finalist).parameters
        except (TypeError, ValueError):
            params = {}
        if "champion_score" in params:
            kwargs["champion_score"] = self._champion.benchmark_score
        if "dethrone_margin" in params:
            kwargs["dethrone_margin"] = self._dethrone_margin
        # Private submissions: report must post to the miner's PRIVATE repo (needs
        # the per-submission token). None for public → callback uses canonical.
        if "repo_token" in params:
            try:
                kwargs["repo_token"] = self._sub_store.get_repo_token(
                    getattr(submission, "submission_id", "") or "",
                )
            except Exception:  # noqa: BLE001
                pass
        # Fresh read so the report carries the relative counts persisted above
        # (the object handed in predates that merge → otherwise no relative block,
        # and the report falls back to the bare "see status endpoint" note).
        try:
            _sid = getattr(submission, "submission_id", "") or ""
            _store = getattr(self, "_sub_store", None)
            if _sid and _store is not None:
                _fresh = _store.get(_sid)
                # Only adopt a fresh object that is genuinely THIS submission — a
                # mock/loose store can return a non-matching object; keep the
                # original then (the reaper path passes a Mock store).
                if _fresh is not None and getattr(_fresh, "submission_id", None) == _sid:
                    submission = _fresh
        except Exception:
            pass
        try:
            self._on_champion_finalist(submission, reason, **kwargs)
        except Exception as exc:
            logger.warning("on_champion_finalist callback failed: %s", exc)

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
        # Rank by benchmark_score descending. On a TRUE exact-float tie, break it
        # DETERMINISTICALLY by a content-addressed key (image digest, then the unique
        # submission_id) instead of the incidental input order. The previous implicit
        # break was the stable sort preserving list_by_round/_epoch order, which is the
        # submissions' LOCAL-clock created_at — so two validators (or a failed-over
        # leader) could order an exact tie differently and nominate a DIFFERENT finalist
        # for the same round, diverging consensus. Negating the score keeps the primary
        # ordering (highest first) while letting the tie-break sort ascending + stable
        # and host-independent.
        eligible.sort(
            key=lambda s: (
                -(s.benchmark_score or 0.0),
                str(s.image_id or ""),
                str(s.submission_id or ""),
            )
        )
        return eligible

    def _maybe_seed_genesis_incumbent(self) -> None:
        """Decision-time: when no champion is seeded, treat a SCORED genesis as the
        incumbent BAR (has_champion=True) so the FIRST real champion must BEAT
        genesis (>= genesis*(1+DETHRONE_MARGIN) + per-app floor + on-chain veto),
        matching the follower's _resolve_incumbent_submission.

        In-memory ONLY — never adopt()/_hot_swap()/set_active_champion() (those
        trigger snapshot persistence + the on-chain certify path). Resolved at
        DECISION time (not init) because genesis is scored mid-round. WEIGHT-SAFE:
        hotkey is copied verbatim (==GENESIS_HOTKEY) so is_real_miner_hotkey stays
        False and _build_weights_mapping still burns 100% to the owner — identical
        to the empty-champion case.
        """
        if self._champion.submission_id:  # real/adopted/restored incumbent — keep it
            return
        if self._sub_store is None:
            return
        genesis = self._sub_store.get_by_hotkey_epoch(GENESIS_HOTKEY, GENESIS_EPOCH)
        if genesis is None or genesis.status not in (
            SubmissionStatus.SCORED,
            SubmissionStatus.ADOPTED,
        ):
            return
        if (genesis.benchmark_score or 0.0) <= 0:  # no usable bar yet -> stay bootstrap
            return
        assert genesis.hotkey == GENESIS_HOTKEY, "genesis incumbent must keep the burn hotkey"
        self._champion = ChampionInfo(
            submission_id=genesis.submission_id,
            solver_name=genesis.solver_name,
            solver_version=genesis.solver_version,
            benchmark_score=genesis.benchmark_score or 0.0,
            epoch_adopted=genesis.epoch,
            image_tag=genesis.image_tag,  # None for genesis -> re-bench resolves the genesis image
            hotkey=GENESIS_HOTKEY,  # keeps weights on the burn branch
            adopted_at=genesis.updated_at,
        )
        logger.info(
            "Seeded genesis as the adoption incumbent bar: %s score=%.4f (weights still burn)",
            genesis.submission_id, genesis.benchmark_score or 0.0,
        )

    async def _refresh_incumbent_score(self) -> None:
        """Re-benchmark the current champion with the latest scenarios.

        When the app's JS scoring code is updated (e.g. new benchmark
        scenarios added), the incumbent's stored score becomes stale —
        it was computed under different conditions. Re-benchmarking
        ensures challenger vs incumbent comparisons are fair.

        The genesis champion (no submission image) is re-benchmarked via the
        configured genesis solver image so the bar stays current on each round's
        pack (issue #177). The score is left unchanged only when no benchmark
        worker — or no genesis image — is available.
        """
        # Genesis-as-bar (#242): seed a SCORED genesis as the incumbent before the
        # refresh, so both adoption paths (on_epoch_boundary + evaluate_round) and
        # the follower agree the first champion must BEAT genesis.
        self._maybe_seed_genesis_incumbent()
        # Stale-bar guard: assume the incumbent score is fresh this round unless a
        # production re-benchmark path below fails (then _should_adopt abstains).
        self._incumbent_refresh_failed = False
        if not self._champion.submission_id:
            return
        if not self._benchmark_worker:
            return

        # Find the incumbent's submission to get its image_tag
        incumbent_sub = None
        if self._sub_store:
            incumbent_sub = self._sub_store.get(self._champion.submission_id)
        if incumbent_sub is None:
            # Incumbent exists but its submission can't be resolved (e.g. a stale
            # cross-process store reload) → can't re-benchmark the bar → STALE.
            self._incumbent_refresh_failed = True
            return

        # Prefer the PULLABLE pushed manifest digest (repo@sha256:…) over the local
        # {{.Id}} screening tag. The `solver-<sha>:screening` tag is host-local and
        # built only during screening — it gets pruned over time, after which the
        # per-round incumbent re-benchmark crashes ("Unable to find image … locally,
        # pull access denied") → STALE bar → the leader abstains and NO challenger can
        # EVER dethrone the champion (an equal-or-better solver is silently rejected).
        # The image_digest is content-addressed and pullable on any host, so docker
        # re-fetches the (identical) image and the bar stays current. Falls back to the
        # local tag for genesis/older champions that never recorded a digest.
        image_tag = getattr(incumbent_sub, "image_digest", None) or incumbent_sub.image_tag
        if not image_tag:
            # Genesis/builtin champion: no submission image. Re-benchmark it via
            # the configured genesis solver image so the champion BAR is current
            # on THIS round's pack — otherwise the stale stored score makes the
            # contest uncontestable (issue #177). _resolve_champion_image returns
            # the genesis image for a genesis champion, else None.
            if callable(getattr(self._benchmark_worker, "_resolve_champion_image", None)):
                image_tag = self._benchmark_worker._resolve_champion_image()
            if not image_tag:
                # Incumbent image unresolvable → cannot re-benchmark the bar → STALE.
                self._incumbent_refresh_failed = True
                return  # leave the stored score; _should_adopt will abstain

        logger.info(
            "Re-benchmarking incumbent %s (%s) with current scenarios",
            self._champion.submission_id, image_tag,
        )

        try:
            # SYMMETRY FIX: score the incumbent through the IDENTICAL challenger path
            # (_score_one_image) that run_once uses for challengers — same round-anchored
            # fork-pin, same intents corpus, same champion reference anchor, same
            # _benchmark_submission. The OLD incumbent path produced an inflated bar
            # (king re-scored ~0.72 where king's OWN image scores ~0.47 as a challenger,
            # empirically proven via the diagnostic endpoint), making the champion
            # unbeatable by an equal solver. Notably the old path never applied
            # _apply_round_anchored_pin; the shared path does. Routing both sides through
            # ONE code path removes the asymmetry by construction.
            #
            # Mock-worker guard (tests): a real/AsyncMock worker exposes an awaitable
            # _score_one_image; a plain MagicMock worker doesn't (iscoroutinefunction
            # is False) → skip without a stale flag, matching the old test-compat path.
            _score_one = getattr(self._benchmark_worker, "_score_one_image", None)
            if not inspect.iscoroutinefunction(_score_one):
                return
            diag = await _score_one(image_tag, context="incumbent")
            if not isinstance(diag, dict) or not isinstance(diag.get("score"), (int, float)):
                return  # degenerate result — test-compat, no stale flag

            fresh_score = float(diag["score"])
            details = diag.get("details")
            old_score = self._champion.benchmark_score
            self._champion.benchmark_score = fresh_score

            # Display-path + adoption consistency (#FIX): ALWAYS persist this round's
            # FRESH re-bench (score + per_intent, incl. shadow_score) back to the
            # champion's submission record. The relative adoption rule
            # (_evaluate_per_order_adoption) and the same-pin display persist
            # (_persist_round_relative_counts, which reads the champion's STORED
            # per_intent) BOTH join against the champion's STORED per_intent;
            # persisting the same-round re-bench guarantees they compare the
            # challenger against the SAME same-round/same-fork reference the adoption
            # used — instead of a stale bench from a different round/fork (which
            # produced transient wrong counts on the frontend). Persist whenever we
            # have fresh details so a JS-unchanged round still refreshes the rows.
            if self._sub_store and details is not None:
                self._sub_store.set_benchmark_result(
                    incumbent_sub.submission_id,
                    score=fresh_score,
                    details=details,
                )

            logger.info(
                "Incumbent re-scored via challenger path (symmetric bar): %s %.4f → %.4f "
                "(%d scenarios)",
                self._champion.submission_id, old_score, fresh_score,
                diag.get("intent_count", 0),
            )
        except Exception:
            # Benchmark error (incl. RealSimulationUnavailable) → bar is stale →
            # _should_adopt abstains rather than deciding on the prior score.
            self._incumbent_refresh_failed = True
            logger.warning(
                "Failed to re-benchmark incumbent %s — STALE bar, will abstain (was %.4f)",
                self._champion.submission_id, self._champion.benchmark_score,
                exc_info=True,
            )

    def _record_would_be_vote(self, challenger: Submission) -> None:
        """Publish this leader's INDEPENDENT would-be adopt vote (observability).

        Computed via the AUTHORITATIVE relative per-order rule
        (:func:`evaluate_relative_adoption`) — the IDENTICAL rule the followers
        publish (``champion_consensus._independent_adopt_vote``) and the leader's
        own live decision (:meth:`_meets_adoption_criteria`) — so the leader's
        published vote now MATCHES the followers' (both relative), not the old
        saturated quote-anchored number. Recorded REGARDLESS of
        DISABLE_CHAMPION_ADOPTION so the fleet quorum can be observed with adoption
        OFF. Best-effort and side-effect-free — never affects the live decision.

        OBSERVABILITY ONLY: the recorded vote flows solely to
        ``ctx.last_independent_vote`` -> the ``/health`` ``independent_vote`` field.
        It is NEVER signed into the quorum certificate nor consumed by the consensus
        decision (the signed follower vote is the RETURN value of
        ``_independent_adopt_vote``; the leader's real verdict is
        ``_meets_adoption_criteria``).

        Gated by the fleet-uniform observability default (CHALLENGER_QUORUM_MODE,
        DEFAULT ON; break-glass {0,false,no,off}).
        """
        from minotaur_subnet.harness.benchmark_worker import _challenger_quorum_mode

        if not _challenger_quorum_mode():
            return
        try:
            from minotaur_subnet.epoch.relative_scoring import (
                evaluate_relative_adoption,
            )

            incumbent_sub = (
                self._sub_store.get(self._champion.submission_id)
                if (self._sub_store and self._champion.submission_id)
                else None
            )
            champ_rows = self._per_intent(incumbent_sub)
            chal_rows = self._per_intent(challenger)
            verdict = evaluate_relative_adoption(champ_rows, chal_rows)
            adopt = bool(verdict["adopt"])
            vote = {
                "candidate_id": getattr(challenger, "submission_id", None),
                "role": "leader",
                "vote": "ADOPT" if adopt else "REJECT",
                "n_wins": verdict["n_wins"],
                "n_regressions": verdict["n_regressions"],
                "n_blind_spots": verdict["n_blind_spots"],
                "n_matched": verdict["n_matched"],
                "scenarios_compared": verdict["scenarios_compared"],
                "reason": verdict["reason"],
            }
            logger.info(
                "[independent-vote] role=leader candidate=%s vote=%s wins=%d "
                "regressions=%d blind_spots=%d matched=%d compared=%d: %s",
                vote["candidate_id"], vote["vote"], verdict["n_wins"],
                verdict["n_regressions"], verdict["n_blind_spots"],
                verdict["n_matched"], verdict["scenarios_compared"], verdict["reason"],
            )
            if self._vote_recorder is not None:
                self._vote_recorder(vote)
        except Exception as exc:  # observe-only — must never break adoption
            logger.warning("[independent-vote] leader vote record failed (ignored): %s", exc)

    def _should_adopt(self, challenger: Submission) -> bool:
        """Check if the challenger should replace the current champion.

        Delegates the verdict to :meth:`_meets_adoption_criteria` — the SOLE
        adoption rule is the relative per-order rule
        (:func:`evaluate_relative_adoption`): the challenger must beat-or-match the
        freshly re-benched champion on EVERY order's RAW delivered output and
        strictly win at least one. Adds the synchronous-path
        ``DISABLE_CHAMPION_ADOPTION`` freeze (that path commits immediately).
        """
        # Observability (CHALLENGER_QUORUM_MODE): publish this leader's would-be vote
        # BEFORE the disable gate so the shadow tally sees it with adoption off.
        self._record_would_be_vote(challenger)

        if _adoption_disabled():
            self._last_adopt_reason = "adoption disabled (DISABLE_CHAMPION_ADOPTION)"
            logger.warning(
                "[no-adopt] DISABLE_CHAMPION_ADOPTION is set — %s scored but NOT "
                "adopted; champion unchanged. Unset the flag to resume adoption.",
                getattr(challenger, "submission_id", "?"),
            )
            return False

        return self._meets_adoption_criteria(challenger)

    def _meets_adoption_criteria(self, challenger: Submission) -> bool:
        """The PURE adoption verdict — the relative per-order rule is the SOLE
        decision: the challenger must beat-or-match the freshly re-benched champion
        on EVERY order's RAW delivered output and strictly win at least one
        (``evaluate_relative_adoption``). This is the IDENTICAL rule the followers
        run (``champion_consensus._independent_adopt_vote``), so the leader and the
        fleet decide alike.

        Does NOT consult ``DISABLE_CHAMPION_ADOPTION``: the freeze is enforced at the
        COMMIT boundary (``activate_certified_round``), so the consensus pipeline can
        broadcast + collect a would-be quorum observe-only under the freeze and the
        fleet's cross-host agreement can be measured without ever adopting.

        The synchronous standalone path (``process_epoch``) uses ``_should_adopt``
        instead, which keeps the freeze check because it commits immediately.
        """
        # Record the human reason for the verdict (relative vocabulary) so the
        # round-abort label + PR-reject message reflect WHY (no challenger delivered
        # more / N regressions), not the obsolete "dethrone_margin_not_met".
        self._last_adopt_reason = None

        # Same submission — no change needed
        if challenger.submission_id == self._champion.submission_id:
            self._last_adopt_reason = "same submission as champion"
            return False

        # Fail-closed stale-bar guard: if an incumbent EXISTS but could not be
        # freshly re-benchmarked this round (_refresh_incumbent_score hit an
        # unresolvable-image / bad-results / benchmark-error path), the champion bar
        # is STALE — ABSTAIN rather than decide adoption on an outdated per-order
        # set. This mirrors the follower's conservative REJECT (champion_consensus),
        # so the leader and fleet never diverge on a stale bar. (No incumbent =>
        # not stale, bootstrap proceeds.)
        # getattr default False: a manager built via __new__ (tests) or never run
        # through a refresh has not had a failed refresh -> not stale.
        if self._champion.submission_id and getattr(self, "_incumbent_refresh_failed", False):
            self._last_adopt_reason = "stale incumbent bar (re-benchmark failed)"
            logger.warning(
                "[abstain] incumbent %s could not be freshly re-benchmarked this "
                "round — abstaining (refusing to adopt %s against a stale bar)",
                self._champion.submission_id,
                getattr(challenger, "submission_id", "?"),
            )
            return False

        # The relative per-order rule is the SOLE adoption decision. On any error
        # (no comparable per-order data) abstain — never adopt on uncertainty.
        verdict = self._evaluate_per_order_adoption(challenger)
        if verdict is None:
            self._last_adopt_reason = "no comparable per-order data"
            logger.warning(
                "adoption decision for %s: ABSTAIN (relative per-order verdict "
                "unavailable — no comparable per-order data)",
                getattr(challenger, "submission_id", "?"),
            )
            return False
        adopt = bool(verdict["adopt"])
        self._last_adopt_reason = verdict["reason"]
        logger.info(
            "adoption decision for %s: adopt=%s (relative per-order: %s)",
            getattr(challenger, "submission_id", "?"), adopt, verdict["reason"],
        )
        return adopt

    @staticmethod
    def _per_intent(submission: Submission | None) -> list[dict[str, Any]]:
        """Per-order benchmark rows (with ``shadow_score``) from a submission's
        ``benchmark_details``. Empty list when absent — the relative rule then
        sees no orders and abstains (adopt=False, scenarios_compared=0)."""
        details = getattr(submission, "benchmark_details", None) or {}
        rows = details.get("per_intent") if isinstance(details, dict) else None
        return rows if isinstance(rows, list) else []

    def _evaluate_per_order_adoption(
        self, challenger: Submission,
    ) -> dict[str, Any] | None:
        """Relative per-order adoption verdict — the SOLE adoption decision.

        Joins the freshly re-benched incumbent's and the challenger's per-order RAW
        delivered outputs (``benchmark_details.per_intent[*].shadow_score``, sourced
        from the LIVE raw-output scorer's ``metadata.raw_output``) via the pure
        :func:`evaluate_relative_adoption`, logs the verdict, and publishes it on
        ``/health`` as ``per_order_adoption_vote``. Returns the verdict dict, or
        ``None`` on error so the caller ABSTAINS (never adopts on uncertainty).

        Relies on ``_refresh_incumbent_score`` having persisted the champion's FRESH
        same-round per_intent back to its submission record (display-path fix), so
        ``champ_rows`` is the same-round reference the challenger was scored against.
        """
        try:
            from minotaur_subnet.epoch.relative_scoring import (
                evaluate_relative_adoption,
            )

            incumbent_sub = (
                self._sub_store.get(self._champion.submission_id)
                if (self._sub_store and self._champion.submission_id)
                else None
            )
            champ_rows = self._per_intent(incumbent_sub)
            chal_rows = self._per_intent(challenger)
            verdict = evaluate_relative_adoption(champ_rows, chal_rows)

            logger.info(
                "[per-order-adoption] challenger=%s verdict=%s wins=%d regressions=%d "
                "blind_spots=%d matched=%d compared=%d: %s",
                getattr(challenger, "submission_id", "?"),
                "ADOPT" if verdict["adopt"] else "REJECT",
                verdict["n_wins"], verdict["n_regressions"],
                verdict["n_blind_spots"], verdict["n_matched"],
                verdict["scenarios_compared"], verdict["reason"],
            )

            vote = {
                "candidate_id": getattr(challenger, "submission_id", None),
                "vote": "ADOPT" if verdict["adopt"] else "REJECT",
                "n_wins": verdict["n_wins"],
                "n_regressions": verdict["n_regressions"],
                "n_blind_spots": verdict["n_blind_spots"],
                "n_matched": verdict["n_matched"],
                "scenarios_compared": verdict["scenarios_compared"],
                "reason": verdict["reason"],
                "per_order": verdict["per_order"],
            }
            try:
                from minotaur_subnet.api.server_context import ctx
                ctx.last_per_order_adoption_vote = dict(vote)
            except Exception:  # publishing must never break adoption
                pass
            return verdict
        except Exception as exc:  # must never crash the decision — abstain instead
            logger.warning("[per-order-adoption] failed (ignored): %s", exc)
            return None

    def _persist_round_relative_counts(self, round_id: str) -> None:
        """DISPLAY-ONLY: persist same-pin relative counts for each competitor.

        Call AFTER :meth:`_refresh_incumbent_score` (which re-benches the champion
        at THIS round's pin and persists its fresh ``per_intent``). For every
        competitor benched this round at the SAME pin, compute its relative counts
        vs that same-pin champion ``per_intent`` and persist them onto the
        competitor's ``benchmark_details["relative"]`` (tagged with ``round_id``),
        so the API surfaces correct same-pin counts instead of recomputing them
        cross-fork against the champion's latest (later, different-pin) record.

        This reads the SAME stored champion rows the authoritative
        :meth:`_evaluate_per_order_adoption` reads, so the displayed counts agree
        with the live verdict by construction. Fully best-effort: a competitor /
        champion lacking ``shadow_score`` rows is skipped (no block → the report
        shows pending), and any failure is swallowed — a display computation must
        never break round evaluation.
        """
        try:
            from minotaur_subnet.epoch.relative_scoring import (
                has_shadow_rows,
                relative_counts,
            )

            if self._sub_store is None or not self._champion.submission_id:
                return
            champ_rows = self._per_intent(self._sub_store.get(self._champion.submission_id))
            if not has_shadow_rows(champ_rows):
                return
            for competitor in self._sub_store.list_by_round(round_id):
                if competitor.submission_id == self._champion.submission_id:
                    continue
                comp_rows = self._per_intent(competitor)
                if not has_shadow_rows(comp_rows):
                    continue
                try:
                    counts = relative_counts(champ_rows, comp_rows)
                    counts["round_id"] = round_id
                    self._sub_store.merge_benchmark_details(
                        competitor.submission_id, {"relative": counts},
                    )
                except Exception:
                    logger.debug(
                        "relative-counts persist failed for %s",
                        getattr(competitor, "submission_id", "?"),
                        exc_info=True,
                    )
        except Exception:
            logger.debug("relative-counts persist pass failed for round %s", round_id, exc_info=True)

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
        force: bool = False,
        capture_previous: bool = True,
    ) -> None:
        """Load the winning submission and swap it into the block loop.

        If a runtime builder is configured, it constructs the live solver object
        for BlockLoop. Otherwise, if orchestrator is available, starts a Docker
        session directly. If neither is configured, updates champion metadata
        only (solver stays the same).

        ``force`` bypasses the DISABLE_CHAMPION_ADOPTION gate — used only by the
        emergency revert path (rolling back to an already-vetted prior champion
        is always safe). ``capture_previous`` records the displaced champion as
        the rollback target; the revert path sets it False (an undo isn't a new
        adoption and must not overwrite its own target).
        """
        # Belt-and-suspenders: even if some path reached activation, never swap
        # the live champion while adoption is disabled — unless this is a forced
        # revert to a previously-vetted champion.
        if _adoption_disabled() and not force:
            logger.warning(
                "[no-adopt] DISABLE_CHAMPION_ADOPTION is set — refusing hot-swap to "
                "%s; live champion solver unchanged.",
                getattr(submission, "submission_id", "?"),
            )
            return
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
            # Record the champion we're displacing as the one-step rollback
            # target — but only on a genuine change (not a restore / re-adopt of
            # the same submission), and not when this swap is itself a revert.
            if capture_previous:
                outgoing = self._round_store.get_active_champion()
                if outgoing.submission_id and outgoing.submission_id != submission.submission_id:
                    self._round_store.set_previous_champion(outgoing)
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

    async def ensure_live_solver_matches_champion(self) -> bool:
        """Boot-restore the live ORDER solver onto the currently-adopted champion.

        ``__init__`` (via ``_restore_active_champion_submission``) recovers the
        champion METADATA after a restart, but the block loop boots its live
        solver from the genesis / ``FORCE_SOLVER_IMAGE`` image. Without this the
        running solver silently stays a NON-champion image (e.g. a stale
        ``:latest``) while ``/solver/champion`` and weights report the adopted
        champion — exactly the split that made a real multi-hop order revert
        (the boot solver emitted outdated SwapRouter calldata). This relaunches
        the live solver to match the champion-of-record.

        Reuses the runtime builder, which pulls the portable ``<repo>@sha256:D``
        champion digest — content-addressed, so it also works on a fresh node /
        new leader that never benchmarked the champion locally. It is a
        deliberate no-op (builder returns ``None``) under ``FORCE_SOLVER_IMAGE``
        or when hot-swap is disabled, preserving the operator break-glass pin.

        FAILS LOUD: if the champion runtime cannot be built it logs ERROR rather
        than silently leaving a non-champion solver serving orders. Returns True
        iff the live solver was swapped onto the champion.
        """
        if self._runtime_builder is None or self._block_loop is None:
            return False
        sub = self._restored_champion_submission
        if sub is None or not is_real_miner_hotkey(sub.hotkey):
            # No persisted real champion → the genesis / forced boot solver is
            # the correct live solver; nothing to restore.
            return False
        epoch = self._champion.epoch_adopted or sub.epoch
        try:
            new_runtime = self._runtime_builder(sub, epoch)
            if inspect.isawaitable(new_runtime):
                new_runtime = await new_runtime
        except Exception:
            logger.error(
                "[boot-restore] FAILED to build the live solver for adopted champion "
                "%s — the live ORDER solver remains the boot (genesis/forced) image and "
                "may serve orders on a NON-champion solver until the next adoption. "
                "Stopgap: set FORCE_SOLVER_IMAGE to the certified champion digest.",
                sub.submission_id, exc_info=True,
            )
            return False
        if new_runtime is None:
            # Builder declined: FORCE_SOLVER_IMAGE active or hot-swap disabled —
            # an intentional no-op (the operator pin / policy keeps the boot solver).
            logger.info(
                "[boot-restore] champion %s NOT swapped into the live solver "
                "(FORCE_SOLVER_IMAGE / hot-swap-disabled) — boot solver kept.",
                sub.submission_id,
            )
            return False
        self._current_session = new_runtime
        self._block_loop.set_solver(new_runtime)
        logger.info(
            "[boot-restore] live ORDER solver relaunched onto adopted champion "
            "%s (%s v%s) after restart",
            sub.submission_id, sub.solver_name, sub.solver_version,
        )
        return True

    async def revert_to_previous_champion(
        self,
        *,
        epoch: int | None = None,
        reason: str = "",
    ) -> dict[str, Any]:
        """Emergency rollback: swap the live champion back to the PREVIOUS one.

        A one-step undo of the most recent adoption — restores the champion that
        was active immediately before the current one (NOT genesis). Intended as
        a kill switch when a freshly-adopted champion misbehaves: set
        ``DISABLE_CHAMPION_ADOPTION=1`` first to stop it being re-adopted, then
        revert. The swap is forced (bypasses the adoption gate — reverting to an
        already-vetted prior champion is always safe) and skips the
        dethrone-margin / scoring path entirely.

        Updates the live block-loop solver and the active-champion snapshot, so
        the next weight emission routes to the restored champion. Leaves the
        rollback target unchanged (reverting again is a no-op).

        Raises ``ValueError`` if there is no previous champion recorded, it's
        already active, or its submission can't be resolved from the store.
        """
        if self._round_store is None:
            raise ValueError("revert unavailable: no round store configured")
        previous = self._round_store.get_previous_champion()
        if not previous.submission_id:
            raise ValueError("no previous champion recorded to revert to")
        if previous.submission_id == self._champion.submission_id:
            raise ValueError(
                f"previous champion {previous.submission_id} is already active "
                "— nothing to revert"
            )
        submission = (
            self._sub_store.get(previous.submission_id)
            if self._sub_store is not None
            else None
        )
        if submission is None:
            raise ValueError(
                f"previous champion submission {previous.submission_id} not found in store"
            )

        from_submission_id = self._champion.submission_id
        target_epoch = epoch if epoch is not None else self._current_epoch
        logger.warning(
            "[revert] rolling champion back %s -> %s (epoch=%d): %s",
            from_submission_id or "<none>",
            previous.submission_id,
            target_epoch,
            reason or "manual revert",
        )
        await self._hot_swap(
            submission,
            target_epoch,
            round_id=previous.activated_round_id,
            force=True,
            capture_previous=False,
        )
        return {
            "reverted": True,
            "from_submission_id": from_submission_id,
            "to_submission_id": previous.submission_id,
            "to_image_tag": submission.image_tag,
            "epoch": target_epoch,
            "reason": reason or "manual revert",
        }

    def set_owner_chain_source(self, source: Any) -> None:
        """Wire a chain source (a MetagraphSync with resolve_subnet_owner()) so the
        burn-target owner is resolved CHAIN-PRIMARY instead of env-only."""
        self._owner_chain_source = source

    def _resolve_owner_hotkey(self) -> str:
        if self._resolved_owner:
            return self._resolved_owner
        owner = ""
        src = self._owner_chain_source
        if src is not None and hasattr(src, "resolve_subnet_owner"):
            try:
                owner = src.resolve_subnet_owner()
            except Exception as exc:
                logger.warning("Owner chain resolution failed (%s); using env owner", exc)
                owner = ""
        if not owner:
            owner = self._owner_hotkey  # env/constructor fallback
        if owner:
            self._resolved_owner = owner  # cache a real value
        return owner

    def _build_weights_mapping(self, epoch: int, *, round_id: str | None = None) -> dict[str, float]:
        """Build a hotkey→weight mapping for emission policy.

        WINNER-TAKES-ALL, champion-only: 100% burn to the subnet owner before a
        real miner-backed champion exists; once one does, the champion gets
        ``CHAMPION_MINER_WEIGHT_FRACTION`` (0.05, the FLOOR) and 0.95 burns to the
        owner. The validator daemon then scales this aggregate miner share ABOVE
        the floor by trailing-24h order volume at emission time (see
        ``_scale_emission_by_order_volume``); the mapping built here is the
        conservative floor and the volume ramp is applied at the single emit
        chokepoint that owns the order store.

        Only ``self._champion`` — the submission that won AND was finalized
        (merge-gate passed → ``_hot_swap`` set it as the live champion) — is ever
        weighted. There is NO score-ranked decay tail across other scored
        submissions: a runner-up, and in particular a candidate whose merge
        FAILED (it never becomes ``self._champion`` — the gate aborts before
        ``_hot_swap``), can never earn weight. The champion is always THIS
        validator's own locally-adopted one — never copied from chain — so a
        third party can't free-ride without doing the benchmark work itself.

        Returns:
            Dict mapping hotkey SS58 → normalized weight.
        """
        if not self._sub_store:
            return {}

        return build_bootstrap_or_champion_weights(
            self._champion.hotkey,
            owner_hotkey=self._resolve_owner_hotkey(),
        )

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

        # #227: reap submissions still BENCHMARKING for this now-terminal round.
        # The benchmark worker only processes the current-open or replay round, so
        # once this round leaves CLOSED/REPLAYING they would be stranded in
        # BENCHMARKING forever with no signal (e.g. non-finalist challengers when
        # the round activates on the first finalist). Fail them with a clear reason
        # and fire the reject callback so the miner knows to resubmit.
        self._reap_orphaned_benchmarking(round_state.round_id)

        return self._round_store.open_next_round(
            opened_epoch=epoch,
            incumbent=self._get_incumbent_snapshot(),
        )

    def _reap_orphaned_benchmarking(self, round_id: str) -> None:
        """Reject submissions left BENCHMARKING after their round terminated (#227)."""
        if self._sub_store is None or not round_id:
            return
        try:
            subs = self._sub_store.list_by_round(round_id)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("Reaper: list_by_round(%s) failed: %s", round_id, exc)
            return
        for sub in subs:
            if sub.status != SubmissionStatus.BENCHMARKING:
                continue
            try:
                self._sub_store.reject(
                    sub.submission_id,
                    "benchmark_window_elapsed: round closed before scoring — "
                    "resubmit to a fresh open round",
                )
                self._notify_champion_rejected(
                    sub, "benchmark window elapsed before scoring",
                )
                logger.info(
                    "Reaped orphaned BENCHMARKING submission %s (round %s terminal)",
                    sub.submission_id, round_id,
                )
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("Reaper: failed to reject %s: %s", sub.submission_id, exc)

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
