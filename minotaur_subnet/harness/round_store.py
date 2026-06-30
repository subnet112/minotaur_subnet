"""Durable solver round state store.

Tracks the currently open solver submission round plus the last activated
champion snapshot. This is the phase-1 foundation for closed-round solver
evaluation; later phases will add finalist, shadow, and certificate state.
"""

from __future__ import annotations

import copy
import json
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)


class RoundStatus(str, Enum):
    """Lifecycle state for a solver submission round."""

    OPEN = "open"
    CLOSED = "closed"
    REPLAYING = "replaying"
    SHADOWING = "shadowing"
    CERTIFYING = "certifying"
    CERTIFIED = "certified"
    ACTIVATED = "activated"
    ABORTED = "aborted"


@dataclass
class ChampionSnapshot:
    """Minimal metadata for the last activated champion artifact."""

    submission_id: str | None = None
    image_id: str | None = None
    image_digest: str | None = None
    solver_name: str | None = None
    solver_version: str | None = None
    hotkey: str | None = None
    activated_round_id: str | None = None
    activated_epoch: int = 0
    activated_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "submission_id": self.submission_id,
            "image_id": self.image_id,
            "image_digest": self.image_digest,
            "solver_name": self.solver_name,
            "solver_version": self.solver_version,
            "hotkey": self.hotkey,
            "activated_round_id": self.activated_round_id,
            "activated_epoch": self.activated_epoch,
            "activated_at": self.activated_at,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> "ChampionSnapshot":
        data = raw or {}
        return cls(
            submission_id=data.get("submission_id"),
            image_id=data.get("image_id"),
            image_digest=data.get("image_digest"),
            solver_name=data.get("solver_name"),
            solver_version=data.get("solver_version"),
            hotkey=data.get("hotkey"),
            activated_round_id=data.get("activated_round_id"),
            activated_epoch=int(data.get("activated_epoch") or 0),
            activated_at=float(data.get("activated_at") or 0.0),
        )


@dataclass
class ChampionApproval:
    """Validator approval envelope for champion certification.

    commit_hash, nonce, and deadline are part of the signed EIP-712 digest
    (v2 of the ChampionApproval struct). See ChampionRegistry.sol.
    """

    validator_id: str
    round_id: str
    committee_hash: str | None = None
    incumbent_image_id: str | None = None
    candidate_submission_id: str | None = None
    candidate_image_id: str | None = None
    benchmark_pack_hash: str | None = None
    shadow_case_log_hash: str | None = None
    effective_epoch: int = 0
    # Signed replay-protection + commit-binding fields.
    commit_hash: str | None = None
    nonce: int = 0
    deadline: int = 0
    # Envelope metadata (not signed).
    timestamp: float = 0.0
    signature: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "validator_id": self.validator_id,
            "round_id": self.round_id,
            "committee_hash": self.committee_hash,
            "incumbent_image_id": self.incumbent_image_id,
            "candidate_submission_id": self.candidate_submission_id,
            "candidate_image_id": self.candidate_image_id,
            "benchmark_pack_hash": self.benchmark_pack_hash,
            "shadow_case_log_hash": self.shadow_case_log_hash,
            "effective_epoch": self.effective_epoch,
            "commit_hash": self.commit_hash,
            "nonce": self.nonce,
            "deadline": self.deadline,
            "timestamp": self.timestamp,
            "signature": self.signature,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> "ChampionApproval":
        data = raw or {}
        return cls(
            validator_id=str(data.get("validator_id") or ""),
            round_id=str(data.get("round_id") or ""),
            committee_hash=data.get("committee_hash"),
            incumbent_image_id=data.get("incumbent_image_id"),
            candidate_submission_id=data.get("candidate_submission_id"),
            candidate_image_id=data.get("candidate_image_id"),
            benchmark_pack_hash=data.get("benchmark_pack_hash"),
            shadow_case_log_hash=data.get("shadow_case_log_hash"),
            effective_epoch=int(data.get("effective_epoch") or 0),
            commit_hash=data.get("commit_hash"),
            nonce=int(data.get("nonce") or 0),
            deadline=int(data.get("deadline") or 0),
            timestamp=float(data.get("timestamp") or 0.0),
            signature=str(data.get("signature") or ""),
        )


@dataclass
class ChampionCertificate:
    """Quorum certificate authorizing a round finalist to become champion."""

    round_id: str
    committee_hash: str | None = None
    candidate_submission_id: str | None = None
    candidate_image_id: str | None = None
    incumbent_image_id: str | None = None
    benchmark_pack_hash: str | None = None
    shadow_case_log_hash: str | None = None
    effective_epoch: int = 0
    quorum_required: int = 0
    approvals: list[ChampionApproval] = field(default_factory=list)
    certified_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "round_id": self.round_id,
            "committee_hash": self.committee_hash,
            "candidate_submission_id": self.candidate_submission_id,
            "candidate_image_id": self.candidate_image_id,
            "incumbent_image_id": self.incumbent_image_id,
            "benchmark_pack_hash": self.benchmark_pack_hash,
            "shadow_case_log_hash": self.shadow_case_log_hash,
            "effective_epoch": self.effective_epoch,
            "quorum_required": self.quorum_required,
            "approvals": [approval.to_dict() for approval in self.approvals],
            "certified_at": self.certified_at,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> "ChampionCertificate | None":
        if raw is None:
            return None
        return cls(
            round_id=str(raw.get("round_id") or ""),
            committee_hash=raw.get("committee_hash"),
            candidate_submission_id=raw.get("candidate_submission_id"),
            candidate_image_id=raw.get("candidate_image_id"),
            incumbent_image_id=raw.get("incumbent_image_id"),
            benchmark_pack_hash=raw.get("benchmark_pack_hash"),
            shadow_case_log_hash=raw.get("shadow_case_log_hash"),
            effective_epoch=int(raw.get("effective_epoch") or 0),
            quorum_required=int(raw.get("quorum_required") or 0),
            approvals=[
                ChampionApproval.from_dict(item)
                for item in (raw.get("approvals") or [])
            ],
            certified_at=float(raw.get("certified_at") or 0.0),
        )


@dataclass
class RoundState:
    """Persisted metadata about a solver submission round."""

    round_id: str
    status: RoundStatus = RoundStatus.OPEN
    opened_epoch: int = 0
    close_epoch: int | None = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    incumbent_submission_id: str | None = None
    incumbent_image_id: str | None = None
    incumbent_hotkey: str | None = None
    benchmark_pack_hash: str | None = None
    committee_block: int | None = None
    committee_hash: str | None = None
    # Canonical per-chain benchmark fork pins for the round ({chain_id: block}),
    # derived from the round anchor. Default None = legacy/live-head. Folded into
    # benchmark_pack_hash when ROUND_ANCHORED_PIN is enabled. See
    # consensus/round_anchor.py.
    fork_pins: dict[int, int] | None = None
    quorum_required: int | None = None
    decision_deadline_epoch: int | None = None
    finalist_submission_id: str | None = None
    finalist_image_id: str | None = None
    finalist_score: float | None = None
    shadow_case_log_hash: str | None = None
    certificate: ChampionCertificate | None = None
    effective_epoch: int | None = None
    abort_reason: str | None = None
    # Set True ONLY when THIS node independently re-benchmarked the round's
    # candidate and its own verdict agreed (the reactive-benchmark APPROVE path),
    # never on a blind-sign or builtin. Persisted so it survives the sign→activate
    # gap. Read at activate time to gate follower self-adoption of champion weights
    # (FOLLOWER_CHAMPION_WEIGHT_ADOPT) — a follower must only weight a champion it
    # itself verified. See epoch/manager.activate_certified_round.
    self_verified: bool = False
    # The submission_id this node actually re-benchmarked + verified (set together with
    # self_verified). The follower self-adopt gate requires this to MATCH the
    # certificate's candidate, so a round that proposed A (which the follower verified)
    # but later certifies a DIFFERENT candidate B can never make the follower weight B
    # it never benchmarked (closes the quorum>1 propose-A / certify-B gap).
    self_verified_submission_id: str | None = None

    def accepting_submissions(self) -> bool:
        return self.status == RoundStatus.OPEN

    def sync_incumbent(self, champion: ChampionSnapshot | None) -> None:
        snapshot = champion or ChampionSnapshot()
        self.incumbent_submission_id = snapshot.submission_id
        self.incumbent_image_id = snapshot.image_id
        self.incumbent_hotkey = snapshot.hotkey
        self.updated_at = time.time()

    def to_dict(self) -> dict[str, Any]:
        return {
            "round_id": self.round_id,
            "status": self.status.value,
            "opened_epoch": self.opened_epoch,
            "close_epoch": self.close_epoch,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "incumbent_submission_id": self.incumbent_submission_id,
            "incumbent_image_id": self.incumbent_image_id,
            "incumbent_hotkey": self.incumbent_hotkey,
            "benchmark_pack_hash": self.benchmark_pack_hash,
            "committee_block": self.committee_block,
            "committee_hash": self.committee_hash,
            "fork_pins": (
                {str(k): int(v) for k, v in self.fork_pins.items()}
                if self.fork_pins else None
            ),
            "quorum_required": self.quorum_required,
            "decision_deadline_epoch": self.decision_deadline_epoch,
            "finalist_submission_id": self.finalist_submission_id,
            "finalist_image_id": self.finalist_image_id,
            "finalist_score": self.finalist_score,
            "shadow_case_log_hash": self.shadow_case_log_hash,
            "certificate": self.certificate.to_dict() if self.certificate else None,
            "effective_epoch": self.effective_epoch,
            "abort_reason": self.abort_reason,
            "self_verified": self.self_verified,
            "self_verified_submission_id": self.self_verified_submission_id,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "RoundState":
        return cls(
            round_id=raw["round_id"],
            status=RoundStatus(raw.get("status", RoundStatus.OPEN.value)),
            opened_epoch=int(raw.get("opened_epoch") or 0),
            close_epoch=raw.get("close_epoch"),
            created_at=float(raw.get("created_at") or time.time()),
            updated_at=float(raw.get("updated_at") or time.time()),
            incumbent_submission_id=raw.get("incumbent_submission_id"),
            incumbent_image_id=raw.get("incumbent_image_id"),
            incumbent_hotkey=raw.get("incumbent_hotkey"),
            benchmark_pack_hash=raw.get("benchmark_pack_hash"),
            committee_block=raw.get("committee_block"),
            committee_hash=raw.get("committee_hash"),
            fork_pins=(
                {int(k): int(v) for k, v in raw["fork_pins"].items()}
                if raw.get("fork_pins") else None
            ),
            quorum_required=raw.get("quorum_required"),
            decision_deadline_epoch=raw.get("decision_deadline_epoch"),
            finalist_submission_id=raw.get("finalist_submission_id"),
            finalist_image_id=raw.get("finalist_image_id"),
            finalist_score=raw.get("finalist_score"),
            shadow_case_log_hash=raw.get("shadow_case_log_hash"),
            certificate=ChampionCertificate.from_dict(raw.get("certificate")),
            effective_epoch=raw.get("effective_epoch"),
            abort_reason=raw.get("abort_reason"),
            self_verified=bool(raw.get("self_verified")),
            self_verified_submission_id=raw.get("self_verified_submission_id"),
        )


class RoundStore:
    """In-memory round store with optional JSON persistence."""

    def __init__(
        self,
        persist_path: Path | None = None,
        record_sink: Callable[[RoundState], None] | None = None,
    ) -> None:
        self._persist_path = persist_path
        # Best-effort mirror of each round mutation to durable history (e.g. the
        # order-book DB). NEVER affects round state — failures are swallowed.
        self._record_sink = record_sink
        self._persist_mtime_ns: int | None = None
        self._rounds: dict[str, RoundState] = {}
        self._current_round_id: str | None = None
        self._active_champion = ChampionSnapshot()
        # The champion that was active immediately BEFORE the current one — the
        # one-step-undo target for an emergency revert (see
        # EpochManager.revert_to_previous_champion). Persisted so the rollback
        # target survives a restart.
        self._previous_champion = ChampionSnapshot()

        if persist_path and persist_path.exists():
            self._load()

    def get_current_round(self) -> RoundState | None:
        self._maybe_reload()
        current = self._get_current_round_ref()
        return copy.deepcopy(current) if current is not None else None

    def get_round(self, round_id: str) -> RoundState | None:
        self._maybe_reload()
        state = self._rounds.get(round_id)
        return copy.deepcopy(state) if state is not None else None

    def list_rounds(self) -> list[RoundState]:
        self._maybe_reload()
        rounds = sorted(self._rounds.values(), key=lambda r: (r.created_at, r.round_id))
        return [copy.deepcopy(state) for state in rounds]

    def get_active_champion(self) -> ChampionSnapshot:
        self._maybe_reload()
        return copy.deepcopy(self._active_champion)

    def set_active_champion(
        self,
        champion: ChampionSnapshot,
        *,
        sync_open_round: bool = True,
    ) -> ChampionSnapshot:
        self._maybe_reload()
        self._active_champion = copy.deepcopy(champion)
        current = self._get_current_round_ref()
        if sync_open_round and current is not None and current.status == RoundStatus.OPEN:
            current.sync_incumbent(champion)
        self._persist()
        return self.get_active_champion()

    def get_previous_champion(self) -> ChampionSnapshot:
        """The champion active immediately before the current one (rollback target)."""
        self._maybe_reload()
        return copy.deepcopy(self._previous_champion)

    def set_previous_champion(self, champion: ChampionSnapshot) -> ChampionSnapshot:
        """Record the rollback target — the champion being displaced by an adoption."""
        self._maybe_reload()
        self._previous_champion = copy.deepcopy(champion)
        self._persist()
        return self.get_previous_champion()

    def ensure_open_round(
        self,
        *,
        opened_epoch: int,
        incumbent: ChampionSnapshot | None = None,
    ) -> RoundState:
        self._maybe_reload()
        current = self._get_current_round_ref()
        if current is not None and current.status == RoundStatus.OPEN:
            if incumbent is not None:
                self._active_champion = copy.deepcopy(incumbent)
                current.sync_incumbent(incumbent)
                self._persist()
                self._record(current)
            return copy.deepcopy(current)

        now = time.time()
        round_id = self._build_round_id(opened_epoch)
        state = RoundState(
            round_id=round_id,
            status=RoundStatus.OPEN,
            opened_epoch=opened_epoch,
            created_at=now,
            updated_at=now,
        )
        if incumbent is not None:
            self._active_champion = copy.deepcopy(incumbent)
            state.sync_incumbent(incumbent)
        self._rounds[round_id] = state
        self._current_round_id = round_id
        self._persist()
        self._record(state)
        return copy.deepcopy(state)

    def adopt_round(
        self,
        *,
        round_id: str,
        opened_epoch: int,
        status: RoundStatus,
        incumbent: ChampionSnapshot | None = None,
        **field_updates: Any,
    ) -> RoundState:
        """Adopt a leader's round verbatim by its broadcast round_id.

        Used by a follower that is BEHIND the leader: it cannot reconstruct the
        leader's exact round_id locally, so it takes the leader's round_id (from
        an already-authenticated lifecycle broadcast) and materializes it in the
        target ``status`` with the broadcast fields. A stale current OPEN round
        whose id differs is superseded (aborted) so it can't shadow the adopted
        one. Only ``field_updates`` values that are not None AND name a real
        ``RoundState`` field are applied; unknown keys are skipped (logged at
        debug) so a buggy/hostile caller can't set arbitrary attributes.
        """
        self._maybe_reload()
        now = time.time()

        # Supersede a stale current OPEN round so it can't keep masquerading as
        # the live round once we adopt the leader's newer one.
        current = self._get_current_round_ref()
        if (
            current is not None
            and current.status == RoundStatus.OPEN
            and current.round_id != round_id
        ):
            current.status = RoundStatus.ABORTED
            current.abort_reason = f"superseded by leader round {round_id}"
            current.updated_at = now
            self._record(current)

        state = self._rounds.get(round_id)
        if state is None:
            state = RoundState(
                round_id=round_id,
                status=status,
                opened_epoch=opened_epoch,
                created_at=now,
                updated_at=now,
            )
        else:
            state.opened_epoch = opened_epoch
            state.status = status
            state.updated_at = now

        allowed_fields = RoundState.__dataclass_fields__
        for key, value in field_updates.items():
            if value is None:
                continue
            if key not in allowed_fields:
                logger.debug("adopt_round: ignoring unknown field %r", key)
                continue
            setattr(state, key, value)
        if incumbent is not None:
            self._active_champion = copy.deepcopy(incumbent)
            state.sync_incumbent(incumbent)

        self._rounds[round_id] = state
        self._current_round_id = round_id
        self._persist()
        self._record(state)
        return copy.deepcopy(state)

    def close_current_round(
        self,
        *,
        close_epoch: int,
        benchmark_pack_hash: str | None = None,
        committee_block: int | None = None,
        committee_hash: str | None = None,
        quorum_required: int | None = None,
        decision_deadline_epoch: int | None = None,
        effective_epoch: int | None = None,
    ) -> RoundState:
        self._maybe_reload()
        current = self._require_current_round()
        current.close_epoch = close_epoch
        current.status = RoundStatus.CLOSED
        if benchmark_pack_hash is not None:
            current.benchmark_pack_hash = benchmark_pack_hash
        if committee_block is not None:
            current.committee_block = committee_block
        if committee_hash is not None:
            current.committee_hash = committee_hash
        if quorum_required is not None:
            current.quorum_required = quorum_required
        if decision_deadline_epoch is not None:
            current.decision_deadline_epoch = decision_deadline_epoch
        if effective_epoch is not None:
            current.effective_epoch = effective_epoch
        current.updated_at = time.time()
        self._persist()
        self._record(current)
        return copy.deepcopy(current)

    def set_round_status(self, round_id: str, status: RoundStatus) -> RoundState:
        self._maybe_reload()
        state = self._rounds.get(round_id)
        if state is None:
            raise KeyError(f"Round not found: {round_id}")
        state.status = status
        state.updated_at = time.time()
        self._persist()
        self._record(state)
        return copy.deepcopy(state)

    def set_round_finalist(
        self,
        round_id: str,
        *,
        submission_id: str,
        image_id: str | None,
        benchmark_score: float | None = None,
        shadow_case_log_hash: str | None = None,
    ) -> RoundState:
        self._maybe_reload()
        state = self._rounds.get(round_id)
        if state is None:
            raise KeyError(f"Round not found: {round_id}")
        state.finalist_submission_id = submission_id
        state.finalist_image_id = image_id
        state.finalist_score = benchmark_score
        if shadow_case_log_hash is not None:
            state.shadow_case_log_hash = shadow_case_log_hash
        state.status = RoundStatus.CERTIFYING
        state.updated_at = time.time()
        self._persist()
        self._record(state)
        return copy.deepcopy(state)

    def set_round_fork_pins(
        self, round_id: str, pins: dict[int, int] | None,
    ) -> RoundState:
        """Store the round's canonical per-chain benchmark fork pins.

        Set by the leader (and, independently, each follower) before the
        benchmark_pack_hash is computed, so the pins enter the hash. ``None`` or empty
        clears them (legacy / live-head). Once a non-empty pin is set it is FIXED: a later
        call that would overwrite it with a DIFFERENT non-None value is refused — the pin is
        anchored at opened_epoch and already folded into the signed hash.
        """
        self._maybe_reload()
        state = self._rounds.get(round_id)
        if state is None:
            raise KeyError(f"Round not found: {round_id}")
        new_pins = {int(k): int(v) for k, v in pins.items()} if pins else None
        # Idempotent guard: refuse a DIFFERING non-None overwrite of an already-set pin.
        # The pin is fixed at opened_epoch; silently changing it after it was folded into
        # the signed pack hash would split scored-pin != hashed-pin across the fleet.
        # Clearing to None stays allowed (gate-off / live-head). With every derivation site
        # on opened_epoch the value is identical anyway — this is a consensus-safety backstop.
        existing = getattr(state, "fork_pins", None)
        if existing and new_pins is not None and new_pins != existing:
            logger.warning(
                "fork-pins: refusing to overwrite round %s pins %s with differing %s "
                "(pin is fixed once set)",
                round_id, existing, new_pins,
            )
            return copy.deepcopy(state)
        state.fork_pins = new_pins
        state.updated_at = time.time()
        self._persist()
        self._record(state)
        return copy.deepcopy(state)

    def certify_round(
        self,
        round_id: str,
        certificate: ChampionCertificate,
    ) -> RoundState:
        self._maybe_reload()
        state = self._rounds.get(round_id)
        if state is None:
            raise KeyError(f"Round not found: {round_id}")
        state.certificate = copy.deepcopy(certificate)
        state.finalist_submission_id = certificate.candidate_submission_id
        state.finalist_image_id = certificate.candidate_image_id
        state.shadow_case_log_hash = certificate.shadow_case_log_hash
        state.committee_hash = certificate.committee_hash
        state.benchmark_pack_hash = certificate.benchmark_pack_hash
        state.quorum_required = certificate.quorum_required
        state.effective_epoch = certificate.effective_epoch
        state.status = RoundStatus.CERTIFIED
        state.updated_at = time.time()
        self._persist()
        self._record(state)
        return copy.deepcopy(state)

    def activate_round(self, round_id: str, *, effective_epoch: int) -> RoundState:
        self._maybe_reload()
        state = self._rounds.get(round_id)
        if state is None:
            raise KeyError(f"Round not found: {round_id}")
        state.status = RoundStatus.ACTIVATED
        state.effective_epoch = effective_epoch
        state.abort_reason = None
        state.updated_at = time.time()
        self._persist()
        self._record(state)
        return copy.deepcopy(state)

    def open_next_round(
        self,
        *,
        opened_epoch: int,
        incumbent: ChampionSnapshot | None = None,
    ) -> RoundState:
        self._maybe_reload()
        current = self._get_current_round_ref()
        if current is not None and current.status == RoundStatus.OPEN:
            raise ValueError(f"Round {current.round_id} is still open")
        return self.ensure_open_round(opened_epoch=opened_epoch, incumbent=incumbent)

    def abort_round(self, round_id: str, reason: str) -> RoundState:
        self._maybe_reload()
        state = self._rounds.get(round_id)
        if state is None:
            raise KeyError(f"Round not found: {round_id}")
        state.status = RoundStatus.ABORTED
        state.abort_reason = reason
        state.updated_at = time.time()
        self._persist()
        self._record(state)
        return copy.deepcopy(state)

    def mark_self_verified(self, round_id: str, submission_id: str | None = None) -> RoundState:
        """Record that THIS node independently re-benchmarked the round's candidate
        (``submission_id``) and its OWN verdict agreed (the reactive-benchmark APPROVE
        path — never a blind-sign or builtin). Binds the verification to the specific
        candidate so the activate gate can require it to match the certified candidate.
        Persisted so it survives the sign→activate gap; read at activate time to gate
        follower self-adoption of champion weights. Idempotent."""
        self._maybe_reload()
        state = self._rounds.get(round_id)
        if state is None:
            raise KeyError(f"Round not found: {round_id}")
        state.self_verified = True
        state.self_verified_submission_id = submission_id
        state.updated_at = time.time()
        self._persist()
        return copy.deepcopy(state)

    def _get_current_round_ref(self) -> RoundState | None:
        if not self._current_round_id:
            return None
        return self._rounds.get(self._current_round_id)

    def _require_current_round(self) -> RoundState:
        current = self._get_current_round_ref()
        if current is None:
            raise ValueError("No current solver round")
        return current

    def _build_round_id(self, opened_epoch: int) -> str:
        count = sum(1 for state in self._rounds.values() if state.opened_epoch == opened_epoch)
        return f"round-e{opened_epoch}-n{count + 1}"

    def _maybe_reload(self) -> None:
        """Refresh persisted round state when another process updates the file."""
        if self._persist_path is None or not self._persist_path.exists():
            return
        try:
            current_mtime_ns = self._persist_path.stat().st_mtime_ns
        except OSError:
            return
        if self._persist_mtime_ns is None or current_mtime_ns > self._persist_mtime_ns:
            self._load()

    def _record(self, state: RoundState) -> None:
        """Best-effort mirror of a round mutation to the durable history sink
        (e.g. the order-book DB). MUST never raise — round consensus does not
        depend on history recording."""
        if self._record_sink is None:
            return
        try:
            self._record_sink(state)
        except Exception as exc:  # noqa: BLE001 — history is best-effort
            logger.warning(
                "round history record failed for %s: %s",
                getattr(state, "round_id", "?"), exc,
            )

    def _persist(self) -> None:
        if self._persist_path is None:
            return
        try:
            data = {
                "current_round_id": self._current_round_id,
                "active_champion": self._active_champion.to_dict(),
                "previous_champion": self._previous_champion.to_dict(),
                "rounds": {
                    round_id: state.to_dict()
                    for round_id, state in self._rounds.items()
                },
            }
            self._persist_path.parent.mkdir(parents=True, exist_ok=True)
            self._persist_path.write_text(json.dumps(data, indent=2))
            self._persist_mtime_ns = self._persist_path.stat().st_mtime_ns
        except Exception as exc:
            logger.warning("Failed to persist round store: %s", exc)

    def _load(self) -> None:
        try:
            data = json.loads(self._persist_path.read_text())
            current_round_id = data.get("current_round_id")
            active_champion = ChampionSnapshot.from_dict(data.get("active_champion"))
            previous_champion = ChampionSnapshot.from_dict(data.get("previous_champion"))
            rounds_raw = data.get("rounds", {}) or {}
            rounds: dict[str, RoundState] = {}
            for round_id, raw in rounds_raw.items():
                payload = dict(raw)
                payload.setdefault("round_id", round_id)
                rounds[round_id] = RoundState.from_dict(payload)
            if current_round_id not in rounds:
                current_round_id = None
            self._current_round_id = current_round_id
            self._active_champion = active_champion
            self._previous_champion = previous_champion
            self._rounds = rounds
            self._persist_mtime_ns = self._persist_path.stat().st_mtime_ns
            logger.info(
                "Loaded %d solver rounds from %s",
                len(self._rounds),
                self._persist_path,
            )
        except Exception as exc:
            logger.warning("Failed to load round store: %s", exc)
