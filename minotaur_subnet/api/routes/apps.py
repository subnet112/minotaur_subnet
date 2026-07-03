"""App Intent CRUD routes."""

from __future__ import annotations

import os
import time
from collections import deque
from pathlib import Path
from threading import Lock
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field

from minotaur_subnet.api import services as _tools

router = APIRouter(tags=["apps"])


def _env_true(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _require_admin(
    request: Request,
    x_admin_key: str | None = Header(None),
) -> None:
    """Validate admin API key for protected endpoints.

    Fail-closed semantics (PR-2 of 7-PR security hardening, audit C1):

      * If ``RELAYER_URL`` is set in the environment (any value), this
        instance is wired to a relayer that spends real gas — admin
        gating is mandatory. ``ADMIN_API_KEY`` MUST be set AND the
        request MUST present a matching ``X-Admin-Key`` header. Either
        missing → 401.

      * If ``RELAYER_URL`` is unset AND ``LOCAL_TESTNET=1`` is set, the
        dev-mode open path is preserved so local stacks without a relayer
        can be poked freely.

      * Otherwise (no relayer, not local) the gate is still enforced.
        This catches "I forgot to set LOCAL_TESTNET on my laptop"
        footguns and makes the safe default the only default.

    Use as ``dependencies=[Depends(_require_admin)]`` on the route so the
    OpenAPI surface stays clean and the header is documented.
    """
    relayer_url = os.environ.get("RELAYER_URL", "").strip()
    admin_key = os.environ.get("ADMIN_API_KEY", "").strip()
    local_testnet = _env_true("LOCAL_TESTNET", default=False)

    if not relayer_url and local_testnet:
        # Dev path: no relayer + explicit local-testnet flag → open.
        return

    if not admin_key:
        raise HTTPException(
            status_code=401,
            detail=(
                "Admin API key required but ADMIN_API_KEY is not configured "
                "on this server. Operator: set ADMIN_API_KEY (and either set "
                "LOCAL_TESTNET=1 to open dev routes, or leave it gated)."
            ),
        )

    if x_admin_key != admin_key:
        raise HTTPException(
            status_code=401,
            detail="Admin API key required (X-Admin-Key header)",
        )


# Per-IP, per-path rate limit buckets for the debug helper routes
# (/apps/validate, /apps/{id}/score). Even behind the admin gate these
# routes spawn Forge / Anvil subprocesses, so an operator-shared key
# can be hammered. Defaults: 5 req/min/IP.
_DEBUG_RATE_LIMIT_BUCKETS: dict[str, deque[float]] = {}
_DEBUG_RATE_LIMIT_LOCK = Lock()


def _client_ip(request: Request) -> str:
    """Resolve the caller IP, honoring proxy headers when TRUST_PROXY_HEADERS=1."""
    if _env_true("TRUST_PROXY_HEADERS", default=False):
        xri = request.headers.get("x-real-ip", "").strip()
        if xri:
            return xri
        xff = request.headers.get("x-forwarded-for", "").strip()
        if xff:
            return xff.split(",", 1)[0].strip()
    return request.client.host if request.client and request.client.host else "unknown"


def _debug_rate_limit(request: Request, *, per_minute: int = 5) -> None:
    """Per-IP fixed-window limiter for debug helper routes.

    Raises 429 when the IP key exceeds ``per_minute`` requests in the
    last 60 seconds against the same path. Mirrors the bucket style used
    in ``submissions/routes._enforce_rate_limit``.
    """
    if per_minute <= 0:
        return
    now = time.monotonic()
    window_start = now - 60.0
    ip = _client_ip(request)
    key = f"{request.url.path}:{ip}"
    with _DEBUG_RATE_LIMIT_LOCK:
        bucket = _DEBUG_RATE_LIMIT_BUCKETS.setdefault(key, deque())
        while bucket and bucket[0] < window_start:
            bucket.popleft()
        if len(bucket) >= per_minute:
            raise HTTPException(
                status_code=429,
                detail=(
                    f"Rate limit exceeded for {request.url.path} "
                    f"(>{per_minute} req/min/IP). Retry shortly."
                ),
            )
        bucket.append(now)

# Module-level JS engine reference, set by server.py at startup
_js_engine = None

# Module-level simulator reference, set by server.py at startup
_simulator = None

# Module-level metagraph_sync reference, set by startup.py once the
# solver-round metagraph sync has populated state. Used by the
# admin-or-signed-miner gate to verify a caller's hotkey is on SN112.
# Matches the same pattern as routes/identity.py:set_metagraph_sync.
_metagraph_sync: Any = None

# Per-hotkey rate-limit buckets for signed-miner endpoints. Sliding-window
# in-memory; resets on process restart (acceptable — the endpoint is
# idempotent and operators shouldn't see 429s outside genuine spam).
# Default: 60 calls/hour per hotkey, overridable via env.
_MINER_RATE_LIMIT_BUCKETS: dict[str, deque[float]] = {}
_MINER_RATE_LIMIT_LOCK = Lock()


def set_js_engine(js_engine: Any) -> None:
    global _js_engine
    _js_engine = js_engine


def set_simulator(simulator: Any) -> None:
    global _simulator
    _simulator = simulator


def set_metagraph_sync(metagraph_sync: Any) -> None:
    """Wire the MetagraphSync instance so the signed-miner gate can check
    membership on SN112. Called by startup.py once the periodic sync has
    been constructed (mirrors routes/identity.py's setter)."""
    global _metagraph_sync
    _metagraph_sync = metagraph_sync


def _require_admin_or_signed_miner(
    request: Request,
    x_admin_key: str | None = Header(None),
    x_bittensor_hotkey: str | None = Header(None),
    x_bittensor_signature: str | None = Header(None),
    x_bittensor_timestamp: str | None = Header(None),
) -> None:
    """Allow either an admin (X-Admin-Key) OR a metagraph-registered miner
    signing the request with their bittensor hotkey.

    Designed for routes whose docstring says "miners use this" but whose
    cost (Anvil + JS sandbox) demands DoS protection. The admin-only
    gate was too restrictive — it locked legitimate miners out of an
    endpoint built for them. Signed-miner path restores access without
    opening DoS surface to anonymous callers.

    Miner protocol (request headers):

        X-Bittensor-Hotkey:    SS58 address of the caller's hotkey
        X-Bittensor-Timestamp: unix seconds (must be within ±300s of now)
        X-Bittensor-Signature: substrate signature (0x-prefixed hex) over
                               the canonical message:
                                   f"{METHOD} {PATH} {TIMESTAMP}"
                               eg "POST /v1/orders/order_abc/dry-run 1779850000"

    The hotkey must be present on the SN112 metagraph (proves the caller
    is a registered participant on this subnet, not a random key). Calls
    are rate-limited per-hotkey via a sliding 60-min window
    (default 60 calls/hour, override with MINER_RATE_LIMIT_PER_HOUR).
    """
    # ── 1. admin bypass + dev-open (mirror _require_admin's matrix so this
    #       gate is a strict SUPERSET — endpoints can swap _require_admin ->
    #       this one without losing the admin / local-testnet paths) ──
    if not os.environ.get("RELAYER_URL", "").strip() and _env_true(
        "LOCAL_TESTNET", default=False
    ):
        return  # dev path: no relayer + local-testnet flag → open (prod gated)
    admin_key = os.environ.get("ADMIN_API_KEY", "").strip()
    if admin_key and x_admin_key and x_admin_key == admin_key:
        return

    # ── 2. signed-miner path ──────────────────────────────────────────
    if not (x_bittensor_hotkey and x_bittensor_signature and x_bittensor_timestamp):
        raise HTTPException(
            status_code=401,
            detail=(
                "This endpoint requires either an admin key (X-Admin-Key) "
                "OR a signed-miner header set "
                "(X-Bittensor-Hotkey, X-Bittensor-Signature, "
                "X-Bittensor-Timestamp). See "
                "scripts/miner_dry_run.py for a reference client."
            ),
        )

    # 2a. timestamp freshness — protects against replay
    try:
        timestamp = int(x_bittensor_timestamp)
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=400,
            detail="X-Bittensor-Timestamp must be a unix-seconds integer",
        )
    skew = abs(time.time() - timestamp)
    if skew > 300:
        raise HTTPException(
            status_code=401,
            detail=f"X-Bittensor-Timestamp is {skew:.0f}s off (max ±300s)",
        )

    # 2b. canonical message + substrate signature verification
    message = f"{request.method} {request.url.path} {timestamp}"
    try:
        from bittensor_wallet.keypair import Keypair  # noqa: E402

        kp = Keypair(ss58_address=x_bittensor_hotkey)
        sig_bytes = bytes.fromhex(
            x_bittensor_signature[2:] if x_bittensor_signature.startswith("0x")
            else x_bittensor_signature
        )
        if not kp.verify(message, sig_bytes):
            raise HTTPException(
                status_code=401,
                detail="Invalid signature for the given hotkey",
            )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=401,
            detail=f"Signature verification failed: {exc}",
        )

    # 2c. hotkey must be on the SN112 metagraph (proves participant)
    state = getattr(_metagraph_sync, "state", None) if _metagraph_sync is not None else None
    peers = getattr(state, "peers", None) if state is not None else None
    if peers is None:
        # Fail-closed: if metagraph isn't synced yet, we can't verify
        # membership — better to 503 than to false-allow.
        raise HTTPException(
            status_code=503,
            detail="Metagraph not yet synced; retry shortly",
        )
    if not any(p.hotkey == x_bittensor_hotkey for p in peers):
        raise HTTPException(
            status_code=403,
            detail=f"Hotkey {x_bittensor_hotkey[:12]}… not on SN112 metagraph",
        )

    # 2d. per-hotkey sliding-window rate limit
    per_hour_raw = os.environ.get("MINER_RATE_LIMIT_PER_HOUR", "").strip()
    try:
        per_hour = int(per_hour_raw) if per_hour_raw else 60
    except ValueError:
        per_hour = 60
    if per_hour <= 0:
        return  # disabled — operator chose to skip rate limit
    now_m = time.monotonic()
    window_start = now_m - 3600.0
    with _MINER_RATE_LIMIT_LOCK:
        bucket = _MINER_RATE_LIMIT_BUCKETS.setdefault(x_bittensor_hotkey, deque())
        while bucket and bucket[0] < window_start:
            bucket.popleft()
        if len(bucket) >= per_hour:
            raise HTTPException(
                status_code=429,
                detail=(
                    f"Rate limit exceeded for this hotkey "
                    f"(>{per_hour} req/hour). Retry shortly."
                ),
            )
        bucket.append(now_m)


# ── request models ───────────────────────────────────────────────────────────


class CreateAppRequest(BaseModel):
    name: str = Field(..., description="Human-readable name")
    description: str = Field("", description="What this app does")
    supported_chains: list[int] = Field(..., description="Chain IDs (e.g. [1, 8453])")
    js_code: str = Field(..., description="JS scoring code (required)")
    solidity_code: str = Field(..., description="Solidity contract code (required)")
    constructor_args: list[list[str]] | None = Field(
        None, description="Extra constructor args: [[abi_type, value], ...]",
    )
    deployer: str = Field("", description="Deployer address (only this address can update JS later)")
    fee_mode: str = Field(
        "", description="Per-App on-chain fee mode: 'USER' (users pay) or 'APP' "
        "(the App's paymaster pays). Empty = operator default (FEE_MODE_DEFAULT).",
    )
    contract_version: str = Field(
        "", description="Contract base generation: 'v1' (AppIntentBase) or "
        "'v2' (AppIntentBaseV2). Empty = v1.",
    )


class ValidateAppRequest(BaseModel):
    js_code: str = Field(..., description="JavaScript scoring code to validate")
    solidity_code: str = Field("", description="Solidity contract code to validate")
    skip_solidity: bool = Field(False, description="Skip Solidity compilation check")


class UpdateScoringRequest(BaseModel):
    new_js_code: str = Field(..., description="New JavaScript scoring source")
    caller: str = Field("", description="Deprecated, ignored. Authorization is by `signature`.")
    signature: str = Field("", description="EIP-712 developer-auth signature from the app's deployer "
                           "(required if a deployer was set). Binds action=update_scoring, app_id, "
                           "keccak(new_js_code), nonce, deadline.")
    nonce: int = Field(0, description="Deployer's next developer-auth nonce (GET /apps/{id}/auth-nonce). "
                       "Must equal last_consumed + 1; consumed once on success.")
    deadline: int = Field(0, description="Unix-seconds expiry the signature was signed with.")


class DeployRequest(BaseModel):
    """Optional body for POST /apps/{id}/deploy. When the payment fields are set,
    the deploy is treated as payment-backed (a public/3rd-party deploy) and
    authorized via the deployer's EIP-712 pay_deploy_fee signature + on-chain
    payment proof. Omit the body entirely for an operator/admin deploy."""
    payment_ref: str = Field("", description="On-chain payment reference (e.g. tx hash) for the deploy fee.")
    payment_nonce: int = Field(0, description="Deployer's next developer-auth nonce (GET /apps/{id}/auth-nonce).")
    payment_deadline: int = Field(0, description="Unix-seconds expiry the pay_deploy_fee signature was signed with.")
    payment_signature: str = Field("", description="EIP-712 pay_deploy_fee signature from the app's deployer, binding "
                                   "(app_id, payment_ref, chain_id, amount).")


class LinkSS58Request(BaseModel):
    """Dual-signed link of the app's EVM deployer to a Bittensor SS58 coldkey.
    Both signatures are required (see api/services/developer_link)."""
    ss58: str = Field(..., description="The Bittensor SS58 coldkey to link as the app's payer.")
    nonce: int = Field(0, description="Deployer's next developer-auth nonce (GET /apps/{id}/auth-nonce).")
    deadline: int = Field(0, description="Unix-seconds expiry the EVM link_ss58 signature was signed with.")
    evm_signature: str = Field(..., description="EIP-712 link_ss58 signature from the EVM deployer, binding "
                               "(app_id, ss58, nonce, deadline).")
    ss58_signature: str = Field(..., description="Substrate signature by the coldkey over "
                                "'MinotaurLinkSS58:{app_id}:{deployer_lower}:{nonce}' (hex).")


class ScorePlanRequest(BaseModel):
    plan: dict[str, Any] = Field(..., description="Execution plan to score")
    params: dict[str, Any] = Field(..., description="Order params → state.raw_params")
    chain_id: int = Field(0, description="Chain ID (0 = auto-detect from deployment)")
    intent_function: str = Field("execute", description="Intent function name")
    fork_block: int | None = Field(
        None,
        description=(
            "Optional historical block number to rewind the anvil fork to "
            "before simulating. Used by miner-side Stage-2 replay of "
            "historical filled orders so the plan is evaluated against "
            "the pool state at the time of the original order. Requires "
            "the upstream RPC to support archive reads."
        ),
    )


# ReplayDebugRequest model + ``/apps/{app_id}/replay-debug`` handler moved
# to ``routes/local_testnet.py`` (2026-05-25 audit). Arbitrary-Python strategy
# replay is a miner-dev debugging tool; only registered with LOCAL_TESTNET=1.


# ── helpers ──────────────────────────────────────────────────────────────────


def _store():
    """Return the shared store from the API server module."""
    from minotaur_subnet.api.server import store
    return store


# ── routes ───────────────────────────────────────────────────────────────────


@router.post("/apps/", dependencies=[Depends(_require_admin)])
def create_app(
    body: CreateAppRequest,
) -> dict[str, Any]:
    """Create a new App Intent with developer-provided JS and Solidity code.

    Requires X-Admin-Key header unless ``RELAYER_URL`` is unset AND
    ``LOCAL_TESTNET=1`` (see ``_require_admin`` for the full matrix). The
    on-chain AppRegistry gate is the final authority — even an
    unauthenticated app record can't be routed against an unregistered
    contract — but the admin gate prevents wasted relayer gas from
    spurious deploy attempts.
    """
    return _tools.create_app_intent(
        _store(),
        name=body.name,
        description=body.description,
        supported_chains=body.supported_chains,
        js_code=body.js_code or None,
        solidity_code=body.solidity_code or None,
        constructor_args=body.constructor_args,
        deployer=body.deployer,
        fee_mode=body.fee_mode,
        contract_version=body.contract_version,
    )


@router.post("/apps/validate", dependencies=[Depends(_require_admin)])
async def validate_app(
    body: ValidateAppRequest,
    request: Request,
) -> dict[str, Any]:
    """Pre-flight validation for App Intent JS and/or Solidity code.

    Admin-gated as of PR-2 (audit H3): this route spawns a Forge
    subprocess to compile attacker-supplied Solidity. Anonymous abuse
    can DoS the validator host by burning CPU on repeated compile
    timeouts. Also rate-limited per source IP (5 req/min) for the case
    where the operator-shared admin key is widely distributed.
    """
    _debug_rate_limit(request, per_minute=5)
    return await _tools.validate_app_intent_code(
        js_code=body.js_code,
        solidity_code=body.solidity_code,
        skip_solidity=body.skip_solidity,
    )


@router.post("/apps/{app_id}/deploy", dependencies=[Depends(_require_admin)])
async def deploy_app(
    app_id: str,
    chain_id: int | None = None,
    body: DeployRequest | None = None,
) -> dict[str, Any]:
    """Deploy an App Intent to a specific chain (or first supported chain).

    Requires X-Admin-Key header unless in LOCAL_TESTNET dev mode (see
    ``_require_admin``). Without this gate, an unauthenticated caller
    could trigger the relayer to spend gas on attacker-defined Solidity;
    the deployed contract still can't execute orders (AppRegistry gate
    is GATED + allowlist) but the gas burn is real.

    With a payment body, the deploy is authorized via the deployer's EIP-712
    pay_deploy_fee signature + on-chain payment proof instead of admin privilege
    (the shape the public deploy API will use). It still can't succeed until
    collection is live — the #238 gate and the (default-off) payment verifier
    keep it closed — so the fee stays unbypassable.

    Runs in a thread executor so the synchronous compile + deploy chain
    can call asyncio.run() without conflicting with the FastAPI event loop.
    """
    import asyncio
    loop = asyncio.get_running_loop()

    payment = None
    if body is not None and (body.payment_signature or body.payment_ref):
        from minotaur_subnet.api.services.deploy_payment import DeployFeePayment
        payment = DeployFeePayment(
            payment_ref=body.payment_ref,
            nonce=body.payment_nonce,
            deadline=body.payment_deadline,
            signature=body.payment_signature,
        )

    # No payment claim → operator/admin deploy (is_admin=True, free, as today).
    # Payment claim → public/3rd-party deploy: deploy_app_intent flips to
    # is_admin=False and authorizes via the fee payment.
    return await loop.run_in_executor(
        None,
        lambda: _tools.deploy_app_intent(
            _store(), app_id, chain_id=chain_id,
            is_admin=(payment is None),
            payment=payment,
        ),
    )


@router.get("/apps/{app_id}/deploy-quote", dependencies=[Depends(_require_admin)])
def deploy_quote(app_id: str, chain_id: int | None = None) -> dict[str, Any]:
    """Quote the cost to deploy this App (#238): estimated gas per targeted chain
    + the deploy fee. Informational — gas is relayer-fronted today and the fee is
    not yet collected (public deployment is gated off until collection is live).
    """
    from minotaur_subnet.deployment.deploy_fee import quote_deployment

    definition = _store().get_app(app_id)
    if definition is None:
        raise HTTPException(status_code=404, detail=f"App not found: {app_id}")
    supported = list(definition.config.supported_chains or [])
    chains = [chain_id] if chain_id is not None else supported
    if not chains:
        raise HTTPException(
            status_code=400, detail="App has no supported_chains configured"
        )
    quote = quote_deployment(chains)
    quote["app_id"] = app_id
    return quote


@router.get("/apps/")
def list_apps(deployer: str = "", status: str = "") -> dict[str, Any]:
    """List all App Intents, optionally filtered by deployer address or status."""
    result = _tools.list_minotaur_subnet(_store(), deployer if deployer else None)
    if status:
        allowed = {s.strip().lower() for s in status.split(",")}
        result["apps"] = [
            a for a in result["apps"]
            if (a.get("status") or "").lower() in allowed
        ]
        result["total"] = len(result["apps"])
    return result


@router.get("/apps/{app_id}/status")
def get_status(app_id: str) -> dict[str, Any]:
    """Get an App Intent's status and execution statistics."""
    return _tools.get_app_status(_store(), app_id)


@router.get("/apps/{app_id}/admin-state", dependencies=[Depends(_require_admin)])
async def get_admin_state(app_id: str) -> dict[str, Any]:
    """Full operator view of an app for the management frontend.

    Aggregates the store record (JS + Solidity source with sha256 hashes,
    constructor args, contract_version, per-chain deployments) with live
    per-chain state: on-chain app config (fee mode, collector, fee bounds,
    paymaster, wrapped native), fee-settlement balances (the V2 app-held
    WETH float, V1 paymaster balance + allowance, relayer gas), and
    AppRegistry registration status (mode, appByContract, record,
    developer allowlist).

    Admin-gated: returns FULL Solidity/JS source and operational balances.
    Chain reads are best-effort — an unreachable RPC degrades to nulls plus
    a per-chain ``errors`` list, never a 5xx, so the frontend can always
    render the store-side state. Runs in a thread executor: the chain reads
    are synchronous web3 calls.
    """
    import asyncio

    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(
        None, lambda: _tools.get_app_admin_state(_store(), app_id),
    )
    if "error" in result and "not found" in str(result.get("error", "")).lower():
        raise HTTPException(status_code=404, detail=result["error"])
    return result


@router.put("/apps/{app_id}/scoring", dependencies=[Depends(_require_admin)])
def update_scoring(
    app_id: str,
    body: UpdateScoringRequest,
) -> dict[str, Any]:
    """Update the JS scoring code for an App Intent.

    Requires X-Admin-Key header unless in LOCAL_TESTNET dev mode (see
    ``_require_admin``).
    """
    return _tools.update_scoring(
        _store(), app_id, body.new_js_code,
        caller=body.caller,
        signature=body.signature,
        nonce=body.nonce,
        deadline=body.deadline,
    )


class UpdateSolidityRequest(BaseModel):
    solidity_code: str = Field(..., description="New contract source")
    constructor_args: list[list[str]] | None = Field(
        None, description="Replacement ctor args: [[abi_type, value], ...]; omit to keep",
    )
    contract_version: str = Field(
        "", description="New base generation ('v1'/'v2'); omit to keep",
    )


@router.put("/apps/{app_id}/solidity", dependencies=[Depends(_require_admin)])
def update_solidity(app_id: str, body: UpdateSolidityRequest) -> dict[str, Any]:
    """Replace the stored contract source so the next deploy compiles it.

    The missing half of the update story (JS already had /scoring): without
    this, a contract-generation migration (e.g. DexAggregatorAppV2) forced a
    brand-new app_id. Refuses mid-deploy. Pair with
    POST .../deployments/{chain_id}/retire + POST .../deploy for an in-place
    redeploy under the same app_id.
    """
    return _tools.update_app_solidity(
        _store(), app_id, body.solidity_code,
        constructor_args=body.constructor_args,
        contract_version=body.contract_version,
    )


@router.post(
    "/apps/{app_id}/deployments/{chain_id}/retire",
    dependencies=[Depends(_require_admin)],
)
def retire_deployment_route(app_id: str, chain_id: int) -> dict[str, Any]:
    """Mark a chain's deployment RETIRED — releases the deploy guard so
    POST /apps/{app_id}/deploy performs an in-place redeploy (the
    deployment record upserts on (app_id, chain_id)). Store-only: recover
    any V2 WETH float FIRST via .../float/withdraw."""
    return _tools.retire_deployment(_store(), app_id, chain_id)


class FloatDepositRequest(BaseModel):
    amount_wei: int = Field(..., gt=0)
    wrap: bool = Field(True, description="Wrap relayer ETH into WETH first")


class FloatWithdrawRequest(BaseModel):
    to: str = Field(..., description="Recipient of the recovered WETH")
    amount_wei: int = Field(..., gt=0)


@router.post(
    "/apps/{app_id}/deployments/{chain_id}/float/deposit",
    dependencies=[Depends(_require_admin)],
)
async def float_deposit_route(
    app_id: str, chain_id: int, body: FloatDepositRequest,
) -> dict[str, Any]:
    """Fund the V2 app-held WETH fee float from the relayer wallet."""
    import asyncio
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: _tools.float_deposit(
        _store(), app_id, chain_id, body.amount_wei, wrap=body.wrap,
    ))


@router.post(
    "/apps/{app_id}/deployments/{chain_id}/float/withdraw",
    dependencies=[Depends(_require_admin)],
)
async def float_withdraw_route(
    app_id: str, chain_id: int, body: FloatWithdrawRequest,
) -> dict[str, Any]:
    """Recover the V2 WETH float (relayer-gated withdrawFloat on-chain) —
    e.g. before retiring a deployment for a version migration."""
    import asyncio
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: _tools.float_withdraw(
        _store(), app_id, chain_id, body.to, body.amount_wei,
    ))


class AppConfigRequest(BaseModel):
    fee_bps: int | None = None
    volume_cap_bps: int | None = None
    fee_collector: str | None = None


@router.patch(
    "/apps/{app_id}/deployments/{chain_id}/config",
    dependencies=[Depends(_require_admin)],
)
async def set_app_config_route(
    app_id: str, chain_id: int, body: AppConfigRequest,
) -> dict[str, Any]:
    """Apply the relayer-gated on-chain config setters (V2 dex app)."""
    import asyncio
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: _tools.set_app_config(
        _store(), app_id, chain_id, body.model_dump(),
    ))


@router.get(
    "/apps/{app_id}/deployments/{chain_id}/registry-calldata",
    dependencies=[Depends(_require_admin)],
)
def registry_calldata_route(app_id: str, chain_id: int) -> dict[str, Any]:
    """Prepared AppRegistry registerApp/revokeApp calldata for the current
    deployment. The revoke needs the registry OWNER key, which stays cold —
    the frontend surfaces this calldata for external signing."""
    return _tools.registry_calldata(_store(), app_id, chain_id)


@router.get("/apps/{app_id}/auth-nonce")
def get_auth_nonce(app_id: str, deployer: str = "") -> dict[str, Any]:
    """Next developer-auth nonce to sign for owner-gated actions on this app.

    The deployer reads this, signs an EIP-712 developer-auth message with
    ``nonce = next_nonce``, and submits it (e.g. to ``PUT .../scoring``). Public
    read — the nonce is a non-secret monotonic counter, like an account nonce.
    """
    s = _store()
    definition = s.get_app(app_id)
    if definition is None:
        raise HTTPException(status_code=404, detail=f"App not found: {app_id}")
    dep = (deployer or definition.deployer or "").strip()
    if not dep:
        raise HTTPException(
            status_code=400,
            detail="App has no deployer; owner-gated actions need no signature",
        )
    current = s.get_developer_nonce(app_id, dep.lower())
    return {"app_id": app_id, "deployer": dep, "next_nonce": current + 1}


@router.post("/apps/{app_id}/link-ss58", dependencies=[Depends(_require_admin)])
def link_ss58(app_id: str, body: LinkSS58Request) -> dict[str, Any]:
    """Link the app's EVM deployer to a Bittensor SS58 coldkey (dual-signed).

    Requires BOTH the deployer's EIP-712 link_ss58 signature AND the coldkey's
    substrate signature (see api/services/developer_link). The linked coldkey is
    what a future finney deploy-fee verifier checks the payment came from.
    """
    from minotaur_subnet.api.services.developer_link import link_payer_ss58

    ok, err = link_payer_ss58(
        _store(), app_id, body.ss58,
        nonce=body.nonce, deadline=body.deadline,
        evm_signature=body.evm_signature, ss58_signature=body.ss58_signature,
    )
    if not ok:
        return {"error": f"Link failed: {err}"}
    return {"app_id": app_id, "payer_ss58": body.ss58, "status": "linked"}


@router.get("/apps/{app_id}/payer-ss58")
def get_payer_ss58(app_id: str) -> dict[str, Any]:
    """The Bittensor coldkey linked to this app's deployer ("" if unlinked)."""
    s = _store()
    definition = s.get_app(app_id)
    if definition is None:
        raise HTTPException(status_code=404, detail=f"App not found: {app_id}")
    return {
        "app_id": app_id,
        "deployer": definition.deployer,
        "payer_ss58": s.get_payer_ss58(app_id),
    }


@router.get("/apps/{app_id}/manifest")
async def get_manifest(app_id: str) -> dict[str, Any]:
    """Extract and return the JS manifest for an app."""
    return await _tools.get_app_manifest(_store(), app_id)


@router.get("/apps/manifests")
async def list_manifests() -> dict[str, Any]:
    """Return manifests for all apps (bulk discovery for miners)."""
    s = _store()
    apps = s.list_apps()
    results: dict[str, Any] = {}
    for app_def in apps:
        result = await _tools.get_app_manifest(s, app_def.app_id)
        if "manifest" in result and result["manifest"] is not None:
            results[app_def.app_id] = result["manifest"]
    return {"manifests": results, "total": len(results)}


@router.get("/apps/{app_id}/historical-scenarios")
async def get_historical_scenarios(
    app_id: str,
    n_per_chain: int = 10,
) -> dict[str, Any]:
    """Return PII-stripped historical filled-order scenarios for an app.

    The live validator benchmark (benchmark_worker._load_historical_scenarios)
    replays real historical orders as Stage 2 of scoring. Miners need to
    be able to preview how their strategy handles those same orders —
    otherwise score_strategy_all gives a misleading "good" reading even
    when the strategy would fail on every historical replay.

    Deterministic: same round_id (implicit: the app_id is used as the
    pseudo-seed here for dry-run purposes) always returns the same
    sample set. Use ``n_per_chain`` to cap sample size.
    """
    from minotaur_subnet.harness.order_sampler import sample_historical_orders

    s = _store()
    # App_id stands in as the pseudo-round-id for deterministic sampling
    # — miners aren't running inside a solver round, but repeatability
    # across dry-runs is still desirable.
    sampled = sample_historical_orders(
        app_store=s,
        round_id=f"dryrun:{app_id}",
        n_per_chain=max(1, min(n_per_chain, 50)),
    )
    # Filter to this app only (the sampler doesn't filter by app_id)
    for_app = [o for o in sampled if o.get("app_id") == app_id]
    return {
        "app_id": app_id,
        "scenarios": for_app,
        "total": len(for_app),
    }


@router.post("/apps/{app_id}/activate", dependencies=[Depends(_require_admin)])
def activate_app(
    app_id: str,
    chain_id: int = 0,
) -> dict[str, Any]:
    """Admin: promote an app from solving → active (for testing).

    Requires X-Admin-Key header unless in LOCAL_TESTNET dev mode (see
    ``_require_admin``).
    """
    from minotaur_subnet.shared.types import AppStatus
    s = _store()
    dep = s.get_deployment(app_id, chain_id=chain_id if chain_id else None)
    if dep is None:
        raise HTTPException(status_code=404, detail=f"No deployment found for {app_id}")
    s.update_deployment_status(app_id, dep.chain_id, AppStatus.ACTIVE)
    return {"app_id": app_id, "chain_id": dep.chain_id, "status": "active"}


@router.post(
    "/apps/{app_id}/score",
    dependencies=[Depends(_require_admin_or_signed_miner)],
)
async def score_plan(
    app_id: str,
    body: ScorePlanRequest,
    request: Request,
) -> dict[str, Any]:
    """Score an execution plan against an app's JS scoring function.

    The miner-facing dry-run: run a plan through the validator's REAL fork
    simulation (the same scoreIntent path production uses) and get the full
    report back — JS score, on-chain score, gas, transfers, AND the decoded
    revert reason when it fails — so a miner can debug a plan WITHOUT running
    their own archive node. Falls back to mock simulation only if Anvil is
    unavailable (``simulation_mode`` says which ran).

    Gate (audit H4): each call rewinds the chain's Anvil fork and runs JS
    scoring in the sandbox, so it's not anonymous. Accepts EITHER an admin key
    OR a metagraph-registered miner signing the request (see
    ``_require_admin_or_signed_miner`` / ``scripts/miner_dry_run.py``);
    signed-miner calls are per-hotkey rate-limited (default 60/hr). The
    ``fork_block`` clamp (±100 from head) bounds the rewind so a dry-run can't
    pin the shared simulator at a deep archive block or burn archive RPC quota.
    """
    _debug_rate_limit(request, per_minute=5)

    # Clamp fork_block: reject anything farther than 100 blocks from the
    # current head on the target chain. Historical replay is the documented
    # use case (Stage-2 miner validation), but unbounded rewinds destroy
    # the live fork state every other simulation depends on. Read the head
    # from the simulator's web3 instance so we don't double-query upstream.
    if body.fork_block is not None and _simulator is not None:
        try:
            target_chain_id = body.chain_id or 0
            target_sim = None
            sims = getattr(_simulator, "simulators", None)
            if sims and target_chain_id:
                target_sim = sims.get(target_chain_id)
            if target_sim is None and sims:
                # Fall back to default chain if specific not set yet.
                default_id = getattr(_simulator, "default_chain_id", None)
                if default_id is not None:
                    target_sim = sims.get(default_id)
            if target_sim is None and not sims:
                target_sim = _simulator  # plain AnvilSimulator
            if target_sim is not None and hasattr(target_sim, "w3"):
                head = int(target_sim.w3.eth.block_number)
                if abs(int(body.fork_block) - head) > 100:
                    raise HTTPException(
                        status_code=400,
                        detail=(
                            f"fork_block {body.fork_block} is more than 100 "
                            f"blocks from current head {head}; rewinds that "
                            "deep would destroy live fork state."
                        ),
                    )
        except HTTPException:
            raise
        except Exception:
            # If we can't read the head we don't block the request — the
            # admin gate + rate limit are still in front. Logging would be
            # noisy under benign RPC blips.
            pass

    import logging
    from minotaur_subnet.shared.types import ExecutionPlan
    from minotaur_subnet.shared.builders import build_intent_state, parse_interactions
    from minotaur_subnet.shared.simulation import build_mock_simulation

    _log = logging.getLogger(__name__)

    s = _store()
    app = s.get_app(app_id)
    if app is None:
        raise HTTPException(status_code=404, detail=f"App not found: {app_id}")

    if not app.js_code:
        raise HTTPException(
            status_code=400,
            detail=f"App {app_id} has no JS scoring code",
        )

    if _js_engine is None:
        raise HTTPException(
            status_code=503,
            detail="JS scoring engine not available",
        )

    # Build ExecutionPlan from request
    plan_dict = body.plan
    interactions_raw = plan_dict.get("interactions", [])
    plan = ExecutionPlan(
        intent_id=plan_dict.get("intent_id", app_id),
        interactions=parse_interactions(interactions_raw),
        deadline=plan_dict.get("deadline", 0),
        nonce=plan_dict.get("nonce", 0),
        metadata=dict(plan_dict.get("metadata", {})),
    )

    params = body.params

    # Look up deployment for contract_address and chain_id
    chain_id = body.chain_id
    contract_address = ""
    deployment = s.get_deployment(app_id)
    if deployment:
        contract_address = deployment.contract_address or ""
        if chain_id == 0:
            chain_id = deployment.chain_id or 1
    if chain_id == 0:
        chain_id = 1

    # Ensure the MultiChainSimulator dispatches to the right chain.
    # Without this hint it falls back to default_chain_id (31337/anvil-eth)
    # and the scoreIntent call runs against the wrong fork — the DexAggregator
    # contract isn't deployed there, relayer() returns empty, and the
    # simulator fails with "Unknown format '0x'".
    plan.metadata.setdefault("chain_id", chain_id)

    state = build_intent_state(
        contract_address=contract_address,
        chain_id=chain_id,
        params=params,
        intent_function=body.intent_function,
        owner=params.get("owner", ""),
    )

    # Synthesize a stand-in IntentOrder so the simulator takes the same
    # scoreIntent path production uses. Without this, simulator.simulate()
    # falls through to its manual-interactions fallback (because the
    # intent_order arg is None), which bypasses the app contract's proxy
    # deploy / invariant checks and silently reports "0 token transfers"
    # even for correct strategies. That divergence was the reason the
    # miner's score_strategy tool kept returning 0 for valid code, so
    # Claude couldn't tell its improvements from regressions.
    #
    # The submitted_by address is a throwaway test account (address 0x…01).
    # It gets seeded with input_amount of input_token and a blanket allowance
    # to the app contract, exactly like AppIntentBase's pull-funding flow.
    from minotaur_subnet.api.services import (
        build_intent_params_hex_from_manifest,
        compute_intent_selector,
    )
    _TEST_USER = "0x0000000000000000000000000000000000000001"

    intent_order: dict | None = None
    token_balances: dict[str, int] | None = None
    # Always seed token_balances from scenario params if available — the
    # manual-fallback simulator path needs the executor funded even when
    # intent_params decoding fails. Without this, the executor has 0 of
    # the input token, the swap router's transferFrom reverts with STF,
    # and miners spend hours debugging a strategy that's actually fine.
    _input_token = params.get("input_token", "")
    _input_amount = params.get("input_amount", "0")
    try:
        _amount_wei = int(_input_amount)
    except (ValueError, TypeError):
        _amount_wei = 0
    if _input_token and _amount_wei > 0:
        token_balances = {_input_token: _amount_wei}
    if contract_address:
        # Generic, manifest-driven encoding — omitted fields (incl. any the
        # quote would normally supply) fall back to the manifest/type default,
        # which is fine for a dry-run simulation.
        intent_params_hex = build_intent_params_hex_from_manifest(
            s, _js_engine, app_id,
            body.intent_function, dict(params), _TEST_USER,
        )
        intent_selector = compute_intent_selector(
            s, _js_engine, app_id, body.intent_function,
        ) or ""
        if intent_params_hex and intent_selector:
            intent_order = {
                "order_id": f"score-dry-run-{app_id}",
                "app": contract_address,
                "intent_selector": intent_selector,
                "intent_params": intent_params_hex,
                "submitted_by": _TEST_USER,
                "chain_id": chain_id,
                "deadline": 0,  # 0 = no deadline check; this is a dry run
                "nonce": 0,
                "perpetual": False,
                "max_executions": 1,
                "cooldown": 0,
            }
            # Seed the test user with input_amount of input_token so
            # AppIntentBase's safeTransferFrom(submitted_by, proxy, ...)
            # actually has tokens to pull. Without this the contract reverts
            # with ERC20: insufficient balance before any swap attempt.
            input_token = params.get("input_token", "")
            input_amount = params.get("input_amount", "0")
            try:
                amount_wei = int(input_amount)
            except (ValueError, TypeError):
                amount_wei = 0
            if input_token and amount_wei > 0:
                token_balances = {input_token: amount_wei}

    # Try Anvil simulation, fall back to mock
    simulation_mode = "mock"
    simulation = None

    if _simulator is not None:
        try:
            simulation = await _simulator.simulate(
                plan,
                contract_address=contract_address or None,
                intent_order=intent_order,
                token_balances=token_balances,
                fork_block=body.fork_block,
            )
            simulation_mode = "anvil"
        except Exception as exc:
            _log.warning("Anvil simulation failed, falling back to mock: %s", exc)

    if simulation is None:
        simulation = build_mock_simulation(plan, params)

    # Ensure JS code is loaded and score
    try:
        if app_id not in _js_engine._intents:
            await _js_engine.load_intent(app_id, app.js_code)
        score_result = await _js_engine.score(app_id, plan, simulation, state)
        return {
            "app_id": app_id,
            "score": score_result.score,
            "valid": score_result.valid,
            "reason": score_result.reason,
            "breakdown": score_result.breakdown,
            "simulation_mode": simulation_mode,
            "simulation": {
                "success": simulation.success,
                "gas_used": simulation.gas_used,
                "on_chain_score": simulation.on_chain_score,
                "token_transfers": len(simulation.token_transfers),
                "error": simulation.error,
                # Decoded on-chain revert reason (Error(string)/Panic/custom
                # error) when the real sim reverted — the actionable "trace" so
                # a miner sees WHY their plan failed, not just that it did.
                "revert_reason": getattr(simulation, "revert_reason", None),
            },
        }
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"JS scoring failed: {exc}",
        )


# replay_debug handler moved to ``routes/local_testnet.py``.
