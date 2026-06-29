"""Solver submission API route handlers.

Endpoints:
    POST /v1/submissions          -- Submit a new IntentSolver for screening
    GET  /v1/submissions/{id}/status -- Poll submission screening/benchmark status
    GET  /v1/submissions          -- List submissions (filtered by epoch/hotkey)

The submission flow:
1. Miner pushes code to a git repo
2. Miner calls POST /submissions with repo_url + commit_hash
3. The screening service clones the pinned commit and runs the 3-stage pipeline
4. Miner polls GET /submissions/{id}/status to track progress

Production expects publicly reachable repos. For controlled demos, the screening
service can be given a separate read-only HTTPS clone credential.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import tempfile
import time
import uuid
from collections import deque
from threading import Lock
from typing import Any
from urllib.parse import urlparse

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Request

from minotaur_subnet.harness.submission_store import SubmissionStatus
from minotaur_subnet.harness.round_store import RoundStatus

from .models import (
    AbortRoundRequest,
    ActivateRoundRequest,
    CertifyRoundRequest,
    ChampionConsensusProposalRequest,
    CloseRoundRequest,
    DiagnosticScoreRequest,
    SolverChampionResponse,
    SolverRoundResponse,
    SolverRoundSummary,
    SolverRoundsResponse,
    SourceSubmitRequest,
    StatusResponse,
    SubmitRequest,
    SubmitResponse,
)
from .state import (
    _rate_limit_buckets,
    _rate_limit_lock,
    get_benchmark_worker,
    get_champion_consensus_manager,
    get_round_store,
    get_store,
)
from .round_manager import (
    _abort_solver_round_state,
    _activate_solver_round_state,
    _broadcast_internal_round_sync,
    _close_solver_round_state,
    _close_round_sync_payload,
    _certify_round_sync_payload,
    _activate_round_sync_payload,
    _abort_round_sync_payload,
    _get_current_solver_round,
    _require_open_submission_round,
    _round_state_to_response,
    _sync_abort_solver_round_state,
    _sync_close_solver_round_state,
    _sync_round_incumbent_from_submission_store,
)
from .champion_consensus import (
    _build_champion_proposal_for_round,
    _certify_solver_round_state,
    _maybe_prepare_round_for_certification,
    _reactive_benchmark_candidate,
    _round_certification_deadline_elapsed,
    _sync_certified_round_state,
)
from .screening_pipeline import _run_screening_pipeline

logger = logging.getLogger(__name__)

router = APIRouter(tags=["submissions"])


# ── Auth / validation helpers ───────────────────────────────────────────────


def _env_true(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _require_submissions_enabled() -> None:
    """Global kill switch for new submissions."""
    if not _env_true("SUBMISSIONS_ACCEPTING", default=True):
        raise HTTPException(
            status_code=503,
            detail="Submissions are temporarily disabled by operator policy",
        )


def _require_source_enabled() -> None:
    """Source submissions are dangerous and must be explicitly enabled."""
    if not _env_true("ENABLE_SOURCE_SUBMISSIONS", default=False):
        raise HTTPException(
            status_code=403,
            detail=(
                "Source submissions are disabled by policy. "
                "Use signed git submissions instead."
            ),
        )


def _require_submission_api_key(request: Request) -> None:
    """Optional shared-secret auth for submission endpoints."""
    expected = os.environ.get("SUBMISSIONS_API_KEY", "").strip()
    if not expected:
        return
    provided = request.headers.get("x-submission-api-key", "").strip()
    if provided != expected:
        raise HTTPException(status_code=401, detail="Invalid submission API key")


def _get_internal_round_api_key() -> str:
    """Resolve the shared secret used for validator-to-validator round control."""
    return (
        os.environ.get("SOLVER_ROUND_INTERNAL_API_KEY", "").strip()
        or os.environ.get("SUBMISSIONS_API_KEY", "").strip()
    )


def _require_internal_round_api_key(request: Request) -> None:
    """Shared-secret auth for internal solver-round coordination endpoints.

    Fail-closed semantics (PR-2 of 7-PR security hardening, audit C2):
    when neither ``SOLVER_ROUND_INTERNAL_API_KEY`` nor the legacy
    ``SUBMISSIONS_API_KEY`` is set, this used to silently allow every
    caller. The PR-1 compose fail-fast covers operators who deploy via
    the canonical Docker compose, but anyone running the api directly
    with ``python -m`` would bypass that. Now: missing env → 503 so the
    operator notices immediately.
    """
    expected = _get_internal_round_api_key()
    if not expected:
        raise HTTPException(
            status_code=503,
            detail=(
                "Champion consensus disabled: operator must set "
                "SOLVER_ROUND_INTERNAL_API_KEY (or legacy SUBMISSIONS_API_KEY) "
                "to enable validator-to-validator coordination endpoints."
            ),
        )
    provided = request.headers.get("x-solver-round-internal-key", "").strip()
    if provided != expected:
        raise HTTPException(status_code=401, detail="Invalid internal solver round key")


def _verify_internal_round_signature(raw: dict[str, Any]) -> str | None:
    """Verify a leader EIP-712 signature over a raw lifecycle sync payload.

    Mirrors ``_verify_champion_proposal_signature`` exactly, but operates over
    the RAW request dict (lifecycle payloads are plain dicts built by the
    round_manager payload builders, not a pydantic body). Canonicalizes the
    payload with only ``proposer_signature`` stripped (``proposer`` is KEPT),
    recovers the signer, checks recovered == declared proposer, then applies
    the locked-leader lock (if set) else the on-chain BT-EVM ValidatorRegistry
    (chain 964) check when enforcement is enabled.

    Returns an error string on failure, or None on pass. Callers MUST treat a
    non-None result as a hard 401 (never fall through to the shared key).
    """
    signer_declared = str(raw.get("proposer", "") or "").strip()
    sig_hex = str(raw.get("proposer_signature", "") or "").strip()
    if not signer_declared or not sig_hex:
        return "Missing proposer / proposer_signature"

    try:
        from eth_account import Account
        from eth_account.messages import encode_defunct
        import json as _json
    except Exception as exc:
        return f"eth_account unavailable: {exc}"

    payload = dict(raw)
    payload.pop("proposer_signature", None)
    canonical = _json.dumps(payload, sort_keys=True, separators=(",", ":"))

    try:
        recovered = Account.recover_message(
            encode_defunct(text=canonical),
            signature=sig_hex,
        )
    except Exception as exc:
        return f"Signature recovery failed: {exc}"

    if recovered.lower() != signer_declared.lower():
        return (
            f"Signer mismatch: signature recovers to {recovered[:10]}... "
            f"but proposer declared {signer_declared[:10]}..."
        )

    # Leader-lock: when LOCKED_LEADER_EVM_ADDRESS is set, only that signer may
    # drive round lifecycle. Falls back to the on-chain registry check only
    # when the lock is cleared. Mirrors _verify_champion_proposal_signature.
    from minotaur_subnet.validator.metagraph_sync import (
        LOCKED_LEADER_EVM_ADDRESS,
    )
    if LOCKED_LEADER_EVM_ADDRESS:
        if recovered.lower() != LOCKED_LEADER_EVM_ADDRESS.lower():
            return (
                f"Signer {recovered[:10]}... is not the locked leader "
                f"({LOCKED_LEADER_EVM_ADDRESS})"
            )
        return None

    from minotaur_subnet.consensus.validator_registry_cache import (
        is_on_chain_validator, enforce_enabled,
    )
    if enforce_enabled():
        if not is_on_chain_validator(recovered, 964):
            return f"Signer {recovered[:10]}... is not a registered validator"
    return None


async def _authorize_internal_round(request: Request) -> None:
    """Authorize a cross-validator round-lifecycle broadcast.

    Backward-compatible auth for the staggered EIP-712 migration:

      1. If the body carries a non-empty ``proposer`` + ``proposer_signature``,
         verify the leader signature. A valid signature authorizes the call.
         A PRESENT-but-INVALID signature is ALWAYS a 401 — it must NOT fall
         through to the shared-key path, so a forged sig can't be retried as
         a key.
      2. If no signature is present and ``REQUIRE_SIGNED_ROUND_LIFECYCLE`` is
         set, reject (the rollout has completed; unsigned legacy leaders are
         no longer accepted).
      3. Otherwise fall back to the legacy shared-key path
         (``_require_internal_round_api_key``) so not-yet-upgraded leaders
         still authenticate during the rollout.

    REPLAY SAFETY: lifecycle payloads carry no nonce/timestamp, so a captured
    valid signed broadcast is replayable indefinitely. This is SAFE because the
    auth layer delegates replay protection to two invariants downstream:
    (1) round_ids are epoch-unique (``round-e{epoch}-n{count}``), so a replay
    only ever targets its original, already-transitioned round; and (2) every
    lifecycle handler is idempotent — close/certify/abort/activate short-circuit
    on an already-transitioned round (status guard), and activate additionally
    requires ``status == CERTIFIED`` plus monotonic effective-epoch gating, so a
    replayed certify/activate can neither re-adopt a stale champion nor roll the
    round back. If a future change ever makes a transition non-idempotent OR
    reuses a round_id, add a signed monotonic nonce to the payloads and reject
    stale/duplicate proposals here — the signature alone provides no freshness.

    Starlette caches the request body, so ``await request.json()`` here is
    safe even though the handler also binds a pydantic model parameter.
    """
    try:
        raw = await request.json()
    except Exception:
        raw = None
    if not isinstance(raw, dict):
        raw = {}

    has_sig = bool(
        str(raw.get("proposer", "") or "").strip()
        and str(raw.get("proposer_signature", "") or "").strip()
    )
    if has_sig:
        err = _verify_internal_round_signature(raw)
        if err is not None:
            # Present-but-invalid signature: hard fail, never fall through to
            # the shared key (a forged sig must not be retryable as a key).
            raise HTTPException(
                status_code=401,
                detail=f"Invalid round-lifecycle signature: {err}",
            )
        return

    if _env_true("REQUIRE_SIGNED_ROUND_LIFECYCLE", default=False):
        raise HTTPException(
            status_code=401,
            detail="signed round-lifecycle broadcasts required",
        )

    # Legacy shared-key fallback for not-yet-upgraded leaders.
    _require_internal_round_api_key(request)


async def _authorize_internal_round_sync(request: Request) -> None:
    """Auth for the cross-validator ``/internal/`` round-lifecycle RECEIVERS — EIP-712 ONLY.

    The leader EIP-712-signs every coordinator broadcast (#337) and the follower
    verifies it against the locked-leader / on-chain ValidatorRegistry, so a valid
    ``proposer`` + ``proposer_signature`` is the SOLE accepted credential here. There
    is NO legacy shared-key fallback: the shared secret never worked across
    independent operators (each runs its own ``SOLVER_ROUND_INTERNAL_API_KEY``), which
    is exactly why relying on it 401'd cross-operator round-sync (the responses=0 root
    cause). The operator-facing ENTRY endpoints (``/solver/round/{close,certify,abort,
    activate}``, used by the MCP tools + manual ops) keep the shared key via
    ``_authorize_internal_round``.

    A present-but-invalid signature is a hard 401 (never retryable as anything weaker).
    Replay safety is as in ``_authorize_internal_round`` (epoch-unique round_ids +
    idempotent handlers). Starlette caches the body, so ``await request.json()`` is safe.
    """
    try:
        raw = await request.json()
    except Exception:
        raw = None
    if not isinstance(raw, dict):
        raw = {}
    has_sig = bool(
        str(raw.get("proposer", "") or "").strip()
        and str(raw.get("proposer_signature", "") or "").strip()
    )
    if not has_sig:
        raise HTTPException(
            status_code=401,
            detail=(
                "internal round-sync requires an EIP-712 proposer signature "
                "(no shared-key fallback)"
            ),
        )
    err = _verify_internal_round_signature(raw)
    if err is not None:
        raise HTTPException(
            status_code=401,
            detail=f"Invalid round-lifecycle signature: {err}",
        )


# Per-signer, per-round rate limit state for champion proposals. Keyed by
# (signer.lower(), round_id). Value is monotonic timestamp of last accepted.
_CHAMPION_PROPOSAL_LAST_SEEN: dict[tuple[str, str], float] = {}
_CHAMPION_PROPOSAL_LAST_SEEN_LOCK = Lock()


def _champion_proposal_rate_limit_check(
    body: Any,
    request: Request | None = None,
) -> str | None:
    """Reject champion proposals that arrive too often per (signer, round_id).

    Default 10s window. Controlled by CHAMPION_PROPOSAL_MIN_INTERVAL_SECONDS.
    Returns an error string to use as the rejection reason, or None on pass.

    Fail-closed semantics (PR-2 of 7-PR security hardening, audit C2):
    when the proposal omits ``proposer`` (the signer field) this used to
    fail open — anyone with the shared API key could spam unsigned
    proposals at unlimited rate. Now: when signer is missing, we key the
    limit on the client IP from the request and apply a tight 10s window
    (1 req per 10s per IP). Signed proposals keep using the original
    (signer, round) bucket.
    """
    try:
        interval = float(
            os.environ.get("CHAMPION_PROPOSAL_MIN_INTERVAL_SECONDS", "10").strip() or 10
        )
    except ValueError:
        interval = 10.0
    if interval <= 0:
        return None  # explicitly disabled

    signer = (getattr(body, "proposer", "") or "").lower()
    round_id = getattr(body, "round_id", "") or ""

    # Signed path: bucket on (signer, round_id) — the original semantics.
    if signer and round_id:
        key: tuple[str, str] = (signer, round_id)
        descr = f"signer {signer[:10]} for round {round_id}"
    else:
        # Unsigned path: bucket on client IP only. We accept slightly
        # over-aggressive limiting here (one IP can stand in for many
        # validators only if they share an egress NAT — vanishingly rare
        # in subnet112 topology). 429-equivalent (returned as rejection
        # reason RATE_LIMITED) is the right answer.
        ip = "unknown"
        if request is not None:
            ip = (
                request.headers.get("x-real-ip", "").strip()
                or (request.headers.get("x-forwarded-for", "").split(",", 1)[0].strip()
                    if request.headers.get("x-forwarded-for") else "")
                or (request.client.host if request.client and request.client.host else "unknown")
            )
        key = (f"ip:{ip}", "_unsigned_")
        descr = f"unsigned proposal from {ip}"
        # Force the unsigned path to 10s minimum even if operator widened
        # the signed interval.
        interval = max(interval, 10.0)

    now = time.monotonic()
    with _CHAMPION_PROPOSAL_LAST_SEEN_LOCK:
        last = _CHAMPION_PROPOSAL_LAST_SEEN.get(key)
        if last is not None and (now - last) < interval:
            return (
                f"Champion proposal rate-limited: {descr} already sent "
                f"a proposal {now - last:.1f}s ago (interval={interval}s)"
            )
        _CHAMPION_PROPOSAL_LAST_SEEN[key] = now
    return None


def _verify_champion_proposal_signature(body: Any) -> str | None:
    """Verify the leader's EIP-712 signature over the canonical proposal.

    ALWAYS required: this signature is the sole cross-validator auth for the
    champion-consensus proposal route now that the shared internal API key
    gate has been removed. The previous
    ``CONSENSUS_REQUIRE_SIGNED_CHAMPION_PROPOSALS=0`` bypass is gone — there
    is no opt-out, otherwise any caller could forge "the leader said adopt
    this champion" claims.

    Requires ``proposer`` and ``proposer_signature`` to be present and
    verifies the signature covers the canonical JSON of the proposal payload
    with the signature field stripped.

    Returns an error string on failure, or None on pass.
    """
    signer_declared = (getattr(body, "proposer", "") or "").strip()
    sig_hex = (getattr(body, "proposer_signature", "") or "").strip()
    if not signer_declared or not sig_hex:
        return "Missing proposer / proposer_signature — signed proposals required"

    try:
        from eth_account import Account
        from eth_account.messages import encode_defunct
        import json as _json
    except Exception as exc:
        return f"eth_account unavailable: {exc}"

    payload = body.model_dump()
    payload.pop("proposer_signature", None)
    canonical = _json.dumps(payload, sort_keys=True, separators=(",", ":"))

    try:
        recovered = Account.recover_message(
            encode_defunct(text=canonical),
            signature=sig_hex,
        )
    except Exception as exc:
        return f"Signature recovery failed: {exc}"

    if recovered.lower() != signer_declared.lower():
        return (
            f"Signer mismatch: signature recovers to {recovered[:10]}... "
            f"but proposer declared {signer_declared[:10]}..."
        )

    # Leader-lock: when LOCKED_LEADER_EVM_ADDRESS is set, only that signer
    # may propose champion adoptions. Mirrors the order-consensus lock in
    # validator/scoring_engine.py:verify_proposer_signature. Falls back to
    # the on-chain registry check only when the lock is cleared.
    from minotaur_subnet.validator.metagraph_sync import (
        LOCKED_LEADER_EVM_ADDRESS,
    )
    if LOCKED_LEADER_EVM_ADDRESS:
        if recovered.lower() != LOCKED_LEADER_EVM_ADDRESS.lower():
            return (
                f"Signer {recovered[:10]}... is not the locked leader "
                f"({LOCKED_LEADER_EVM_ADDRESS})"
            )
        return None

    # Verify the signer is a registered validator on the order's chain OR
    # (for champion consensus) the BT EVM validator registry. Re-uses the
    # Phase 3 on-chain registry cache.
    from minotaur_subnet.consensus.validator_registry_cache import (
        is_on_chain_validator, enforce_enabled,
    )
    if enforce_enabled():
        if not is_on_chain_validator(recovered, 964):
            return f"Signer {recovered[:10]}... is not a registered validator"
    return None


def _require_registered_miner(hotkey: str) -> None:
    """Reject submissions from hotkeys not currently in the subnet metagraph.

    Resource-hygiene gate — keeps the leaderboard, benchmark queue, and
    Docker build cache scoped to actual subnet participants.

    M1 (2026-05-25 audit): previously failed OPEN when ``solver_round_metagraph_sync``
    was None or its state had never synced yet. The audit demonstrated a
    live exploit: a fake hotkey was accepted on prod because metagraph
    state was momentarily None. The gate is now FAIL-CLOSED — unavailable
    metagraph means we reject, not accept. Operators can override via
    ``SUBMISSIONS_ALLOW_UNREGISTERED=1`` for emergency or local dev.

    Bypass cases (explicit only):
      - ``SUBMISSIONS_ALLOW_UNREGISTERED=1`` — operator opt-in.
      - ``LOCAL_TESTNET=1`` — dev stacks without a subtensor connection.
    """
    if _env_true("SUBMISSIONS_ALLOW_UNREGISTERED", default=False):
        return
    if _env_true("LOCAL_TESTNET", default=False):
        return
    from minotaur_subnet.api.server_context import ctx
    if ctx.solver_round_metagraph_sync is None:
        raise HTTPException(
            status_code=503,
            detail=(
                "Submissions temporarily unavailable: metagraph sync is not "
                "wired on this node. Operator: ensure SUBTENSOR_URL is set "
                "and the metagraph poller is running, or set "
                "SUBMISSIONS_ALLOW_UNREGISTERED=1 to override (not recommended "
                "in production)."
            ),
        )
    state = ctx.solver_round_metagraph_sync.state
    if state is None:
        raise HTTPException(
            status_code=503,
            detail=(
                "Submissions temporarily unavailable: metagraph state has not "
                "synced yet. Retry in a few seconds; subtensor poll is in flight."
            ),
        )
    hotkey = (hotkey or "").strip()
    registered = {p.hotkey for p in state.peers}
    if hotkey not in registered:
        raise HTTPException(
            status_code=403,
            detail=(
                f"Hotkey {hotkey[:12]}... is not registered on the subnet "
                "metagraph. Register a miner UID before submitting."
            ),
        )


def _resolve_client_ip(request: Request) -> str:
    """Determine the real client IP behind a reverse proxy.

    M2 (2026-05-25 audit): when the api runs behind nginx,
    ``request.client.host`` is always ``127.0.0.1`` (the proxy upstream).
    The rate limiter was keyed off that value, so every external caller
    shared one global bucket regardless of source IP. Reading
    ``X-Real-IP`` / ``X-Forwarded-For`` first restores per-source-IP
    isolation. The nginx config sets both headers; we only honor them
    when ``TRUST_PROXY_HEADERS=1`` is set (default in prod compose),
    otherwise we fall back to ``request.client.host`` to avoid header
    spoofing on direct-exposure deployments.
    """
    if _env_true("TRUST_PROXY_HEADERS", default=False):
        # X-Real-IP first (nginx sets this with ``$remote_addr``, single value).
        xri = request.headers.get("x-real-ip", "").strip()
        if xri:
            return xri
        # X-Forwarded-For is a comma-separated chain; the LEFTMOST entry is
        # the original client. Subsequent hops are transparent proxies.
        xff = request.headers.get("x-forwarded-for", "").strip()
        if xff:
            return xff.split(",", 1)[0].strip()
    return request.client.host if request.client and request.client.host else "unknown"


def _max_submissions_per_round() -> int:
    """Per-(hotkey, round) submission cap — anti-spam for the screening pipeline.

    Each accepted submission queues an expensive build + benchmark, so a single
    miner flooding one round can starve the validator. Configurable via
    ``SUBMISSIONS_MAX_PER_ROUND`` (default 1); a value <= 0 disables the cap.

    This is operator-local admission control enforced at the leader gateway —
    the only ingress for submissions — NOT a fleet-consensus parameter, so an
    env knob is the right shape (mirrors SUBMISSIONS_RATE_LIMIT_PER_MINUTE).
    """
    raw = os.environ.get("SUBMISSIONS_MAX_PER_ROUND", "1").strip()
    try:
        return int(raw)
    except ValueError:
        return 1


def _max_submissions_per_round_total() -> int:
    """Round-wide submission cap across ALL miners — bounds the per-round
    benchmark batch (first-come; the rest retry next round).

    Configurable via ``SOLVER_ROUND_MAX_SUBMISSIONS`` (default 0 = unlimited =
    today's behaviour). Like the per-hotkey cap, this is operator-local admission
    control at the leader gateway (the only ingress) — submissions are
    leader-canonical and followers mirror the leader's accepted set — so it is
    NOT a fleet-consensus parameter and an env knob is the right shape.
    """
    raw = os.environ.get("SOLVER_ROUND_MAX_SUBMISSIONS", "0").strip()
    try:
        return int(raw)
    except ValueError:
        return 0


def _enforce_rate_limit(request: Request, principal: str) -> None:
    """Simple in-memory fixed-window limiter for submission creation endpoints."""
    raw_limit = os.environ.get("SUBMISSIONS_RATE_LIMIT_PER_MINUTE", "60").strip()
    try:
        limit = int(raw_limit)
    except ValueError:
        limit = 60
    if limit <= 0:
        return

    now = time.monotonic()
    window_start = now - 60.0
    remote = _resolve_client_ip(request)
    bucket_key = f"{request.url.path}:{principal or remote}"

    with _rate_limit_lock:
        bucket = _rate_limit_buckets.setdefault(bucket_key, deque())
        while bucket and bucket[0] < window_start:
            bucket.popleft()
        if len(bucket) >= limit:
            raise HTTPException(
                status_code=429,
                detail="Submission rate limit exceeded; try again later",
            )
        bucket.append(now)


# NOTE: the old repo_url/commit_hash submission validators (host allowlist, SSRF
# hardening, commit-hash format) were removed with the PR-based submission cutover.
# A submission now references a PR number on the canonical solver repo, so the host
# is FIXED (no arbitrary repo_url) and the head SHA comes from GitHub — the SSRF
# surface is gone structurally. PR resolution + validation lives in github_pr.py.


# ── Signature verification ──────────────────────────────────────────────────


def verify_hotkey_signature(
    hotkey: str,
    pr_number: int,
    head_sha: str,
    signature_b64: str,
    round_id: str,
) -> bool:
    """Verify that the signature was produced by the given hotkey.

    Message format: "{pr_number}:{head_sha}:{round_id}".
    Uses bittensor Keypair.verify() for Ed25519 signature checks.

    Returns True if valid, False otherwise.
    """
    try:
        import base64
        from bittensor import Keypair

        message = build_submission_message(
            pr_number,
            head_sha,
            round_id=round_id,
        )
        signature_bytes = base64.b64decode(signature_b64)
        keypair = Keypair(ss58_address=hotkey)
        return keypair.verify(message.encode("utf-8"), signature_bytes)
    except Exception:
        logger.warning("Signature verification failed for hotkey %s", hotkey)
        return False


def build_submission_message(
    pr_number: int,
    head_sha: str,
    *,
    round_id: str,
) -> str:
    """Build the signed submission payload: {pr_number}:{head_sha}:{round_id}."""
    if not round_id:
        raise ValueError("round_id is required")
    return f"{pr_number}:{head_sha}:{round_id}"


# ── Routes ──────────────────────────────────────────────────────────────────


@router.post("/submissions/source", status_code=201)
async def create_source_submission(
    body: SourceSubmitRequest,
    request: Request,
) -> dict[str, Any]:
    """Submit solver source code directly for benchmarking.

    Lightweight alternative to git-based submission: accepts Python source
    inline, writes it to a temp file, and queues it for benchmarking
    immediately (no screening, no Docker build).

    Designed for the local testnet workflow where miners submit strategies
    directly without the git+Docker overhead.
    """
    _require_submissions_enabled()
    _require_source_enabled()
    _require_submission_api_key(request)
    _enforce_rate_limit(request, body.hotkey.strip())
    _require_registered_miner(body.hotkey)

    store = get_store()
    current_round = _require_open_submission_round(
        epoch_hint=body.epoch,
        requested_round_id=body.round_id,
    )

    code_hash = hashlib.sha256(body.solver_source.encode()).hexdigest()[:12]

    # Same per-round caps as the git path so SUBMISSIONS_MAX_PER_ROUND and
    # SOLVER_ROUND_MAX_SUBMISSIONS apply uniformly across both ingresses.
    try:
        sub = store.create(
            repo_url="source://inline",
            commit_hash=code_hash,
            epoch=body.epoch,
            hotkey=body.hotkey,
            round_id=current_round.round_id,
            max_per_round=_max_submissions_per_round(),
            max_total_per_round=_max_submissions_per_round_total(),
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))

    if body.solver_name:
        store.set_solver_info(sub.submission_id, name=body.solver_name)

    # Write source to temp file for subprocess-based benchmarking
    solver_dir = tempfile.mkdtemp(prefix=f"solver-{code_hash}-")
    solver_path = os.path.join(solver_dir, "solver.py")
    with open(solver_path, "w") as f:
        f.write(body.solver_source)

    store.set_solver_path(sub.submission_id, solver_path)

    # Skip screening, go straight to BENCHMARKING
    store.update_status(sub.submission_id, SubmissionStatus.BENCHMARKING)

    logger.info(
        "Source submission created: %s (hotkey=%s, solver=%s, path=%s)",
        sub.submission_id, body.hotkey[:12], body.solver_name, solver_path,
    )

    return {
        "submission_id": sub.submission_id,
        "status": "benchmarking",
        "status_url": f"/v1/submissions/{sub.submission_id}/status",
        "round_id": sub.round_id,
        "epoch": sub.epoch,
    }


@router.post("/submissions", status_code=201, response_model=SubmitResponse)
async def create_submission(
    body: SubmitRequest,
    background_tasks: BackgroundTasks,
    request: Request,
) -> SubmitResponse:
    """Submit a new IntentSolver for screening and benchmarking.

    The submission is queued immediately and screening runs asynchronously.
    Poll the status_url to track progress.
    """
    _require_submissions_enabled()
    _require_submission_api_key(request)
    _enforce_rate_limit(request, body.hotkey.strip())
    _require_registered_miner(body.hotkey)

    store = get_store()
    current_round = _require_open_submission_round(
        epoch_hint=body.epoch,
        requested_round_id=body.round_id,
    )

    # Per-round submission cap (anti-spam). Reject an over-cap miner BEFORE the
    # expensive PR resolution + signature verification + screening queue. The
    # store enforces the same cap atomically inside create() as a backstop
    # against a race between this check and the insert.
    max_per_round = _max_submissions_per_round()
    if max_per_round > 0:
        already = store.count_by_hotkey_round(body.hotkey, current_round.round_id)
        if already >= max_per_round:
            # 409 Conflict (not 429) to match the store-level backstop and the
            # historical per-round duplicate semantics: this is a quota conflict
            # for the round, not a transient rate limit — retry NEXT round.
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Miner has already submitted {already} time(s) for round "
                    f"{current_round.round_id} (max {max_per_round} per round); "
                    f"try again next round."
                ),
            )

    # Round-wide cap across all miners (bounds the per-round benchmark batch).
    # First-come; the store re-checks atomically inside create() as the backstop.
    max_total = _max_submissions_per_round_total()
    if max_total > 0:
        round_total = store.count_by_round(current_round.round_id)
        if round_total >= max_total:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Round {current_round.round_id} is full "
                    f"({round_total}/{max_total} submissions); try again next round."
                ),
            )

    # Private path: the PR lives in the miner's OWN private repo, resolved + cloned
    # with the per-submission token. Public path is unchanged (canonical repo, env
    # token). The token is transport only and is never signed/persisted.
    _private = body.is_private
    _owner_repo = None
    if _private:
        _owner, _repo = body.private_repo.split("/", 1)
        _owner_repo = (_owner, _repo)

    # Resolve the PR -> head clone_url + live head SHA, and reject if the live head
    # != the miner-signed head_sha (force-push / TOCTOU guard). For the private
    # path this fetch also exercises the token's repo read access (Metadata +
    # Pull requests: Read); a bad/under-scoped token surfaces here as a 400.
    from minotaur_subnet.api.routes.submissions.github_pr import (
        PRResolutionError,
        resolve_pr,
    )
    try:
        pr = resolve_pr(
            body.pr_number,
            owner_repo=_owner_repo,
            token=(body.repo_token if _private else None),
        )
    except PRResolutionError as exc:
        _hint = (
            " (check the token can read this repo: Metadata:Read + "
            "Pull requests:Read)" if _private else ""
        )
        raise HTTPException(
            status_code=400, detail=f"PR resolution failed: {exc}{_hint}",
        )
    if pr["head_sha"].lower() != body.head_sha.strip().lower():
        raise HTTPException(
            status_code=400,
            detail=(
                f"PR #{body.pr_number} live head {pr['head_sha']} != signed head "
                f"{body.head_sha} (force-push after signing?)"
            ),
        )

    # Verify hotkey signature over (pr_number, head_sha, round) against the
    # authoritative open round (not the client-supplied round_id).
    if not verify_hotkey_signature(
        hotkey=body.hotkey,
        pr_number=body.pr_number,
        head_sha=body.head_sha,
        round_id=current_round.round_id,
        signature_b64=body.signature,
    ):
        raise HTTPException(
            status_code=401,
            detail="Invalid hotkey signature",
        )

    # Fail-fast: reject a PR that can't actually be merged (merge conflicts /
    # stale base after a newer champion landed / draft) with a clear message,
    # rather than spend a benchmark on a PR the leader's merge gate would later
    # reject. Authenticated above (hotkey sig), so this runs only for a real
    # submitter. Transient/uncertain GitHub signals never block (see
    # assess_pr_mergeability) — the on-chain-certified merge gate is the backstop.
    from minotaur_subnet.api.routes.submissions.github_pr import assess_pr_mergeability
    _merge_ok, _merge_reason = assess_pr_mergeability(
        body.pr_number,
        owner_repo=_owner_repo,
        token=(body.repo_token if _private else None),
    )
    if not _merge_ok:
        raise HTTPException(status_code=409, detail=_merge_reason)

    # Check for duplicate submission. Store the RESOLVED clone_url + head SHA
    # as repo_url/commit_hash (downstream screening/champion plumbing is unchanged).
    # For the private path also stash is_private/private_repo_full (persisted) and
    # the per-submission token (in-memory only, purged on terminal state).
    try:
        sub = store.create(
            repo_url=pr["clone_url"],
            commit_hash=pr["head_sha"],
            epoch=body.epoch,
            hotkey=body.hotkey,
            round_id=current_round.round_id,
            pr_number=body.pr_number,
            max_per_round=max_per_round,
            max_total_per_round=max_total,
            is_private=_private,
            private_repo_full=(body.private_repo if _private else None),
            repo_token=(body.repo_token if _private else None),
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))

    # Queue the screening pipeline in the background
    background_tasks.add_task(
        _run_screening_pipeline,
        sub.submission_id,
    )

    return SubmitResponse(
        submission_id=sub.submission_id,
        status=sub.status.value,
        status_url=f"/v1/submissions/{sub.submission_id}/status",
        round_id=sub.round_id,
        epoch=sub.epoch,
    )


@router.post("/solver/round/close", response_model=SolverRoundResponse)
async def close_solver_round(
    body: CloseRoundRequest,
    request: Request,
) -> SolverRoundResponse:
    """Explicitly close the current solver round for replay evaluation."""
    await _authorize_internal_round(request)
    closed = _close_solver_round_state(body)
    await _broadcast_internal_round_sync(
        "/v1/solver/round/internal/close",
        _close_round_sync_payload(closed),
    )
    return _round_state_to_response(closed)


@router.post("/solver/round/certify", response_model=SolverRoundResponse)
async def certify_solver_round(
    body: CertifyRoundRequest,
    request: Request,
) -> SolverRoundResponse:
    """Persist a champion certificate for a replay-qualified finalist."""
    await _authorize_internal_round(request)
    # Close the explicit-certify bypass: this public (operator) endpoint must not
    # silently certify an ARBITRARY candidate that never won the round's adoption
    # rule. Allow only the round's rule-selected finalist, a genesis/builtin
    # bootstrap candidate, or an explicit audited force override. (The automated
    # coordinator, genesis bootstrap, and peer-sync call the internal functions
    # directly and never hit this endpoint, so they're unaffected.)
    if body.candidate_submission_id:
        _rs = get_round_store().get_round(body.round_id)
        _is_finalist = _rs is not None and _rs.finalist_submission_id == body.candidate_submission_id
        _cand = get_store().get(body.candidate_submission_id)
        _is_genesis = _cand is not None and (
            _cand.hotkey == "__genesis__"
            or (_cand.repo_url or "").startswith("builtin://")
        )
        if not (_is_finalist or _is_genesis or body.force):
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Candidate {body.candidate_submission_id} is not the round's "
                    f"finalist (={_rs.finalist_submission_id if _rs else None}) and is "
                    "not a genesis/builtin candidate; it never passed the adoption "
                    "rule. Pass force=true to override deliberately."
                ),
            )
        if body.force and not (_is_finalist or _is_genesis):
            logger.warning(
                "[certify-override] FORCE certify of non-finalist candidate %s for "
                "round %s (finalist=%s) — operator override, bypassing the adoption rule",
                body.candidate_submission_id, body.round_id,
                _rs.finalist_submission_id if _rs else None,
            )
    certified = await _certify_solver_round_state(body)
    await _broadcast_internal_round_sync(
        "/v1/solver/round/internal/certify",
        _certify_round_sync_payload(certified),
    )
    return _round_state_to_response(certified)


@router.post("/solver/champion/reattest")
async def reattest_current_champion(request: Request) -> dict:
    """Force-resync the fleet to the CURRENT certified champion.

    Re-broadcasts the existing champion certificate to all peer validators, so a
    follower that missed the original election (it was down, or running a build with
    the broken cert broadcast) re-runs its reactive benchmark, verifies the now
    round-tripping EIP-712 digest, and switches from full burn to champion-weight.
    Re-sends the EXISTING signed certificate — idempotent, mutates no local state, and
    a harmless no-op on a node with no peers. An operator "force-sync the fleet" lever
    for incident recovery, not just champion adoption. Run it on the leader.
    """
    await _authorize_internal_round(request)
    store = get_round_store()
    champ = store.get_active_champion()
    rid = getattr(champ, "activated_round_id", None)
    if not rid:
        raise HTTPException(status_code=404, detail="no active champion to re-attest")
    state = store.get_round(rid)
    if state is None or state.certificate is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"certified round {rid} for champion "
                f"{getattr(champ, 'submission_id', None)} not found or carries no certificate"
            ),
        )
    approvals = len(state.certificate.approvals)
    # EMERGENCY FORCE-SYNC ("remind him + make him accept"): re-send the WHOLE round so a
    # follower that never saw it — or pruned it on a restart — can re-adopt the standing
    # champion instead of 404-ing the bare cert. Three ordered broadcasts:
    #   1) close (force=True): upsert the round state + submission snapshot, bypassing the
    #      adopt-if-behind staleness guard (the champion round is OLDER than the follower's
    #      current round, so normal sync would refuse it);
    #   2) certify: the signed certificate (verify_approval still round-trips — not blind);
    #   3) activate: drive adoption now (q1-trust) instead of waiting for a leader tick.
    _close_payload = _close_round_sync_payload(state)
    _close_payload["force"] = True
    await _broadcast_internal_round_sync(
        "/v1/solver/round/internal/close", _close_payload,
    )
    _certify_payload = _certify_round_sync_payload(state)
    _certify_payload["force"] = True  # bypass the (long-elapsed) certification deadline
    await _broadcast_internal_round_sync(
        "/v1/solver/round/internal/certify", _certify_payload,
    )
    await _broadcast_internal_round_sync(
        "/v1/solver/round/internal/activate",
        {
            "round_id": state.round_id,
            "activation_epoch": state.effective_epoch or state.close_epoch,
            "champion_changed": True,
        },
    )
    logger.info(
        "[champion-reattest] FORCE-synced champion %s (round=%s, %d approval(s)): "
        "close+certify+activate broadcast to peers",
        getattr(champ, "submission_id", None), rid, approvals,
    )
    return {
        "reattested_submission_id": getattr(champ, "submission_id", None),
        "round_id": rid,
        "approvals": approvals,
        "forced": True,
    }


@router.post("/solver/round/internal/close", response_model=SolverRoundResponse)
async def internal_close_solver_round(
    body: CloseRoundRequest,
    request: Request,
) -> SolverRoundResponse:
    """Persist a leader-broadcast round close on this validator."""
    await _authorize_internal_round_sync(request)
    closed = _sync_close_solver_round_state(body)
    return _round_state_to_response(closed)


@router.post("/solver/round/internal/certify", response_model=SolverRoundResponse)
async def internal_certify_solver_round(
    body: CertifyRoundRequest,
    request: Request,
) -> SolverRoundResponse:
    """Persist a leader-broadcast round certificate on this validator."""
    await _authorize_internal_round_sync(request)
    certified = await _sync_certified_round_state(body)
    return _round_state_to_response(certified)


@router.post("/solver/round/internal/abort", response_model=SolverRoundResponse)
async def internal_abort_solver_round(
    body: AbortRoundRequest,
    request: Request,
) -> SolverRoundResponse:
    """Persist a leader-broadcast round abort on this validator."""
    await _authorize_internal_round_sync(request)
    aborted = _sync_abort_solver_round_state(body)
    return _round_state_to_response(aborted)


@router.post("/solver/round/consensus/proposal")
async def solver_round_consensus_proposal(
    body: ChampionConsensusProposalRequest,
    request: Request,
) -> dict[str, Any]:
    """Receive a champion certification proposal from the current round leader."""
    # EIP-712 signature check is the sole cross-validator auth for this route.
    # The shared internal API key gate was removed: peers reached over the
    # metagraph don't hold it, and signing the canonical payload with the
    # leader validator key already proves the caller is the round leader.
    from minotaur_subnet.consensus.dissent import RejectionCode
    auth_err = _verify_champion_proposal_signature(body)
    if auth_err:
        return {
            "approved": False,
            "reason": auth_err,
            "reason_code": RejectionCode.UNAUTHENTICATED.value,
        }

    # Per-signer, per-round rate limit. Prevents a peer that somehow has the
    # API key from making us burn CPU on repeated reactive benchmarks. When
    # the proposal is unsigned, the limiter falls back to a per-client-IP
    # bucket so anonymous spam can't bypass it (PR-2, audit C2).
    rate_err = _champion_proposal_rate_limit_check(body, request)
    if rate_err:
        return {
            "approved": False,
            "reason": rate_err,
            "reason_code": "RATE_LIMITED",
        }

    consensus_manager = get_champion_consensus_manager()
    if consensus_manager is None:
        raise HTTPException(status_code=503, detail="Champion consensus not configured")

    from minotaur_subnet.consensus.dissent import RejectionCode
    try:
        round_state = await _maybe_prepare_round_for_certification(
            body.round_id,
            close_epoch=body.close_epoch,
            benchmark_pack_hash=body.benchmark_pack_hash,
            committee_block=body.committee_block,
            committee_hash=body.committee_hash,
            quorum_required=body.quorum_required,
            decision_deadline_epoch=body.decision_deadline_epoch,
            effective_epoch=body.effective_epoch,
            # Pass the leader's candidate so prepare SKIPS evaluate_round and
            # transitions the round straight to CERTIFYING with that candidate (same
            # as the leader's own certify path). Without it, prepare runs the full
            # evaluate flow — which is a no-op on a follower (no benchmark worker; see
            # #385), leaving the round CLOSED so the gate below rejects the proposal
            # ("is closed; expected certifying") and the quorum can never form. The
            # proposal is leader-sig-verified (_verify_champion_proposal_signature)
            # before this, so adopting the leader's candidate here is authorized.
            candidate_submission_id=body.candidate_submission_id,
        )
    except HTTPException as exc:
        if exc.status_code == 404:
            return {
                "approved": False,
                "reason": exc.detail,
                "reason_code": RejectionCode.ROUND_UNKNOWN.value,
            }
        raise

    if round_state.status not in (RoundStatus.CERTIFYING, RoundStatus.CERTIFIED):
        return {
            "approved": False,
            "reason": (
                f"Round {body.round_id} is {round_state.status.value}; "
                "expected certifying"
            ),
            "reason_code": RejectionCode.ROUND_WRONG_STATE.value,
        }
    if _round_certification_deadline_elapsed(round_state):
        return {
            "approved": False,
            "reason": (
                f"Round {body.round_id} exceeded certification deadline "
                f"{round_state.decision_deadline_epoch}"
            ),
            "reason_code": RejectionCode.ROUND_DEADLINE_ELAPSED.value,
        }

    try:
        proposal, _candidate, local_quorum = _build_champion_proposal_for_round(
            round_state,
            candidate_submission_id=body.candidate_submission_id,
            candidate_image_id=body.candidate_image_id,
            committee_hash=body.committee_hash,
            benchmark_pack_hash=body.benchmark_pack_hash,
            shadow_case_log_hash=body.shadow_case_log_hash,
            effective_epoch=body.effective_epoch,
            # Peer side: digest fields MUST come from the leader's payload so
            # our re-sign produces the same EIP-712 hash the leader expects.
            commit_hash_override=body.commit_hash,
            nonce_override=int(body.nonce or 0) or None,
            deadline_override=int(body.deadline or 0) or None,
        )
    except HTTPException as exc:
        return {
            "approved": False,
            "reason": exc.detail,
            "reason_code": RejectionCode.MALFORMED_PAYLOAD.value,
        }

    # Refuse to sign stale proposals. The contract enforces this too, but
    # surfacing it here gives the leader a clear rejection reason.
    import time as _time
    if int(body.deadline or 0) and int(body.deadline) < int(_time.time()):
        return {
            "approved": False,
            "reason": f"Proposal deadline {body.deadline} already elapsed",
            "reason_code": RejectionCode.DEADLINE_EXPIRED.value,
        }

    if body.quorum_required not in (None, 0, local_quorum):
        return {
            "approved": False,
            "reason": (
                f"Quorum mismatch: local={local_quorum} "
                f"proposed={body.quorum_required}"
            ),
            "reason_code": RejectionCode.QUORUM_MISMATCH.value,
        }

    # Pre-flight pack-hash check. If our locally-computed benchmark_pack_hash
    # differs from the leader's, reactive benchmarking over the "same" scenario
    # set is meaningless — we'd be scoring different inputs and falsely
    # rejecting valid champions on score divergence. Fail fast with
    # PACK_HASH_MISMATCH so the operator can resync manifests / orderbook
    # state between peer and leader.
    leader_pack_hash = (body.benchmark_pack_hash or "").strip()
    if leader_pack_hash:
        try:
            from minotaur_subnet.api.startup import (
                _build_solver_round_benchmark_pack_hash,
            )
            from minotaur_subnet.api.server_context import ctx as _ctx
            local_pack_hash = _build_solver_round_benchmark_pack_hash(
                _ctx, round_state.round_id,
            )
        except Exception as exc:
            logger.warning(
                "pack_hash: local computation failed for round %s: %s",
                round_state.round_id, exc,
            )
            local_pack_hash = None

        if local_pack_hash and local_pack_hash != leader_pack_hash:
            return {
                "approved": False,
                "reason": (
                    f"Benchmark pack hash mismatch: local={local_pack_hash[:16]}... "
                    f"leader={leader_pack_hash[:16]}..."
                ),
                "reason_code": RejectionCode.PACK_HASH_MISMATCH.value,
            }

    # Reactive benchmark: independently verify the leader's score claim.
    # Genesis/builtin submissions are trusted (SDK code, not miner code).
    is_builtin = (
        _candidate.commit_hash == "builtin"
        or (_candidate.repo_url or "").startswith("builtin://")
    )
    # Benchmark when we have something to run: a locally-built image_tag (legacy)
    # OR a content-addressed digest to pull (digest mode — the follower need not
    # have built it locally). Without this, a digest-mode follower with no local
    # image_tag would SKIP verification and sign blind.
    from minotaur_subnet.harness.image_transport import is_bare_digest
    _proposed_image = body.candidate_image_id or proposal.candidate_image_id
    _can_benchmark = bool(_candidate.image_tag) or is_bare_digest(_proposed_image)
    if not is_builtin and _can_benchmark:
        try:
            verified, local_score = await _reactive_benchmark_candidate(
                candidate=_candidate,
                leader_score=round_state.finalist_score or 0.0,
                tolerance_pct=0.15,
                round_id=round_state.round_id,
                # The leader-signed candidate_image_id: a bare 64-hex digest D in
                # content-addressed mode (pull <repo>@sha256:D), else legacy {{.Id}}.
                candidate_image_id=body.candidate_image_id or proposal.candidate_image_id,
            )
            if not verified:
                return {
                    "approved": False,
                    "reason": (
                        f"Independent benchmark rejected: "
                        f"local_score={local_score:.4f} "
                        f"vs leader_score={round_state.finalist_score:.4f}"
                    ),
                    "reason_code": RejectionCode.BENCHMARK_MISMATCH.value,
                }
            # This node INDEPENDENTLY re-benchmarked the candidate and its own verdict
            # AGREED — record provenance (persisted) so the follower's activate-time
            # gate knows it may self-adopt the champion for weights. Set ONLY here:
            # never on the blind-sign branch (_can_benchmark False) and never for
            # builtin. Best-effort — must never fail the approval.
            try:
                get_round_store().mark_self_verified(
                    round_state.round_id, _candidate.submission_id,
                )
            except Exception:
                pass
        except Exception as exc:
            logger.exception(
                "Reactive benchmark failed for %s: %s",
                _candidate.submission_id, exc,
            )
            return {
                "approved": False,
                "reason": f"Reactive benchmark error: {exc}",
                "reason_code": RejectionCode.BENCHMARK_ERROR.value,
            }

    approval = consensus_manager.sign_approval(proposal)
    return {
        "approved": True,
        "validator_id": approval.validator_id,
        "round_id": approval.round_id,
        "committee_hash": approval.committee_hash,
        "incumbent_image_id": approval.incumbent_image_id,
        "candidate_submission_id": approval.candidate_submission_id,
        "candidate_image_id": approval.candidate_image_id,
        "benchmark_pack_hash": approval.benchmark_pack_hash,
        "shadow_case_log_hash": approval.shadow_case_log_hash,
        "effective_epoch": approval.effective_epoch,
        # Echo v2 signed fields so the leader can reconstruct the digest
        # identically when verifying the signature.
        "commit_hash": approval.commit_hash,
        "nonce": int(approval.nonce or 0),
        "deadline": int(approval.deadline or 0),
        "signature": approval.signature,
        "timestamp": approval.timestamp,
    }


@router.post("/solver/round/internal/activate")
async def internal_activate_solver_round(
    body: ActivateRoundRequest,
    request: Request,
) -> dict[str, Any]:
    """Persist a leader-broadcast round activation on this validator."""
    await _authorize_internal_round_sync(request)
    try:
        return await _activate_solver_round_state(body)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.post("/solver/round/activate")
async def activate_solver_round(
    body: ActivateRoundRequest,
    request: Request,
) -> dict[str, Any]:
    """Activate a previously certified round at an explicit epoch."""
    await _authorize_internal_round(request)
    try:
        result = await _activate_solver_round_state(body)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    await _broadcast_internal_round_sync(
        "/v1/solver/round/internal/activate",
        # Carry the leader's own adopt outcome so followers refuse to weight a champion
        # the leader's merge-gate rejected (parity with the autonomous coordinator path).
        _activate_round_sync_payload(body, result.get("champion_changed")),
    )
    return result


@router.post("/solver/round/abort", response_model=SolverRoundResponse)
async def abort_solver_round(
    body: AbortRoundRequest,
    request: Request,
) -> SolverRoundResponse:
    """Abort a solver round without activating a challenger."""
    await _authorize_internal_round(request)
    aborted = _abort_solver_round_state(body)
    await _broadcast_internal_round_sync(
        "/v1/solver/round/internal/abort",
        _abort_round_sync_payload(aborted),
    )
    return _round_state_to_response(aborted)


@router.get("/solver/round", response_model=SolverRoundResponse)
async def get_solver_round() -> SolverRoundResponse:
    """Return the current solver submission round."""
    current = _get_current_solver_round(epoch_hint=0)
    return _round_state_to_response(current)


@router.get("/solver/round/{round_id}", response_model=SolverRoundResponse)
async def get_solver_round_by_id(round_id: str) -> SolverRoundResponse:
    """Return a specific solver submission round by ID."""
    round_state = get_round_store().get_round(round_id)
    if round_state is None:
        raise HTTPException(status_code=404, detail="Solver round not found")
    return _round_state_to_response(round_state)


def _round_summary_from_dict(d: dict[str, Any]) -> SolverRoundSummary:
    """Build a compact history row from a persisted round dict (RoundState.to_dict)."""
    status = str(d.get("status") or "")
    cert = d.get("certificate") or {}
    adopted = status == "activated"
    return SolverRoundSummary(
        round_id=str(d.get("round_id") or ""),
        status=status,
        opened_epoch=int(d.get("opened_epoch") or 0),
        close_epoch=d.get("close_epoch"),
        finalist_submission_id=d.get("finalist_submission_id"),
        finalist_score=d.get("finalist_score"),
        incumbent_submission_id=d.get("incumbent_submission_id"),
        adopted=adopted,
        adopted_submission_id=(
            (cert.get("candidate_submission_id") or d.get("finalist_submission_id"))
            if adopted else None
        ),
        effective_epoch=d.get("effective_epoch"),
        abort_reason=d.get("abort_reason"),
        created_at=float(d.get("created_at") or 0.0),
        updated_at=float(d.get("updated_at") or 0.0),
    )


@router.get("/solver/rounds", response_model=SolverRoundsResponse)
async def list_solver_rounds(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    status: str | None = Query(None),
) -> SolverRoundsResponse:
    """Paginated solver-round HISTORY (newest first), sourced from the durable
    order-book DB (AppIntentStore) that the round store mirrors into."""
    # Ensure the round-store singleton is wired (installs the history sink + the
    # one-time backfill of rounds already in the JSON store).
    get_round_store()
    from minotaur_subnet.api.server_context import ctx
    store = getattr(ctx, "store", None)
    rows: list[dict[str, Any]] = []
    total = 0
    if store is not None and hasattr(store, "list_rounds"):
        rows = store.list_rounds(limit=limit, offset=offset, status=status)
        total = store.count_rounds(status=status)
    return SolverRoundsResponse(
        total=total,
        limit=limit,
        offset=offset,
        rounds=[_round_summary_from_dict(d) for d in rows],
    )


@router.get("/solver/champion", response_model=SolverChampionResponse)
async def get_solver_champion() -> SolverChampionResponse:
    """Return the last activated/adopted champion snapshot."""
    round_store = get_round_store()
    _sync_round_incumbent_from_submission_store(round_store, get_store())
    champion = round_store.get_active_champion()
    return SolverChampionResponse(**champion.to_dict())


@router.get("/submissions/{submission_id}/status", response_model=StatusResponse)
async def get_submission_status(submission_id: str) -> StatusResponse:
    """Get the current status of a submission."""
    store = get_store()
    sub = store.get(submission_id)
    if sub is None:
        raise HTTPException(status_code=404, detail="Submission not found")
    d = sub.status_dict()
    # Feedback report (P1): cheap read+shape of the already-persisted benchmark
    # detail + aggregate-vs-champion. Best-effort — never break /status on it.
    try:
        from .report import build_submission_report
        from minotaur_subnet.epoch.adopt_rule import PER_APP_MIN_SCORE
        from minotaur_subnet.epoch.manager import DETHRONE_MARGIN

        champion_score: float | None = None
        champ = get_round_store().get_active_champion()
        if champ is not None and champ.submission_id:
            champ_sub = store.get(champ.submission_id)
            if champ_sub is not None:
                champion_score = champ_sub.benchmark_score

        # The report's absolute "too low" floor is the surviving per-app sanity
        # floor (PER_APP_MIN_SCORE) — the absolute GLOBAL floor was purged. The
        # dethrone bar (champion*(1+margin)) is computed inside the report as
        # score_to_beat, which drives the "scored but didn't dethrone" outcome;
        # keeping threshold distinct from it keeps both outcomes reachable.
        threshold = PER_APP_MIN_SCORE
        reason = d.get("rejection_reason")
        if not reason and sub.round_id:
            rs = get_round_store().get_round(sub.round_id)
            if rs is not None and getattr(rs, "abort_reason", None):
                reason = rs.abort_reason

        d["report"] = build_submission_report(
            sub,
            champion_score=champion_score,
            threshold=threshold,
            dethrone_margin=DETHRONE_MARGIN,
            reason=reason,
        )
    except Exception as exc:
        logger.warning("submission report build failed for %s: %s", submission_id, exc)
        d["report"] = None
    return StatusResponse(**d)


@router.get("/submissions")
async def list_submissions(
    round_id: str | None = None,
    epoch: int | None = None,
    hotkey: str | None = None,
    include_details: bool = False,
) -> dict[str, Any]:
    """List submissions, optionally filtered by round, epoch, and/or hotkey.

    The heavy per-submission ``benchmark_details`` blob is OMITTED by default.
    It is the per-scenario benchmark dump and dominates the list payload — ~800
    submissions ship ~16 MB, ~100% of which is ``benchmark_details``, even though
    list consumers (e.g. the dashboard's /miners page, polled every 15s) only
    read the light fields. Pass ``include_details=true`` to keep it, or fetch a
    single submission's full report via ``GET /v1/submissions/{id}/status``.
    """
    store = get_store()

    if round_id is not None:
        subs = store.list_by_round(round_id)
    elif epoch is not None:
        subs = store.list_by_epoch(epoch)
    else:
        subs = sorted(store._submissions.values(), key=lambda s: s.created_at, reverse=True)

    if hotkey:
        subs = [s for s in subs if s.hotkey == hotkey]

    def _shape(s: Any) -> dict[str, Any]:
        d = s.to_dict()
        if not include_details:
            d.pop("benchmark_details", None)
        return d

    return {
        "count": len(subs),
        "submissions": [_shape(s) for s in subs],
    }


# ── Diagnostic: score an arbitrary image via the challenger path ─────────────
# Operator-only (internal key). Benchmarks ANY image through the EXACT challenger
# scoring path (champion reference anchor + round pin + corpus) with NO submission,
# NO round, and NO chance of adoption. Used to settle scoring-symmetry questions —
# e.g. score king's own image as a challenger and confirm it ties the incumbent.
# Async job (a benchmark takes minutes): POST starts it, GET polls the result.

_DIAGNOSTIC_RESULTS: dict[str, dict[str, Any]] = {}
_DIAGNOSTIC_LOCK = Lock()


@router.post("/internal/diagnostic/score-image")
async def internal_diagnostic_score_image(body: DiagnosticScoreRequest, request: Request):
    _require_internal_round_api_key(request)
    worker = get_benchmark_worker()
    if worker is None:
        raise HTTPException(
            status_code=503,
            detail="Benchmark worker not available (diagnostic scoring is leader-only).",
        )
    job_id = f"diag_{uuid.uuid4().hex[:12]}"
    with _DIAGNOSTIC_LOCK:
        _DIAGNOSTIC_RESULTS[job_id] = {
            "status": "running", "image": body.image, "label": body.label,
        }

    async def _run() -> None:
        try:
            res = await worker.score_image_diagnostic(body.image)
            with _DIAGNOSTIC_LOCK:
                _DIAGNOSTIC_RESULTS[job_id] = {"status": "done", "label": body.label, **res}
        except Exception as exc:  # noqa: BLE001 — surface any failure to the poller
            logger.exception("[diagnostic] score-image failed for %s: %s", body.image, exc)
            with _DIAGNOSTIC_LOCK:
                _DIAGNOSTIC_RESULTS[job_id] = {
                    "status": "error", "image": body.image,
                    "label": body.label, "error": str(exc),
                }

    asyncio.create_task(_run())
    logger.info("[diagnostic] started score-image job %s for image %s (label=%s)",
                job_id, body.image, body.label)
    return {"job_id": job_id, "status": "running", "image": body.image}


@router.get("/internal/diagnostic/score-image/{job_id}")
async def internal_diagnostic_score_image_result(job_id: str, request: Request):
    _require_internal_round_api_key(request)
    with _DIAGNOSTIC_LOCK:
        res = _DIAGNOSTIC_RESULTS.get(job_id)
    if res is None:
        raise HTTPException(status_code=404, detail="Unknown diagnostic job_id")
    return res
