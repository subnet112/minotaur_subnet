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

import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

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


class SubmissionStore:
    """In-memory store for submissions with optional JSON persistence."""

    def __init__(self, persist_path: Path | None = None) -> None:
        self._submissions: dict[str, Submission] = {}
        self._by_hotkey_round: dict[str, str] = {}  # "hotkey:round_id" → submission_id
        self._by_hotkey_epoch: dict[str, str] = {}  # "hotkey:epoch" → submission_id
        self._persist_path = persist_path
        self._persist_mtime_ns: int | None = None

        if persist_path and persist_path.exists():
            self._load()

    def create(
        self,
        repo_url: str,
        commit_hash: str,
        epoch: int,
        hotkey: str,
        round_id: str | None = None,
        pr_number: int | None = None,
        max_per_round: int = 1,
    ) -> Submission:
        """Create a new submission. Raises ValueError when the per-round cap is hit.

        ``max_per_round`` caps how many submissions a single hotkey may make for
        one round — anti-spam protection for the validator's screening +
        benchmark pipeline (each accepted submission queues a build + score).
        Default 1, the historical behaviour (one entry per miner per round). A
        value <= 0 disables the cap (unlimited). The cap counts ALL of the
        miner's submissions for the round, regardless of their final status, so
        a screening rejection still consumes an attempt.
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

        now = time.time()
        sub = Submission(
            submission_id=f"sub_{uuid.uuid4().hex[:12]}",
            repo_url=repo_url,
            commit_hash=commit_hash,
            epoch=epoch,
            hotkey=hotkey,
            round_id=resolved_round_id,
            pr_number=pr_number,
            created_at=now,
            updated_at=now,
        )

        self._submissions[sub.submission_id] = sub
        self._by_hotkey_round[round_key] = sub.submission_id
        self._by_hotkey_epoch[epoch_key] = sub.submission_id
        self._persist()

        logger.info(
            "Submission created: %s (miner=%s, round=%s, epoch=%d, repo=%s@%s)",
            sub.submission_id, hotkey[:12], resolved_round_id, epoch, repo_url, commit_hash[:8],
        )
        return sub

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

        self._persist()

    def set_image_tag(self, submission_id: str, image_tag: str) -> None:
        """Set the Docker image tag after successful build."""
        self._maybe_reload()
        sub = self._submissions.get(submission_id)
        if sub is None:
            raise KeyError(f"Submission not found: {submission_id}")
        sub.image_tag = image_tag
        sub.updated_at = time.time()
        self._persist()

    def set_image_id(self, submission_id: str, image_id: str) -> None:
        """Set immutable image identifier after successful build."""
        self._maybe_reload()
        sub = self._submissions.get(submission_id)
        if sub is None:
            raise KeyError(f"Submission not found: {submission_id}")
        sub.image_id = image_id
        sub.updated_at = time.time()
        self._persist()

    def set_image_digest(self, submission_id: str, image_digest: str) -> None:
        """Set the global GHCR manifest ref (<repo>@sha256:<64hex>) after push."""
        self._maybe_reload()
        sub = self._submissions.get(submission_id)
        if sub is None:
            raise KeyError(f"Submission not found: {submission_id}")
        sub.image_digest = image_digest
        sub.updated_at = time.time()
        self._persist()

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

    def set_solver_path(self, submission_id: str, solver_path: str) -> None:
        """Set the local solver file path for source submissions."""
        self._maybe_reload()
        sub = self._submissions.get(submission_id)
        if sub is None:
            raise KeyError(f"Submission not found: {submission_id}")
        sub.solver_path = solver_path
        sub.updated_at = time.time()
        self._persist()

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
        else:
            sub.status = SubmissionStatus.SCORED
        sub.updated_at = time.time()
        self._persist()

    def reject(self, submission_id: str, reason: str) -> None:
        """Reject a submission with a reason."""
        self._maybe_reload()
        sub = self._submissions.get(submission_id)
        if sub is None:
            raise KeyError(f"Submission not found: {submission_id}")
        sub.status = SubmissionStatus.REJECTED
        sub.rejection_reason = reason
        sub.updated_at = time.time()
        self._persist()

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
        """Write state to disk if persist_path is set."""
        if self._persist_path is None:
            return
        try:
            data = {
                sid: sub.to_dict()
                for sid, sub in self._submissions.items()
            }
            self._persist_path.parent.mkdir(parents=True, exist_ok=True)
            self._persist_path.write_text(json.dumps(data, indent=2))
            self._persist_mtime_ns = self._persist_path.stat().st_mtime_ns
        except Exception as exc:
            logger.warning("Failed to persist submissions: %s", exc)

    def _load(self) -> None:
        """Load state from disk."""
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
            logger.info("Loaded %d submissions from %s", len(data), self._persist_path)
        except Exception as exc:
            logger.warning("Failed to load submissions: %s", exc)

    @staticmethod
    def _legacy_round_id(epoch: int) -> str:
        return f"legacy-epoch-{epoch}"
