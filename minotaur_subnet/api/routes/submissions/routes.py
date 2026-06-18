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

import hashlib
import logging
import os
import tempfile
import time
from collections import deque
from threading import Lock
from typing import Any
from urllib.parse import urlparse

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request

from minotaur_subnet.harness.submission_store import SubmissionStatus
from minotaur_subnet.harness.round_store import RoundStatus

from .models import (
    AbortRoundRequest,
    ActivateRoundRequest,
    CertifyRoundRequest,
    ChampionConsensusProposalRequest,
    CloseRoundRequest,
    SolverChampionResponse,
    SolverRoundResponse,
    SourceSubmitRequest,
    StatusResponse,
    SubmitRequest,
    SubmitResponse,
)
from .state import (
    _COMMIT_HASH_RE,
    _rate_limit_buckets,
    _rate_limit_lock,
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

    Default ON as of PR-2 (audit C2): opt-OUT via
    ``CONSENSUS_REQUIRE_SIGNED_CHAMPION_PROPOSALS=0`` for emergency
    incident handling only. Previously defaulted to off, which meant the
    shared API key was the only thing standing between any leaked-key
    holder and forging "the leader said adopt this champion" claims.
    Mirrors the compose default flipped in PR-1.

    When on, requires ``proposer`` and ``proposer_signature`` to be
    present and verifies the signature covers the canonical JSON of the
    proposal payload with the signature field stripped.

    Returns an error string on failure, or None on pass.
    """
    require = os.environ.get(
        "CONSENSUS_REQUIRE_SIGNED_CHAMPION_PROPOSALS", "1",
    ).strip().lower() in ("1", "true", "yes", "on")
    if not require:
        return None

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


def _validate_repo_url_policy(repo_url: str) -> None:
    """Validate repo URL format and host policy.

    Hardened against URL tricks that would otherwise let a miner clone from
    an unapproved host while passing the hostname check:
      - @-userinfo (``https://github.com@attacker.com/x``) is rejected.
      - IP-literal hostnames (``https://192.168.1.1/x``, ``https://[::1]/x``)
        are rejected.
      - Fragments / queries that embed another URL are not parsed — urlparse
        already strips those, so they can't shadow the real host.
    """
    parsed = urlparse(repo_url)
    if parsed.scheme == "file":
        if not _env_true("ALLOW_FILE_REPO_URLS", default=False):
            raise HTTPException(
                status_code=400,
                detail="repo_url must be an HTTP(S) URL unless ALLOW_FILE_REPO_URLS=1 enables file:// URLs",
            )
        if not parsed.path.startswith("/"):
            raise HTTPException(
                status_code=400,
                detail="file repo_url must use an absolute path",
            )
        return
    if parsed.scheme not in ("https", "http"):
        raise HTTPException(
            status_code=400,
            detail="repo_url must be an HTTP(S) URL",
        )
    if parsed.scheme == "http" and not _env_true("ALLOW_INSECURE_REPO_URLS", default=False):
        raise HTTPException(
            status_code=400,
            detail="repo_url must use HTTPS unless ALLOW_INSECURE_REPO_URLS=1",
        )
    if not parsed.netloc:
        raise HTTPException(status_code=400, detail="repo_url must include a hostname")

    # No userinfo is allowed — `git@github.com:x/y.git` SSH form isn't our
    # use case (we require http/https), so any `@` in netloc means someone
    # is trying to shadow the hostname.
    if "@" in parsed.netloc:
        raise HTTPException(
            status_code=400,
            detail=(
                "repo_url must not contain userinfo (credentials belong in "
                "SUBMISSION_GIT_CLONE_* env vars, not in the URL)"
            ),
        )

    host = (parsed.hostname or "").lower()
    if not host:
        raise HTTPException(status_code=400, detail="repo_url must include a hostname")

    # Reject IP literals: IPv4 dotted quad, IPv6 in brackets (urlparse strips
    # the brackets for .hostname but preserves the colons), and numeric-only
    # hostnames. We require a DNS name the allowlist is meaningful against.
    if _looks_like_ip_literal(host):
        raise HTTPException(
            status_code=400,
            detail="repo_url must use a DNS hostname, not an IP address",
        )

    allowed_hosts_raw = os.environ.get("SUBMISSION_ALLOWED_REPO_HOSTS", "").strip()
    if not allowed_hosts_raw:
        return
    allowed_hosts = {h.strip().lower() for h in allowed_hosts_raw.split(",") if h.strip()}
    if host not in allowed_hosts:
        raise HTTPException(
            status_code=400,
            detail=(
                f"repo_url host '{host}' is not allowed by policy. "
                f"Allowed: {sorted(allowed_hosts)}"
            ),
        )


def _looks_like_ip_literal(host: str) -> bool:
    """True if *host* parses as an IPv4 or IPv6 literal."""
    import ipaddress
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def _validate_commit_hash_format(commit_hash: str) -> None:
    """Ensure commit hash is hex and bounded."""
    if not _COMMIT_HASH_RE.fullmatch(commit_hash):
        raise HTTPException(
            status_code=400,
            detail="commit_hash must be a 7-64 char hexadecimal git hash",
        )


# ── Signature verification ──────────────────────────────────────────────────


def verify_hotkey_signature(
    hotkey: str,
    repo_url: str,
    commit_hash: str,
    signature_b64: str,
    round_id: str,
) -> bool:
    """Verify that the signature was produced by the given hotkey.

    Message format: "{repo_url}:{commit_hash}:{round_id}".
    Uses bittensor Keypair.verify() for Ed25519 signature checks.

    Returns True if valid, False otherwise.
    """
    try:
        import base64
        from bittensor import Keypair

        message = build_submission_message(
            repo_url,
            commit_hash,
            round_id=round_id,
        )
        signature_bytes = base64.b64decode(signature_b64)
        keypair = Keypair(ss58_address=hotkey)
        return keypair.verify(message.encode("utf-8"), signature_bytes)
    except Exception:
        logger.warning("Signature verification failed for hotkey %s", hotkey)
        return False


def build_submission_message(
    repo_url: str,
    commit_hash: str,
    *,
    round_id: str,
) -> str:
    """Build the signed submission payload: {repo_url}:{commit_hash}:{round_id}."""
    if not round_id:
        raise ValueError("round_id is required")
    return f"{repo_url}:{commit_hash}:{round_id}"


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

    try:
        sub = store.create(
            repo_url="source://inline",
            commit_hash=code_hash,
            epoch=body.epoch,
            hotkey=body.hotkey,
            round_id=current_round.round_id,
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

    _validate_repo_url_policy(body.repo_url)
    _validate_commit_hash_format(body.commit_hash)

    # Verify hotkey signature against the authoritative open round (not the
    # client-supplied round_id). round-based only — epoch signing is gone.
    if not verify_hotkey_signature(
        hotkey=body.hotkey,
        repo_url=body.repo_url,
        commit_hash=body.commit_hash,
        round_id=current_round.round_id,
        signature_b64=body.signature,
    ):
        raise HTTPException(
            status_code=401,
            detail="Invalid hotkey signature",
        )

    # Check for duplicate submission
    try:
        sub = store.create(
            repo_url=body.repo_url,
            commit_hash=body.commit_hash,
            epoch=body.epoch,
            hotkey=body.hotkey,
            round_id=current_round.round_id,
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
    _require_internal_round_api_key(request)
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
    _require_internal_round_api_key(request)
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


@router.post("/solver/round/internal/close", response_model=SolverRoundResponse)
async def internal_close_solver_round(
    body: CloseRoundRequest,
    request: Request,
) -> SolverRoundResponse:
    """Persist a leader-broadcast round close on this validator."""
    _require_internal_round_api_key(request)
    closed = _sync_close_solver_round_state(body)
    return _round_state_to_response(closed)


@router.post("/solver/round/internal/certify", response_model=SolverRoundResponse)
async def internal_certify_solver_round(
    body: CertifyRoundRequest,
    request: Request,
) -> SolverRoundResponse:
    """Persist a leader-broadcast round certificate on this validator."""
    _require_internal_round_api_key(request)
    certified = await _sync_certified_round_state(body)
    return _round_state_to_response(certified)


@router.post("/solver/round/internal/abort", response_model=SolverRoundResponse)
async def internal_abort_solver_round(
    body: AbortRoundRequest,
    request: Request,
) -> SolverRoundResponse:
    """Persist a leader-broadcast round abort on this validator."""
    _require_internal_round_api_key(request)
    aborted = _sync_abort_solver_round_state(body)
    return _round_state_to_response(aborted)


@router.post("/solver/round/consensus/proposal")
async def solver_round_consensus_proposal(
    body: ChampionConsensusProposalRequest,
    request: Request,
) -> dict[str, Any]:
    """Receive a champion certification proposal from the current round leader."""
    _require_internal_round_api_key(request)

    # Additional EIP-712 signature check (opt-in via
    # CONSENSUS_REQUIRE_SIGNED_CHAMPION_PROPOSALS=1). The shared API key is
    # broadcast, so it alone can't prove the caller is an actual registered
    # validator. Signing the canonical payload with a validator key does.
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
    if not is_builtin and _candidate.image_tag:
        try:
            verified, local_score = await _reactive_benchmark_candidate(
                candidate=_candidate,
                leader_score=round_state.finalist_score or 0.0,
                tolerance_pct=0.15,
                round_id=round_state.round_id,
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
    _require_internal_round_api_key(request)
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
    _require_internal_round_api_key(request)
    try:
        result = await _activate_solver_round_state(body)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    await _broadcast_internal_round_sync(
        "/v1/solver/round/internal/activate",
        _activate_round_sync_payload(body),
    )
    return result


@router.post("/solver/round/abort", response_model=SolverRoundResponse)
async def abort_solver_round(
    body: AbortRoundRequest,
    request: Request,
) -> SolverRoundResponse:
    """Abort a solver round without activating a challenger."""
    _require_internal_round_api_key(request)
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
        from minotaur_subnet.epoch.manager import DETHRONE_MARGIN

        champion_score: float | None = None
        champ = get_round_store().get_active_champion()
        if champ is not None and champ.submission_id:
            champ_sub = store.get(champ.submission_id)
            champion_score = champ_sub.benchmark_score if champ_sub is not None else None

        # The report's absolute "too low" floor is the surviving per-app sanity
        # floor (PER_APP_MIN_SCORE) — the absolute GLOBAL floor was purged. The
        # dethrone bar (champion*(1+margin)) is computed inside the report as
        # score_to_beat, which drives the "scored but didn't dethrone" outcome;
        # keeping threshold distinct from it keeps both outcomes reachable.
        threshold = float(os.environ.get("PER_APP_MIN_SCORE", "0.3"))
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
) -> dict[str, Any]:
    """List submissions, optionally filtered by round, epoch, and/or hotkey."""
    store = get_store()

    if round_id is not None:
        subs = store.list_by_round(round_id)
    elif epoch is not None:
        subs = store.list_by_epoch(epoch)
    else:
        subs = sorted(store._submissions.values(), key=lambda s: s.created_at, reverse=True)

    if hotkey:
        subs = [s for s in subs if s.hotkey == hotkey]

    return {
        "count": len(subs),
        "submissions": [s.to_dict() for s in subs],
    }
