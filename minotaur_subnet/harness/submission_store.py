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

import asyncio
import functools
import json
import logging
import os
import threading
import time
import unicodedata
import uuid
from concurrent.futures import ThreadPoolExecutor
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


# ── Private-repo token encryption at rest ─────────────────────────────────────
#
# Private submissions carry a per-submission PAT so the relayer can clone the
# miner's private tree at finalize time. Keeping it in memory only meant ANY
# api restart between submission and finalize voided the dethrone fail-closed
# ("has no token", round-e29716440-n1, 2026-07-02). Tokens are now ALSO written
# to a sidecar file next to the store, encrypted with a key derived from
# VALIDATOR_PRIVATE_KEY (NaCl SecretBox, authenticated), never plaintext on
# disk. No signing key (or SUBMISSION_TOKEN_PERSIST=0) → in-memory-only, the
# historical behaviour.

_TOKEN_KEY_PERSON = b"mino-token-store"  # blake2b personalization, must be ≤16 bytes


def _derive_token_key() -> bytes | None:
    """32-byte SecretBox key derived from the validator signing key, or None.

    Uses keyed BLAKE2b over the raw key material with a store-specific
    personalization, so the derived key is bound to this purpose and cannot be
    confused with a signature over the same material.
    """
    raw = (os.environ.get("VALIDATOR_PRIVATE_KEY") or "").strip()
    if not raw:
        return None
    try:
        import nacl.encoding
        import nacl.hash
        import nacl.secret
    except ImportError:  # pragma: no cover - pynacl is a hard dep in prod
        logger.warning("pynacl unavailable — private-repo tokens stay in-memory only")
        return None
    material = raw.lower().removeprefix("0x").encode("utf-8")
    return nacl.hash.blake2b(
        material,
        digest_size=nacl.secret.SecretBox.KEY_SIZE,
        person=_TOKEN_KEY_PERSON,
        encoder=nacl.encoding.RawEncoder,
    )


def _encrypt_token(key: bytes, token: str) -> str:
    import base64

    import nacl.secret

    blob = nacl.secret.SecretBox(key).encrypt(token.encode("utf-8"))
    return base64.b64encode(bytes(blob)).decode("ascii")


def _decrypt_token(key: bytes, blob_b64: str) -> str:
    import base64

    import nacl.secret

    return nacl.secret.SecretBox(key).decrypt(base64.b64decode(blob_b64)).decode("utf-8")


class SubmissionStatus(str, Enum):
    """Submission lifecycle states."""
    QUEUED = "queued"
    SCREENING_STAGE_1 = "screening_stage_1"
    SCREENING_STAGE_2 = "screening_stage_2"
    SCREENING_STAGE_3 = "screening_stage_3"
    # Screening passed; awaiting the close-time rotation slate decision (PR-C
    # promotes this into the flow). Terminal-for-this-round only via WAITLISTED.
    PENDING_SELECTION = "pending_selection"
    BENCHMARKING = "benchmarking"
    SCORED = "scored"
    # NOT selected onto this round's benched slate, OR the bench window elapsed
    # before scoring — a NON-fault, NON-terminal-across-rounds outcome carrying
    # next-round priority. Distinct from REJECTED (for-cause) so miners and the
    # UI never read "didn't get a slot" as "your code was refused".
    WAITLISTED = "waitlisted"
    REJECTED = "rejected"
    ADOPTED = "adopted"


# Machine-readable terminal-outcome taxonomy (persisted as Submission.
# outcome_code). The frontend switches on THIS, never on the human
# rejection_reason prose. Grouped by the status they accompany:
#   REJECTED (for-cause — miner must change something):
OUTCOME_QUOTA_HOTKEY = "quota_hotkey"          # per-hotkey / per-round cap
OUTCOME_QUOTA_COMMIT = "quota_commit"          # per-(hotkey, commit) cap
OUTCOME_FINGERPRINT_REPEAT = "fingerprint_repeat"  # cross-hotkey normalized-code cap
OUTCOME_CLONE_FAILED = "clone_failed"          # bad token / unreachable repo
OUTCOME_STATIC_CHECKS = "static_checks_failed"  # stage-1 static policy
OUTCOME_TOO_ENTANGLED = "too_entangled"        # factorization floor (when armed)
OUTCOME_BUILD_FAILED = "build_failed"          # stage-2 docker build
OUTCOME_INVALID_PLANS = "invalid_plans"        # stage-3 plan validation
OUTCOME_BENCHMARK_FAILED = "benchmark_failed"  # bench produced no usable rows
OUTCOME_SCREENING_ERROR = "screening_error"    # unexpected pipeline error
#   WAITLISTED (no-fault — resubmit keeps seniority):
OUTCOME_ROTATION_NOT_SELECTED = "rotation_not_selected"
OUTCOME_WINDOW_ELAPSED = "window_elapsed"


# Statuses meaning the submission actually occupied a benchmark slate slot (was
# selected into a round and benched, or is being benched now). The per-commit
# participation cap counts THESE — a rotation "not selected" or screening
# rejection never consumed sim time, so it doesn't burn the commit's quota.
BENCHED_STATUSES = frozenset({
    SubmissionStatus.BENCHMARKING,
    SubmissionStatus.SCORED,
    SubmissionStatus.ADOPTED,
})


# Cap how many submissions keep their (heavy — ~40-70KB each) benchmark_details
# in the persisted store. Terminal submissions beyond the N most-recent (by
# epoch) get their details dropped on persist. Without this the store grew
# unbounded to 142MB (3556 subs) and, because _persist re-serializes the WHOLE
# store on every write ON THE EVENT LOOP, each write froze the api for ~25s
# (every request — sync and async — stalled). Keeps active + recent submissions
# intact. 0 disables the cap.
_BENCHMARK_DETAILS_RETENTION = int(
    os.environ.get("SUBMISSION_BENCHMARK_DETAILS_RETENTION", "300")
)
# Only terminal submissions are strippable — an in-flight one may still need its
# details for the round decision / report.
_DETAILS_STRIPPABLE_STATUSES = frozenset({
    SubmissionStatus.SCORED,
    SubmissionStatus.REJECTED,
})


# ── Solver-name coinage (copycat labeling) ──────────────────────────────────
# A solver's display name comes from the miner-authored ``metadata().name`` —
# unvalidated free text — so multiple hotkeys can submit the same name (usually
# a forked/replayed solver that kept the original's name). The store keeps a
# first-to-coin registry keyed by NORMALIZED name (see SubmissionStore._names):
# the first hotkey to submit a distinct, non-boilerplate name owns it; a later
# DIFFERENT hotkey reusing it is flagged is_copycat. Purely cosmetic — the name
# feeds no scoring/adoption path, this only drives attribution in status views.

_ZERO_WIDTH = {"\u200b", "\u200c", "\u200d", "\u2060", "\ufeff"}


def _normalize_solver_name(name: str | None) -> str:
    """Canonicalize a solver display name for coinage/copycat comparison.

    Applies NFKC (folds width/compatibility variants), strips zero-width
    characters, casefolds, and collapses whitespace, so ``"King"``, ``"king "``
    and ``"k\u200bing"`` all map to the same key. Returns ``""`` for empty/None,
    which is never coinable. NOTE: this does not merge cross-script confusables
    (e.g. a Cyrillic homoglyph) — that would need a Unicode-TR39 skeleton map and
    is a deliberate follow-up; copycat labeling is best-effort, not airtight.
    """
    if not name:
        return ""
    s = unicodedata.normalize("NFKC", str(name))
    s = "".join(ch for ch in s if ch not in _ZERO_WIDTH)
    return " ".join(s.casefold().split())


# Names shipped in the SDK example + canonical solver template. Every unmodified
# fork carries one, so they are NON-COINABLE: no miner can "own" boilerplate and
# no honest fork is flagged a copycat for keeping the placeholder. Copycat
# labeling only applies to distinctive, miner-coined names.
_UNCOINABLE_NAMES = frozenset(
    _normalize_solver_name(n)
    for n in ("my-swap-solver", "baseline-swap-solver", "unknown")
)


def _exempt_names() -> frozenset[str]:
    """Non-coinable names: the shipped boilerplate plus any operator additions
    from ``SOLVER_NAME_UNCOINABLE`` (comma-separated, normalized)."""
    extra = os.environ.get("SOLVER_NAME_UNCOINABLE", "").strip()
    if not extra:
        return _UNCOINABLE_NAMES
    return _UNCOINABLE_NAMES | frozenset(
        _normalize_solver_name(n) for n in extra.split(",") if n.strip()
    )


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
    # credential and NEVER appears on the record or in the main JSON — it lives
    # in the store's token side map (encrypted sidecar file at rest).
    is_private: bool = False
    private_repo_full: str | None = None  # "owner/repo" of the miner's private repo
    # Head-repo GitHub account (lowercased owner login), derived from the resolved
    # PR clone_url. The anti-sybil key for the per-(account, round) cap: a miner
    # spreading one GitHub account across multiple hotkeys/PRs still collapses to
    # one account here. None for inline-source submissions (no GitHub identity).
    github_owner: str | None = None
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

    # Copycat naming (first-to-coin, hotkey-keyed). solver_name is self-declared
    # free text, so the store coins each distinct non-boilerplate name to the
    # first hotkey that submits it; a later DIFFERENT hotkey reusing it sets
    # is_copycat=True and coined_by_hotkey=<original owner>. Cosmetic only.
    is_copycat: bool = False
    coined_by_hotkey: str | None = None

    # Factorization metric (Phase 0, OBSERVE-ONLY): the largest AST-node count of
    # any single named region (module / function / class body) across the
    # submission's in-tree Python, computed once in screening stage 1. Persisted
    # but NOT gated yet — we soak the live distribution to calibrate the floor and
    # the saturated-tie dethrone tie-break. See harness/screening.max_region_nodes.
    max_region_nodes: int | None = None
    # Normalized content fingerprint (harness/code_fingerprint, screening stage 1)
    # — the "same code, same quota" identity that comment/whitespace/nonce
    # rotation cannot refresh. None on records that predate the metric.
    content_fingerprint: str | None = None

    # Deadwood metric (Phase 0, OBSERVE-ONLY): AST-node mass of the submission
    # tree that provably does no work at runtime (unreachable files + dead
    # bindings inside reachable files + exempt-tree overflow), computed once in
    # screening stage 1 and never recomputed by any consumer. Persisted but NOT
    # gated yet. None ⇔ not yet screened OR a non-exempt file was unparseable.
    # unproductive_metric_version stamps the algorithm semantics — consumers
    # must only compare EQUAL versions. unproductive_top_offenders is the
    # [path, qualname-or-None, nodes] deletion list miners see in the report.
    # See harness/deadwood.unproductive_nodes.
    unproductive_nodes: int | None = None
    unproductive_metric_version: int | None = None
    unproductive_top_offenders: list | None = None

    # Set after benchmarking. NOTE: the scalar composite benchmark_score was
    # removed — adoption is decided by the per-order relative rule
    # (epoch/relative_scoring.evaluate_relative_adoption) and finalist ranking by
    # relative net-better vs the champion. benchmark_rank is a DISPLAY rank derived
    # from that same net-better ordering; benchmark_details carries the per-order
    # raw_output rows the relative rule consumes.
    benchmark_rank: int | None = None
    benchmark_details: dict[str, Any] | None = None

    # Set on any terminal outcome (rejected / waitlisted).
    rejection_reason: str | None = None
    # Machine-readable terminal outcome (one of the OUTCOME_* codes). The
    # miner-facing surfaces (frontend, PR comment router) switch on THIS, not on
    # the human rejection_reason. None while in-flight or on legacy records.
    outcome_code: str | None = None
    # Waitlist context for a WAITLISTED submission — {"position", "contenders",
    # "slots"} + is priority for next round. Lets the UI say "queued for next
    # round, #2 of 13". None for every non-waitlisted state.
    waitlist: dict[str, Any] | None = None

    # Local path to cloned repo (transient, not persisted)
    _repo_path: str | None = field(default=None, repr=False)

    @property
    def display_name(self) -> str | None:
        """Solver name for display, with a ``-copycat`` suffix when this
        submission reused a name a different hotkey coined first."""
        if self.solver_name and self.is_copycat:
            return f"{self.solver_name}-copycat"
        return self.solver_name

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
            "github_owner": self.github_owner,
            # NOTE: the repo token is intentionally NEVER serialized here —
            # at rest it exists only encrypted, in the sidecar token file.
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
            "is_copycat": self.is_copycat,
            "coined_by_hotkey": self.coined_by_hotkey,
            "max_region_nodes": self.max_region_nodes,
            "content_fingerprint": self.content_fingerprint,
            "unproductive_nodes": self.unproductive_nodes,
            "unproductive_metric_version": self.unproductive_metric_version,
            "unproductive_top_offenders": self.unproductive_top_offenders,
            "benchmark_rank": self.benchmark_rank,
            "benchmark_details": self.benchmark_details,
            "rejection_reason": self.rejection_reason,
            "outcome_code": self.outcome_code,
            "waitlist": self.waitlist,
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
            "display_name": self.display_name,
            "is_copycat": self.is_copycat,
            "max_region_nodes": self.max_region_nodes,
            "content_fingerprint": self.content_fingerprint,
            "unproductive_nodes": self.unproductive_nodes,
            "benchmark_rank": self.benchmark_rank,
            "rejection_reason": self.rejection_reason,
            "outcome_code": self.outcome_code,
            "waitlist": self.waitlist,
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


async def offload_write(bound_method, /, *args, **kwargs):
    """Run a bound store mutator off the event loop and await its completion.

    On a real :class:`SubmissionStore` this dispatches to :meth:`aoffload`, so
    the whole locked read-modify-write (the ~44MB ``json.loads``/``json.dumps``)
    runs on the store's dedicated writer thread instead of blocking the loop.
    On any object WITHOUT ``aoffload`` — a test double / stub store — it falls
    back to running the sync mutator inline, so call sites stay decoupled from
    the store's async surface and no test double needs to grow async methods.
    Awaited, so durability + persist-before-caller-continues ordering hold.
    Pass a BOUND method, e.g. ``await offload_write(store.set_benchmark_result,
    sid, valid=True, details=d)``.
    """
    store = getattr(bound_method, "__self__", None)
    aoff = getattr(store, "aoffload", None)
    if aoff is not None:
        return await aoff(bound_method, *args, **kwargs)
    return bound_method(*args, **kwargs)


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
        # Per-submission private-repo PATs. Kept OUTSIDE _submissions so the
        # secret is never part of to_dict()/the main JSON file. _tokens is the
        # in-process plaintext cache; _enc_tokens mirrors the encrypted sidecar
        # file (SecretBox blobs, base64) that lets the token survive restarts
        # and reach sibling workers. Without a derivable key the sidecar is
        # disabled and tokens are in-process only (the historical behaviour:
        # the miner re-submits after a restart).
        self._tokens: dict[str, str] = {}
        self._enc_tokens: dict[str, str] = {}
        _persist_tokens_enabled = (
            (os.environ.get("SUBMISSION_TOKEN_PERSIST", "1").strip() or "1") != "0"
        )
        self._token_key = _derive_token_key() if _persist_tokens_enabled else None
        self._tokens_path = (
            persist_path.with_name(persist_path.name + ".tokens")
            if (persist_path is not None and self._token_key is not None)
            else None
        )
        self._tokens_mtime_ns: int | None = None
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
        # Dedicated single writer thread for the async offload path
        # (:meth:`aoffload`). Lazily created so sync-only / test usage never
        # spawns a thread. One thread ⇒ offloaded writes self-serialize, and
        # the whole locked read-modify-write (the ~44MB json.loads reload +
        # mutate + json.dumps persist) runs off the event loop rather than
        # blocking it.
        self._writer_executor: ThreadPoolExecutor | None = None
        # Solver-name coinage registry (copycat labeling). Kept in a sibling
        # sidecar next to the main JSON — like the tokens sidecar — so it
        # survives restarts AND submission pruning (name ownership must outlive
        # the coining submission). normalized_name -> {owner_hotkey, coined_at,
        # coined_submission_id, display}.
        self._names: dict[str, dict[str, Any]] = {}
        self._names_path = (
            persist_path.with_name(persist_path.name + ".names")
            if persist_path is not None
            else None
        )
        self._names_mtime_ns: int | None = None

        if persist_path and persist_path.exists():
            self._load()
        if self._tokens_path is not None and self._tokens_path.exists():
            self._load_tokens()
        if self._names_path is not None:
            if self._names_path.exists():
                self._load_names()
            else:
                # First start after this ships: seed ownership from existing
                # history so the earliest submitter of each name is credited,
                # not whoever submits next. Deterministic, so concurrent
                # first-run workers converge on the same registry.
                self._backfill_names()

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
        github_owner: str | None = None,
        max_per_owner_per_round: int = 0,
        max_rounds_per_commit: int = 0,
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
        benchmark batch. Default 0 = unlimited.

        ``max_per_owner_per_round`` caps submissions per ``github_owner`` (the
        head-repo GitHub account) for the round, across ALL hotkeys — the anti-sybil
        dedup that the per-hotkey cap can't provide (one account spread over many
        hotkeys). Default 0 = disabled; skipped when ``github_owner`` is unset
        (inline-source). Case-insensitive.

        ``max_rounds_per_commit`` caps how many ROUNDS the same (hotkey, commit)
        may occupy a benchmark slate slot — anti-resubmit-spam (measured live:
        61% of benchmark slots re-scored an already-benched commit; one bot
        resubmitted the identical commit 36 rounds straight). Counts DISTINCT
        rounds with a BENCHED status (see ``BENCHED_STATUSES``) so rotation
        "not selected" / screening rejections don't burn quota. Keyed per hotkey
        so a third party can't poison someone else's commit, and trivially
        evaded by pushing a new commit — it stops automation, not adversaries.
        Default 0 = disabled.

        All checks run atomically here as the backstop against a TOCTOU race
        between the route's pre-check and the insert. The per-round counts are
        over ALL statuses.
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
        # Per-(github-account, round) cap — the anti-sybil backstop. Counts ALL of
        # this GitHub account's submissions for the round REGARDLESS of hotkey, so a
        # miner spreading one account across N hotkeys still collapses to the cap.
        # Case-insensitive (GitHub logins are). Skipped when the owner is unknown
        # (inline-source) or the cap is disabled.
        owner_key = (github_owner or "").lower()
        if owner_key and max_per_owner_per_round > 0:
            owner_count = sum(
                1 for s in self._submissions.values()
                if (s.github_owner or "").lower() == owner_key
                and s.round_id == resolved_round_id
            )
            if owner_count >= max_per_owner_per_round:
                raise ValueError(
                    f"GitHub account {owner_key!r} already submitted {owner_count} "
                    f"time(s) for round {resolved_round_id} "
                    f"(max {max_per_owner_per_round} per round per account)"
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
        if max_rounds_per_commit > 0:
            benched_rounds = self.count_benched_rounds_by_commit(hotkey, commit_hash)
            if benched_rounds >= max_rounds_per_commit:
                raise ValueError(
                    f"Commit {commit_hash[:12]} has already been benchmarked in "
                    f"{benched_rounds} round(s) for this hotkey "
                    f"(max {max_rounds_per_commit}); submit new code to "
                    f"participate again"
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
            github_owner=(github_owner or None),
            created_at=now,
            updated_at=now,
        )
        # Stash the secret in the reload-safe side map, never on the record.
        if repo_token:
            self._tokens[sub.submission_id] = repo_token

        # Copy-on-write: rebind rather than mutate the published dict in place.
        # Once writes run on the writer thread (aoffload), lock-free loop-thread
        # reads iterate self._submissions.values(); an in-place insert would
        # raise "dictionary changed size during iteration". A fresh dict leaves
        # any in-flight reader's view intact. The index dicts are only .get()-
        # accessed (never iterated), so in-place update stays safe.
        self._submissions = {**self._submissions, sub.submission_id: sub}
        self._by_hotkey_round[round_key] = sub.submission_id
        self._by_hotkey_epoch[epoch_key] = sub.submission_id
        self._persist()
        # With an encryption key available the secret also goes to the sidecar
        # file (after the record is indexed — _persist_tokens prunes to known
        # submissions) so finalize still works after a restart / from a
        # sibling worker.
        if repo_token and self._token_key is not None and self._tokens_path is not None:
            self._enc_tokens[sub.submission_id] = _encrypt_token(
                self._token_key, repo_token
            )
            self._persist_tokens()

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
            github_owner=record.get("github_owner"),
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
            is_copycat=bool(record.get("is_copycat", False)),
            coined_by_hotkey=record.get("coined_by_hotkey"),
            max_region_nodes=record.get("max_region_nodes"),
            content_fingerprint=record.get("content_fingerprint"),
            unproductive_nodes=record.get("unproductive_nodes"),
            unproductive_metric_version=record.get("unproductive_metric_version"),
            unproductive_top_offenders=record.get("unproductive_top_offenders"),
            benchmark_rank=record.get("benchmark_rank"),
            benchmark_details=record.get("benchmark_details"),
            rejection_reason=record.get("rejection_reason"),
            outcome_code=record.get("outcome_code"),
            waitlist=record.get("waitlist"),
        )
        # _submissions is the source of truth for list_by_round / the pack hash;
        # the indexes are best-effort lookups (last-wins is fine, they aren't
        # consulted by the pack hash). Copy-on-write (rebind, never mutate the
        # published dict in place) keeps lock-free loop-thread readers safe
        # against a concurrent writer-thread insert.
        self._submissions = {**self._submissions, sid: sub}
        self._by_hotkey_round[f"{sub.hotkey}:{sub.round_id}"] = sid
        self._by_hotkey_epoch[f"{sub.hotkey}:{sub.epoch}"] = sid
        return sub

    @_write_locked
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

    @_write_locked
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

    def count_by_owner_round(self, github_owner: str, round_id: str) -> int:
        """Number of submissions for ``round_id`` made by ``github_owner`` — the
        head-repo GitHub account — across ALL hotkeys. The submission gate reads
        this to enforce the per-account cap BEFORE any expensive work. Case-
        insensitive (GitHub logins are), so ``Alice`` and ``alice`` count as one.
        An empty/None owner returns 0 (inline-source has no GitHub identity)."""
        self._maybe_reload()
        owner = (github_owner or "").lower()
        if not owner:
            return 0
        return sum(
            1 for s in self._submissions.values()
            if (s.github_owner or "").lower() == owner and s.round_id == round_id
        )

    def count_benched_rounds_by_commit(self, hotkey: str, commit_hash: str) -> int:
        """DISTINCT rounds where this miner's exact commit occupied a benchmark
        slate slot (status in ``BENCHED_STATUSES``).

        The submission gate reads this to enforce the per-commit participation
        cap BEFORE any expensive work. Scoped to the (hotkey, commit) pair —
        another miner submitting the same commit can't burn this miner's quota.
        Case-insensitive on the hash (git SHAs are hex). Empty commit returns 0.
        """
        self._maybe_reload()
        commit = (commit_hash or "").strip().lower()
        if not commit:
            return 0
        return len({
            s.round_id for s in self._submissions.values()
            if s.hotkey == hotkey
            and (s.commit_hash or "").strip().lower() == commit
            and s.status in BENCHED_STATUSES
        })

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
            # Machine-readable outcome derived from the stage + error_code:
            # stage 1 = static policy (or the armed factorization floor), stage 2
            # = build, stage 3 = plan validation.
            if stage == 1 and error_code == "too_entangled":
                sub.outcome_code = OUTCOME_TOO_ENTANGLED
            elif stage == 1:
                sub.outcome_code = OUTCOME_STATIC_CHECKS
            elif stage == 2:
                sub.outcome_code = OUTCOME_BUILD_FAILED
            elif stage == 3:
                sub.outcome_code = OUTCOME_INVALID_PLANS
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
    def set_content_fingerprint(self, submission_id: str, value: str) -> None:
        """Persist the normalized content fingerprint from screening stage 1.

        Same compute-once-read-forever discipline as ``set_max_region_nodes`` —
        every consumer reads the stored value, never recomputes.
        """
        self._maybe_reload()
        sub = self._submissions.get(submission_id)
        if sub is None:
            raise KeyError(f"Submission not found: {submission_id}")
        sub.content_fingerprint = value
        sub.updated_at = time.time()
        self._persist()

    def count_benched_rounds_for_fingerprint(
        self,
        fingerprint: str,
        *,
        exclude_submission_id: str | None = None,
    ) -> int:
        """DISTINCT rounds in which this normalized fingerprint occupied a
        benchmark slot — ACROSS ALL HOTKEYS.

        The cross-hotkey scope is the point: the per-(hotkey, commit) cap gives
        every sybil hotkey its own quota for the same bytes; this counter gives
        the CODE one quota, however many hotkeys ship it. Mirrors the commit
        cap's accounting: only BENCHED statuses count, so rotation-not-selected
        and screening rejections don't burn quota.
        """
        self._maybe_reload()
        rounds: set[str] = set()
        for sub in self._submissions.values():
            if sub.content_fingerprint != fingerprint:
                continue
            if exclude_submission_id and sub.submission_id == exclude_submission_id:
                continue
            if sub.status in BENCHED_STATUSES and sub.round_id:
                rounds.add(sub.round_id)
        return len(rounds)

    @_write_locked
    def set_max_region_nodes(self, submission_id: str, value: int) -> None:
        """Persist the Phase-0 factorization metric computed in screening stage 1.

        Observe-only: nothing gates on this yet. Stored once so every downstream
        consumer READS the same integer (never recomputes), keeping the metric
        consensus-safe across CPython versions.
        """
        self._maybe_reload()
        sub = self._submissions.get(submission_id)
        if sub is None:
            raise KeyError(f"Submission not found: {submission_id}")
        sub.max_region_nodes = value
        sub.updated_at = time.time()
        self._persist()

    @_write_locked
    def set_deadwood_metric(
        self,
        submission_id: str,
        nodes: int | None,
        version: int,
        top_offenders: list | None,
    ) -> None:
        """Persist the Phase-0 deadwood metric computed in screening stage 1.

        Observe-only: nothing gates on this yet. Stored once so every downstream
        consumer READS the same values (never recomputes), keeping the metric
        consensus-safe across CPython versions. ``nodes`` may be None (a
        non-exempt file failed ast.parse) — persisted explicitly so consumers
        can tell "measured unparseable" from "never measured" via ``version``.
        """
        self._maybe_reload()
        sub = self._submissions.get(submission_id)
        if sub is None:
            raise KeyError(f"Submission not found: {submission_id}")
        sub.unproductive_nodes = nodes
        sub.unproductive_metric_version = version
        sub.unproductive_top_offenders = top_offenders
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
    @_write_locked
    def set_solver_info(
        self,
        submission_id: str,
        name: str | None = None,
        version: str | None = None,
    ) -> None:
        """Set solver metadata from screening and apply first-to-coin labeling.

        The display name (``metadata().name``) is self-declared free text, so a
        distinct, non-boilerplate name is coined to the FIRST hotkey that submits
        it; a later DIFFERENT hotkey reusing it is flagged ``is_copycat`` with
        ``coined_by_hotkey`` set to the original owner. Cosmetic only — the name
        feeds no scoring/adoption path. Write-locked so two workers can't both
        coin one name; the registry lives in the ``.names`` sidecar (survives
        restarts and submission pruning). The ``_write_guard`` already reloaded
        ``_submissions`` from disk, so the fetched record is fresh.
        """
        sub = self._submissions.get(submission_id)
        if sub is None:
            raise KeyError(f"Submission not found: {submission_id}")
        sub.solver_name = name
        sub.solver_version = version
        # Recompute copycat state from scratch each call, so a re-screen with a
        # changed name re-evaluates cleanly rather than sticking to a stale flag.
        sub.is_copycat = False
        sub.coined_by_hotkey = None
        norm = _normalize_solver_name(name)
        if norm and norm not in _exempt_names():
            self._maybe_reload_names()
            entry = self._names.get(norm)
            if entry is None:
                self._names[norm] = {
                    "owner_hotkey": sub.hotkey,
                    "coined_at": time.time(),
                    "coined_submission_id": submission_id,
                    "display": name,
                }
                self._persist_names()
            elif entry.get("owner_hotkey") != sub.hotkey:
                sub.is_copycat = True
                sub.coined_by_hotkey = entry.get("owner_hotkey")
        sub.updated_at = time.time()
        self._persist()

    @_write_locked
    def set_benchmark_result(
        self,
        submission_id: str,
        *,
        valid: bool,
        rank: int | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        """Record benchmark results and flip terminal status.

        ``valid`` is the per-order VALIDITY GATE (see
        ``relative_scoring.has_delivered_value_rows``): a submission is SCORED iff it
        delivered a usable output on >= 1 order, else REJECTED. This replaced the
        retired scalar ``benchmark_score > 0`` gate. Adoption itself is decided later
        by the per-order relative rule; this only records ``details`` (the per-order
        raw_output rows), an optional display ``rank``, and the SCORED/REJECTED
        verdict. The display rank is written via :meth:`set_benchmark_rank` in a
        separate pass, so there is no longer a "don't clobber a real score" guard.

        NO RESURRECTION: a terminally REJECTED submission is never flipped back
        to SCORED, no matter what a late benchmark result says. Rotation rejects
        the slate overflow at round close "regardless of benchmark progress" and
        PURGES the private-repo token (irreversibly — memory AND encrypted
        sidecar), but an in-flight bench finishing after that, or a restart
        re-benching an orphaned round, used to resurrect the submission here.
        The resurrected record then ranked (and under the tie-break ladder
        frequently WON) as finalist, certified, and died at relayer-finalize
        "no token — FAIL-CLOSED", aborting the round (observed live 2026-07-07:
        5 consecutive merge_failed rounds). Bench details are still recorded so
        the miner's report shows how they scored; the terminal status and its
        reason are immutable.
        """
        self._maybe_reload()
        sub = self._submissions.get(submission_id)
        if sub is None:
            raise KeyError(f"Submission not found: {submission_id}")

        if rank is not None:
            sub.benchmark_rank = rank
        if details is not None:
            sub.benchmark_details = details

        if sub.status == SubmissionStatus.REJECTED:
            logger.info(
                "set_benchmark_result: %s is terminally REJECTED (%s) — "
                "recording bench details but NOT resurrecting to SCORED",
                submission_id, (sub.rejection_reason or "?")[:80],
            )
            sub.updated_at = time.time()
            self._persist()
            return

        # The validity gate: no order delivered value -> the solver produced no
        # usable plans, reject it instead of marking it scored.
        if not valid:
            sub.status = SubmissionStatus.REJECTED
            sub.rejection_reason = (
                sub.rejection_reason
                or "no order delivered value (solver produced no valid plans)"
            )
            self.purge_token(submission_id)  # terminal — drop the secret
        else:
            sub.status = SubmissionStatus.SCORED
            # A legitimately scored submission carries no rejection: clear any
            # stale reason so the miner-facing headline can never contradict the
            # outcome (a terminally rejected sub never reaches this branch).
            sub.rejection_reason = None
        sub.updated_at = time.time()
        self._persist()

    @_write_locked
    def set_benchmark_rank(self, submission_id: str, rank: int) -> None:
        """Set the DISPLAY rank only (the relative net-better ordering), no status flip.

        Replaces the old rank-only re-call of :meth:`set_benchmark_result` — the
        display-rank pass must never re-evaluate the SCORED/REJECTED verdict.
        """
        self._maybe_reload()
        sub = self._submissions.get(submission_id)
        if sub is None:
            raise KeyError(f"Submission not found: {submission_id}")
        sub.benchmark_rank = rank
        sub.updated_at = time.time()
        self._persist()

    @_write_locked
    def merge_benchmark_details(
        self,
        submission_id: str,
        extra: dict[str, Any],
    ) -> None:
        """Merge ``extra`` keys into a submission's ``benchmark_details`` in place,
        WITHOUT touching its score, rank, or status.

        Unlike :meth:`set_benchmark_result` (which replaces the whole details blob
        and flips status to SCORED/REJECTED), this only adds/overwrites the named
        top-level keys, preserving ``per_intent`` / ``scorecard`` / everything else.
        Used to attach the DISPLAY-ONLY same-pin ``relative`` count block computed
        at round evaluation; it must never mutate the authoritative score/status.
        """
        self._maybe_reload()
        sub = self._submissions.get(submission_id)
        if sub is None:
            raise KeyError(f"Submission not found: {submission_id}")
        details = dict(sub.benchmark_details or {})
        details.update(extra)
        sub.benchmark_details = details
        sub.updated_at = time.time()
        self._persist()

    @_write_locked
    def reject(
        self, submission_id: str, reason: str, *, outcome_code: str | None = None,
    ) -> None:
        """Reject a submission FOR CAUSE (the miner must change something).

        ``outcome_code`` is the machine-readable OUTCOME_* the frontend switches
        on; ``reason`` is the human prose. Kept optional so the many existing
        call sites keep compiling — sites are being tagged with a code
        incrementally.
        """
        self._maybe_reload()
        sub = self._submissions.get(submission_id)
        if sub is None:
            raise KeyError(f"Submission not found: {submission_id}")
        sub.status = SubmissionStatus.REJECTED
        sub.rejection_reason = reason
        sub.outcome_code = outcome_code
        sub.updated_at = time.time()
        self.purge_token(submission_id)  # terminal — drop the secret
        self._persist()

    @_write_locked
    def waitlist(
        self,
        submission_id: str,
        reason: str,
        *,
        outcome_code: str,
        position: int | None = None,
        contenders: int | None = None,
        slots: int | None = None,
    ) -> None:
        """Mark a submission WAITLISTED — a NO-FAULT outcome (not selected onto
        this round's slate, or the bench window elapsed).

        Distinct from :meth:`reject`: the miner did nothing wrong, keeps
        next-round seniority, and the UI must render it as "queued for next
        round", never as a refusal. ``position``/``contenders``/``slots`` (when
        known — rotation supplies them) populate the ``waitlist`` context the
        status API and PR comment surface. The private token is purged like any
        terminal-for-this-round state (a resubmit next round carries its own).
        """
        self._maybe_reload()
        sub = self._submissions.get(submission_id)
        if sub is None:
            raise KeyError(f"Submission not found: {submission_id}")
        sub.status = SubmissionStatus.WAITLISTED
        sub.rejection_reason = reason
        sub.outcome_code = outcome_code
        sub.waitlist = {
            "position": position,
            "contenders": contenders,
            "slots": slots,
            "next_round_priority": True,
        }
        sub.updated_at = time.time()
        self.purge_token(submission_id)
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
        # Terminal for the credential too: adoption only happens AFTER the
        # relayer's attest + merge consumed the token (merge-gate ordering).
        self.purge_token(submission_id)
        self._persist()

    def get_champion(self) -> Any:
        """Return the currently adopted champion submission, or None."""
        adopted = self.list_by_status(SubmissionStatus.ADOPTED)
        return adopted[0] if adopted else None

    # ── Private-submission token handling (transient secret) ─────────────────

    def get_repo_token(self, submission_id: str) -> str | None:
        """Return the per-submission private-repo PAT, or None.

        Served from the in-process cache first, then from the encrypted
        sidecar (which survives restarts and is shared across workers). With
        no encryption key configured the token is in-memory only and
        unavailable after a restart (the miner re-submits in that case).
        """
        token = self._tokens.get(submission_id)
        if token is not None:
            return token
        if self._token_key is None or self._tokens_path is None:
            return None
        self._maybe_reload_tokens()
        blob = self._enc_tokens.get(submission_id)
        if blob is None:
            return None
        try:
            token = _decrypt_token(self._token_key, blob)
        except Exception:
            # Fail-closed, same outcome as a lost token: the miner re-submits.
            logger.warning(
                "Could not decrypt persisted repo token for %s "
                "(VALIDATOR_PRIVATE_KEY rotated?) — treating as absent",
                submission_id,
            )
            return None
        self._tokens[submission_id] = token
        return token

    @_write_locked
    def purge_token(self, submission_id: str) -> None:
        """Drop the private-repo token once it is no longer needed.

        Called when a submission reaches a terminal state (or after a successful
        private-champion publish) to minimise how long the credential lives.
        Removes both the in-memory copy and the encrypted sidecar entry.
        Idempotent and never raises for an unknown id. Write-locked because the
        sidecar rewrite prunes against ``_submissions``, which must be fresh
        when several workers share the backing files.
        """
        self._tokens.pop(submission_id, None)
        if self._tokens_path is not None:
            self._maybe_reload_tokens()
            if self._enc_tokens.pop(submission_id, None) is not None:
                self._persist_tokens()

    def _persist_tokens(self) -> None:
        """Atomically rewrite the encrypted-token sidecar (0600).

        Prunes entries whose submission no longer exists so the sidecar cannot
        accumulate ciphertext for records that were purged from the store.
        """
        if self._tokens_path is None:
            return
        try:
            self._enc_tokens = {
                sid: blob
                for sid, blob in self._enc_tokens.items()
                if sid in self._submissions
            }
            self._tokens_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = self._tokens_path.with_name(
                f".{self._tokens_path.name}.{os.getpid()}.tmp"
            )
            tmp_path.touch(mode=0o600)
            tmp_path.write_text(json.dumps(self._enc_tokens, indent=2))
            os.replace(tmp_path, self._tokens_path)
            self._tokens_mtime_ns = self._tokens_path.stat().st_mtime_ns
        except Exception as exc:
            logger.warning("Failed to persist submission tokens: %s", exc)

    def _load_tokens(self) -> None:
        """Load the encrypted-token sidecar (ciphertext only — no decryption)."""
        if self._tokens_path is None:
            return
        try:
            data = json.loads(self._tokens_path.read_text())
            self._enc_tokens = {
                str(sid): str(blob) for sid, blob in data.items()
            }
            self._tokens_mtime_ns = self._tokens_path.stat().st_mtime_ns
        except Exception as exc:
            logger.warning("Failed to load submission tokens: %s", exc)

    def _maybe_reload_tokens(self) -> None:
        """Re-read the sidecar when another process updated it."""
        if self._tokens_path is None or not self._tokens_path.exists():
            return
        try:
            current_mtime_ns = self._tokens_path.stat().st_mtime_ns
        except OSError:
            return
        if self._tokens_mtime_ns is None or current_mtime_ns > self._tokens_mtime_ns:
            self._load_tokens()

    # ── Solver-name coinage sidecar ──────────────────────────────────────────

    def _persist_names(self) -> None:
        """Atomically rewrite the coinage registry sidecar.

        Kept OUTSIDE the main submissions JSON on purpose: that file is a flat
        ``{id: submission}`` map (a stray top-level key breaks the loader) and is
        pruned, whereas name ownership must be permanent. Same atomic temp-write
        + ``os.replace`` as the other sidecars.
        """
        if self._names_path is None:
            return
        try:
            self._names_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = self._names_path.with_name(
                f".{self._names_path.name}.{os.getpid()}.tmp"
            )
            tmp_path.write_text(json.dumps(self._names, indent=2))
            os.replace(tmp_path, self._names_path)
            self._names_mtime_ns = self._names_path.stat().st_mtime_ns
        except Exception as exc:
            logger.warning("Failed to persist solver-name registry: %s", exc)

    def _load_names(self) -> None:
        """Load the coinage registry sidecar."""
        if self._names_path is None:
            return
        try:
            data = json.loads(self._names_path.read_text())
            self._names = {str(k): dict(v) for k, v in data.items()}
            self._names_mtime_ns = self._names_path.stat().st_mtime_ns
        except Exception as exc:
            logger.warning("Failed to load solver-name registry: %s", exc)

    def _maybe_reload_names(self) -> None:
        """Re-read the coinage sidecar when another process updated it."""
        if self._names_path is None or not self._names_path.exists():
            return
        try:
            current_mtime_ns = self._names_path.stat().st_mtime_ns
        except OSError:
            return
        if self._names_mtime_ns is None or current_mtime_ns > self._names_mtime_ns:
            self._load_names()

    def _backfill_names(self) -> None:
        """Seed the registry from existing submissions (earliest coiner wins).

        Runs once when the sidecar doesn't exist yet, so the true originator of a
        name owns it rather than crediting whoever submits first after this
        ships. Seeds ownership ONLY; it does not retroactively re-flag historical
        submissions (that would rewrite the whole store) — labeling is
        forward-looking from here.
        """
        if self._names_path is None:
            return
        exempt = _exempt_names()
        registry: dict[str, dict[str, Any]] = {}
        for s in sorted(
            self._submissions.values(),
            key=lambda x: (x.created_at or 0.0, x.submission_id),
        ):
            norm = _normalize_solver_name(s.solver_name)
            if not norm or norm in exempt or norm in registry:
                continue
            registry[norm] = {
                "owner_hotkey": s.hotkey,
                "coined_at": s.created_at or 0.0,
                "coined_submission_id": s.submission_id,
                "display": s.solver_name,
            }
        self._names = registry
        self._persist_names()
        logger.info("Backfilled %d coined solver name(s) from history", len(registry))

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

    # ── Async offload ──────────────────────────────────────────────────────

    def _get_writer_executor(self) -> ThreadPoolExecutor:
        """Lazily create the single dedicated writer thread."""
        if self._writer_executor is None:
            self._writer_executor = ThreadPoolExecutor(
                max_workers=1, thread_name_prefix="substore-writer"
            )
        return self._writer_executor

    async def aoffload(self, fn, /, *args, **kwargs):
        """Run a synchronous ``@_write_locked`` mutator on the dedicated writer
        thread, off the event loop, and await its completion.

        The whole locked critical section (advisory flock + in-process RLock →
        ``_load`` reload → mutate → ``_persist`` write) executes on the single
        writer thread, so the ~44MB ``json.loads``/``json.dumps`` no longer
        block the asyncio loop. Because the one thread runs writes serially and
        the RLock still serializes against any sync on-loop writer, ordering is
        preserved. Awaited, so the atomic ``os.replace`` has landed before the
        caller proceeds — keeping persist-before-broadcast ordering for the
        consensus callers. Pass a BOUND mutator, e.g.
        ``await store.aoffload(store.set_benchmark_result, sid, result)``.
        """
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            self._get_writer_executor(),
            functools.partial(fn, *args, **kwargs),
        )

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
            self._enforce_benchmark_details_retention()
            data = {
                sid: sub.to_dict()
                for sid, sub in self._submissions.items()
            }
            self._persist_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = self._persist_path.with_name(
                f".{self._persist_path.name}.{os.getpid()}.tmp"
            )
            # Compact (no indent): the store is re-serialized on EVERY write, so
            # pretty-printing ~doubles both the bytes written and the encode time
            # on the (previously loop-blocking) hot path for zero machine benefit.
            tmp_path.write_text(json.dumps(data))
            os.replace(tmp_path, self._persist_path)
            self._persist_mtime_ns = self._persist_path.stat().st_mtime_ns
        except Exception as exc:
            logger.warning("Failed to persist submissions: %s", exc)

    def _enforce_benchmark_details_retention(self) -> None:
        """Drop benchmark_details from terminal submissions beyond the retention
        cap so the persisted store stays bounded.

        benchmark_details is ~40-70KB per submission; unbounded it grew the store
        to 142MB, and _persist re-serializes the WHOLE store on every write on the
        event loop → ~25s api-wide freezes. We keep details for all non-terminal
        (in-flight) submissions plus the ``_BENCHMARK_DETAILS_RETENTION`` most
        recent by epoch, and strip the rest in place. In-memory mutation is fine:
        the field is Optional and every reader uses ``.get()``/``or {}``.
        """
        cap = _BENCHMARK_DETAILS_RETENTION
        if cap <= 0:
            return
        # Candidates: terminal submissions that still carry details.
        withdetails = [
            s for s in self._submissions.values()
            if s.status in _DETAILS_STRIPPABLE_STATUSES and s.benchmark_details
        ]
        if len(withdetails) <= cap:
            return
        # Keep the `cap` most recent by epoch; strip the older tail.
        withdetails.sort(key=lambda s: s.epoch, reverse=True)
        for sub in withdetails[cap:]:
            sub.benchmark_details = None

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
                    github_owner=d.get("github_owner"),
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
                    is_copycat=bool(d.get("is_copycat", False)),
                    coined_by_hotkey=d.get("coined_by_hotkey"),
                    max_region_nodes=d.get("max_region_nodes"),
                    content_fingerprint=d.get("content_fingerprint"),
                    unproductive_nodes=d.get("unproductive_nodes"),
                    unproductive_metric_version=d.get("unproductive_metric_version"),
                    unproductive_top_offenders=d.get("unproductive_top_offenders"),
                    benchmark_rank=d.get("benchmark_rank"),
                    benchmark_details=d.get("benchmark_details"),
                    rejection_reason=d.get("rejection_reason"),
                    outcome_code=d.get("outcome_code"),
                    waitlist=d.get("waitlist"),
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
