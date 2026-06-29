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
    _adopt_leader_round_if_behind,
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


async def _pull_image_by_digest(digest_ref: str) -> bool:
    """Pull a ``<repo>@sha256:D`` image. Returns True on success.

    Pulling BY DIGEST is self-verifying: the Docker daemon refuses a manifest
    whose computed digest != D, so a successful pull guarantees the follower runs
    byte-identical bytes to what the leader built and signed — this is what makes
    cross-host content-addressed benchmarking sound (no ``{{.Id}}`` divergence).
    """
    import asyncio as _asyncio
    try:
        proc = await _asyncio.create_subprocess_exec(
            "docker", "pull", digest_ref,
            stdout=_asyncio.subprocess.PIPE,
            stderr=_asyncio.subprocess.STDOUT,
        )
        out, _ = await _asyncio.wait_for(proc.communicate(), timeout=600)
    except _asyncio.TimeoutError:
        logger.warning("docker pull timed out for %s", digest_ref)
        return False
    except FileNotFoundError:
        logger.warning("docker not found while pulling %s", digest_ref)
        return False
    if proc.returncode != 0:
        logger.warning(
            "docker pull %s failed: %s",
            digest_ref, out.decode("utf-8", errors="replace")[:300],
        )
        return False
    return True


async def _reactive_benchmark_candidate(
    candidate: Any,
    leader_score: float,
    tolerance_pct: float = 0.15,
    round_id: str | None = None,
    candidate_image_id: str | None = None,
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
        require_real_sim_default,
        run_benchmark,
    )
    from minotaur_subnet.harness.benchmark_worker import BenchmarkWorker

    # Resolve the image this follower will run. Two modes, decided by the SHAPE of
    # the leader-proposed candidate_image_id (which the whole quorum signed):
    #
    #   digest mode  (bare 64-hex D): reconstruct <candidate_repo>@sha256:D and
    #     `docker pull` it. Pull-by-digest is self-verifying — the daemon rejects a
    #     manifest whose digest != D — so the follower runs byte-identical bytes to
    #     what the leader built. This REPLACES the broken cross-host {{.Id}} compare
    #     (two hosts that rebuild from source get different {{.Id}}s → false reject).
    #
    #   legacy mode  (sha256:<id> / builtin / unset): keep the local {{.Id}} compare
    #     against the leader's image_id — the candidate must already be built locally.
    from minotaur_subnet.harness.image_transport import (
        candidate_repo,
        is_bare_digest,
        make_digest_ref,
    )

    if is_bare_digest(candidate_image_id):
        run_image = make_digest_ref(candidate_repo(), candidate_image_id)
        if not run_image:
            logger.warning(
                "Reactive benchmark: cannot build digest ref for %s (D=%s) — refusing",
                candidate.submission_id, candidate_image_id,
            )
            return False, 0.0
        if not await _pull_image_by_digest(run_image):
            logger.warning(
                "Reactive benchmark: pull-by-digest failed for %s (%s) — refusing to sign",
                candidate.submission_id, run_image,
            )
            return False, 0.0
        logger.info(
            "Reactive benchmark: pulled content-addressed candidate %s (%s)",
            candidate.submission_id, run_image,
        )
    else:
        run_image = candidate.image_tag
        if not run_image:
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
            local_image_id = await _resolve_local_image_id(run_image)
            if local_image_id is None:
                logger.warning(
                    "Reactive benchmark: cannot resolve local image_id for %s — "
                    "refusing to benchmark",
                    run_image,
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

    # This follower's stable identity is an OBSERVABILITY LABEL only (#242): the
    # Stage-2 corpus is a single round-seeded SHARED draw (sample_historical_orders
    # seeds on round_id ALONE), identical on every validator, so the follower scores
    # the champion-vs-challenger verdict over the SAME corpus as the leader — that
    # shared corpus is what makes the independent verdict ratifiable by quorum. The
    # identity is NOT a per-validator corpus seed (that was retired; a disjoint slice
    # makes a concentrated improvement invisible). Best-effort: None is fine.
    try:
        from minotaur_subnet.api.startup import _resolve_solver_round_hotkey
        _my_identity = _resolve_solver_round_hotkey()
    except Exception:
        _my_identity = None

    worker = BenchmarkWorker(
        submission_store=get_store(),  # SubmissionStore (needed by constructor)
        app_store=app_store,
        use_docker=True,
        simulator=simulator,
        require_real_sim=require_real_sim_default(),
        validator_identity=_my_identity,
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

    # Stage 2 historical scenarios — a single round-seeded SHARED corpus, identical
    # on every validator (#242). The follower re-runs this same corpus, so its
    # independent champion-vs-challenger verdict (below) is directly comparable to
    # the leader's and ratifiable by quorum.
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

    # Match the leader benchmark path: challenger and incumbent/champion are both
    # scored against the current champion's quote anchor for the same shared corpus.
    # If the reference pre-pass cannot be built on the follower, fail closed rather
    # than vote on a different benchmark definition.
    try:
        reference_quotes = await worker._build_reference_quotes(intents)
    except Exception as exc:
        logger.error(
            "Reactive verify for %s could not build champion reference quotes "
            "— failing closed: %s",
            candidate.submission_id,
            exc,
        )
        return False, 0.0

    # Run the Docker benchmark. Honor the same fail-closed switch as the leader
    # (BENCHMARK_REQUIRE_REAL_SIM) so a follower never re-verifies a candidate on
    # fabricated mock data while the leader fail-closes it — that asymmetry would
    # silently diverge consensus.
    _require_real_sim = require_real_sim_default()
    orch = SolverOrchestrator()
    session = await orch.start_docker(run_image)
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
            reference_quotes=reference_quotes,
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
    # against the leader's on-chain scoreIntent numbers.
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

    # (#242) Follower verification = INDEPENDENT verdict over the SHARED corpus.
    # Re-benchmark the CURRENT champion on the SAME shared intents and apply the
    # adoption rule ourselves, signing only if WE conclude adopt — not merely
    # reproducing the leader's number (the old relative-diff tolerance check). The
    # corpus is identical fleet-wide, so a concentrated improvement is visible to
    # every validator and the quorum of independent verdicts is meaningful.
    return await _independent_adopt_vote(
        worker=worker, intents=intents, score_fn=score_fn, simulator=simulator,
        chal_results=results, chal_score=local_score, candidate=candidate,
        round_id=round_id, reference_quotes=reference_quotes,
    )


async def _independent_adopt_vote(
    *,
    worker: Any,
    intents: list,
    score_fn: Any,
    simulator: Any,
    chal_results: list,
    chal_score: float,
    candidate: Any,
    round_id: str | None,
    reference_quotes: dict[str, dict[str, str]] | None = None,
) -> tuple[bool, float]:
    """This follower's INDEPENDENT adopt verdict over the SHARED corpus (#242).

    Benchmarks the CURRENT champion on the SAME ``intents`` the challenger was just
    scored on — the single round-seeded corpus that is identical on every validator
    — and applies the shared ``evaluate_adoption`` rule, so the vote is this
    validator's own judgement (challenger beats champion), not reproduction of the
    leader's number. Because the corpus is shared, a concentrated improvement is
    visible to the whole fleet and the quorum of YES verdicts is meaningful.
    Returns ``(adopt, chal_score)``.

    Conservative on uncertainty: if the champion image can't be resolved, or its
    benchmark needs a real simulator and none is available, vote REJECT rather than
    risk adopting an unverified challenger.
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

    chal_card = worker._build_scorecard(chal_results).to_dict()
    # has_champion mirrors the leader EXACTLY: adopted champion, active-champion
    # snapshot, OR a SCORED genesis with a usable score (genesis-as-bar, #242 — the
    # first champion must BEAT genesis). The leader seeds self._champion from the
    # same predicate at decision time (_maybe_seed_genesis_incumbent), so
    # _resolve_incumbent_submission() replicates it. The bootstrap branch below is
    # reached only at TRUE bootstrap — no champion AND no scored genesis yet.
    has_champion = worker._resolve_incumbent_submission() is not None
    champ_image = worker._resolve_champion_image()

    if not has_champion:
        # BOOTSTRAP (has_champion=False): no incumbent to dethrone. Match the leader
        # — adopt a first champion that clears the absolute floor (no margin). MUST
        # NOT auto-reject here: that would deadlock the very first adoption.
        adopt, reason = evaluate_adoption(
            challenger_score=chal_score,
            champion_score=0.0,
            challenger_scorecard=chal_card,
            champion_scorecard={},
            dethrone_margin=DETHRONE_MARGIN,
            has_champion=False,
        )
        logger.info(
            "[independent-vote] role=follower candidate=%s round=%s vote=%s "
            "chal_score=%.4f champ=BOOTSTRAP(no incumbent): %s",
            candidate.submission_id, round_id,
            "ADOPT" if adopt else "REJECT", chal_score, reason,
        )
        try:
            from minotaur_subnet.api.server_context import ctx
            ctx.last_independent_vote = {
                "candidate_id": candidate.submission_id, "role": "follower",
                "vote": "ADOPT" if adopt else "REJECT",
                "chal_score": round(float(chal_score), 4), "champ_score": None,
                "round_id": round_id, "reason": reason,
            }
        except Exception:  # observe-only — must never break verification
            pass
        return adopt, chal_score

    if not champ_image:
        # has_champion=True but the incumbent's image can't be resolved — we cannot
        # benchmark it to verify the challenger beats it, so REJECT conservatively
        # (NOT a bootstrap: an incumbent exists, the margin must be proven).
        logger.warning(
            "[independent-vote] candidate=%s: champion exists but image unresolvable "
            "— voting REJECT (cannot verify improvement)",
            candidate.submission_id,
        )
        return False, chal_score

    _require_real_sim = require_real_sim_default()
    orch = SolverOrchestrator()

    async def _run_champ():
        champ_session = await orch.start_docker(champ_image)
        try:
            return await run_benchmark(
                champ_session,
                intents,
                config=BenchmarkConfig(
                    chain_ids=list({s.chain_id for _, s, _ in intents} or {1}),
                ),
                score_fn=score_fn,
                simulator=simulator,
                require_real_sim=_require_real_sim,
                fork_block=worker._epoch_block_number,
                reference_quotes=reference_quotes,
            )
        finally:
            await champ_session.shutdown()

    try:
        # Champion run #2 (the quorum verdict). When CONSOLIDATE_CHAMPION_BENCH is
        # on and the key matches the dethrone re-bench (same round/image/fork/corpus
        # /real-sim), REUSE that result — it is the identical deterministic
        # computation, so this validator's verdict is unchanged. On a cache hit
        # _run_champ never executes (no champion session is started). Off → runs
        # _run_champ directly, exactly as before.
        champ_results = await worker.memo_champion_bench(
            round_id=round_id,
            image=champ_image,
            fork_block=worker._epoch_block_number,
            intents=intents,
            require_real_sim=_require_real_sim,
            reference_quotes=reference_quotes,
            run=_run_champ,
        )
    except RealSimulationUnavailable:
        logger.error(
            "[independent-vote] candidate=%s: champion benchmark needs a real sim "
            "but none available — voting REJECT",
            candidate.submission_id,
        )
        return False, chal_score

    champ_score = worker._compute_avg_score(champ_results)
    champ_card = worker._build_scorecard(champ_results).to_dict()
    # Use the SAME margin source the leader's EpochManager uses (the DETHRONE_MARGIN
    # constant — see routes.py EpochManager construction) so leader and follower
    # apply an identical bar; do not diverge via a follower-only env override.
    margin = DETHRONE_MARGIN
    adopt, reason = evaluate_adoption(
        challenger_score=chal_score,
        champion_score=champ_score,
        challenger_scorecard=chal_card,
        champion_scorecard=champ_card,
        dethrone_margin=margin,
        has_champion=True,
    )
    logger.info(
        "[independent-vote] role=follower candidate=%s round=%s vote=%s chal_score=%.4f "
        "champ_score=%.4f fork_block=%s: %s",
        candidate.submission_id,
        round_id,
        "ADOPT" if adopt else "REJECT",
        chal_score,
        champ_score,
        worker._epoch_block_number,
        reason,
    )
    # Publish for the fleet shadow tally (/health independent_vote). Best-effort.
    try:
        from minotaur_subnet.api.server_context import ctx
        ctx.last_independent_vote = {
            "candidate_id": candidate.submission_id,
            "role": "follower",
            "vote": "ADOPT" if adopt else "REJECT",
            "chal_score": round(float(chal_score), 4),
            "champ_score": round(float(champ_score), 4),
            "round_id": round_id,
            "reason": reason,
        }
    except Exception:  # observe-only — must never break verification
        pass
    return adopt, chal_score


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
    # Adopt the leader's round verbatim when this follower is BEHIND and doesn't
    # have it yet. SAFETY: every caller authenticates the leader BEFORE reaching
    # here. The /solver/round/consensus/proposal handler is sig-only
    # (_verify_champion_proposal_signature, EIP-712 leader sig). The sync certify
    # handler goes through _authorize_internal_round, which accepts the leader's
    # EIP-712 signature OR (default, since REQUIRE_SIGNED_ROUND_LIFECYCLE is off)
    # the shared SOLVER_ROUND_INTERNAL_API_KEY. Without this the get_round() below
    # 404s on the leader's unknown round_id. Adopt as CLOSED so the existing prep
    # flow (close->evaluate->CERTIFYING) advances it normally.
    if round_store.get_round(round_id) is None:
        _adopt_leader_round_if_behind(
            round_id,
            status=RoundStatus.CLOSED,
            close_epoch=close_epoch,
            benchmark_pack_hash=benchmark_pack_hash,
            committee_block=committee_block,
            committee_hash=committee_hash,
            quorum_required=quorum_required,
            decision_deadline_epoch=decision_deadline_epoch,
            effective_epoch=effective_epoch,
        )
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

    # Content-addressed (digest mode): when the candidate has a pushed GHCR manifest
    # digest, carry its BARE 64-hex so on-chain candidateImageId == D and followers
    # reconstruct <repo>@sha256:D to pull. Falls back to the local {{.Id}} image_id
    # (legacy) when no digest was pushed. Peers receive the leader's resolved value
    # via candidate_image_id, so the whole quorum signs the same D.
    from minotaur_subnet.harness.image_transport import bare_hex, is_bare_digest
    # Resolve the on-chain candidateImageId. Order:
    #   1. an already-bare value passed by the caller (a follower reconstructing the
    #      leader's authoritative bare digest to verify the signature) — use verbatim;
    #   2. the candidate's REAL pushed GHCR manifest digest (bare 64-hex) — what the
    #      quorum>1 gate + followers' pull-by-digest require;
    #   3. else the RAW local id (candidate_image_id / image_id / finalist_image_id) —
    #      legacy, fine at quorum<=1 where the single leader benchmarks locally, and
    #      correctly REJECTED by the quorum>1 gate below (it isn't pull-able cross-host).
    # Critical: the local ids are NOT run through bare_hex — turning a "sha256:<id>" into
    # a bare hex would pass the gate with a non-pullable id and defeat its purpose.
    # Why this matters: the leader's coordinator passes the PREFIXED local image id
    # (finalist_image_id = "sha256:<hex>") as candidate_image_id, so the old
    # `candidate_image_id or _candidate_digest or ...` left the prefix in place and the
    # quorum>1 gate `is_bare_digest(...)` wrongly fired "no pushed image digest" — blocking
    # EVERY multi-validator certification even when a valid pushed digest existed. Preferring
    # the real digest (2) over the passed prefixed id fixes it.
    _passed_bare = candidate_image_id if is_bare_digest(candidate_image_id or "") else None
    resolved_image_id = (
        _passed_bare
        or bare_hex(getattr(candidate, "image_digest", None))
        or candidate_image_id
        or candidate.image_id
        or round_state.finalist_image_id
    )
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

    # Cross-host verifiability gate (quorum>1): a follower can only re-benchmark +
    # vote on a candidate it can independently PULL by content digest. If the image
    # push was best-effort-skipped (image_digest unset), resolved_image_id falls back
    # to the leader's LOCAL {{.Id}} sha — unverifiable on any other host, so every
    # follower would be forced to REJECT (reads as dissent) and the round could never
    # reach quorum. Fail CLOSED here rather than broadcast an un-poolable candidate.
    # Genesis/builtin candidates carry no image and are exempt; at quorum<=1 the
    # single (leader) voter benchmarks locally so the legacy id is fine.
    _is_builtin = str(resolved_image_id).startswith("builtin:")
    if resolved_quorum > 1 and not _is_builtin and not is_bare_digest(resolved_image_id):
        raise HTTPException(
            status_code=409,
            detail=(
                f"Candidate {candidate.submission_id} has no pushed image digest "
                f"(image_digest unset) — refusing to propose at quorum {resolved_quorum}: "
                f"followers cannot pull-by-digest to independently verify it. The "
                f"candidate image must be pushed to the registry (CANDIDATE_IMAGE_REPO) "
                f"before it can be certified by a multi-validator quorum."
            ),
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
        # Observability: publish the would-be quorum tally to /health so operators
        # can watch fleet agreement — including under DISABLE_CHAMPION_ADOPTION,
        # where the full consensus runs but the commit is blocked at activation.
        # Pure side-effect-free recording; never affects the decision.
        try:
            from minotaur_subnet.api.server_context import ctx
            ctx.last_champion_quorum = {
                "round_id": proposal.round_id,
                "candidate_submission_id": proposal.candidate_submission_id,
                "candidate_image_id": proposal.candidate_image_id,
                "collected": result.collected,
                "quorum_required": result.quorum,
                "reached": bool(result.reached),
                "signers": [a.validator_id for a in (result.approvals or [])],
            }
        except Exception:  # observe-only — must never break certification
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
