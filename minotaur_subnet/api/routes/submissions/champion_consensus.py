"""Champion adoption logic.

Handles:
- Champion proposal building
- Peer approval collection and verification
- Round certification via consensus
- Preparing rounds for certification
- Reactive benchmarking for peer verification
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import HTTPException

from minotaur_subnet.harness.round_store import (
    ChampionApproval,
    ChampionCertificate,
    RoundStatus,
    RoundState,
)

from .models import (
    CertifyRoundRequest,
    ChampionApprovalPayload,
)
from .state import (
    get_champion_consensus_manager,
    get_champion_peer_network,
    get_epoch_manager,
    get_round_store,
    get_store,
)
from .round_manager import (
    _current_solver_round_epoch,
    _round_certification_deadline_elapsed,
)

logger = logging.getLogger(__name__)


async def _resolve_local_image_id(image_tag: str) -> str | None:
    """Return the sha256 image_id of a local Docker image tag, or None.

    Used by peers to verify that the image they're about to benchmark is
    byte-for-byte the same one the leader certified — a tag alone is a
    mutable local reference.
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


async def _reactive_benchmark_candidate(
    candidate: Any,
    leader_score: float,
    tolerance_pct: float = 0.15,
    round_id: str | None = None,
) -> tuple[bool, float]:
    """Independently benchmark a champion candidate to verify the leader's score.

    Peers call this when they receive a champion proposal. It mirrors how
    order consensus peers re-simulate plans before signing — the peer runs
    the same benchmark pipeline (Stage 1 synthetic + Stage 2 historical)
    and checks that the score is within tolerance.

    Args:
        candidate: Submission object (has image_tag, commit_hash, repo_url).
        leader_score: The score the leader claims for this candidate.
        tolerance_pct: Maximum allowed relative difference (default 15%).
        round_id: Current round ID — used to deterministically sample
            the same historical orders the leader used for Stage 2.

    Returns:
        (verified, local_score) — verified is True if within tolerance.
    """
    from minotaur_subnet.api.server_context import ctx
    from minotaur_subnet.harness.orchestrator import (
        BenchmarkConfig,
        BenchmarkResult,
        RealSimulationUnavailable,
        SolverOrchestrator,
        run_benchmark,
    )
    from minotaur_subnet.harness.benchmark_worker import BenchmarkWorker

    image_tag = candidate.image_tag
    if not image_tag:
        logger.warning(
            "Candidate %s has no image_tag — cannot benchmark reactively",
            candidate.submission_id,
        )
        return False, 0.0

    # Before burning CPU, make sure the image tag on this peer resolves to
    # the same sha256 image_id the leader built. Tags are local refs; two
    # hosts can end up with different bytecode under the same tag.
    expected_image_id = (candidate.image_id or "").strip()
    if expected_image_id:
        local_image_id = await _resolve_local_image_id(image_tag)
        if local_image_id is None:
            logger.warning(
                "Reactive benchmark: cannot resolve local image_id for %s — "
                "refusing to benchmark",
                image_tag,
            )
            return False, 0.0
        if local_image_id.lower() != expected_image_id.lower():
            logger.warning(
                "Reactive benchmark image_id mismatch for %s: local=%s expected=%s "
                "— refusing to benchmark",
                candidate.submission_id, local_image_id, expected_image_id,
            )
            return False, 0.0

    # Build a temporary BenchmarkWorker to reuse intent loading and scoring
    app_store = ctx.store
    # Get the Anvil simulator for real simulation (not mock).
    # Mock simulation results are zeroed by _compute_avg_score, so without
    # a real simulator the reactive benchmark always scores 0.0.
    from minotaur_subnet.api.routes import apps as _apps_module
    simulator = getattr(_apps_module, "_simulator", None)

    worker = BenchmarkWorker(
        submission_store=get_store(),  # SubmissionStore (needed by constructor)
        app_store=app_store,
        use_docker=True,
        simulator=simulator,
    )

    # Input parity with the leader. Prefer the ROUND-ANCHORED pin: the follower
    # derives the SAME canonical fork block from the round's anchor (Option b —
    # no trust in the leader's number) and re-verifies at it, so on-chain scores
    # reproduce. Falls back to the BENCHMARK_EPOCH_BLOCK env path when the gate is
    # off (unset env -> live head, unchanged). Without parity a follower would
    # re-verify at its own live head — exactly the divergence the band papers over.
    _round_pin = None
    if round_id:
        try:
            from minotaur_subnet.api.startup import (
                _resolve_round_fork_pins,
                _round_anchor_chains,
            )
            _pins = _resolve_round_fork_pins(round_id)
            if _pins:
                _round_pin = _pins.get(_round_anchor_chains()[0])
        except Exception as exc:
            logger.warning("fork-pins: follower resolve failed for %s: %s", round_id, exc)
    if _round_pin is not None:
        worker.set_epoch_block(int(_round_pin))
    else:
        worker._apply_epoch_block_pin()

    intents = worker._load_benchmark_intents()
    if not intents:
        logger.warning("No active intents for reactive benchmark")
        return False, 0.0

    score_fn = await worker._build_score_fn(intents)
    intents = worker._enrich_intents_with_manifests(intents)

    # Stage 2: historical scenarios deterministic from round_id.
    # Peers must sample the same set as the leader or scores will diverge.
    if round_id:
        try:
            historical = worker._load_historical_scenarios(round_id)
            if historical:
                intents.extend(historical)
                logger.info(
                    "Reactive benchmark: added %d historical scenarios for round %s",
                    len(historical), round_id,
                )
        except Exception as exc:
            logger.warning("Reactive historical sampling failed: %s", exc)

    # Run the Docker benchmark. Honor the same fail-closed switch as the leader
    # (BENCHMARK_REQUIRE_REAL_SIM) so a follower never re-verifies a candidate on
    # fabricated mock data while the leader fail-closes it — that asymmetry would
    # silently diverge consensus.
    import os
    _require_real_sim = (
        os.environ.get("BENCHMARK_REQUIRE_REAL_SIM", "").strip().lower()
        in ("1", "true", "yes", "on")
    )
    orch = SolverOrchestrator()
    session = await orch.start_docker(image_tag)
    try:
        results = await run_benchmark(
            session,
            intents,
            config=BenchmarkConfig(
                chain_ids=list({s.chain_id for _, s, _ in intents} or {1}),
            ),
            score_fn=score_fn,
            simulator=simulator,
            require_real_sim=_require_real_sim,
            fork_block=worker._epoch_block_number,
        )
    except RealSimulationUnavailable:
        logger.error(
            "Reactive verify for %s requires a real simulator but none is "
            "available — failing closed (verify=fail).",
            candidate.submission_id,
        )
        return False, 0.0
    finally:
        await session.shutdown()

    # Compute average score (same logic as BenchmarkWorker._compute_avg_score)
    local_score = worker._compute_avg_score(results)

    logger.info(
        "Reactive benchmark for %s: local_score=%.4f leader_score=%.4f",
        candidate.submission_id, local_score, leader_score,
    )

    # Determinism-comparable signals: the JS local_score alone can't be diffed
    # against the leader's on-chain-ranked (p2oc / SHADOW_DETERMINISM) numbers.
    # Log the per-app on-chain scoreIntent means so operators can grep-compare
    # leader vs follower for the same candidate + pinned block across the fleet.
    try:
        card = worker._build_scorecard(results)
        oc_means: dict[str, float | None] = {}
        for app, scores in card.app_onchain.items():
            present = [s for s in scores if s is not None]
            oc_means[app] = round(sum(present) / len(present), 1) if present else None
        logger.info(
            "[reactive-determinism] candidate=%s round=%s fork_block=%s "
            "local_score=%.4f app_onchain_means=%s",
            candidate.submission_id, round_id, worker._epoch_block_number,
            local_score, oc_means,
        )
    except Exception as exc:  # observe-only — must never break verification
        logger.warning("[reactive-determinism] logging failed (ignored): %s", exc)

    # Shadow phase (ROUND_ANCHOR_SHADOW): when the real gate is off, derive + log
    # the fork pins this follower WOULD use, so operators can diff the leader's
    # and every follower's '[round-anchor-shadow]' lines to confirm fleet-wide pin
    # parity before flipping ROUND_ANCHORED_PIN. Observe-only, no consensus effect.
    if round_id:
        try:
            from minotaur_subnet.api.startup import _maybe_shadow_log_round_fork_pins
            _maybe_shadow_log_round_fork_pins(ctx, round_id, role="follower")
        except Exception as exc:  # observe-only — must never break verification
            logger.warning("[round-anchor-shadow] follower logging failed (ignored): %s", exc)

    if leader_score <= 0:
        # Leader claims zero — accept if we also scored zero
        return local_score <= 0, local_score

    relative_diff = abs(local_score - leader_score) / max(leader_score, 0.01)
    verified = relative_diff <= tolerance_pct
    if not verified:
        logger.warning(
            "Reactive benchmark REJECTED %s: relative_diff=%.2f%% > tolerance=%.2f%%",
            candidate.submission_id,
            relative_diff * 100,
            tolerance_pct * 100,
        )
    return verified, local_score


async def _maybe_prepare_round_for_certification(
    round_id: str,
    *,
    close_epoch: int | None = None,
    benchmark_pack_hash: str | None = None,
    committee_block: int | None = None,
    committee_hash: str | None = None,
    quorum_required: int | None = None,
    decision_deadline_epoch: int | None = None,
    effective_epoch: int | None = None,
    candidate_submission_id: str | None = None,
) -> RoundState:
    """Close/evaluate a round on demand so peers can verify the same tuple.

    When candidate_submission_id is provided (from the certify API), skip
    the full evaluate_round flow and directly transition to CERTIFYING with
    the specified candidate. evaluate_round runs _find_champion/_should_adopt
    which can abort the round if the candidate doesn't beat the incumbent by
    the dethrone margin — but that check belongs in the coordinator's automated
    flow, not in explicit certification requests.
    """
    round_store = get_round_store()
    round_state = round_store.get_round(round_id)
    if round_state is None:
        raise HTTPException(status_code=404, detail="Solver round not found")

    if round_state.status == RoundStatus.OPEN and close_epoch is not None:
        current = round_store.get_current_round()
        if current is not None and current.round_id == round_id:
            round_state = round_store.close_current_round(
                close_epoch=close_epoch,
                benchmark_pack_hash=benchmark_pack_hash,
                committee_block=committee_block,
                committee_hash=committee_hash,
                quorum_required=quorum_required,
                decision_deadline_epoch=decision_deadline_epoch,
                effective_epoch=effective_epoch,
            )

    if round_state.status in (RoundStatus.CLOSED, RoundStatus.REPLAYING):
        if candidate_submission_id:
            # Explicit candidate — skip evaluate_round and go straight to
            # CERTIFYING. The caller already knows which submission to certify.
            candidate = get_store().get(candidate_submission_id)
            if candidate is not None:
                round_state = round_store.set_round_finalist(
                    round_id,
                    submission_id=candidate.submission_id,
                    image_id=candidate.image_id,
                    benchmark_score=candidate.benchmark_score,
                )
                logger.info(
                    "Round %s → CERTIFYING with explicit candidate %s (score=%.4f)",
                    round_id, candidate.submission_id,
                    candidate.benchmark_score or 0,
                )
            else:
                logger.warning(
                    "Explicit candidate %s not found, falling back to evaluate",
                    candidate_submission_id,
                )
                # Fall through to evaluate_round below

        if round_state.status in (RoundStatus.CLOSED, RoundStatus.REPLAYING):
            # No explicit candidate or candidate not found — run full evaluate
            manager = get_epoch_manager()
            if manager is not None:
                try:
                    await manager.evaluate_round(
                        round_id,
                        epoch=(
                            round_state.close_epoch
                            if round_state.close_epoch is not None
                            else round_state.opened_epoch
                        ),
                    )
                    refreshed = round_store.get_round(round_id)
                    if refreshed is not None:
                        round_state = refreshed
                except Exception:
                    logger.warning(
                        "Failed to prepare round %s for champion certification",
                        round_id,
                        exc_info=True,
                    )

    return round_state


def _build_champion_proposal_for_round(
    round_state: RoundState,
    *,
    candidate_submission_id: str | None = None,
    candidate_image_id: str | None = None,
    committee_hash: str | None = None,
    benchmark_pack_hash: str | None = None,
    shadow_case_log_hash: str | None = None,
    effective_epoch: int | None = None,
    commit_hash_override: str | None = None,
    nonce_override: int | None = None,
    deadline_override: int | None = None,
) -> tuple[Any, Any, int]:
    """Resolve the frozen round tuple into a signable ChampionProposal.

    Peers receive the leader's commit_hash/nonce/deadline in the proposal
    payload and pass them via *_override so their digest matches the leader's
    signature exactly. The leader calls without overrides; values are then
    minted (nonce = ms-since-epoch, deadline = now + hour)."""
    from minotaur_subnet.consensus.champion_manager import ChampionProposal

    store = get_store()
    consensus_manager = get_champion_consensus_manager()

    resolved_submission_id = candidate_submission_id or round_state.finalist_submission_id
    if not resolved_submission_id:
        raise HTTPException(status_code=400, detail="No finalist candidate selected")
    candidate = store.get(resolved_submission_id)
    if candidate is None:
        raise HTTPException(status_code=404, detail="Certified submission not found")

    resolved_image_id = candidate_image_id or candidate.image_id or round_state.finalist_image_id
    if not resolved_image_id:
        # Genesis/subprocess submissions don't have Docker image_ids.
        # Use a placeholder so they can still be certified as bootstrap champions.
        if candidate.hotkey == "__genesis__" or candidate.repo_url.startswith("builtin://"):
            resolved_image_id = f"builtin:{candidate.commit_hash or 'genesis'}"
        else:
            raise HTTPException(status_code=400, detail="Certified candidate is missing image_id")

    resolved_committee_hash = committee_hash or round_state.committee_hash
    if not resolved_committee_hash and consensus_manager is not None:
        resolved_committee_hash = consensus_manager.committee_hash
    if not resolved_committee_hash:
        raise HTTPException(status_code=400, detail="Round is missing committee_hash")

    resolved_benchmark_pack_hash = benchmark_pack_hash or round_state.benchmark_pack_hash
    if not resolved_benchmark_pack_hash:
        raise HTTPException(status_code=400, detail="Round is missing benchmark_pack_hash")

    resolved_effective_epoch = (
        effective_epoch
        if effective_epoch is not None
        else round_state.effective_epoch
    )
    if resolved_effective_epoch is None:
        raise HTTPException(status_code=400, detail="Round is missing effective_epoch")

    resolved_quorum = (
        round_state.quorum_required
        or (consensus_manager.quorum_required if consensus_manager is not None else 0)
    )

    # v2 digest fields: commit_hash binds the git SHA, nonce/deadline are
    # replay protection. Nonce uses millisecond wall-clock — monotonic in
    # practice across champion proposals from a given leader, and enforced
    # strictly-greater on-chain per-signer. Deadline is 1 hour from now.
    # Peers pass *_override from the leader's payload so digests match.
    import time as _time
    from minotaur_subnet.consensus.champion_manager import (
        CHAMPION_APPROVAL_DEADLINE_SECONDS,
    )
    if nonce_override is not None:
        nonce = int(nonce_override)
    else:
        nonce = int(_time.time() * 1000)
    if deadline_override is not None:
        deadline = int(deadline_override)
    else:
        deadline = int(_time.time()) + CHAMPION_APPROVAL_DEADLINE_SECONDS
    if commit_hash_override is not None:
        resolved_commit_hash: str | None = commit_hash_override or None
    else:
        resolved_commit_hash = candidate.commit_hash or None

    proposal = ChampionProposal(
        round_id=round_state.round_id,
        committee_hash=resolved_committee_hash,
        incumbent_image_id=round_state.incumbent_image_id,
        candidate_submission_id=resolved_submission_id,
        candidate_image_id=resolved_image_id,
        benchmark_pack_hash=resolved_benchmark_pack_hash,
        shadow_case_log_hash=shadow_case_log_hash or round_state.shadow_case_log_hash,
        effective_epoch=int(resolved_effective_epoch),
        commit_hash=resolved_commit_hash,
        nonce=nonce,
        deadline=deadline,
    )
    return proposal, candidate, int(resolved_quorum or 0)


def _build_champion_approval_from_payload(
    approval: ChampionApprovalPayload,
    *,
    proposal: Any,
) -> ChampionApproval:
    """Expand a partial approval payload using the frozen proposal tuple."""
    return ChampionApproval(
        validator_id=approval.validator_id,
        round_id=proposal.round_id,
        committee_hash=approval.committee_hash or proposal.committee_hash,
        incumbent_image_id=approval.incumbent_image_id or proposal.incumbent_image_id,
        candidate_submission_id=(
            approval.candidate_submission_id or proposal.candidate_submission_id
        ),
        candidate_image_id=approval.candidate_image_id or proposal.candidate_image_id,
        benchmark_pack_hash=approval.benchmark_pack_hash or proposal.benchmark_pack_hash,
        shadow_case_log_hash=approval.shadow_case_log_hash or proposal.shadow_case_log_hash,
        effective_epoch=approval.effective_epoch or proposal.effective_epoch,
        # v2 signed fields — fall back to proposal values so an older peer
        # that doesn't include them still produces a verifiable digest.
        commit_hash=approval.commit_hash or proposal.commit_hash,
        nonce=int(approval.nonce or proposal.nonce or 0),
        deadline=int(approval.deadline or proposal.deadline or 0),
        timestamp=approval.timestamp,
        signature=approval.signature,
    )


async def _certify_solver_round_state(body: CertifyRoundRequest) -> RoundState:
    """Internal helper to certify a round without HTTP context."""
    round_store = get_round_store()
    round_state = await _maybe_prepare_round_for_certification(
        body.round_id,
        candidate_submission_id=body.candidate_submission_id,
    )
    if round_state.status not in (RoundStatus.CERTIFYING, RoundStatus.CERTIFIED):
        raise HTTPException(
            status_code=409,
            detail=(
                f"Round {body.round_id} is {round_state.status.value}; "
                "expected certifying"
            ),
        )
    if _round_certification_deadline_elapsed(round_state):
        raise HTTPException(
            status_code=409,
            detail=(
                f"Round {body.round_id} exceeded certification deadline "
                f"{round_state.decision_deadline_epoch}"
            ),
        )

    proposal, _candidate, default_quorum = _build_champion_proposal_for_round(
        round_state,
        candidate_submission_id=body.candidate_submission_id,
        candidate_image_id=body.candidate_image_id,
        committee_hash=body.committee_hash,
        benchmark_pack_hash=body.benchmark_pack_hash,
        shadow_case_log_hash=body.shadow_case_log_hash,
        effective_epoch=body.effective_epoch,
    )
    quorum_required = body.quorum_required or default_quorum
    consensus_manager = get_champion_consensus_manager()
    if round_state.quorum_required not in (None, 0, quorum_required):
        raise HTTPException(
            status_code=409,
            detail=(
                f"Round quorum {round_state.quorum_required} does not match "
                f"requested quorum {quorum_required}"
            ),
        )

    if body.approvals:
        approvals = [
            _build_champion_approval_from_payload(approval, proposal=proposal)
            for approval in body.approvals
        ]
        if consensus_manager is not None:
            invalid = [
                approval.validator_id
                for approval in approvals
                if not consensus_manager.verify_approval(approval, proposal)
            ]
            if invalid:
                raise HTTPException(
                    status_code=409,
                    detail=f"Invalid champion approvals from validators: {sorted(invalid)}",
                )
        if quorum_required > 0 and len(approvals) < quorum_required:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Round {body.round_id} requires {quorum_required} approvals; "
                    f"received {len(approvals)}"
                ),
            )
        certificate = ChampionCertificate(
            round_id=proposal.round_id,
            committee_hash=proposal.committee_hash,
            candidate_submission_id=proposal.candidate_submission_id,
            candidate_image_id=proposal.candidate_image_id,
            incumbent_image_id=proposal.incumbent_image_id,
            benchmark_pack_hash=proposal.benchmark_pack_hash,
            shadow_case_log_hash=proposal.shadow_case_log_hash,
            effective_epoch=proposal.effective_epoch,
            quorum_required=quorum_required,
            approvals=approvals,
        )
    else:
        if consensus_manager is None:
            raise HTTPException(
                status_code=503,
                detail=(
                    "Champion consensus not configured; supply approvals manually "
                    "or configure VALIDATOR_PRIVATE_KEY plus VALIDATOR_REGISTRY_<chain> "
                    "and CHAMPION_REGISTRY_<chain> so peer discovery can run"
                ),
            )
        if body.quorum_required not in (None, 0, consensus_manager.quorum_required):
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Requested quorum {body.quorum_required} does not match "
                    f"configured validator quorum {consensus_manager.quorum_required}"
                ),
            )
        if (
            round_state.quorum_required is not None
            and round_state.quorum_required != consensus_manager.quorum_required
        ):
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Round quorum {round_state.quorum_required} does not match "
                    f"configured validator quorum {consensus_manager.quorum_required}"
                ),
            )
        peer_network = get_champion_peer_network()
        broadcast_task = None
        if peer_network is not None:
            broadcast_task = asyncio.create_task(
                peer_network.broadcast_champion_proposal(
                    proposal,
                    collector=consensus_manager,
                    close_epoch=round_state.close_epoch,
                    quorum_required=consensus_manager.quorum_required,
                    decision_deadline_epoch=round_state.decision_deadline_epoch,
                    committee_block=round_state.committee_block,
                )
            )
        result = await consensus_manager.propose(proposal)
        if broadcast_task is not None and not broadcast_task.done():
            broadcast_task.cancel()
            try:
                await broadcast_task
            except asyncio.CancelledError:
                pass
        if not result.reached or result.certificate is None:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Champion quorum not reached for round {body.round_id}: "
                    f"{result.collected}/{result.quorum}"
                ),
            )
        certificate = result.certificate

    return round_store.certify_round(body.round_id, certificate)


async def _sync_certified_round_state(body: CertifyRoundRequest) -> RoundState:
    """Apply a leader-broadcast certificate to the local round store idempotently."""
    round_store = get_round_store()
    existing = round_store.get_round(body.round_id)
    if existing is not None and existing.status in (RoundStatus.CERTIFIED, RoundStatus.ACTIVATED):
        return existing
    return await _certify_solver_round_state(body)
