"""Pydantic request/response models for solver submission endpoints."""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

# owner/repo, GitHub's allowed character set for each segment.
_REPO_FULL_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


class SubmitRequest(BaseModel):
    pr_number: int = Field(
        ...,
        ge=1,
        description=(
            "Pull-request number on the canonical solver repo "
            "(subnet112/minotaur-solver). The miner forks, opens a PR, and submits "
            "its number; the leader resolves it to the fork clone_url + head SHA."
        ),
        examples=[123],
    )
    head_sha: str = Field(
        ...,
        min_length=40, max_length=40,
        description=(
            "Full 40-char PR head commit SHA the signature covers. The leader "
            "rejects the submission if the live PR head != this (force-push guard)."
        ),
    )
    round_id: str | None = Field(
        default=None,
        description=(
            "Solver round ID this submission targets (from GET /v1/solver/round). "
            "When present it must match the current open round, else 409. "
            "Signatures are always verified against the current open round."
        ),
        min_length=1,
    )
    epoch: int = Field(..., description="Current epoch number", ge=0)
    hotkey: str = Field(
        ..., description="Miner's Bittensor hotkey (SS58)",
        min_length=10,
    )
    signature: str = Field(
        ..., description="Signature proving hotkey ownership (base64-encoded)",
        min_length=1,
    )

    # ── Private-submission path (opt-in) ────────────────────────────────────
    # When both are present the PR lives in the miner's OWN private repo instead
    # of the canonical public solver repo. The token is transport credential
    # ONLY (it is not part of the signed message) and is used by the leader for
    # the duration of this one submission: clone (Contents:Read), resolve the PR
    # + post benchmark/error comments (Pull requests:Read+Write). It is never
    # persisted and is purged when the submission reaches a terminal state. The
    # winning code is published to canonical main on adoption (leak-on-champion).
    private_repo: str | None = Field(
        default=None,
        description=(
            "Full 'owner/repo' of the miner's PRIVATE solver repo. When set, "
            "the submission uses the private path: pr_number/head_sha refer to a "
            "PR in THIS repo, not the canonical solver repo. Requires repo_token."
        ),
        examples=["my-org/my-private-solver"],
    )
    repo_token: str | None = Field(
        default=None,
        description=(
            "Fine-grained GitHub PAT scoped to private_repo, valid for this "
            "submission only (Metadata:Read, Contents:Read, Pull requests:Read+Write). "
            "Transport credential — NOT covered by the signature, never persisted, "
            "purged on terminal state. Send over HTTPS only."
        ),
        min_length=1,
    )

    @model_validator(mode="after")
    def _validate_private_pair(self) -> SubmitRequest:
        """private_repo and repo_token are all-or-nothing; validate the repo shape."""
        if bool(self.private_repo) != bool(self.repo_token):
            raise ValueError(
                "private_repo and repo_token must be provided together "
                "(both for a private submission, or neither for the public path)"
            )
        if self.private_repo and not _REPO_FULL_RE.match(self.private_repo):
            raise ValueError(
                f"private_repo must be 'owner/repo', got {self.private_repo!r}"
            )
        return self

    @property
    def is_private(self) -> bool:
        """True when this submission targets the miner's private repo."""
        return bool(self.private_repo and self.repo_token)


class SubmitResponse(BaseModel):
    submission_id: str
    status: str
    status_url: str
    round_id: str
    epoch: int


class DiagnosticScoreRequest(BaseModel):
    """Score an arbitrary image through the EXACT challenger path — diagnostic only.

    No submission, no round, never adoption-eligible. Used to score a known solver
    (e.g. a king-clone, image digest sha256:...) against the live champion reference
    anchor + round pin, to verify scoring symmetry.
    """
    image: str = Field(
        ..., min_length=1,
        description="Image tag or digest to benchmark as a challenger (e.g. ghcr.io/...@sha256:...)",
    )
    label: str | None = Field(default=None, description="Optional human label for logs/results")


class StatusResponse(BaseModel):
    submission_id: str
    status: str
    round_id: str | None = None
    screening: dict[str, Any]
    image_tag: str | None = None
    image_id: str | None = None
    solver_name: str | None = None
    solver_version: str | None = None
    # Factorization metric (Phase 0, OBSERVE-ONLY): the largest AST-node count of
    # any single named region (module / function / class body) in the submission's
    # in-tree Python — a golf-immune proxy for worst entanglement. Measured and
    # surfaced, NOT gated yet. Present once screening stage 1 completes; None while
    # queued. See harness/screening.max_region_nodes.
    max_region_nodes: int | None = None
    # benchmark_score (the retired scalar composite) was removed; benchmark_rank is
    # the DISPLAY rank derived from relative net-better vs the champion.
    benchmark_rank: int | None = None
    # SN112 UID of the submitting hotkey, looked up in the CURRENT metagraph at
    # read time (not a historical snapshot). null when the metagraph hasn't
    # synced yet or the hotkey is no longer registered — deregistered miners
    # lose their UID and re-registration can reassign a UID to a different
    # hotkey, so old submissions from churned hotkeys resolve to null by design.
    miner_uid: int | None = None
    rejection_reason: str | None = None
    # Feedback report (P1): the same-pin per-order ``relative`` block (better /
    # worse / matched / new + per-order deltas) and a verdict-derived outcome.
    # The legacy aggregate-vs-champion scalars were removed (see report.py).
    # Present once benchmarked (or screening-rejected); None while in-flight.
    report: dict[str, Any] | None = None


class SourceSubmitRequest(BaseModel):
    """Request body for source-based submission (no git/Docker)."""
    solver_source: str = Field(
        ...,
        description="Complete Python source of the solver",
        max_length=500_000,
    )
    hotkey: str = Field("local-miner", description="Miner identifier")
    round_id: str | None = Field(
        default=None,
        description="Current solver round ID",
        min_length=1,
    )
    epoch: int = Field(0, description="Epoch number", ge=0)
    solver_name: str = Field("", description="Solver name")


class SolverRoundResponse(BaseModel):
    # ``extra="allow"`` lets the round-state builder ATTACH the additive relative
    # fields (``scoring_mode``, ``finalist_relative``, ``reason_relative``) — the
    # relative rule is the sole adoption path, so these are always passed as
    # constructor extras and serialized. See round_manager._round_relative_extra.
    model_config = ConfigDict(extra="allow")

    round_id: str
    status: str
    accepting_submissions: bool
    opened_epoch: int
    close_epoch: int | None = None
    incumbent_submission_id: str | None = None
    incumbent_image_id: str | None = None
    benchmark_pack_hash: str | None = None
    committee_block: int | None = None
    committee_hash: str | None = None
    quorum_required: int | None = None
    decision_deadline_epoch: int | None = None
    # Wall-clock unix seconds for the epoch fields above/below (epoch *
    # EPOCH_SECONDS — round epochs are wall-clock buckets), so API consumers can
    # render "activates at" without knowing the epoch width.
    decision_deadline_at: float | None = None
    finalist_submission_id: str | None = None
    finalist_image_id: str | None = None
    shadow_case_log_hash: str | None = None
    effective_epoch: int | None = None
    effective_at: float | None = None
    abort_reason: str | None = None
    certificate_candidate_submission_id: str | None = None
    certificate_candidate_image_id: str | None = None
    certificate_quorum_required: int | None = None
    certificate_approvals: int = 0


class SolverChampionResponse(BaseModel):
    submission_id: str | None = None
    image_id: str | None = None
    solver_name: str | None = None
    solver_version: str | None = None
    hotkey: str | None = None
    activated_round_id: str | None = None
    activated_epoch: int = 0
    activated_at: float = 0.0


class SolverRoundSummary(BaseModel):
    """Compact per-round history row (for GET /v1/solver/rounds list views)."""
    round_id: str
    status: str
    opened_epoch: int = 0
    close_epoch: int | None = None
    finalist_submission_id: str | None = None
    incumbent_submission_id: str | None = None
    # Outcome: `adopted` is True when the round activated a new champion;
    # `adopted_submission_id` is the certified challenger that won.
    adopted: bool = False
    adopted_submission_id: str | None = None
    effective_epoch: int | None = None
    # Unix seconds for effective_epoch (see SolverRoundResponse.effective_at).
    effective_at: float | None = None
    abort_reason: str | None = None
    created_at: float = 0.0
    updated_at: float = 0.0


class SolverRoundsResponse(BaseModel):
    """Paginated solver-round history, newest first."""
    total: int = 0
    limit: int = 0
    offset: int = 0
    rounds: list[SolverRoundSummary] = []


class CloseRoundRequest(BaseModel):
    round_id: str | None = None
    close_epoch: int = Field(..., ge=0)
    benchmark_pack_hash: str | None = None
    committee_block: int | None = Field(default=None, ge=0)
    committee_hash: str | None = None
    quorum_required: int | None = Field(default=None, ge=0)
    decision_deadline_epoch: int | None = Field(default=None, ge=0)
    effective_epoch: int | None = Field(default=None, ge=0)
    # Leader's close-time submission snapshot (full records). Followers upsert
    # these so their local pack-hash recompute matches the leader's. Always sent by
    # an up-to-date leader (the SUBMISSION_SNAPSHOT_SYNC gate was removed — it is now
    # required for cross-host determinism); None only from a legacy/pre-snapshot leader.
    # max_length bounds a hostile/buggy leader's payload (a real round holds at
    # most one submission per active miner; far below this cap).
    submissions: list[dict[str, Any]] | None = Field(default=None, max_length=1024)
    # Operator force-sync ("emergency reattach"): when True the follower adopts this round
    # even if it is older-or-equal to its current round (bypasses the adopt-if-behind
    # staleness guard). Used by the champion re-attest lever to remind a follower of a
    # champion round it never saw / has pruned, so it re-activates the standing champion.
    # Carried inside the signed payload like every other field.
    force: bool = False
    # Leader EIP-712 signature over the canonical JSON of this sync payload
    # (with proposer_signature stripped). Backward-compatible: empty during the
    # staggered rollout, when followers fall back to the shared-key header.
    proposer: str = ""
    proposer_signature: str = ""


class ChampionApprovalPayload(BaseModel):
    validator_id: str
    timestamp: float = 0.0
    signature: str = ""
    committee_hash: str | None = None
    incumbent_image_id: str | None = None
    candidate_submission_id: str | None = None
    candidate_image_id: str | None = None
    benchmark_pack_hash: str | None = None
    shadow_case_log_hash: str | None = None
    effective_epoch: int | None = None
    # v2 signed fields
    commit_hash: str | None = None
    nonce: int = 0
    deadline: int = 0


class CertifyRoundRequest(BaseModel):
    round_id: str
    candidate_submission_id: str | None = None
    candidate_image_id: str | None = None
    committee_hash: str | None = None
    # The incumbent (previous champion) image id is part of the SIGNED proposal
    # digest but is NOT reproducible across hosts (a local {{.Id}} at quorum<=1),
    # so — like commit_hash/nonce/deadline — the leader's signed value MUST ride in
    # this payload. Without it a follower rebuilds the incumbent from its OWN round
    # record, the digest diverges, and the leader's approval is rejected as
    # "Invalid champion approvals" — stranding the round leader-only.
    incumbent_image_id: str | None = None
    benchmark_pack_hash: str | None = None
    shadow_case_log_hash: str | None = None
    effective_epoch: int = Field(..., ge=0)
    quorum_required: int = Field(0, ge=0)
    # v2 EIP-712 digest fields from the leader's SIGNED proposal. MUST be declared so
    # the /certify broadcast can carry the leader's signed commit_hash/nonce/deadline to
    # a follower — otherwise Pydantic drops them (default extra="ignore") and
    # _certify_solver_round_state's body.commit_hash/nonce/deadline raise AttributeError
    # (and a follower would rebuild the proposal with its OWN wall-clock nonce → the
    # digest diverges → "Invalid champion approvals" → quorum stranded leader-only).
    # Mirrors ChampionConsensusProposalRequest's v2 fields.
    commit_hash: str | None = None
    nonce: int = 0
    deadline: int = 0
    approvals: list[ChampionApprovalPayload] = Field(default_factory=list)
    # Operator override for the public certify endpoint: certify a candidate
    # that is NOT the round's rule-selected finalist (and not genesis/builtin).
    # Audited (logged loudly). Default off so the public endpoint can't silently
    # bypass the adoption rule.
    force: bool = False
    # Leader EIP-712 signature over the canonical JSON of this sync payload
    # (with proposer_signature stripped). Empty during the staggered rollout.
    proposer: str = ""
    proposer_signature: str = ""


class ActivateRoundRequest(BaseModel):
    round_id: str
    activation_epoch: int = Field(..., ge=0)
    # The leader's adopt outcome: True if the leader finalized (adopted) the champion,
    # False if it refused (merge_failed). A follower self-adopts weights only when this
    # is not explicitly False. None = absent (old leader / staggered rollout) => the
    # follower keeps legacy behavior, never stranded. Signed as part of the payload.
    champion_changed: bool | None = None
    # Leader EIP-712 signature over the canonical JSON of this sync payload
    # (with proposer_signature stripped). Empty during the staggered rollout.
    proposer: str = ""
    proposer_signature: str = ""


class AbortRoundRequest(BaseModel):
    round_id: str
    reason: str = Field(..., min_length=1, max_length=256)
    # Leader EIP-712 signature over the canonical JSON of this sync payload
    # (with proposer_signature stripped). Empty during the staggered rollout.
    proposer: str = ""
    proposer_signature: str = ""


class ChampionConsensusProposalRequest(BaseModel):
    round_id: str
    candidate_submission_id: str
    candidate_image_id: str
    committee_hash: str | None = None
    incumbent_image_id: str | None = None
    benchmark_pack_hash: str | None = None
    shadow_case_log_hash: str | None = None
    effective_epoch: int = Field(..., ge=0)
    close_epoch: int | None = Field(default=None, ge=0)
    quorum_required: int | None = Field(default=None, ge=0)
    decision_deadline_epoch: int | None = Field(default=None, ge=0)
    committee_block: int | None = Field(default=None, ge=0)
    # v2 signed fields — leader propagates to peers so they can rebuild and
    # verify the identical digest.
    commit_hash: str | None = None
    nonce: int = 0
    deadline: int = 0
    # Per-proposer signature over the canonical JSON of this payload (with
    # proposer_signature field stripped). Required when
    # CONSENSUS_REQUIRE_SIGNED_CHAMPION_PROPOSALS=1 — then shared API key
    # alone is no longer enough.
    proposer: str | None = None
    proposer_signature: str | None = None
