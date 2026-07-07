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
import os
from typing import Any, Callable

from fastapi import HTTPException

from minotaur_subnet.consensus.protocol_config import read_champion_last_nonce
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
    _broadcast_internal_round_sync,
    _close_round_sync_payload,
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
    round_id: str | None = None,
    candidate_image_id: str | None = None,
) -> tuple[bool, dict[str, Any]]:
    """Independently verify a champion candidate via the relative adopt rule.

    Peers call this when they receive a champion proposal. It mirrors how order
    consensus peers re-simulate plans before signing — the peer benchmarks the
    candidate on the round's shared flat scenario set (synthetic ∪ the round-seeded
    historical order draw) and applies the AUTHORITATIVE per-order relative rule
    itself (via :func:`_independent_adopt_vote`), signing only if IT concludes adopt
    — NOT a leader-score tolerance check (that scalar was removed).

    Args:
        candidate: Submission object (has image_tag, commit_hash, repo_url).
        round_id: Current round ID — used to deterministically sample the same
            historical orders the leader used.

    Returns:
        (verified, counts) — verified is this validator's own relative adopt verdict;
        counts is the relative better/worse/matched breakdown.
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
            return False, {}
        if not await _pull_image_by_digest(run_image):
            logger.warning(
                "Reactive benchmark: pull-by-digest failed for %s (%s) — refusing to sign",
                candidate.submission_id, run_image,
            )
            return False, {}
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
            return False, {}

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
                return False, {}
            if local_image_id.lower() != expected_image_id.lower():
                logger.warning(
                    "Reactive benchmark image_id mismatch for %s: local=%s expected=%s "
                    "— refusing to benchmark",
                    candidate.submission_id, local_image_id, expected_image_id,
                )
                return False, {}

    # Build a temporary BenchmarkWorker to reuse intent loading and scoring
    app_store = ctx.store
    # Get the Anvil simulator for real simulation (not mock).
    # Mock simulation results deliver no raw_output, so without a real simulator the
    # reactive benchmark sees no delivered value and the relative rule abstains.
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
        return False, {}

    score_fn = await worker._build_score_fn(intents)
    intents = worker._enrich_intents_with_manifests(intents)

    # Historical order draw — a single round-seeded SHARED corpus, identical
    # on every validator (#242), joined flat with the synthetic scenarios. The
    # follower re-runs this same corpus, so its
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

    # Match the leader benchmark path EXACTLY — including the quote regime.
    # The getter is flag-aware: under static quoting (the default) it returns
    # {} without starting a champion session (the enrichment injects a static
    # zero quote, same as the leader's scoring definition); under the legacy
    # champion-anchored mode it builds the same reference pre-pass the leader
    # graded against (this worker has no round store, so the getter falls
    # through to a plain build — no checkpoint reuse). Calling
    # _build_reference_quotes directly here would bypass the flag and vote on
    # a DIFFERENT benchmark definition than the leader's. If the legacy
    # pre-pass cannot be built, fail closed rather than vote on a different
    # definition.
    try:
        reference_quotes = await worker._get_or_build_reference_quotes(intents)
    except Exception as exc:
        logger.error(
            "Reactive verify for %s could not build champion reference quotes "
            "— failing closed: %s",
            candidate.submission_id,
            exc,
        )
        return False, {}

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
        return False, {}
    finally:
        await session.shutdown()

    logger.info(
        "Reactive benchmark for %s: %d orders benchmarked",
        candidate.submission_id, len(results),
    )

    # Determinism-comparable signals: log the per-app on-chain scoreIntent means so
    # operators can grep-compare leader vs follower for the same candidate + pinned
    # block across the fleet.
    try:
        card = worker._build_scorecard(results)
        oc_means: dict[str, float | None] = {}
        for app, scores in card.app_onchain.items():
            present = [s for s in scores if s is not None]
            oc_means[app] = round(sum(present) / len(present), 1) if present else None
        logger.info(
            "[reactive-determinism] candidate=%s round=%s fork_block=%s "
            "app_onchain_means=%s",
            candidate.submission_id, round_id, worker._epoch_block_number,
            oc_means,
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
        chal_results=results, candidate=candidate,
        round_id=round_id, reference_quotes=reference_quotes,
    )


async def _independent_adopt_vote(
    *,
    worker: Any,
    intents: list,
    score_fn: Any,
    simulator: Any,
    chal_results: list,
    candidate: Any,
    round_id: str | None,
    reference_quotes: dict[str, dict[str, str]] | None = None,
) -> tuple[bool, dict[str, Any]]:
    """This follower's INDEPENDENT adopt verdict over the SHARED corpus (#242).

    Benchmarks the CURRENT champion on the SAME ``intents`` the challenger was just
    scored on — the single round-seeded corpus that is identical on every validator
    — and applies the AUTHORITATIVE relative per-order rule
    (:func:`evaluate_relative_adoption`), the IDENTICAL rule the leader runs
    (``EpochManager._meets_adoption_criteria``), so the vote is this validator's own
    per-order judgement under the BOUNDED-REGRESSION NET-BETTER rule (no order cut
    >1%, no dropped order, net wins+blind-spots exceed regressions by the margin),
    NOT reproduction of any aggregate number. Because the corpus is shared and the
    rule is fleet-uniform, the quorum of YES verdicts is meaningful. Returns
    ``(adopt, counts)`` — counts is the relative better/worse/matched breakdown.

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
    from minotaur_subnet.epoch.relative_scoring import (
        deadwood_delta_between,
        evaluate_relative_adoption,
        factor_delta_between,
    )

    def _counts(v: dict[str, Any]) -> dict[str, Any]:
        # Relative better/worse/matched breakdown — the display + tally shape that
        # replaced the retired aggregate chal_score/champ_score. Armed blind-spot
        # repeats are compared-but-neutral, folded into matched (mirroring
        # relative_counts) with the separate count kept for the tally.
        repeats = v.get("n_blind_spot_repeats", 0)
        return {
            "better": v["n_wins"] + v["n_blind_spots"],
            "worse": v["n_regressions"] + v["n_dropped"],
            "matched": v["n_matched"] + repeats,
            "repeats": repeats,
            "compared": v["scenarios_compared"],
        }

    def _bar_kwargs(incumbent: Any) -> dict[str, Any]:
        # Blind-spot REPEAT bar for THIS validator's independent verdict — the
        # SAME rule input the leader passes (_blind_spot_bar_kwargs), sourced
        # from the local round store (persisted at adoption by _hot_swap). A
        # follower whose store predates the bar (trust-adopted at quorum<=1 /
        # pre-upgrade) degrades to an inert guard — identical to the leader
        # after a snapshot-less restore. Best-effort: never blocks the vote.
        try:
            import time as _time

            from minotaur_subnet.epoch.relative_scoring import bar_kwargs_from_record

            round_store = getattr(worker, "_round_store", None)
            if round_store is None or incumbent is None:
                return {}
            return bar_kwargs_from_record(
                round_store.get_champion_adoption_bar(),
                getattr(incumbent, "submission_id", None),
                _time.time(),
            )
        except Exception:
            return {}

    # has_champion mirrors the leader EXACTLY: adopted champion, active-champion
    # snapshot, OR a genesis that DELIVERED VALUE (genesis-as-bar, #242 — the
    # first champion must BEAT genesis). The leader seeds self._champion from the
    # same predicate at decision time (_maybe_seed_genesis_incumbent), so
    # _resolve_incumbent_submission() replicates it. The bootstrap branch below is
    # reached only at TRUE bootstrap — no champion AND no scored genesis yet.
    incumbent_sub = worker._resolve_incumbent_submission()
    has_champion = incumbent_sub is not None
    champ_image = worker._resolve_champion_image()

    if not has_champion:
        # BOOTSTRAP (has_champion=False): no incumbent to dethrone. The relative rule
        # with an empty champion treats every value-delivering challenger order as a
        # blind-spot cover, so a first champion that delivers value on ANY order
        # adopts (no incumbent benchmark). MUST NOT auto-reject: that would deadlock
        # the very first adoption.
        verdict = evaluate_relative_adoption([], chal_results)
        adopt = bool(verdict["adopt"])
        counts = _counts(verdict)
        logger.info(
            "[independent-vote] role=follower candidate=%s round=%s vote=%s "
            "champ=BOOTSTRAP(no incumbent) better=%d worse=%d: %s",
            candidate.submission_id, round_id,
            "ADOPT" if adopt else "REJECT",
            counts["better"], counts["worse"], verdict["reason"],
        )
        try:
            from minotaur_subnet.api.server_context import ctx
            ctx.last_independent_vote = {
                "candidate_id": candidate.submission_id, "role": "follower",
                "vote": "ADOPT" if adopt else "REJECT",
                **counts,
                "round_id": round_id, "reason": verdict["reason"],
            }
        except Exception:  # observe-only — must never break verification
            pass
        return adopt, counts

    if not champ_image:
        # has_champion=True but the incumbent's image can't be resolved — we cannot
        # benchmark it to verify the challenger beats it, so REJECT conservatively
        # (NOT a bootstrap: an incumbent exists, the margin must be proven).
        logger.warning(
            "[independent-vote] candidate=%s: champion exists but image unresolvable "
            "— voting REJECT (cannot verify improvement)",
            candidate.submission_id,
        )
        return False, {}

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
        return False, {}

    # AUTHORITATIVE relative per-order verdict — IDENTICAL to the leader's
    # _meets_adoption_criteria, so leader and follower decide alike (fleet-uniform).
    # Joins champion vs challenger BenchmarkResults by intent_id on raw_output (the
    # RAW delivered output the live raw-output scorer emits via metadata.raw_output).
    # factor_delta: the Phase-2 factorization tie-break, from the PERSISTED
    # screening metrics on this follower's LOCAL records (leader-computed once,
    # mirrored: the candidate's via the round close snapshot + the leader's
    # pre-proposal refresh (_refresh_round_submission_mirror — the close-time
    # snapshot can predate screening stage 1, e.g. a restart-requeued screening
    # on a round closed at boot); the incumbent's only via a champion
    # force-sync — a leader-side BACKFILL does NOT reach here on its own, close
    # snapshots are round-scoped, hence the mandatory post-backfill reattest in
    # scripts/backfill_factor_metric.py). None on either side ⇒ 0 ⇒ clause
    # inert, exactly like the leader.
    # deadwood_delta: the 4th ladder key, threaded IDENTICALLY; the
    # metric-version guard lives in the ONE shared helper
    # (deadwood_delta_between: 0 unless BOTH records carry SAME-VERSION
    # unproductive metrics — cross-version node counts are not comparable, so
    # a mismatched pair must never produce a nonzero delta). The fields ship
    # on the #575 lineage; getattr keeps this inert until the lineages merge
    # and records carry values (activation-by-data, exactly like factor).
    #
    # Read the metrics off the FRESHEST store record for the candidate: the
    # `candidate` reference was fetched at proposal receipt
    # (_build_champion_proposal_for_round → store.get), and the reactive
    # benchmark above runs for MINUTES in between — a snapshot heal landing
    # meanwhile REPLACES the store object (upsert builds a new Submission), so
    # the in-hand reference can be a stale pre-metrics copy. Best-effort with
    # the passed candidate as fallback: a missing store/record degrades to the
    # exact pre-refresh behavior (None ⇒ 0 ⇒ inert), never a crash.
    try:
        candidate_rec = get_store().get(
            getattr(candidate, "submission_id", "") or ""
        ) or candidate
    except Exception:  # noqa: BLE001 — the passed candidate is always usable
        candidate_rec = candidate
    factor_delta = factor_delta_between(
        getattr(incumbent_sub, "max_region_nodes", None),
        getattr(candidate_rec, "max_region_nodes", None),
    )
    deadwood_delta = deadwood_delta_between(
        getattr(incumbent_sub, "unproductive_nodes", None),
        getattr(candidate_rec, "unproductive_nodes", None),
        getattr(incumbent_sub, "unproductive_metric_version", None),
        getattr(candidate_rec, "unproductive_metric_version", None),
    )
    verdict = evaluate_relative_adoption(
        champ_results, chal_results,
        factor_delta=factor_delta,
        deadwood_delta=deadwood_delta,
        **_bar_kwargs(incumbent_sub),
    )
    adopt = bool(verdict["adopt"])
    counts = _counts(verdict)
    logger.info(
        "[independent-vote] role=follower candidate=%s round=%s vote=%s "
        "fork_block=%s better=%d worse=%d wins=%d regressions=%d blind_spots=%d "
        "compared=%d factor_delta=%d deadwood_delta=%d: %s",
        candidate.submission_id,
        round_id,
        "ADOPT" if adopt else "REJECT",
        worker._epoch_block_number,
        counts["better"], counts["worse"],
        verdict["n_wins"], verdict["n_regressions"], verdict["n_blind_spots"],
        verdict["scenarios_compared"], factor_delta, deadwood_delta,
        verdict["reason"],
    )
    # Publish for the fleet tally (/health independent_vote). Best-effort.
    try:
        from minotaur_subnet.api.server_context import ctx
        ctx.last_independent_vote = {
            "candidate_id": candidate.submission_id,
            "role": "follower",
            "vote": "ADOPT" if adopt else "REJECT",
            **counts,
            "round_id": round_id,
            "reason": verdict["reason"],
        }
    except Exception:  # observe-only — must never break verification
        pass
    return adopt, counts


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
                )
                logger.info(
                    "Round %s → CERTIFYING with explicit candidate %s",
                    round_id, candidate.submission_id,
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


def _floor_champion_nonce(
    wallclock_ms: int,
    consensus_manager: Any,
    *,
    nonce_reader: Callable[[str, str, str], int] | None = None,
) -> int:
    """Floor a freshly-minted champion nonce against the on-chain per-signer
    high-water so it is ALWAYS strictly greater than ``lastNonce[signer]``.

    Why: the leader mints the nonce from wall-clock ms (``time.time()*1000``).
    ``ChampionRegistry.certify()`` enforces
    ``require(nonces[i] > lastNonce[signer], "Nonce not increasing")`` per
    signer, and the relayer swallows that revert to ``None`` (round aborts
    ``merge_failed`` with no nonce-specific diagnosis). So if the leader's clock
    moves BACKWARD — NTP step-back, VM migration, restart onto a skewed host, or
    a leader change to a lagging-clock validator — the minted nonce can be <= the
    stored high-water and EVERY future champion certification reverts, silently,
    for the whole skew duration. Flooring removes the only un-guarded path to a
    stale nonce.

    All co-signers share the leader's single minted nonce (a follower approval
    must carry ``approval.nonce == proposal.nonce``), and the contract checks the
    nonce against EACH signer's slot, so we clear the MAX high-water across the
    whole committee, not just the leader's own address.

    Best-effort / FAIL-OPEN: any chain-read failure (RPC down, registry
    unconfigured, no protocol_config) returns the wall-clock value unchanged —
    flooring is a safety boost layered on top of monotonic wall-clock, never a
    gate that may block proposing a champion. Only ever applied when the leader
    mints fresh; followers reuse the leader's nonce verbatim via *_override and
    must NOT re-floor (it would diverge their signed digest from the leader's).
    """
    # Belt-and-suspenders fail-open: the per-signer reader is already guarded
    # below, but the attribute reads here (protocol_config / quorum_address /
    # validators — any of which could be a property that raises) are not. Wrap the
    # whole body so NOTHING in flooring can ever propagate and block proposing a
    # champion; an unexpected error just degrades to the raw wall-clock nonce.
    try:
        pc = getattr(consensus_manager, "protocol_config", None)
        if pc is None:
            return wallclock_ms
        rpc_url = (getattr(pc, "rpc_url", "") or "").strip()
        # The ChampionRegistry lives at quorum_address (distinct from the
        # ValidatorRegistry); mirror _read_quorum_bps' quorum_address-or-registry
        # fallback for the single-contract topology.
        registry_address = (
            (getattr(pc, "quorum_address", "") or "").strip()
            or (getattr(pc, "registry_address", "") or "").strip()
        )
        if not rpc_url or not registry_address:
            return wallclock_ms

        # NOTE: ``signers`` is the leader's DISCOVERED committee (its in-memory peer
        # view), NOT the full on-chain authorized signer set. At quorum>1 an
        # authorized co-signer the leader hasn't discovered yet whose lastNonce slot
        # is higher could still make certify() revert "Nonce not increasing" — that
        # residual is fail-open here AND now surfaced by the relayer's revert
        # diagnostic. Production runs at the quorum floor (=1, leader-only signs),
        # where the leader's own slot is the only one that matters.
        signers = [s for s in (getattr(consensus_manager, "validators", None) or []) if s]
        if not signers:
            vid = (getattr(consensus_manager, "validator_id", "") or "").strip()
            signers = [vid] if vid else []
        if not signers:
            return wallclock_ms

        reader = nonce_reader or read_champion_last_nonce
        highwater = 0
        for signer in signers:
            try:
                highwater = max(highwater, int(reader(rpc_url, registry_address, signer)))
            except Exception as exc:  # noqa: BLE001 — fail-open, never block proposing
                logger.warning(
                    "champion nonce floor: lastNonce(%s) read failed on %s (%s); "
                    "falling back to wall-clock nonce %d (no floor applied this round)",
                    signer, registry_address, exc, wallclock_ms,
                )
                return wallclock_ms

        floored = max(int(wallclock_ms), highwater + 1)
        if floored != wallclock_ms:
            logger.warning(
                "champion nonce floored %d -> %d (on-chain per-signer high-water %d "
                "across %d signer(s) exceeds wall-clock) — the leader host clock is "
                "BEHIND the on-chain champion nonce; check NTP/clock skew",
                wallclock_ms, floored, highwater, len(signers),
            )
        return floored
    except Exception as exc:  # noqa: BLE001 — fail-open: flooring must NEVER block proposing
        logger.warning(
            "champion nonce floor: unexpected error (%s); falling back to "
            "wall-clock nonce %d (no floor applied this round)",
            exc, wallclock_ms,
        )
        return wallclock_ms


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
    incumbent_image_id_override: str | None = None,
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
        # Follower (or re-broadcast): reuse the leader's signed nonce verbatim so
        # the EIP-712 digest matches. Must NOT re-floor — that would diverge the
        # follower's signed struct from the leader's and fail signature recovery.
        nonce = int(nonce_override)
    else:
        # Leader minting fresh: floor the wall-clock nonce against the on-chain
        # per-signer high-water so a backward clock movement can't mint a stale
        # nonce that silently bricks certification. Fail-open (see helper).
        nonce = _floor_champion_nonce(int(_time.time() * 1000), consensus_manager)
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
        # incumbent_image_id is part of the SIGNED digest but is resolved from the
        # round record, whose local image-id representation differs per host (a
        # non-reproducible {{.Id}} at quorum<=1). A peer verifying the leader's
        # approval MUST rebuild it from the leader's SIGNED value carried in the
        # payload (incumbent_image_id_override) — else the digest diverges and
        # verify_approval fails ("Invalid champion approvals"), stranding the round
        # leader-only. The leader (no override) uses its own round record verbatim.
        incumbent_image_id=incumbent_image_id_override or round_state.incumbent_image_id,
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


# ── Best-effort champion quorum (monitor) ─────────────────────────────────────
# The leader certifies FAST with its own approval at the on-chain quorum FLOOR (1),
# so the cert always validates and NEVER deadlocks. Followers ALREADY run a full
# reactive benchmark + independent verify + sign when they receive a proposal — but
# at the floor the leader self-certifies and never waits on them. AFTER the floor
# cert is committed, this block re-broadcasts the SAME proposal with collector=None
# (harvested approvals can NEVER reach the live cert at ANY quorum) and a long
# timeout, then RECORDS which validators approved vs are MISSING (n-of-target) for
# /health + the fleet dashboard. "Quorum N" is a broadcast+monitor target only — it
# is NEVER routed into propose()/quorum_bps. A 3rd-party validator down/disagreeing
# just shows as 'missing'; the champion still certs. Mirrors the ROUND_ANCHOR_PARITY
# ship-dark convention.
_BEST_EFFORT_QUORUM_TARGET_BPS_DEFAULT = 6000
_BEST_EFFORT_QUORUM_OFF_VALUES = frozenset({"0", "false", "no", "off"})
# Strong refs to in-flight harvest tasks so a detached create_task is not GC'd
# before completion ("Task was destroyed but it is pending").
_BEST_EFFORT_TASKS: set = set()


def best_effort_champion_quorum_enabled() -> bool:
    """BEST_EFFORT_CHAMPION_QUORUM master gate — DEFAULT ON (monitor-only). Disable
    with one of {0,false,no,off}. Read at call time. Pure observability: it only
    toggles the post-cert harvest + recording and NEVER feeds the real quorum/cert.
    Accepts the legacy SHADOW_CHAMPION_QUORUM name as an alias."""
    raw = os.environ.get("BEST_EFFORT_CHAMPION_QUORUM")
    if raw is None:
        raw = os.environ.get("SHADOW_CHAMPION_QUORUM")  # legacy alias
    if raw is None:
        return True
    return raw.strip().lower() not in _BEST_EFFORT_QUORUM_OFF_VALUES


def best_effort_champion_quorum_target_bps() -> int:
    """The MONITOR target threshold (bps) used only to render 'would N-of-M have
    certified?' on /health. DEFAULT 6000 (=60%). NEVER routed into
    ``consensus_manager.quorum_bps`` / ``propose()`` / the live cert — applied only to
    the collected-approval count for monitoring. Accepts the legacy
    SHADOW_CHAMPION_QUORUM_BPS name as an alias."""
    raw = os.environ.get("BEST_EFFORT_CHAMPION_QUORUM_TARGET_BPS")
    if raw is None:
        raw = os.environ.get("SHADOW_CHAMPION_QUORUM_BPS")  # legacy alias
    try:
        return int(raw) if raw is not None else _BEST_EFFORT_QUORUM_TARGET_BPS_DEFAULT
    except (TypeError, ValueError):
        return _BEST_EFFORT_QUORUM_TARGET_BPS_DEFAULT


def _quorum_required_at(n: int, bps: int) -> int:
    """Approvals needed at *bps* over *n* validators. SAME integer ceil-div as
    ``ChampionConsensusManager.quorum_required`` — kept in lock-step (no float,
    host-deterministic). Monitor-only; never used for the live decision."""
    return max(1, (n * bps + 9999) // 10000)


def _best_effort_request_timeout() -> float:
    """Per-peer POST timeout (s) for the best-effort harvest. Long by default (300s):
    a follower runs a full reactive benchmark before it signs, far longer than the
    ~30s the real broadcast uses. Accepts the legacy SHADOW_CHAMPION_QUORUM_TIMEOUT_S."""
    raw = os.environ.get("BEST_EFFORT_CHAMPION_QUORUM_TIMEOUT_S")
    if raw is None:
        raw = os.environ.get("SHADOW_CHAMPION_QUORUM_TIMEOUT_S")  # legacy alias
    try:
        return float(raw) if raw is not None else 300.0
    except (TypeError, ValueError):
        return 300.0


async def _run_best_effort_champion_quorum(
    proposal: Any, leader_result: Any, consensus_manager: Any,
    peer_network: Any, round_state: Any,
) -> None:
    """Post-cert, monitor-only: record which validators approved the certified champion
    vs which are MISSING (best-effort n-of-target).

    Re-broadcasts the SAME proposal with ``collector=None`` (read-only — the returned
    approvals NEVER touch the live consensus state) and a long per-request timeout so
    followers can finish their reactive benchmark and return a signed approval.
    Publishes the tally to ``ctx.last_best_effort_champion_quorum`` + logs one diffable
    line. Swallows everything — must never affect the already-committed floor cert.
    """
    try:
        try:
            follower_approvals = await peer_network.broadcast_champion_proposal(
                proposal,
                collector=None,  # read-only: cannot reach the live certificate
                close_epoch=round_state.close_epoch,
                quorum_required=consensus_manager.quorum_required,
                decision_deadline_epoch=round_state.decision_deadline_epoch,
                committee_block=round_state.committee_block,
                request_timeout=_best_effort_request_timeout(),
            )
        except asyncio.CancelledError:
            follower_approvals = []
        except Exception:
            follower_approvals = []
        # Unique approvers: the leader's own approval ∪ followers, deduped by lc id.
        approved: dict[str, str] = {}
        for a in (getattr(leader_result, "approvals", None) or []):
            try:
                approved[a.validator_id.lower()] = a.validator_id
            except Exception:
                pass
        for a in (follower_approvals or []):
            try:
                approved[a.validator_id.lower()] = a.validator_id
            except Exception:
                pass
        collected = len(approved)
        # Peers that did NOT approve (down / disagreed / slow) — the monitor signal.
        missing: list[str] = []
        try:
            for p in (peer_network.peers or []):
                pid = getattr(p, "validator_id", None)
                if pid and pid.lower() not in approved:
                    missing.append(pid)
        except Exception:
            pass
        # Same denominator as the live quorum (on-chain count, else validator set).
        n = 0
        pc = getattr(consensus_manager, "protocol_config", None)
        if pc is not None:
            n = getattr(pc, "on_chain_validator_count", 0) or 0
        if n == 0:
            try:
                n = len(consensus_manager.validators)
            except Exception:
                n = 0
        target_bps = best_effort_champion_quorum_target_bps()
        target_required = _quorum_required_at(n, target_bps)
        would_reach_at_target = collected >= target_required
        live_reached = bool(getattr(leader_result, "reached", False))
        live_quorum_required = getattr(consensus_manager, "quorum_required", None)
        # FLOOR WARNING: the leader-always-self-certs property holds ONLY while the
        # DERIVED on-chain quorum is 1 (fleet small enough at the configured bps). If it
        # ever exceeds 1, a lone leader can no longer self-cert → adoption could
        # DEADLOCK. Surface it loudly so quorum is raised DELIBERATELY, never by accident.
        if isinstance(live_quorum_required, int) and live_quorum_required > 1:
            logger.warning(
                "[champion-best-effort-quorum] LIVE quorum_required=%d (>1) — the leader "
                "can no longer self-certify alone; best-effort floor breached. Ensure "
                "followers actually certify in time or champion adoption may deadlock.",
                live_quorum_required,
            )
        rec = {
            "round_id": proposal.round_id,
            "candidate_submission_id": proposal.candidate_submission_id,
            "candidate_image_id": proposal.candidate_image_id,
            "validator_count": n,
            "target_bps": target_bps,
            "target_required": target_required,
            "collected": collected,
            "approved": list(approved.values()),
            "missing": missing,
            "would_reach_at_target": would_reach_at_target,
            "live_reached": live_reached,
            "live_quorum_required": live_quorum_required,
        }
        try:
            from minotaur_subnet.api.server_context import ctx
            ctx.last_best_effort_champion_quorum = rec
        except Exception:
            pass
        logger.info(
            "[champion-best-effort-quorum] round=%s candidate=%s n=%d approved=%d/%d "
            "(target@%dbps) would_reach=%s live_reached=%s missing=%s",
            proposal.round_id, proposal.candidate_submission_id, n, collected,
            target_required, target_bps, would_reach_at_target, live_reached,
            [m[:10] for m in missing],
        )
    except Exception:  # monitor-only — must never raise
        pass


async def _refresh_round_submission_mirror(round_state: RoundState) -> None:
    """Re-broadcast the round's CURRENT submission records (force-close snapshot)
    so follower votes compute factor/deadwood deltas from the SAME persisted
    metrics the leader's decision read.

    Root cause this closes: the close-time snapshot is the ONLY path that
    mirrors a candidate's ladder metrics (max_region_nodes / unproductive_*) to
    followers, but it is serialized AT CLOSE — and screening stage 1 (which
    computes those metrics) can complete AFTER close. Rotation deliberately
    keeps not-yet-screened submissions in the slate, and a leader restart
    re-kicks screening from scratch (resume_stranded_screenings) while the
    coordinator closes the elapsed round within seconds of boot. The leader's
    adopt decision then reads its LIVE record (metrics present by decision
    time), while every follower's mirrored record still carries the close-time
    None ⇒ factor_delta/deadwood_delta 0 ⇒ a factor-tie dethrone the leader
    adopts collects BENCHMARK_MISMATCH dissents fleet-wide (observed 2026-07-07
    19:16Z: round-e29724169-n1 / sub_c2ea85aa9641, leader factor_delta=-1647
    vs both followers "0 better / 0 worse").

    Re-sending the close payload with ``force=True`` re-delivers the snapshot
    through the follower's existing force-heal branch
    (``_sync_close_solver_round_state``), which upserts the records on an
    already-closed round WITHOUT touching its FSM — the exact mechanism the
    champion re-attest lever uses, live fleet-wide, so followers need NO new
    code (leader-only deploys via :latest fix the whole fleet). The lifecycle
    payload is signed over the RAW dict, so this carries no request-model /
    signature-canonical hazard (unlike extending the champion-proposal model).

    AWAITED by the caller BEFORE the proposal fan-out, so the heal lands before
    any follower's ``_independent_adopt_vote`` reads its store. Best-effort: a
    refresh failure must never block certification — followers then vote on the
    close-time mirror, exactly the pre-fix behavior.
    """
    try:
        payload = _close_round_sync_payload(round_state)
        payload["force"] = True
        await _broadcast_internal_round_sync(
            "/v1/solver/round/internal/close", payload,
        )
        logger.info(
            "[candidate-metric-refresh] round=%s: re-broadcast the submission "
            "snapshot (%d record(s)) before collecting champion votes",
            round_state.round_id, len(payload.get("submissions") or []),
        )
    except Exception:  # noqa: BLE001 — never block certification on the refresh
        logger.warning(
            "[candidate-metric-refresh] round=%s: snapshot re-broadcast failed "
            "(followers will vote on the close-time mirror)",
            round_state.round_id, exc_info=True,
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
    # The operator force-sync (emergency reattach) bypasses the certification deadline:
    # re-installing a STANDING champion whose decision window has long elapsed is the
    # whole point — the round was certified at the time, this just re-broadcasts it so a
    # follower that lost the round re-adopts. body.force is the same operator override
    # already used to certify a non-finalist candidate.
    if not body.force and _round_certification_deadline_elapsed(round_state):
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
        # v2 EIP-712 digest fields from the leader's SIGNED payload. A follower
        # verifying the leader's approval must rebuild the proposal with the
        # leader's nonce/deadline/commit_hash — otherwise it stamps its own
        # wall-clock, the digest diverges, and the signature recovers a wrong
        # address ("Invalid champion approvals"). `or None`: the leader's OWN cert
        # call carries the unset default (0) so the builder computes its own nonce,
        # leaving the leader's signing path unchanged (nonce is wall-clock-ms, never 0).
        commit_hash_override=body.commit_hash,
        nonce_override=body.nonce or None,
        deadline_override=body.deadline or None,
        # Rebuild the incumbent from the leader's SIGNED value (payload), not our
        # own round record — otherwise the digest diverges and the leader's
        # approval is rejected as "Invalid champion approvals". `or None`: the
        # leader's own certify call carries no incumbent override (unset), so the
        # builder uses its round record verbatim, leaving the signing path unchanged.
        incumbent_image_id_override=body.incumbent_image_id or None,
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
        if consensus_manager is not None and not body.force:
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
        elif body.force:
            # Operator force-sync (emergency reattach): bypass per-approval signature
            # verification. Re-installing a STANDING champion from an OLD round means the
            # follower cannot perfectly reconstruct the leader's signed proposal (it does
            # not carry every signed field), so the digest — and the recovered signer —
            # diverge even on a legitimate cert. Trust here rests on the internal-key auth
            # (only the leader/operator can broadcast this) + the quorum<=1 leader being
            # the sole on-chain authority. Logged loudly for audit. NEVER reached off the
            # force path (force defaults False), so the normal dethrone flow still fully
            # verifies signatures.
            logger.warning(
                "[force-sync] round %s: BYPASSING per-approval signature verification "
                "for %d approval(s) (operator force-adopt of the standing champion).",
                body.round_id, len(approvals),
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
        # Vote-input parity: refresh the fleet's submission mirror BEFORE asking
        # peers to vote, so every follower's factor/deadwood deltas are computed
        # from the SAME persisted metrics the leader's decision read (the
        # close-time snapshot can predate screening stage 1 — see
        # _refresh_round_submission_mirror). Awaited so the heal lands before
        # the proposal fan-out below triggers any _independent_adopt_vote.
        if peer_network is not None:
            await _refresh_round_submission_mirror(round_state)
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

        # Best-effort champion quorum (monitor-only): now that the floor certificate is
        # in hand, re-broadcast the SAME proposal to harvest follower approvals and
        # record approved-vs-missing (n-of-target) for /health + the fleet dashboard.
        # create_task is non-blocking, so certify_round below runs first; the harvest
        # uses collector=None so harvested approvals can never reach the live
        # certificate. Gated (default-on), swallow-all — strictly observability. A
        # strong ref is kept so the detached task is not GC'd before it completes.
        if best_effort_champion_quorum_enabled() and peer_network is not None:
            try:
                _be_task = asyncio.create_task(_run_best_effort_champion_quorum(
                    proposal, result, consensus_manager, peer_network, round_state,
                ))
                _BEST_EFFORT_TASKS.add(_be_task)
                _be_task.add_done_callback(_BEST_EFFORT_TASKS.discard)
            except Exception:  # monitor-only — must never break certification
                pass

    return round_store.certify_round(body.round_id, certificate)


async def _sync_certified_round_state(body: CertifyRoundRequest) -> RoundState:
    """Apply a leader-broadcast certificate to the local round store idempotently."""
    round_store = get_round_store()
    existing = round_store.get_round(body.round_id)
    if existing is not None and existing.status in (RoundStatus.CERTIFIED, RoundStatus.ACTIVATED):
        return existing
    return await _certify_solver_round_state(body)
