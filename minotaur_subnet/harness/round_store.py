"""Durable solver round state store.

Tracks the currently open solver submission round plus the last activated
champion snapshot. This is the phase-1 foundation for closed-round solver
evaluation; later phases will add finalist, shadow, and certificate state.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import stat
import tempfile
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

# Cap on retained rounds in the in-memory/persisted store. Rounds close ~1 per
# tempo (~72 min), so this default keeps months of history while bounding the
# whole-file rewrite + parse. Eviction NEVER drops the current round or the
# standing/previous champion's activated round (their RoundState carries the
# certificate that /champion/reattest + /champion/sync-bundle serve — an evicted
# champion round 404s the follower re-adopt path → burn), nor the newest
# opened_epoch (so a same-epoch reopen can't find a reset per-epoch count in
# _build_round_id and mint a duplicate round_id). Every evicted round was already
# mirrored to the durable record_sink. Set <= 0 to disable bounding.
_ROUND_STORE_MAX_ROUNDS = int(os.environ.get("SOLVER_ROUND_STORE_MAX_ROUNDS", "2000"))


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
    shadow_case_log_hash: str | None = None
    certificate: ChampionCertificate | None = None
    effective_epoch: int | None = None
    # Real wall-clock epoch (unix//EPOCH_SECONDS) at which the LEADER opened this
    # round, stamped at open and broadcast for followers to adopt verbatim. This
    # exists ONLY to anchor the benchmark fork-pin: opened_epoch is the champion
    # ACTIVATION schedule (close_epoch + activation_delay, ~1 tempo in the FUTURE
    # for commit-reveal alignment), so anchoring the pin to it lands ~40 min ahead
    # and defers. benchmark_anchor_epoch is a recent-PAST time-epoch that
    # confirm-brackets immediately. Purely the fork-pin anchor source — gated by
    # BENCHMARK_ANCHOR_REAL_EPOCH (see api/startup._round_fork_anchor_epoch); until
    # armed it is inert. None on legacy rounds / rounds opened before this shipped.
    benchmark_anchor_epoch: int | None = None
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
    # The submission_ids the close-time LRU rotation selected onto the benched
    # slate (harness/rotation.apply_rotation_slate). RECORDED at close so it is
    # the single source of truth for "who gets benched this round" — the
    # benchmark worker's slate belt READS this instead of recomputing the
    # rotation, which raced the ledger it had already advanced (mark_selected)
    # and double-benched a disjoint trio (2026-07-08, round-e29724975-n1: 4
    # scored on 3 slots). None on rounds closed before this field existed /
    # rounds where rotation was disabled — the belt falls back to recomputation
    # only then.
    benched_slate: list[str] | None = None
    # Distributed-veto Phase 0 OBSERVE record (compact — counts + resolution,
    # never per-order rows). Written by the leader coordinator's non-blocking
    # observe pass; consumed only by /health and the durable soak record. Never
    # gates certification (Phase 0). See api/routes/submissions/veto_wire.py.
    veto_observe: dict[str, Any] | None = None

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
            "shadow_case_log_hash": self.shadow_case_log_hash,
            "certificate": self.certificate.to_dict() if self.certificate else None,
            "effective_epoch": self.effective_epoch,
            "benchmark_anchor_epoch": self.benchmark_anchor_epoch,
            "abort_reason": self.abort_reason,
            "self_verified": self.self_verified,
            "self_verified_submission_id": self.self_verified_submission_id,
            "benched_slate": (
                list(self.benched_slate) if self.benched_slate is not None else None
            ),
            "veto_observe": self.veto_observe,
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
            shadow_case_log_hash=raw.get("shadow_case_log_hash"),
            certificate=ChampionCertificate.from_dict(raw.get("certificate")),
            effective_epoch=raw.get("effective_epoch"),
            benchmark_anchor_epoch=raw.get("benchmark_anchor_epoch"),
            abort_reason=raw.get("abort_reason"),
            self_verified=bool(raw.get("self_verified")),
            self_verified_submission_id=raw.get("self_verified_submission_id"),
            benched_slate=(
                list(raw["benched_slate"])
                if isinstance(raw.get("benched_slate"), list) else None
            ),
            veto_observe=raw.get("veto_observe"),
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
        # Blind-spot REPEAT bar (relative_scoring.BLIND_SPOT_BAR_TTL_S): the
        # active champion's ADOPTION-TIME per-order delivered outputs —
        # {"submission_id", "outputs" ({intent_id: exact wei string}),
        # "activated_at"}. Written by EpochManager._hot_swap at adoption; its
        # OWN top-level key, NOT a ChampionSnapshot field — the snapshot is
        # rebuilt from the adopted submission (whose per_intent is overwritten
        # by every incumbent re-bench, so the bar is unrecoverable there) and
        # compared via to_dict() in _sync_round_incumbent_from_submission_store;
        # embedding the bar would make every comparison mismatch and clobber it
        # with None. Persisted so a watchtower restart doesn't disarm the guard
        # until the next adoption.
        self._champion_adoption_bar: dict[str, Any] = {}

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

    def get_champion_adoption_bar(self) -> dict[str, Any]:
        """The active champion's adoption-time bar record (blind-spot REPEAT
        guard) — ``{"submission_id", "outputs", "activated_at"}``, ``{}`` when
        never set. Feed to ``relative_scoring.bar_kwargs_from_record``."""
        self._maybe_reload()
        return copy.deepcopy(self._champion_adoption_bar)

    def set_champion_adoption_bar(
        self,
        *,
        submission_id: str | None,
        outputs: dict[str, str] | None,
        activated_at: float,
    ) -> None:
        """Record the adoption-time bar for the champion just activated.

        Call alongside ``set_active_champion`` (EpochManager._hot_swap). An
        empty/None ``outputs`` still overwrites — a champion adopted without
        rows must CLEAR the displaced champion's bar, never inherit it (the
        record is matched to the incumbent by submission_id at read time as the
        second guard)."""
        self._maybe_reload()
        self._champion_adoption_bar = {
            "submission_id": submission_id,
            "outputs": dict(outputs or {}),
            "activated_at": float(activated_at),
        }
        self._persist()

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
        # Stamp the real wall-clock epoch at open as the fork-pin anchor source.
        # opened_epoch is the (future) champion-activation schedule, so it is NOT a
        # valid pin anchor; this is. Fleet-broadcast + adopted so every validator
        # anchors identically. See RoundState.benchmark_anchor_epoch.
        from minotaur_subnet.epoch.clock import EPOCH_SECONDS
        state = RoundState(
            round_id=round_id,
            status=RoundStatus.OPEN,
            opened_epoch=opened_epoch,
            benchmark_anchor_epoch=int(now // EPOCH_SECONDS),
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

    def set_round_veto_observe(
        self, round_id: str, summary: dict[str, Any] | None,
    ) -> RoundState:
        """Store the round's distributed-veto Phase-0 observe record (compact
        counts + resolution). Best-effort durability for /health and the soak;
        never gates anything. Missing round is a no-op (the round may have been
        pruned/reopened by the time an observe pass resolves)."""
        self._maybe_reload()
        state = self._rounds.get(round_id)
        if state is None:
            return RoundState(round_id=round_id)
        state.veto_observe = summary
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
        shadow_case_log_hash: str | None = None,
    ) -> RoundState:
        self._maybe_reload()
        state = self._rounds.get(round_id)
        if state is None:
            raise KeyError(f"Round not found: {round_id}")
        state.finalist_submission_id = submission_id
        state.finalist_image_id = image_id
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

    def set_benched_slate(self, round_id: str, submission_ids: list[str]) -> RoundState:
        """Record the close-time rotation's selected slate — the single source of
        truth for which submissions get benched this round.

        Written by ``apply_rotation_slate`` right after selection so the
        benchmark worker's belt reads THIS instead of recomputing the rotation
        against a ledger the close already advanced (the double-bench race).
        Idempotent; stores a copy."""
        self._maybe_reload()
        state = self._rounds.get(round_id)
        if state is None:
            raise KeyError(f"Round not found: {round_id}")
        state.benched_slate = list(submission_ids)
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

    def _evict_rounds(self) -> None:
        """Bound ``_rounds`` to ``_ROUND_STORE_MAX_ROUNDS``, keeping the most
        recent rounds plus a protected pin set. Rebinds ``_rounds`` (copy-on-
        write) rather than mutating in place, so a lock-free reader's dict view
        is never changed mid-iteration."""
        cap = _ROUND_STORE_MAX_ROUNDS
        if cap <= 0 or len(self._rounds) <= cap:
            return
        # Protected regardless of age: the current round, the standing and
        # previous champion's activated rounds (their certificate lives here and
        # the reattest / sync-bundle serve paths 404 without it), and every round
        # in the newest opened_epoch (guarantees _build_round_id's per-epoch
        # count is never reset by eviction → no duplicate round_id).
        pinned = {
            self._current_round_id,
            self._active_champion.activated_round_id,
            self._previous_champion.activated_round_id,
        }
        pinned.discard(None)
        max_epoch = max((s.opened_epoch for s in self._rounds.values()), default=0)
        protected = {
            rid for rid, s in self._rounds.items()
            if rid in pinned or s.opened_epoch >= max_epoch
        }
        survivors = set(protected)
        room = cap - len(protected)
        if room > 0:
            ranked = sorted(
                (rid for rid in self._rounds if rid not in protected),
                key=lambda rid: (
                    self._rounds[rid].opened_epoch,
                    self._rounds[rid].created_at,
                    rid,
                ),
                reverse=True,
            )
            survivors.update(ranked[:room])
        if len(survivors) >= len(self._rounds):
            return
        self._rounds = {
            rid: s for rid, s in self._rounds.items() if rid in survivors
        }

    def _persist(self) -> None:
        if self._persist_path is None:
            return
        try:
            self._evict_rounds()
            data = {
                "current_round_id": self._current_round_id,
                "active_champion": self._active_champion.to_dict(),
                "previous_champion": self._previous_champion.to_dict(),
                "champion_adoption_bar": self._champion_adoption_bar,
                "rounds": {
                    round_id: state.to_dict()
                    for round_id, state in self._rounds.items()
                },
            }
            self._persist_path.parent.mkdir(parents=True, exist_ok=True)
            # Atomic write: a crash (or a concurrent _load reader) mid-write must
            # never observe a truncated / half-written round store — that would
            # lose or corrupt the leader's round + champion state on restart.
            # Write to a UNIQUE temp file in the SAME directory (so the rename stays
            # on one filesystem), fsync it durable, then os.replace over the target
            # (atomic on POSIX: a reader sees either the whole old file or the whole
            # new one, never a partial). mkstemp gives a unique name so two
            # overlapping persists can never share/truncate one temp inode and rename
            # a corrupt file into place (a fixed ".name.tmp" left that latent hazard).
            parent = self._persist_path.parent
            fd, tmp_path = tempfile.mkstemp(
                dir=str(parent), prefix=f".{self._persist_path.name}.", suffix=".tmp",
            )
            try:
                with os.fdopen(fd, "w") as fh:
                    fh.write(json.dumps(data, indent=2))
                    fh.flush()
                    os.fsync(fh.fileno())
                # mkstemp creates the temp 0600; match the target's mode so a replace
                # never silently narrows a custom/group-readable permission. On the
                # FIRST persist there is no target to stat — fall back to 0644 (the
                # umask-default the old write_text produced) so we don't ship a
                # 0600 store that a co-located reader can't open.
                try:
                    target_mode = stat.S_IMODE(self._persist_path.stat().st_mode)
                except OSError:
                    target_mode = 0o644
                try:
                    os.chmod(tmp_path, target_mode)
                except OSError:
                    pass
                os.replace(tmp_path, self._persist_path)
                # fsync the PARENT DIR so the rename itself is crash-durable —
                # os.replace is atomic, but the directory entry isn't guaranteed on
                # disk until the dir is fsync'd, so a hard power-loss could otherwise
                # roll back to the prior (whole, valid) file. Best-effort: not every
                # platform/filesystem permits a directory fsync.
                try:
                    dir_fd = os.open(str(parent), os.O_RDONLY)
                    try:
                        os.fsync(dir_fd)
                    finally:
                        os.close(dir_fd)
                except OSError:
                    pass
            finally:
                # On any failure the partial temp must not linger; on success it
                # was renamed away and this is a no-op.
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
            self._persist_mtime_ns = self._persist_path.stat().st_mtime_ns
        except Exception as exc:
            logger.warning("Failed to persist round store: %s", exc)

    def _sweep_orphan_temps(self) -> None:
        """Remove leftover ``.<name>.<rand>.tmp`` files from a crash between
        mkstemp and os.replace. Temp names are unique (mkstemp), so without this
        they'd accumulate across crashes. Best-effort; never raises."""
        if self._persist_path is None:
            return
        try:
            pattern = f".{self._persist_path.name}.*.tmp"
            for stale in self._persist_path.parent.glob(pattern):
                try:
                    stale.unlink()
                except OSError:
                    pass
        except OSError:
            pass

    def _load(self) -> None:
        try:
            # Clean up any orphan temp files from a prior crashed persist before
            # loading (the unique-temp-name scheme would otherwise let them pile up).
            self._sweep_orphan_temps()
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
            bar_raw = data.get("champion_adoption_bar")
            self._champion_adoption_bar = bar_raw if isinstance(bar_raw, dict) else {}
            self._rounds = rounds
            self._persist_mtime_ns = self._persist_path.stat().st_mtime_ns
            logger.info(
                "Loaded %d solver rounds from %s",
                len(self._rounds),
                self._persist_path,
            )
        except Exception as exc:
            logger.warning("Failed to load round store: %s", exc)
