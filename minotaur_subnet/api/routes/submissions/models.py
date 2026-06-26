"""Pydantic request/response models for solver submission endpoints."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


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


class SubmitResponse(BaseModel):
    submission_id: str
    status: str
    status_url: str
    round_id: str
    epoch: int


class StatusResponse(BaseModel):
    submission_id: str
    status: str
    round_id: str | None = None
    screening: dict[str, Any]
    image_tag: str | None = None
    image_id: str | None = None
    solver_name: str | None = None
    solver_version: str | None = None
    benchmark_score: float | None = None
    benchmark_rank: int | None = None
    rejection_reason: str | None = None
    # Feedback report (P1): per-case scores + aggregate-vs-champion + worst cases.
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
    finalist_submission_id: str | None = None
    finalist_image_id: str | None = None
    finalist_score: float | None = None
    shadow_case_log_hash: str | None = None
    effective_epoch: int | None = None
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
    finalist_score: float | None = None
    incumbent_submission_id: str | None = None
    # Outcome: `adopted` is True when the round activated a new champion;
    # `adopted_submission_id` is the certified challenger that won.
    adopted: bool = False
    adopted_submission_id: str | None = None
    effective_epoch: int | None = None
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
    # these so their local pack-hash recompute matches the leader's. Present only
    # when the leader has SUBMISSION_SNAPSHOT_SYNC enabled; None = legacy.
    # max_length bounds a hostile/buggy leader's payload (a real round holds at
    # most one submission per active miner; far below this cap).
    submissions: list[dict[str, Any]] | None = Field(default=None, max_length=1024)
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
    benchmark_pack_hash: str | None = None
    shadow_case_log_hash: str | None = None
    effective_epoch: int = Field(..., ge=0)
    quorum_required: int = Field(0, ge=0)
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
