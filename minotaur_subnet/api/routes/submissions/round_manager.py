"""Solver round state machine.

Manages the round lifecycle: open -> close -> certify -> activate (or abort).
Includes round state queries, epoch management integration, and peer sync.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
from typing import Any

from fastapi import HTTPException

from minotaur_subnet.epoch import SolverRoundEpochClock
from minotaur_subnet.harness.round_store import (
    ChampionApproval,
    ChampionCertificate,
    ChampionSnapshot,
    RoundState,
    RoundStatus,
)

from .models import (
    AbortRoundRequest,
    ActivateRoundRequest,
    CertifyRoundRequest,
    CloseRoundRequest,
    SolverRoundResponse,
)
from .state import (
    get_champion_consensus_manager,
    get_champion_peer_network,
    get_epoch_manager,
    get_round_store,
    get_solver_round_epoch_provider,
    get_store,
    set_epoch_manager,
)

logger = logging.getLogger(__name__)


def _env_true(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _current_solver_round_epoch() -> int:
    """Return the current solver round epoch from the shared provider."""
    provider = get_solver_round_epoch_provider()
    if provider is not None:
        try:
            return max(0, int(provider()))
        except Exception:
            logger.debug("Solver round epoch provider failed; falling back to wall clock", exc_info=True)
    return SolverRoundEpochClock.from_env().current_epoch()


def _sign_internal_round_payload(network: Any, payload: dict[str, Any]) -> dict[str, Any]:
    """Add ``proposer`` + ``proposer_signature`` to a lifecycle sync payload.

    Mirrors ``ValidatorPeerNetwork._build_champion_proposal_payload``: set
    ``proposer`` to the leader's EVM address, then sign the canonical JSON of
    the payload *including* the ``proposer`` field (only ``proposer_signature``
    is ever stripped by the verifier). The follower's
    ``_authorize_internal_round`` reproduces this exact canonicalization.

    Backward-compatible: when no signing key is available (or signing fails),
    the payload is returned unsigned — not-yet-upgraded followers still
    authenticate via the legacy shared-key header, which the broadcast carries
    in ``default_headers``. NEVER raises into the broadcast path.
    """
    private_key = getattr(network, "private_key", None)
    if not private_key:
        return payload
    try:
        from eth_account import Account
        from eth_account.messages import encode_defunct

        signed = dict(payload)
        signed.pop("proposer_signature", None)
        signed["proposer"] = Account.from_key(private_key).address
        canonical = json.dumps(signed, sort_keys=True, separators=(",", ":"))
        signature = Account.sign_message(
            encode_defunct(text=canonical),
            private_key=private_key,
        )
        signed["proposer_signature"] = signature.signature.hex()
        return signed
    except Exception:
        logger.warning(
            "Failed to sign internal round-lifecycle payload; broadcasting "
            "unsigned (legacy shared-key path)",
            exc_info=True,
        )
        return payload


async def _broadcast_internal_round_sync(path: str, payload: dict[str, Any]) -> None:
    """Broadcast round state to peer validators when peer sync is configured."""
    network = get_champion_peer_network()
    if network is None or not getattr(network, "peers", None):
        return
    try:
        broadcast = getattr(network, "broadcast_json", None)
        if broadcast is None:
            return
        payload = _sign_internal_round_payload(network, payload)
        result = broadcast(path, payload)
        if inspect.isawaitable(result):
            await result
    except Exception:
        logger.warning("Internal solver round sync failed for %s", path, exc_info=True)


def _submission_to_champion_snapshot(submission: Any | None) -> ChampionSnapshot:
    """Convert an adopted submission into round-store champion metadata."""
    if submission is None:
        return ChampionSnapshot()
    return ChampionSnapshot(
        submission_id=submission.submission_id,
        image_id=submission.image_id,
        solver_name=submission.solver_name,
        solver_version=submission.solver_version,
        hotkey=submission.hotkey,
        activated_round_id=getattr(submission, "round_id", None),
        activated_epoch=int(getattr(submission, "epoch", 0) or 0),
        activated_at=float(getattr(submission, "updated_at", 0.0) or 0.0),
    )


def _sync_round_incumbent_from_submission_store(
    round_store,
    store,
) -> ChampionSnapshot:
    """Keep the round-store incumbent aligned with the currently adopted solver."""
    current = round_store.get_active_champion()
    adopted = store.get_champion()
    if adopted is None:
        return current

    snapshot = _submission_to_champion_snapshot(adopted)
    if snapshot.to_dict() != current.to_dict():
        round_store.set_active_champion(snapshot, sync_open_round=True)
    return snapshot


def _round_state_to_response(state: RoundState) -> SolverRoundResponse:
    certificate = state.certificate
    return SolverRoundResponse(
        round_id=state.round_id,
        status=state.status.value,
        accepting_submissions=state.accepting_submissions(),
        opened_epoch=state.opened_epoch,
        close_epoch=state.close_epoch,
        incumbent_submission_id=state.incumbent_submission_id,
        incumbent_image_id=state.incumbent_image_id,
        benchmark_pack_hash=state.benchmark_pack_hash,
        committee_block=state.committee_block,
        committee_hash=state.committee_hash,
        quorum_required=state.quorum_required,
        decision_deadline_epoch=state.decision_deadline_epoch,
        finalist_submission_id=state.finalist_submission_id,
        finalist_image_id=state.finalist_image_id,
        finalist_score=state.finalist_score,
        shadow_case_log_hash=state.shadow_case_log_hash,
        effective_epoch=state.effective_epoch,
        abort_reason=state.abort_reason,
        certificate_candidate_submission_id=(
            certificate.candidate_submission_id if certificate else None
        ),
        certificate_candidate_image_id=(
            certificate.candidate_image_id if certificate else None
        ),
        certificate_quorum_required=certificate.quorum_required if certificate else None,
        certificate_approvals=len(certificate.approvals) if certificate else 0,
    )


def _get_or_create_epoch_manager() -> Any:
    manager = get_epoch_manager()
    if manager is not None:
        return manager
    from minotaur_subnet.epoch.manager import EpochManager

    manager = EpochManager(
        submission_store=get_store(),
        round_store=get_round_store(),
    )
    set_epoch_manager(manager)
    return manager


def _get_current_solver_round(*, epoch_hint: int = 0) -> RoundState:
    """Return the current round, lazily creating the first open round."""
    store = get_store()
    round_store = get_round_store()
    incumbent = _sync_round_incumbent_from_submission_store(round_store, store)
    current = round_store.get_current_round()
    if current is None:
        current = round_store.ensure_open_round(
            opened_epoch=epoch_hint,
            incumbent=incumbent,
        )
    return current


def _require_open_submission_round(
    *,
    epoch_hint: int = 0,
    requested_round_id: str | None = None,
) -> RoundState:
    """Return the current open round or raise if submissions are closed."""
    current = _get_current_solver_round(epoch_hint=epoch_hint)
    if requested_round_id and requested_round_id != current.round_id:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Submission targets round {requested_round_id}, but current open "
                f"round is {current.round_id}"
            ),
        )
    if not current.accepting_submissions():
        raise HTTPException(
            status_code=409,
            detail=(
                f"Solver round {current.round_id} is {current.status.value}; "
                "new submissions are not accepted"
            ),
        )
    return current


def _close_solver_round_state(body: CloseRoundRequest) -> RoundState:
    """Internal helper to close the current solver round without HTTP context."""
    round_store = get_round_store()
    consensus_manager = get_champion_consensus_manager()
    current = _get_current_solver_round(epoch_hint=body.close_epoch)
    if body.round_id and body.round_id != current.round_id:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Requested round {body.round_id} does not match current round "
                f"{current.round_id}"
            ),
        )
    if current.status != RoundStatus.OPEN:
        raise HTTPException(
            status_code=409,
            detail=f"Round {current.round_id} is {current.status.value}, not open",
        )
    committee_hash = body.committee_hash
    if committee_hash is None and consensus_manager is not None:
        committee_hash = consensus_manager.committee_hash
    quorum_required = body.quorum_required
    if quorum_required is None and consensus_manager is not None:
        quorum_required = consensus_manager.quorum_required
    return round_store.close_current_round(
        close_epoch=body.close_epoch,
        benchmark_pack_hash=body.benchmark_pack_hash,
        committee_block=body.committee_block,
        committee_hash=committee_hash,
        quorum_required=quorum_required,
        decision_deadline_epoch=body.decision_deadline_epoch,
        effective_epoch=body.effective_epoch,
    )


def _sync_close_solver_round_state(body: CloseRoundRequest) -> RoundState:
    """Apply a leader-broadcast close to the local round store idempotently."""
    # Idempotency FIRST: if the round is already closed locally, a late/duplicate
    # close broadcast must NOT re-upsert its (now stale) snapshot over fresher
    # state. The very first close (round still OPEN) is the one that mirrors the
    # snapshot; followers never close a round on their own (close is leader-gated).
    round_store = get_round_store()
    existing = round_store.get_round(body.round_id or "")
    if existing is not None and existing.status != RoundStatus.OPEN:
        return existing
    # Mirror the leader's close-time submission snapshot so the local pack-hash
    # recompute matches the leader's (else PACK_HASH_MISMATCH drops us from the
    # round's quorum). Batch-persisted + best-effort (bad records skipped inside).
    if body.submissions:
        try:
            n = get_store().upsert_submissions(body.submissions)
            if n:
                logger.info(
                    "Submission snapshot: upserted %d records for round %s from leader close",
                    n, body.round_id,
                )
        except Exception:  # noqa: BLE001 — snapshot mirroring must not drop the close
            logger.warning("submission snapshot upsert failed", exc_info=True)
    return _close_solver_round_state(body)


def _round_certification_deadline_elapsed(round_state: RoundState) -> bool:
    """Return whether the round can no longer be certified."""
    if round_state.certificate is not None:
        return False
    if round_state.decision_deadline_epoch is None:
        return False
    return _current_solver_round_epoch() > int(round_state.decision_deadline_epoch)


def _close_round_sync_payload(state: RoundState) -> dict[str, Any]:
    """Serialize a closed round for peer sync."""
    payload: dict[str, Any] = {
        "round_id": state.round_id,
        "close_epoch": state.close_epoch,
        "benchmark_pack_hash": state.benchmark_pack_hash,
        "committee_block": state.committee_block,
        "committee_hash": state.committee_hash,
        "quorum_required": state.quorum_required,
        "decision_deadline_epoch": state.decision_deadline_epoch,
        "effective_epoch": state.effective_epoch,
    }
    # Same close-time submission snapshot as the coordinator-loop close path
    # (_close_sync_payload in startup.py), so the explicit /solver/round/close
    # route also lets followers reproduce the pack hash. Default-off.
    if _env_true("SUBMISSION_SNAPSHOT_SYNC", default=False):
        try:
            _subs = get_store().list_by_round(state.round_id)
            payload["submissions"] = [s.to_dict() for s in _subs]
        except Exception:
            logger.warning("close payload: submission snapshot failed", exc_info=True)
    return payload


def _certify_round_sync_payload(state: RoundState) -> dict[str, Any]:
    """Serialize a certified round for peer sync."""
    certificate = state.certificate
    return {
        "round_id": state.round_id,
        "candidate_submission_id": state.finalist_submission_id,
        "candidate_image_id": state.finalist_image_id,
        "committee_hash": state.committee_hash,
        "benchmark_pack_hash": state.benchmark_pack_hash,
        "shadow_case_log_hash": state.shadow_case_log_hash,
        "effective_epoch": state.effective_epoch or 0,
        "quorum_required": state.quorum_required or 0,
        "approvals": [
            {
                "validator_id": approval.validator_id,
                "timestamp": approval.timestamp,
                "signature": approval.signature,
                "committee_hash": approval.committee_hash,
                "incumbent_image_id": approval.incumbent_image_id,
                "candidate_submission_id": approval.candidate_submission_id,
                "candidate_image_id": approval.candidate_image_id,
                "benchmark_pack_hash": approval.benchmark_pack_hash,
                "shadow_case_log_hash": approval.shadow_case_log_hash,
                "effective_epoch": approval.effective_epoch,
            }
            for approval in (certificate.approvals if certificate is not None else [])
        ],
    }


def _activate_round_sync_payload(body: ActivateRoundRequest) -> dict[str, Any]:
    """Serialize an activation request for peer sync."""
    return {
        "round_id": body.round_id,
        "activation_epoch": body.activation_epoch,
    }


def _abort_round_sync_payload(state: RoundState) -> dict[str, Any]:
    """Serialize an aborted round for peer sync."""
    return {
        "round_id": state.round_id,
        "reason": state.abort_reason or "round_aborted",
    }


def _abort_solver_round_state(body: AbortRoundRequest) -> RoundState:
    """Abort a round locally without HTTP context."""
    round_store = get_round_store()
    round_state = round_store.get_round(body.round_id)
    if round_state is None:
        raise HTTPException(status_code=404, detail="Solver round not found")
    if round_state.status == RoundStatus.ACTIVATED:
        raise HTTPException(
            status_code=409,
            detail=f"Round {body.round_id} is activated and cannot be aborted",
        )
    if round_state.status == RoundStatus.CERTIFIED:
        raise HTTPException(
            status_code=409,
            detail=f"Round {body.round_id} is certified and cannot be aborted",
        )
    if round_state.status == RoundStatus.ABORTED:
        if body.reason and round_state.abort_reason != body.reason:
            round_state = round_store.abort_round(body.round_id, body.reason)
        return round_state
    return round_store.abort_round(body.round_id, body.reason)


def _sync_abort_solver_round_state(body: AbortRoundRequest) -> RoundState:
    """Apply a leader-broadcast abort to the local round store idempotently."""
    round_store = get_round_store()
    existing = round_store.get_round(body.round_id)
    if existing is not None and existing.status == RoundStatus.ABORTED:
        if body.reason and existing.abort_reason != body.reason:
            return round_store.abort_round(body.round_id, body.reason)
        return existing
    return _abort_solver_round_state(body)


async def _activate_solver_round_state(body: ActivateRoundRequest) -> dict[str, Any]:
    """Activate a certified round idempotently."""
    round_store = get_round_store()
    existing = round_store.get_round(body.round_id)
    if existing is not None and existing.status == RoundStatus.ACTIVATED:
        return {
            "round_id": body.round_id,
            "epoch": body.activation_epoch,
            "effective_epoch": existing.effective_epoch,
            "champion_changed": False,
            "new_champion": None,
            "next_round_id": round_store.get_current_round().round_id if round_store.get_current_round() else None,
            "weights_emitted": False,
            "status_after": existing.status.value,
        }
    manager = _get_or_create_epoch_manager()
    return await manager.activate_certified_round(
        body.round_id,
        epoch=body.activation_epoch,
    )
