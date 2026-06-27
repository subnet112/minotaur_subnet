"""In-memory submission state store with optional JSON persistence.

Tracks solver submissions through their lifecycle:
    queued → screening_stage_1 → screening_stage_2 → screening_stage_3
    → benchmarking → scored → adopted
                                    (or → rejected at any stage)

Submissions are uniquely scoped by `(hotkey, round_id)`. The older `epoch`
field is kept for compatibility and reporting while round-based intake is
rolled out.

Usage:
    store = SubmissionStore()
    sub = store.create("https://github.com/user/solver", "abc123", epoch=42, hotkey="5Gxyz...")
    store.update_status(sub.submission_id, SubmissionStatus.SCREENING_STAGE_1)
    store.set_screening_result(sub.submission_id, screening_result)
"""

from __future__ import annotations

import functools
import json
import logging
import os
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX (e.g. Windows)
    # Without fcntl we cannot take a cross-process advisory lock; the store
    # degrades to in-process locking only (its historical behaviour).
    fcntl = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)


class SubmissionStatus(str, Enum):
    """Submission lifecycle states."""
    QUEUED = "queued"
    SCREENING_STAGE_1 = "screening_stage_1"
    SCREENING_STAGE_2 = "screening_stage_2"
    SCREENING_STAGE_3 = "screening_stage_3"
    BENCHMARKING = "benchmarking"
    SCORED = "scored"
    REJECTED = "rejected"
    ADOPTED = "adopted"


@dataclass
class Submission:
    """A solver submission and its lifecycle state."""
    submission_id: str
    repo_url: str
    commit_hash: str
    epoch: int
    hotkey: str
    round_id: str = ""
    pr_number: int | None = None         # Solver-repo PR number (PR-based submission)
    # Private-submission path: PR lives in the miner's own private repo, cloned
    # with a per-submission token. is_private/private_repo_full are persisted
    # (non-secret, drive finalization dispatch + status); repo_token is the
    # transient credential and is NEVER persisted (see _repo_token below).
    is_private: bool = False
    private_repo_full: str | None = None  # "owner/repo" of the miner's private repo
    status: SubmissionStatus = SubmissionStatus.QUEUED
    created_at: float = 0.0
    updated_at: float = 0.0

    # Screening results per stage
    screening: dict[str, Any] = field(default_factory=lambda: {
        "stage_1": {"passed": None, "duration_ms": None, "details": None, "error_code": None},
        "stage_2": {"passed": None, "duration_ms": None, "details": None, "error_code": None},
        "stage_3": {"passed": None, "duration_ms": None, "details": None, "error_code": None},
    })

    # Set after screening passes
    image_tag: str | None = None
    image_id: str | None = None          # Immutable local image identifier (sha256:...)
    image_digest: str | None = None      # Global GHCR manifest ref <repo>@sha256:<64hex> (content-addressed transport)
    provenance: dict[str, Any] | None = None
    solver_path: str | None = None  # Local path to solver .py (source submissions)
    solver_name: str | None = None
    solver_version: str | None = None

    # Set after benchmarking
    benchmark_score: float | None = None
    benchmark_rank: int | None = None
    benchmark_details: dict[str, Any] | None = None

    # Set on rejection
    rejection_reason: str | None = None

    # Local path to cloned repo (transient, not persisted)
    _repo_path: str | None = field(default=None, repr=False)

    def to_dict(self) -> dict[str, Any]:
        """Convert to API-friendly dict."""
        return {
            "submission_id": self.submission_id,
            "repo_url": self.repo_url,
            "commit_hash": self.commit_hash,
            "epoch": self.epoch,
            "hotkey": self.hotkey,
            "round_id": self.round_id,
            "pr_number": self.pr_number,
            "is_private": self.is_private,
            "private_repo_full": self.private_repo_full,
            # NOTE: _repo_token is intentionally NEVER serialized.
            "status": self.status.value,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "screening": self.screening,
            "image_tag": self.image_tag,
            "image_id": self.image_id,
            "image_digest": self.image_digest,
            "provenance": self.provenance,
            "solver_path": self.solver_path,
            "solver_name": self.solver_name,
            "solver_version": self.solver_version,
            "benchmark_score": self.benchmark_score,
            "benchmark_rank": self.benchmark_rank,
            "benchmark_details": self.benchmark_details,
            "rejection_reason": self.rejection_reason,
        }

    def status_dict(self) -> dict[str, Any]:
        """Compact status view for GET /status endpoint."""
        return {
            "submission_id": self.submission_id,
            "status": self.status.value,
            "round_id": self.round_id,
            "pr_number": self.pr_number,
            "is_private": self.is_private,
            "screening": self.screening,
            "image_tag": self.image_tag,
            "image_id": self.image_id,
            "image_digest": self.image_digest,
            "provenance": self.provenance,
            "solver_name": self.solver_name,
            "solver_version": self.solver_version,
            "benchmark_score": self.benchmark_score,
            "benchmark_rank": self.benchmark_rank,
            "rejection_reason": self.rejection_reason,
        }


def _write_locked(method):
    """Serialize a store mutation across threads *and* processes.

    Every mutating method here is a read-modify-write: it reloads the latest
    persisted state, mutates in memory, then rewrites the whole JSON file.
    When several workers share one backing file (FastAPI workers, or the
    validator + API processes) two such sequences can interleave — two
    concurrent ``create`` calls both pass the per-round cap check before either
    persists, or one writer's whole-file rewrite clobbers another's
    just-created record. This decorator brackets the method with an exclusive
    advisory file lock (plus an in-process lock) so each read-modify-write runs
    to completion before the next begins.
    """

    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._write_guard():
            return method(self, *args, **kwargs)

    return wrapper


class SubmissionStore:
    """In-memory store for submissions with optional JSON persistence.

    Mutations are serialized across threads and processes (see
    :func:`_write_locked`) so the per-(hotkey, round) cap in :meth:`create`
    holds even when multiple workers share the backing file. Reads stay
    lock-free; :meth:`_persist` writes atomically (temp file + ``os.replace``)
    so a concurrent reader never observes a half-written file.
    """

    def __init__(self, persist_path: Path | None = None) -> None:
        self._submissions: dict[str, Submission] = {}
        self._by_hotkey_round: dict[str, str] = {}  # "hotkey:round_id" → submission_id
        self._by_hotkey_epoch: dict[str, str] = {}  # "hotkey:epoch" → submission_id
        # Per-submission private-repo PATs. Kept OUTSIDE _submissions (and never
        # persisted) so the secret survives the reload-on-write in _write_guard,
        # which rebuilds _submissions from disk. In-process only: unavailable to
        # other workers / after a restart (the miner re-submits in that case).
        self._tokens: dict[str, str] = {}
        self._persist_path = persist_path
        # Cross-process advisory lock lives in a sibling file that is never
        # rewritten — locking the data file itself would break, since each
        # persist replaces it (a new inode the held fd no longer refers to).
        self._lock_path = (
            persist_path.with_name(persist_path.name + ".lock")
            if persist_path is not None
            else None
        )
        self._rmw_lock = threading.RLock()  # in-process serialization
        self._lock_fd: int | None = None
        self._lock_depth = 0
        self._persist_mtime_ns: int | None = None

        if persist_path and persist_path.exists():
            self._load()

    @_write_locked
    def create(
        self,
        repo_url: str,
        commit_hash: str,
        epoch: int,
        hotkey: str,
        round_id: str | None = None,
        pr_number: int | None = None,
        max_per_round: int = 1,
        max_total_per_round: int = 0,
        is_private: bool = False,
        private_repo_full: str | None = None,
        repo_token: str | None = None,
    ) -> Submission:
        """Create a new submission. Raises ValueError when a per-round cap is hit.

        ``max_per_round`` caps how many submissions a single hotkey may make for
        one round — anti-spam protection for the validator's screening +
        benchmark pipeline (each accepted submission queues a build + score).
        Default 1, the historical behaviour (one entry per miner per round). A
        value <= 0 disables the cap (unlimited). The cap counts ALL of the
        miner's submissions for the round, regardless of their final status, so
        a screening rejection still consumes an attempt.

        ``max_total_per_round`` caps the TOTAL submissions for the round across
        ALL miners (first-come, rest retry next round) — bounds the per-round
        benchmark batch. Default 0 = unlimited. Both checks run atomically here
        as the backstop against a TOCTOU race between the route's pre-check and
        the insert. Counts are over ALL statuses.
        """
        self._maybe_reload()
        resolved_round_id = (round_id or "").strip() or self._legacy_round_id(epoch)
        round_key = f"{hotkey}:{resolved_round_id}"
        epoch_key = f"{hotkey}:{epoch}"
        if max_per_round > 0:
            existing_count = sum(
                1 for s in self._submissions.values()
                if s.hotkey == hotkey and s.round_id == resolved_round_id
            )
            if existing_count >= max_per_round:
                raise ValueError(
                    f"Miner {hotkey[:12]}... already submitted {existing_count} "
                    f"time(s) for round {resolved_round_id} "
                    f"(max {max_per_round} per round)"
                )
        if max_total_per_round > 0:
            round_total = sum(
                1 for s in self._submissions.values()
                if s.round_id == resolved_round_id
            )
            if round_total >= max_total_per_round:
                raise ValueError(
                    f"Round {resolved_round_id} is full "
                    f"({round_total}/{max_total_per_round} submissions); "
                    f"try again next round"
                )

        now = time.time()
        sub = Submission(
            submission_id=f"sub_{uuid.uuid4().hex[:12]}",
            repo_url=repo_url,
            commit_hash=commit_hash,
            epoch=epoch,
            hotkey=hotkey,
            round_id=resolved_round_id,
            pr_number=pr_number,
            is_private=is_private,
            private_repo_full=private_repo_full,
            created_at=now,
            updated_at=now,
        )
        # Stash the secret in the reload-safe side map, never on the record.
        if repo_token:
            self._tokens[sub.submission_id] = repo_token

        self._submissions[sub.submission_id] = sub
        self._by_hotkey_round[round_key] = sub.submission_id
        self._by_hotkey_epoch[epoch_key] = sub.submission_id
        self._persist()

        logger.info(
            "Submission created: %s (miner=%s, round=%s, epoch=%d, repo=%s@%s)",
            sub.submission_id, hotkey[:12], resolved_round_id, epoch, repo_url, commit_hash[:8],
        )
        return sub

    def _upsert_one(self, record: dict[str, Any]) -> Submission:
        """Build + index a submission from a caller-provided record WITHOUT
        persisting. Caller-provided ``submission_id`` is preserved (no new uuid).
        Tolerant of an unknown ``status`` (falls back to QUEUED) — status is a
        local lifecycle marker and is NOT part of the benchmark pack hash, so a
        record must never be dropped just because its status string is unknown.
        Raises ValueError only when ``submission_id`` is missing.
        """
        sid = (record.get("submission_id") or "").strip()
        if not sid:
            raise ValueError("upsert requires a submission_id")
        round_id = record.get("round_id") or self._legacy_round_id(record.get("epoch", 0))
        raw_status = record.get("status", SubmissionStatus.QUEUED.value)
        try:
            status = SubmissionStatus(raw_status)
        except ValueError:
            logger.warning("upsert: unknown status %r for %s; defaulting QUEUED", raw_status, sid)
            status = SubmissionStatus.QUEUED
        sub = Submission(
            submission_id=sid,
            repo_url=record.get("repo_url", ""),
            commit_hash=record.get("commit_hash", ""),
            epoch=int(record.get("epoch", 0) or 0),
            hotkey=record.get("hotkey", ""),
            round_id=round_id,
            pr_number=record.get("pr_number"),
            is_private=bool(record.get("is_private", False)),
            private_repo_full=record.get("private_repo_full"),
            status=status,
            created_at=record.get("created_at", 0.0) or 0.0,
            updated_at=record.get("updated_at", 0.0) or 0.0,
            screening=record.get("screening") or {},
            image_tag=record.get("image_tag"),
            image_id=record.get("image_id"),
            image_digest=record.get("image_digest"),
            provenance=record.get("provenance"),
            solver_path=record.get("solver_path"),
            solver_name=record.get("solver_name"),
            solver_version=record.get("solver_version"),
            benchmark_score=record.get("benchmark_score"),
            benchmark_rank=record.get("benchmark_rank"),
            benchmark_details=record.get("benchmark_details"),
            rejection_reason=record.get("rejection_reason"),
        )
        # _submissions is the source of truth for list_by_round / the pack hash;
        # the indexes are best-effort lookups (last-wins is fine, they aren't
        # consulted by the pack hash).
        self._submissions[sid] = sub
        self._by_hotkey_round[f"{sub.hotkey}:{sub.round_id}"] = sid
        self._by_hotkey_epoch[f"{sub.hotkey}:{sub.epoch}"] = sid
        return sub

    def upsert_submission(self, record: dict[str, Any]) -> Submission:
        """Insert or replace a single submission by caller-provided
        ``submission_id`` and persist. See ``_upsert_one`` for field handling.

        Used to mirror the leader's close-time submission snapshot so the
        benchmark pack hash agrees fleet-wide (a follower lacking the leader's
        records recomputes a divergent hash → PACK_HASH_MISMATCH). No per-round
        cap (the leader already enforced it at ingest).
        """
        self._maybe_reload()
        sub = self._upsert_one(record)
        self._persist()
        return sub

    def upsert_submissions(self, records: list[dict[str, Any]]) -> int:
        """Batch upsert the leader's snapshot, persisting ONCE (O(n), not the
        O(n²) of per-record persist). Bad records (missing submission_id) are
        skipped + logged so one malformed entry can't drop the whole snapshot;
        returns the number successfully upserted.
        """
        self._maybe_reload()
        n = 0
        for record in records or []:
            try:
                self._upsert_one(record)
                n += 1
            except Exception as exc:  # noqa: BLE001 — skip the bad record, keep the rest
                logger.warning(
                    "upsert_submissions: skipped record %r: %s",
                    (record or {}).get("submission_id"), exc,
                )
        if n:
            self._persist()
        return n

    def get(self, submission_id: str) -> Submission | None:
        """Get a submission by ID."""
        self._maybe_reload()
        return self._submissions.get(submission_id)

    def get_by_hotkey_epoch(self, hotkey: str, epoch: int) -> Submission | None:
        """Get a submission by miner hotkey and epoch."""
        self._maybe_reload()
        key = f"{hotkey}:{epoch}"
        sub_id = self._by_hotkey_epoch.get(key)
        if sub_id:
            return self._submissions.get(sub_id)
        return None

    def get_by_hotkey_round(self, hotkey: str, round_id: str) -> Submission | None:
        """Get a submission by miner hotkey and round ID.

        When the per-round cap allows more than one, this returns the most
        recently indexed submission for the (hotkey, round) pair.
        """
        self._maybe_reload()
        key = f"{hotkey}:{round_id}"
        sub_id = self._by_hotkey_round.get(key)
        if sub_id:
            return self._submissions.get(sub_id)
        return None

    def count_by_hotkey_round(self, hotkey: str, round_id: str) -> int:
        """Number of submissions this miner has made for ``round_id``.

        The submission gate reads this to enforce the per-round cap BEFORE any
        expensive work (PR resolution, screening). Scoped strictly to the
        (hotkey, round) pair — other miners and other rounds never leak in.
        """
        self._maybe_reload()
        return sum(
            1 for s in self._submissions.values()
            if s.hotkey == hotkey and s.round_id == round_id
        )

    def count_by_round(self, round_id: str) -> int:
        """Total submissions for ``round_id`` across ALL miners.

        The submission gate reads this to enforce the round-wide cap (bounding
        the per-round benchmark batch) BEFORE any expensive work. Counts every
        status, so a screening rejection still consumes one of the round's slots.
        """
        self._maybe_reload()
        return sum(1 for s in self._submissions.values() if s.round_id == round_id)

    def list_by_epoch(self, epoch: int) -> list[Submission]:
        """List all submissions for an epoch, ordered by creation time."""
        self._maybe_reload()
        subs = [
            s for s in self._submissions.values()
            if s.epoch == epoch
        ]
        return sorted(subs, key=lambda s: s.created_at)

    def list_by_round(self, round_id: str) -> list[Submission]:
        """List all submissions for a round, ordered by creation time."""
        self._maybe_reload()
        subs = [
            s for s in self._submissions.values()
            if s.round_id == round_id
        ]
        return sorted(subs, key=lambda s: s.created_at)

    def list_queued(self) -> list[Submission]:
        """List all submissions in QUEUED status."""
        return self.list_by_status(SubmissionStatus.QUEUED)

    def list_by_status(self, status: SubmissionStatus) -> list[Submission]:
        """List all submissions with the given status."""
        self._maybe_reload()
        return [
            s for s in self._submissions.values()
            if s.status == status
        ]

    @_write_locked
    def update_status(
        self,
        submission_id: str,
        status: SubmissionStatus,
    ) -> None:
        """Update the status of a submission."""
        self._maybe_reload()
        sub = self._submissions.get(submission_id)
        if sub is None:
            raise KeyError(f"Submission not found: {submission_id}")

        sub.status = status
        sub.updated_at = time.time()
        self._persist()

    @_write_locked
    def set_screening_result(
        self,
        submission_id: str,
        stage: int,
        passed: bool,
        duration_ms: int = 0,
        details: str = "",
        error_code: str | None = None,
    ) -> None:
        """Record the result of a screening stage."""
        self._maybe_reload()
        sub = self._submissions.get(submission_id)
        if sub is None:
            raise KeyError(f"Submission not found: {submission_id}")

        stage_key = f"stage_{stage}"
        sub.screening[stage_key] = {
            "passed": passed,
            "duration_ms": duration_ms,
            "details": details,
            "error_code": error_code,
        }
        sub.updated_at = time.time()

        if not passed:
            sub.status = SubmissionStatus.REJECTED
            sub.rejection_reason = f"Stage {stage}: {error_code} — {details}"
            self.purge_token(submission_id)  # terminal — drop the secret

        self._persist()

    @_write_locked
    def set_image_tag(self, submission_id: str, image_tag: str) -> None:
        """Set the Docker image tag after successful build."""
        self._maybe_reload()
        sub = self._submissions.get(submission_id)
        if sub is None:
            raise KeyError(f"Submission not found: {submission_id}")
        sub.image_tag = image_tag
        sub.updated_at = time.time()
        self._persist()

    @_write_locked
    def set_image_id(self, submission_id: str, image_id: str) -> None:
        """Set immutable image identifier after successful build."""
        self._maybe_reload()
        sub = self._submissions.get(submission_id)
        if sub is None:
            raise KeyError(f"Submission not found: {submission_id}")
        sub.image_id = image_id
        sub.updated_at = time.time()
        self._persist()

    @_write_locked
    def set_image_digest(self, submission_id: str, image_digest: str) -> None:
        """Set the global GHCR manifest ref (<repo>@sha256:<64hex>) after push."""
        self._maybe_reload()
        sub = self._submissions.get(submission_id)
        if sub is None:
            raise KeyError(f"Submission not found: {submission_id}")
        sub.image_digest = image_digest
        sub.updated_at = time.time()
        self._persist()

    @_write_locked
    def set_provenance(
        self,
        submission_id: str,
        provenance: dict[str, Any],
    ) -> None:
        """Attach signed provenance metadata for a screened artifact."""
        self._maybe_reload()
        sub = self._submissions.get(submission_id)
        if sub is None:
            raise KeyError(f"Submission not found: {submission_id}")
        sub.provenance = provenance
        sub.updated_at = time.time()
        self._persist()

    @_write_locked
    def set_solver_path(self, submission_id: str, solver_path: str) -> None:
        """Set the local solver file path for source submissions."""
        self._maybe_reload()
        sub = self._submissions.get(submission_id)
        if sub is None:
            raise KeyError(f"Submission not found: {submission_id}")
        sub.solver_path = solver_path
        sub.updated_at = time.time()
        self._persist()

    @_write_locked
    def set_solver_info(
        self,
        submission_id: str,
        name: str | None = None,
        version: str | None = None,
    ) -> None:
        """Set solver metadata extracted during screening."""
        self._maybe_reload()
        sub = self._submissions.get(submission_id)
        if sub is None:
            raise KeyError(f"Submission not found: {submission_id}")
        sub.solver_name = name
        sub.solver_version = version
        sub.updated_at = time.time()
        self._persist()

    @_write_locked
    def set_benchmark_result(
        self,
        submission_id: str,
        score: float,
        rank: int | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        """Record benchmark results.

        Will not overwrite a real score (>0) with 0.0 to prevent
        the rank-assignment pass from erasing Docker benchmark results.
        """
        self._maybe_reload()
        sub = self._submissions.get(submission_id)
        if sub is None:
            raise KeyError(f"Submission not found: {submission_id}")

        # Don't overwrite a real score with 0
        if score <= 0 and sub.benchmark_score is not None and sub.benchmark_score > 0:
            # Only update rank/details, keep the real score
            if rank is not None:
                sub.benchmark_rank = rank
            sub.updated_at = time.time()
            self._persist()
            return

        sub.benchmark_score = score
        if rank is not None:
            sub.benchmark_rank = rank
        if details is not None:
            sub.benchmark_details = details
        # A zero or negative score means the solver failed to produce
        # any valid plans — reject it instead of marking it scored.
        # Previously this was SCORED regardless, allowing broken solvers
        # to proceed through the pipeline.
        if score <= 0:
            sub.status = SubmissionStatus.REJECTED
            sub.rejection_reason = (
                sub.rejection_reason
                or f"Benchmark score {score:.4f} <= 0 (solver produced no valid plans)"
            )
            self.purge_token(submission_id)  # terminal — drop the secret
        else:
            sub.status = SubmissionStatus.SCORED
        sub.updated_at = time.time()
        self._persist()

    @_write_locked
    def reject(self, submission_id: str, reason: str) -> None:
        """Reject a submission with a reason."""
        self._maybe_reload()
        sub = self._submissions.get(submission_id)
        if sub is None:
            raise KeyError(f"Submission not found: {submission_id}")
        sub.status = SubmissionStatus.REJECTED
        sub.rejection_reason = reason
        sub.updated_at = time.time()
        self.purge_token(submission_id)  # terminal — drop the secret
        self._persist()

    @_write_locked
    def adopt(self, submission_id: str) -> None:
        """Mark a submission as the adopted champion.

        Un-adopts any previous champion first (at most one champion at a time).
        """
        self._maybe_reload()
        # Un-adopt previous champion
        for s in self._submissions.values():
            if s.status == SubmissionStatus.ADOPTED and s.submission_id != submission_id:
                s.status = SubmissionStatus.SCORED
                s.updated_at = time.time()

        sub = self._submissions.get(submission_id)
        if sub is None:
            raise KeyError(f"Submission not found: {submission_id}")
        sub.status = SubmissionStatus.ADOPTED
        sub.updated_at = time.time()
        self._persist()

    def get_champion(self) -> Any:
        """Return the currently adopted champion submission, or None."""
        adopted = self.list_by_status(SubmissionStatus.ADOPTED)
        return adopted[0] if adopted else None

    # ── Private-submission token handling (transient secret) ─────────────────

    def get_repo_token(self, submission_id: str) -> str | None:
        """Return the per-submission private-repo PAT, or None.

        The token is in-memory only and never persisted, so it is unavailable
        after a process restart (the miner re-submits in that case).
        """
        return self._tokens.get(submission_id)

    def purge_token(self, submission_id: str) -> None:
        """Drop the in-memory private-repo token once it is no longer needed.

        Called when a submission reaches a terminal state (or after a successful
        private-champion publish) to minimise how long the credential lives in
        memory. Idempotent and never raises for an unknown id. No file lock /
        persist needed — the token never touched disk.
        """
        self._tokens.pop(submission_id, None)

    # ── Cross-process write lock ─────────────────────────────────────────────

    @contextmanager
    def _write_guard(self):
        """Hold the write lock around a single read-modify-write.

        Acquires the in-process lock (threads) then the exclusive advisory file
        lock (processes), adopts the freshest persisted state, and yields for
        the caller to mutate + persist. Re-entrant on one thread: nested guards
        share the outermost lock and skip the reload so an in-progress mutation
        is never discarded. When there is no ``persist_path`` (pure in-memory)
        or ``fcntl`` is unavailable, the file lock is a no-op and only the
        in-process lock applies.
        """
        with self._rmw_lock:
            outermost = self._lock_depth == 0
            self._lock_depth += 1
            if outermost:
                self._lock_fd = self._acquire_file_lock()
            try:
                if (
                    outermost
                    and self._persist_path is not None
                    and self._persist_path.exists()
                ):
                    # Under the exclusive lock, take the latest committed state
                    # so the check-and-write below cannot race another writer.
                    self._load(quiet=True)
                yield
            finally:
                self._lock_depth -= 1
                if self._lock_depth == 0:
                    self._release_file_lock(self._lock_fd)
                    self._lock_fd = None

    def _acquire_file_lock(self) -> int | None:
        """Open the sibling lock file and take an exclusive advisory lock."""
        if self._lock_path is None or fcntl is None:
            return None
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self._lock_path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
        except OSError:
            os.close(fd)
            raise
        return fd

    @staticmethod
    def _release_file_lock(fd: int | None) -> None:
        if fd is None:
            return
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    # ── Persistence ────────────────────────────────────────────────────────

    def _maybe_reload(self) -> None:
        """Refresh persisted state when another process updated the backing file."""
        if self._persist_path is None or not self._persist_path.exists():
            return
        try:
            current_mtime_ns = self._persist_path.stat().st_mtime_ns
        except OSError:
            return
        if self._persist_mtime_ns is None or current_mtime_ns > self._persist_mtime_ns:
            self._load()

    def _persist(self) -> None:
        """Write state to disk atomically if persist_path is set.

        Writes a temp file then ``os.replace``s it into place so a concurrent
        lock-free reader always sees a complete file, never a half-written one.
        """
        if self._persist_path is None:
            return
        try:
            data = {
                sid: sub.to_dict()
                for sid, sub in self._submissions.items()
            }
            self._persist_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = self._persist_path.with_name(
                f".{self._persist_path.name}.{os.getpid()}.tmp"
            )
            tmp_path.write_text(json.dumps(data, indent=2))
            os.replace(tmp_path, self._persist_path)
            self._persist_mtime_ns = self._persist_path.stat().st_mtime_ns
        except Exception as exc:
            logger.warning("Failed to persist submissions: %s", exc)

    def _load(self, *, quiet: bool = False) -> None:
        """Load state from disk. Set ``quiet`` to skip the info log on hot paths."""
        try:
            data = json.loads(self._persist_path.read_text())
            submissions: dict[str, Submission] = {}
            by_hotkey_round: dict[str, str] = {}
            by_hotkey_epoch: dict[str, str] = {}
            for sid, d in data.items():
                round_id = d.get("round_id") or self._legacy_round_id(d.get("epoch", 0))
                sub = Submission(
                    submission_id=d["submission_id"],
                    repo_url=d["repo_url"],
                    commit_hash=d["commit_hash"],
                    epoch=d["epoch"],
                    hotkey=d["hotkey"],
                    round_id=round_id,
                    pr_number=d.get("pr_number"),
                    is_private=bool(d.get("is_private", False)),
                    private_repo_full=d.get("private_repo_full"),
                    status=SubmissionStatus(d["status"]),
                    created_at=d.get("created_at", 0),
                    updated_at=d.get("updated_at", 0),
                    screening=d.get("screening", {}),
                    image_tag=d.get("image_tag"),
                    image_id=d.get("image_id"),
                    image_digest=d.get("image_digest"),
                    provenance=d.get("provenance"),
                    solver_path=d.get("solver_path"),
                    solver_name=d.get("solver_name"),
                    solver_version=d.get("solver_version"),
                    benchmark_score=d.get("benchmark_score"),
                    benchmark_rank=d.get("benchmark_rank"),
                    benchmark_details=d.get("benchmark_details"),
                    rejection_reason=d.get("rejection_reason"),
                )
                submissions[sid] = sub
                round_key = f"{sub.hotkey}:{sub.round_id}"
                by_hotkey_round[round_key] = sid
                key = f"{sub.hotkey}:{sub.epoch}"
                by_hotkey_epoch[key] = sid
            self._submissions = submissions
            self._by_hotkey_round = by_hotkey_round
            self._by_hotkey_epoch = by_hotkey_epoch
            self._persist_mtime_ns = self._persist_path.stat().st_mtime_ns
            if not quiet:
                logger.info("Loaded %d submissions from %s", len(data), self._persist_path)
        except Exception as exc:
            logger.warning("Failed to load submissions: %s", exc)

    @staticmethod
    def _legacy_round_id(epoch: int) -> str:
        return f"legacy-epoch-{epoch}"
