"""Order management REST endpoints for the Intent OrderBook.

POST /v1/apps/{app_id}/orders        → Submit a new order
GET  /v1/orders/{order_id}           → Get order status
GET  /v1/orders                      → List orders (filterable)
DELETE /v1/orders/{order_id}         → Cancel an order
POST /v1/apps/{app_id}/quote         → Get a quote (dry-run, no order created)
GET  /v1/orders/{order_id}/bridge    → Bridge status for cross-chain orders
GET  /v1/blockloop/status            → Block loop tick stats
"""

from __future__ import annotations

import logging
import os
import time
from collections import deque
from threading import Lock
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from pydantic import BaseModel

from minotaur_subnet.api.routes.apps import (
    _client_ip,
    _env_true,
    _require_admin,
    _require_admin_or_signed_miner,
)
from minotaur_subnet.orderbook.rejection import classify_rejection
from minotaur_subnet.shared.feature_flags import (
    quote_capture_enabled,
    quote_corpus_enabled,
)

router = APIRouter()
logger = logging.getLogger(__name__)


# Leader-mode detection (PR-2, audit H5): third-party follower validators
# set ENABLE_SOLVER_ROUND_COORDINATOR=0 in their compose (canonical
# follower-mode flag). On those nodes, POST /v1/apps/{id}/orders MUST
# 404 — orders go to the leader, period. Followers accepting orders is
# how unauthenticated callers were disk-filling the local store.json.
# Cached at module load (single env read), not per-request — operators
# who change the flag must restart the container, same as every other
# coordinator-mode flag.
def _compute_is_leader_node() -> bool:
    # Default True matches startup.py — coordinator on unless explicitly
    # disabled. Follower compose sets ENABLE_SOLVER_ROUND_COORDINATOR=0.
    return _env_true("ENABLE_SOLVER_ROUND_COORDINATOR", default=True)


_IS_LEADER_NODE: bool = _compute_is_leader_node()

# Module-level references set by server.py at startup
_orderbook = None
_block_loop = None
_app_store = None
_js_engine = None

# Quote-endpoint rate limit. Every quote now runs a real simulation to measure
# gas (so the fee can be priced by us, not the miner), which means an
# unauthenticated caller spamming quotes can exhaust the leader's simulation
# capacity. Fixed-window per-IP limiter mirroring apps._debug_rate_limit; tune
# via QUOTE_RATE_LIMIT_PER_MINUTE (default 30, 0 disables).
_QUOTE_RATE_LIMIT_BUCKETS: dict[str, deque] = {}
_QUOTE_RATE_LIMIT_LOCK = Lock()


def _quote_rate_limit(request: Request) -> None:
    per_minute = int(os.environ.get("QUOTE_RATE_LIMIT_PER_MINUTE", "30") or "30")
    if per_minute <= 0:
        return
    now = time.monotonic()
    window_start = now - 60.0
    key = f"quote:{_client_ip(request)}"
    with _QUOTE_RATE_LIMIT_LOCK:
        bucket = _QUOTE_RATE_LIMIT_BUCKETS.setdefault(key, deque())
        while bucket and bucket[0] < window_start:
            bucket.popleft()
        if len(bucket) >= per_minute:
            raise HTTPException(
                status_code=429,
                detail=(
                    f"Quote rate limit exceeded (>{per_minute}/min/IP). "
                    "Retry shortly."
                ),
            )
        bucket.append(now)


def set_orderbook(orderbook: Any) -> None:
    global _orderbook
    _orderbook = orderbook


def set_block_loop(block_loop: Any) -> None:
    global _block_loop
    _block_loop = block_loop


def set_app_store(app_store: Any) -> None:
    global _app_store
    _app_store = app_store


def set_js_engine(js_engine: Any) -> None:
    global _js_engine
    _js_engine = js_engine


# How long (seconds) a quote is considered indicative
_QUOTE_VALID_SECONDS = 30

# Bound the leader's quote-CASE table so unauthenticated /quote traffic can't grow
# it without limit. Capture is LEADER-ONLY, so the leader is the sole writer and
# QuoteSync mirrors its (pruned) set to followers — pruning the leader bounds the
# whole fleet. Retention is ROUND-ANCHORED (Phase-2): drop quotes captured more than
# QUOTE_RETENTION_EPOCHS opened-epochs before the current round (and legacy unstamped
# rows). Because the key is the fleet-uniform, first-seen-frozen captured_opened_epoch
# (not wall-clock), the deletion is consensus-safe once BENCHMARK_QUOTE_CORPUS is on —
# it can never remove a row inside a live round's sampling window. Runs amortized
# (once per _QUOTE_PRUNE_EVERY captures) to keep it off the hot path.
_QUOTE_PRUNE_EVERY = 200
_quote_capture_counter = 0


def _maybe_prune_quotes(store: Any, current_opened_epoch: int) -> None:
    """Amortized round-anchored prune of the quotes table (leader-only writer).

    ``current_opened_epoch`` is the live round's opened_epoch; everything captured
    before ``current_opened_epoch - QUOTE_RETENTION_EPOCHS`` (plus legacy null-epoch
    rows) is dropped. Followers inherit the pruned set via QuoteSync reconcile.
    """
    global _quote_capture_counter
    _quote_capture_counter += 1
    if _quote_capture_counter % _QUOTE_PRUNE_EVERY != 0:
        return
    if not hasattr(store, "prune_quotes"):
        return
    try:
        from minotaur_subnet.harness.order_sampler import QUOTE_RETENTION_EPOCHS
        store.prune_quotes(int(current_opened_epoch) - QUOTE_RETENTION_EPOCHS)
    except Exception:
        logger.debug("quote prune failed", exc_info=True)

# The /quote endpoint now derives estimated_output ENTIRELY from generate_plan +
# an anvil-fork simulation of that plan (see get_quote). That fork sim is
# expensive and runs on EVERY quote, so its result is cached briefly keyed by
# (app_id, chain_id, intent_function, normalized params). The short TTL stands in
# for the fork block — a fresh quote re-simulates once the pinned state drifts.
_QUOTE_PLAN_CACHE_TTL = 12.0   # seconds (~1 Base block); bounds re-sim frequency
_QUOTE_PLAN_CACHE_MAX = 512    # hard cap on entries to keep memory bounded
_QUOTE_PLAN_CACHE: dict[str, tuple[float, dict]] = {}


def _quote_plan_cache_key(
    app_id: str, chain_id: int, intent_function: str, params: dict,
) -> str:
    """Stable cache key over the inputs that determine a plan sim's output."""
    import json as _json
    return "|".join([
        str(app_id), str(chain_id), str(intent_function),
        _json.dumps(params, sort_keys=True, default=str),
    ])


def _quote_plan_cache_get(key: str) -> dict | None:
    import time as _time
    entry = _QUOTE_PLAN_CACHE.get(key)
    if entry is None:
        return None
    ts, value = entry
    if (_time.monotonic() - ts) > _QUOTE_PLAN_CACHE_TTL:
        _QUOTE_PLAN_CACHE.pop(key, None)
        return None
    return value


def _quote_plan_cache_put(key: str, value: dict) -> None:
    import time as _time
    # Cheap eviction: drop everything if we blow the cap (bounded, no LRU book-keeping).
    if len(_QUOTE_PLAN_CACHE) >= _QUOTE_PLAN_CACHE_MAX:
        _QUOTE_PLAN_CACHE.clear()
    _QUOTE_PLAN_CACHE[key] = (_time.monotonic(), value)


def _quote_delivered_output(sim: Any, params: dict, deployed: str) -> int:
    """Delivered output of a simulated plan, measured the SAME way the benchmark
    scores it (harness/scoring_lab/stages.realized_output + the raw-output
    scorer): prefer the exact-wei ``metadata.raw_output`` the live scorer emits,
    else sum output-token transfers to the recipient/contract. The plan-only sim
    carries no scorer metadata here, so in practice the transfer-sum is what
    yields the delivered amount. Never raises — returns 0 on anything unexpected.
    """
    try:
        raw = (getattr(sim, "metadata", None) or {}).get("raw_output")
    except Exception:
        raw = None
    if raw not in (None, ""):
        try:
            return int(str(raw))
        except (ValueError, TypeError):
            return 0
    out_tok = (params.get("output_token") or "").lower()
    recips = {"0x000000000000000000000000000000000000dead", (deployed or "").lower()}
    delivered = 0
    for t in (getattr(sim, "token_transfers", None) or []):
        if (getattr(t, "token", "") or "").lower() == out_tok and \
           (getattr(t, "to_addr", "") or "").lower() in recips:
            try:
                delivered += int(t.amount)
            except (TypeError, ValueError):
                pass
    return delivered


def _require_orderbook():
    if _orderbook is None:
        raise HTTPException(
            status_code=503,
            detail="OrderBook not initialized",
        )
    return _orderbook


def _enforce_order_owner_sig(
    order: Any,
    *,
    action: bytes,
    content_hash: str,
    deadline: int,
    signature: str,
    claimed_owner: str | None = None,
) -> None:
    """Verify ``signature`` proves the caller controls ``order.submitted_by``.

    M3 + M4 (2026-05-25 audit): three order-modifying endpoints previously
    accepted unsigned ownership claims (cancel via query param, signature
    PATCH with no auth at all, tx-confirmed PATCH with no auth). This
    helper enforces an EIP-191 signature from the order owner over a
    domain-separated, content-bound, deadline-bound payload.

    Override: set ``REQUIRE_ORDER_OWNER_SIG=0`` to bypass for one-off
    incident handling. Default is enforce.

    Args:
        order: The Order object whose ``submitted_by`` is the authoritative owner.
        action: One of ACTION_CANCEL / ACTION_CONFIRM_TX / ACTION_ATTACH_SIG.
        content_hash: Hash of the action-specific content (e.g. tx_hash being
            confirmed, signature being attached). Empty string for cancel.
        deadline: Unix-seconds deadline that was included in the signed payload.
        signature: Hex EIP-191 signature from the order owner.
        claimed_owner: If provided (cancel passes ``submitted_by`` query
            param), must match order.submitted_by — second-layer check
            against the legacy ``submitted_by`` plumbing on cancel.
    """
    if os.environ.get("REQUIRE_ORDER_OWNER_SIG", "1").strip().lower() in ("0", "false", "no"):
        return

    from minotaur_subnet.consensus.order_owner_sig import verify_order_action

    owner = (getattr(order, "submitted_by", "") or "").strip()
    if not owner:
        raise HTTPException(
            status_code=400,
            detail="Order has no submitted_by; can't verify ownership",
        )
    if claimed_owner and claimed_owner.lower() != owner.lower():
        raise HTTPException(
            status_code=403,
            detail=(
                f"submitted_by query param ({claimed_owner[:10]}...) does not "
                f"match order owner ({owner[:10]}...)"
            ),
        )

    ok, err = verify_order_action(
        expected_owner=owner,
        action=action,
        order_id=getattr(order, "order_id", "") or "",
        content_hash=content_hash or "",
        deadline=int(deadline or 0),
        chain_id=int(getattr(order, "chain_id", 0) or 0),
        signature_hex=signature or "",
    )
    if not ok:
        raise HTTPException(status_code=403, detail=err)


def _resolve_token_params(params: dict, chain_id: int) -> dict:
    """Resolve token params: symbols → addresses, CAIP-10 → plain addresses.

    Accepts:
    - Token symbols: USDC, WETH → resolved via token registry
    - Plain 0x addresses: pass-through
    - CAIP-10: eip155:8453:0x833589... → parsed, chain extracted
    - Native sentinel: 0xEeee...eEEeE → replaced with wrappedNative for the chain

    If input and output tokens are on different chains (detected via CAIP-10),
    auto-sets ``dest_chain_id`` for cross-chain routing.
    """
    from minotaur_subnet.blockchain.tokens import resolve_token, WRAPPED_NATIVE_TOKEN
    from minotaur_subnet.shared.interop_address import parse_address

    # Well-known sentinel address for "native ETH/TAO" (used by frontends).
    # The solver only knows ERC-20s, so we swap it for the chain's WETH/WTAO.
    _NATIVE_SENTINEL = "0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"

    result = dict(params)
    token_chains: dict[str, int] = {}  # key → chain_id

    for key in ("input_token", "output_token"):
        val = result.get(key, "")
        if not val:
            continue

        # Native sentinel → wrapped native token for the chain
        if val.lower() == _NATIVE_SENTINEL:
            wnt = WRAPPED_NATIVE_TOKEN.get(chain_id)
            if wnt:
                result[key] = wnt
                result[f"_{key}_is_native"] = True  # tag for downstream
            token_chains[key] = chain_id
        # CAIP-10 format: eip155:chain_id:0xaddress
        elif val.startswith("eip155:"):
            try:
                ia = parse_address(val, default_chain_id=chain_id)
                result[key] = ia.address
                if ia.chain_id is not None:
                    token_chains[key] = ia.chain_id
            except ValueError:
                pass
        # Plain 0x address: pass-through
        elif val.startswith("0x"):
            token_chains[key] = chain_id
        # Symbol: resolve to address
        else:
            try:
                addr, resolved_chain = resolve_token(val, fallback_chain_id=chain_id)
                result[key] = addr
                token_chains[key] = resolved_chain
            except ValueError:
                pass

    # Auto-detect cross-chain from token chain IDs
    input_chain = token_chains.get("input_token", chain_id)
    output_chain = token_chains.get("output_token", chain_id)
    if input_chain != output_chain and not result.get("dest_chain_id"):
        result["dest_chain_id"] = str(output_chain)
        result["input_chain_id"] = input_chain
        result["output_chain_id"] = output_chain

    return result


def _fetch_user_nonce(contract_address: str, user_address: str, chain_id: int) -> int | None:
    """Read on-chain nonce for a user from AppIntentBase. Returns None on failure."""
    try:
        from minotaur_subnet.blockchain.chains import get_web3
        w3 = get_web3(chain_id)
        contract = w3.eth.contract(
            address=w3.to_checksum_address(contract_address),
            abi=[{"inputs": [{"name": "", "type": "address"}], "name": "nonces",
                  "outputs": [{"name": "", "type": "uint256"}], "stateMutability": "view", "type": "function"}],
        )
        return contract.functions.nonces(w3.to_checksum_address(user_address)).call()
    except Exception:
        return None


_MIN_PERPETUAL_COOLDOWN = 60.0  # seconds
_MAX_PERPETUAL_EXECUTIONS = 10_000


class SubmitOrderRequest(BaseModel):
    intent_function: str = "execute"
    params: dict[str, Any] = {}
    submitted_by: str
    chain_id: int = 1
    deadline: float = 0.0
    perpetual: bool = False
    max_executions: int = 1
    cooldown: float = 0.0
    user_signature: str = ""  # EIP-712 signature (hex), optional for backward compat


class DryRunRequest(BaseModel):
    interactions: list[dict[str, Any]]
    deadline: int = 0
    nonce: int = 0
    metadata: dict[str, Any] = {}


class QuoteRequest(BaseModel):
    """Request a quote without creating an order.

    The validator dry-runs the Solving Engine against the current MarketSnapshot
    and returns an estimated output. No order is created, no signature required.
    """
    intent_function: str = "execute"
    params: dict[str, Any]  # e.g. {input_token, output_token, input_amount}
    chain_id: int = 1
    slippage_bps: int = 50  # Default 0.5% slippage for suggested_min_output
    # Perpetual sizing: when set, the quote also returns the standing ERC-20
    # allowance the user must approve up front to fund all fills (no prefund/
    # escrow — the wallet + allowance IS the funding, drawn down per fill).
    perpetual: bool = False
    max_executions: int = 1
    cooldown: float = 0.0


@router.post("/apps/{app_id}/orders", status_code=201)
def submit_order(app_id: str, req: SubmitOrderRequest) -> dict:
    """Submit a new order to the Intent OrderBook.

    Follower-mode (audit H5, PR-2): when this api runs as a third-party
    follower validator (``ENABLE_SOLVER_ROUND_COORDINATOR=0``), orders
    don't belong here — they go to the leader. Reject with 404 so the
    caller can route to the leader instead of disk-filling the
    follower's ``store.json``.
    """
    if not _IS_LEADER_NODE:
        raise HTTPException(
            status_code=404,
            detail=(
                "Order submission is only available on the leader api. "
                "Route orders to the elected leader of subnet 112 — the "
                "leader's endpoint is discoverable via the on-chain "
                "ValidatorRegistry."
            ),
        )

    from minotaur_subnet.shared.interop_address import parse_address
    from minotaur_subnet.shared.feature_flags import (
        cross_chain_enabled,
        CROSS_CHAIN_DISABLED_MESSAGE,
    )

    ob = _require_orderbook()

    # Parse submitted_by: accept plain 0x, CAIP-10, or ERC-7930
    try:
        ia = parse_address(req.submitted_by, default_chain_id=req.chain_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    if ia.chain_id is not None and ia.chain_id != req.chain_id:
        raise HTTPException(
            status_code=400,
            detail=f"Address chain_id {ia.chain_id} != request chain_id {req.chain_id}",
        )

    # Beta scope is single-chain (Base). Cross-chain / multi-leg orders
    # live behind CROSS_CHAIN_ENABLED=1 until the bridge path clears
    # Phase 5 exit criteria.
    _input_chain = int(req.params.get("input_chain_id", req.chain_id) or req.chain_id)
    _output_chain = int(req.params.get("output_chain_id", req.chain_id) or req.chain_id)
    _dest_chain = int(req.params.get("dest_chain_id", 0) or 0) or _output_chain
    if (_input_chain != _output_chain or _dest_chain != _input_chain) and not cross_chain_enabled():
        raise HTTPException(status_code=400, detail=CROSS_CHAIN_DISABLED_MESSAGE)

    # ── Auto-resolve: token symbols → addresses ──
    req.params = _resolve_token_params(req.params, req.chain_id)

    # Check app exists and is deployed/order-ready (API-7)
    deployment = None
    if _app_store is not None:
        from minotaur_subnet.shared.types import AppStatus
        app_def = _app_store.get_app(app_id)
        if app_def is None:
            raise HTTPException(status_code=404, detail=f"App not found: {app_id}")

        # ── Auto-resolve: chain_id ──
        # If no order-ready deployment on the requested chain, find any ready one
        deployment = _app_store.get_deployment(app_id, chain_id=req.chain_id)
        if deployment is None or not deployment.status.is_order_ready():
            fallback = _app_store.get_deployment(app_id, chain_id=None)
            if fallback and fallback.status.is_order_ready():
                req.chain_id = fallback.chain_id
                deployment = fallback

        if deployment is None or not deployment.status.is_order_ready():
            status_label = deployment.status.value if deployment else "not deployed"
            raise HTTPException(
                status_code=400,
                detail=f"App {app_id} is not ready for orders (status: {status_label})",
            )

        # Off-chain mirror of the on-chain AppRegistry gate
        # (see subnet112/minotaur_contracts/src/AppRegistry.sol). Reject at
        # ingestion when the App contract is not authorised on this chain;
        # otherwise we'd burn validator + relayer work only to revert in the
        # contract's _requireRegistered() check. No-op on chains with no
        # APP_REGISTRY_{chain_id} env configured.
        from minotaur_subnet.consensus.app_registry_cache import is_registered_app
        if deployment.contract_address and not is_registered_app(
            deployment.contract_address, req.chain_id,
        ):
            raise HTTPException(
                status_code=403,
                detail=(
                    f"App {app_id} contract {deployment.contract_address} is "
                    f"not registered in the AppRegistry on chain {req.chain_id}"
                ),
            )

        # ── Auto-resolve: intent_function ──
        # If default "execute" isn't valid and only 1 function exists, use it
        if _js_engine is not None and req.intent_function == "execute":
            manifest = _js_engine.get_manifest(app_id) if hasattr(_js_engine, "get_manifest") else None
            if manifest and "intent_functions" in manifest:
                names = {
                    (f.get("name") if isinstance(f, dict) else f)
                    for f in manifest["intent_functions"]
                }
                if "execute" not in names and len(names) == 1:
                    req.intent_function = next(iter(names))

        # APP-7: Validate intent_function against manifest (if available)
        if _js_engine is not None and req.intent_function:
            manifest = _js_engine.get_manifest(app_id) if hasattr(_js_engine, "get_manifest") else None
            if manifest and "intent_functions" in manifest:
                declared = manifest["intent_functions"]
                valid_names = {
                    f.get("name", f) if isinstance(f, dict) else f
                    for f in declared
                }
                if req.intent_function not in valid_names:
                    raise HTTPException(
                        status_code=400,
                        detail=(
                            f"Unknown intent function '{req.intent_function}' for app {app_id}. "
                            f"Available: {sorted(valid_names)}"
                        ),
                    )

        # ── Auto-resolve: user nonce for managed wallets ──
        # Perpetuals are exempt: they must be signed ONCE with the sentinel
        # nonce (type(uint256).max) so the single signature stays valid across
        # every fill — the contract neither verifies nor increments the
        # sentinel. Pinning a concrete per-user nonce here would bind the
        # signature to a value the contract advances after fill #1, reverting
        # every fill thereafter. Leaving user_nonce absent resolves to the
        # sentinel downstream (see relayer.encoder._resolve_nonce).
        if not req.perpetual and "user_nonce" not in req.params and deployment and deployment.contract_address:
            wallet = _app_store.get_wallet(ia.address)
            if wallet is None:
                for w in _app_store.list_wallets():
                    if w.address.lower() == ia.address.lower():
                        wallet = w
                        break
            if wallet is not None:
                nonce = _fetch_user_nonce(deployment.contract_address, ia.address, req.chain_id)
                if nonce is not None:
                    req.params["user_nonce"] = nonce

    # Auto-approve tokens for managed wallets (WAL-3)
    if _app_store is not None:
        from minotaur_subnet.api import services as _svc
        # Pass intent_function so approval logic can skip permit for non-swap
        approval_params = {**req.params, "intent_function": req.intent_function}
        permit_params = _svc.ensure_token_approval(
            _app_store, ia.address, app_id, req.chain_id, approval_params,
        )
        if permit_params:
            req.params = {**req.params, **permit_params}

        # Build ABI-encoded intentParams for on-chain execution. ONE encoder for
        # every app, no app names: the generic, manifest-driven encoder lays out
        # exactly the fields THIS app's manifest declares for THIS intent
        # function (full fixed-width tuple, omitted fields filled from the
        # manifest/type default). Returns None only when the app has no usable
        # manifest/param spec — nothing to encode — in which case we leave
        # intent_params_hex unset, same as any app.
        intent_hex = _svc.build_intent_params_hex_from_manifest(
            _app_store, _js_engine, app_id,
            req.intent_function, req.params, ia.address,
        )
        if intent_hex:
            req.params["intent_params_hex"] = intent_hex
        # Set app_address and intent_selector for the relayer encoder
        if deployment is not None and deployment.contract_address:
            req.params["app_address"] = deployment.contract_address
        if req.intent_function and "intent_selector" not in req.params:
            selector = _svc.compute_intent_selector(
                _app_store, _js_engine, app_id, req.intent_function,
            )
            if selector:
                req.params["intent_selector"] = selector

    # Validate perpetual order parameters
    import time as _time
    if req.perpetual:
        if req.cooldown < _MIN_PERPETUAL_COOLDOWN:
            raise HTTPException(
                status_code=400,
                detail=f"Perpetual orders require cooldown >= {_MIN_PERPETUAL_COOLDOWN}s",
            )
        if req.max_executions > _MAX_PERPETUAL_EXECUTIONS:
            raise HTTPException(
                status_code=400,
                detail=f"max_executions cannot exceed {_MAX_PERPETUAL_EXECUTIONS}",
            )
        # One signature authorizes N fills, which only works with the sentinel
        # nonce (type(uint256).max): the contract skips the nonce check and never
        # increments it. A concrete nonce is spent on fill #1 and reverts every
        # fill after — reject it here rather than let the order die at fill #2.
        from minotaur_subnet.relayer.encoder import _resolve_nonce, _SENTINEL_NONCE
        _pnonce = req.params.get("user_nonce")
        if _pnonce is not None and _resolve_nonce(_pnonce) != _SENTINEL_NONCE:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Perpetual orders must use the sentinel nonce "
                    "(type(uint256).max) so one signature authorizes every fill; "
                    "a concrete user_nonce is only valid for one-shot orders."
                ),
            )
        # A perpetual is bound by its signed deadline on EVERY fill (the contract
        # enforces block.timestamp <= deadline each time), so the 1-hour default
        # below would silently cap it at ~1h regardless of max_executions. Require
        # an explicit, far-enough deadline instead of silently defaulting.
        _now = _time.time()
        if req.deadline <= 0:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Perpetual orders require an explicit far-future deadline: the "
                    "signed deadline caps every fill, so set one that spans the "
                    "intended schedule."
                ),
            )
        if req.deadline < _now + req.cooldown:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Perpetual deadline too soon: must be at least one cooldown "
                    f"({req.cooldown:.0f}s) in the future so more than one fill is "
                    "possible."
                ),
            )

    # Default deadline: 1 hour from now (on-chain rejects deadline=0).
    # Perpetuals are already validated to carry an explicit deadline above.
    effective_deadline = req.deadline
    if effective_deadline == 0:
        effective_deadline = _time.time() + 3600

    try:
        order = ob.submit(
            app_id=app_id,
            intent_function=req.intent_function,
            params=req.params,
            submitted_by=ia.address,
            chain_id=req.chain_id,
            deadline=effective_deadline,
            perpetual=req.perpetual,
            max_executions=req.max_executions,
            cooldown=req.cooldown,
            user_signature=req.user_signature,
        )

        # User signature validation: the on-chain contract (EIP712Verifier)
        # is the authoritative verifier. Server-side check is skipped because
        # the orderId is generated server-side after the frontend signs, so
        # the digests can't match. The contract verifies at executeIntent time.

        # Auto-sign EIP-712 order for managed wallets (WAL-4)
        if not req.user_signature and _app_store is not None:
            app_address = req.params.get("app_address", "")
            if app_address:
                sig = _svc.sign_user_order_for_managed_wallet(
                    _app_store, order, app_address, req.chain_id,
                )
                if sig:
                    ob.update_order(order.order_id, user_signature=sig)

        # Persist to store (OB-11)
        if _app_store is not None:
            try:
                _app_store.save_order(order.to_dict())
            except Exception:
                pass  # Best-effort persistence
        return order.to_dict()
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/orders/{order_id}/prepare-direct")
async def prepare_direct_submit(order_id: str) -> dict:
    """Wait for consensus, return executeIntent calldata for user to submit directly.

    Used for native ETH/TAO swaps where the user must provide ``msg.value``
    themselves — the relayer can't pull native tokens from an external wallet.
    The order must have been submitted with ``params._user_submit = True`` so
    the blockloop stops at APPROVED instead of relaying.

    Flow:
      1. Poll order status up to ~30s waiting for APPROVED (or terminal error).
      2. Extract validator signatures from consensus_result, sort by validator_id
         (to match the on-chain quorum verification order).
      3. Rebuild the ExecutionPlan from the stored dict.
      4. Encode the full ``executeIntent(order, plan, userSig, validatorSigs)``
         calldata using the relayer's encoder.
      5. Return everything the frontend needs to build a TX with ``msg.value``.
    """
    import asyncio as _asyncio
    from minotaur_subnet.orderbook.orderbook import OrderStatus
    from minotaur_subnet.shared.types import ExecutionPlan, Interaction
    from minotaur_subnet.relayer.encoder import encode_execute_intent_calldata

    ob = _require_orderbook()

    # Poll for APPROVED — status transitions from OPEN → ASSIGNED → SOLVED →
    # SCORED → APPROVED (when consensus reached). Terminal error states short-
    # circuit the wait.
    terminal_error_states = {
        OrderStatus.REJECTED,
        OrderStatus.EXPIRED,
        OrderStatus.CANCELLED,
        OrderStatus.BRIDGE_FAILED,
    }
    post_approved_states = {
        OrderStatus.APPROVED,
        OrderStatus.SUBMITTED,
        OrderStatus.FILLED,
    }

    order = None
    for _ in range(60):  # ~30s at 0.5s intervals
        order = ob.get(order_id)
        if order is None:
            raise HTTPException(status_code=404, detail=f"Order not found: {order_id}")
        if order.status in post_approved_states:
            break
        if order.status in terminal_error_states:
            raise HTTPException(
                status_code=400,
                detail=f"Order reached terminal state before approval: "
                       f"{order.status.value} ({order.error or 'unknown'})",
            )
        await _asyncio.sleep(0.5)

    if order is None or order.status not in post_approved_states:
        raise HTTPException(
            status_code=408,
            detail=f"Order did not reach APPROVED within timeout "
                   f"(current status: {order.status.value if order else 'missing'})",
        )

    # Extract validator signatures from consensus_result dict (JSON-safe form).
    consensus = order.consensus_result or {}
    approvals = consensus.get("approvals") or []
    if not approvals:
        raise HTTPException(
            status_code=500,
            detail="Consensus reached but no approvals recorded",
        )
    # Sort by validator_id (address) ascending — must match the order used by
    # the on-chain quorum verification in EIP712Verifier.
    sorted_approvals = sorted(
        approvals,
        key=lambda a: int(str(a.get("validator_id", "0x0")).replace("0x", "") or "0", 16),
    )
    validator_sigs: list[bytes] = []
    for a in sorted_approvals:
        sig_hex = a.get("signature", "")
        if sig_hex:
            validator_sigs.append(bytes.fromhex(sig_hex.replace("0x", "")))

    if not validator_sigs:
        raise HTTPException(
            status_code=500,
            detail="Consensus approvals present but no signatures",
        )

    # Rebuild ExecutionPlan from the stored dict.
    plan_dict = order.plan or {}
    interactions_raw = plan_dict.get("interactions") or []
    plan = ExecutionPlan(
        intent_id=plan_dict.get("intent_id", ""),
        interactions=[
            Interaction(
                target=ix.get("target", ""),
                value=str(ix.get("value", "0")),
                call_data=ix.get("call_data", "0x"),
                chain_id=int(ix.get("chain_id", 0) or 0),
            )
            for ix in interactions_raw
        ],
        deadline=int(plan_dict.get("deadline", 0) or 0),
        nonce=int(plan_dict.get("nonce", 0) or 0),
        metadata=plan_dict.get("metadata") or {},
    )

    # User EIP-712 signature — required for user-direct submit (no empty fallback
    # because the contract will call EIP712Verifier.verify with this exact sig).
    user_sig_hex = (order.user_signature or "").replace("0x", "")
    if not user_sig_hex:
        raise HTTPException(
            status_code=400,
            detail="Order is missing user EIP-712 signature — "
                   "call PATCH /orders/{id}/signature first",
        )
    user_sig = bytes.fromhex(user_sig_hex)

    calldata = encode_execute_intent_calldata(
        order, plan, user_sig, validator_sigs,
    )

    # msg.value = the input amount (native ETH/TAO the user is swapping).
    # The contract wraps msg.value → WETH inside _fundAndExecute.
    value_wei = str(order.params.get("input_amount", "0") or "0")

    # app_address is set by the submit endpoint from the deployment record.
    contract_address = order.params.get("app_address", "")
    if not contract_address:
        raise HTTPException(
            status_code=500,
            detail="Order has no app_address recorded",
        )

    return {
        "order_id": order_id,
        "contract_address": contract_address,
        "chain_id": order.chain_id,
        "calldata": calldata,
        "value": value_wei,
        "status": order.status.value,
    }


@router.patch("/orders/{order_id}/tx-confirmed")
async def confirm_user_submitted_tx(order_id: str, request: Request) -> dict:
    """Mark a user-submitted order as FILLED after the user's TX lands.

    For native ETH/TAO orders, the user sends the executeIntent TX themselves
    (with msg.value) instead of routing through the relayer. Once MetaMask
    confirms the receipt, the frontend calls this endpoint with the tx_hash
    so the API can finalize the order status.

    M4 (2026-05-25 audit): previously accepted any caller marking any
    APPROVED ``_user_submit`` order as FILLED with any tx_hash. Now
    requires an EIP-191 ``owner_signature`` over the ``ConfirmTx`` action
    payload (action, order_id, keccak(tx_hash), deadline, chain_id) from
    the order's ``submitted_by``.

    Body shape: ``{"tx_hash": "0x...", "owner_signature": "0x...", "deadline": int}``
    """
    from minotaur_subnet.consensus.order_owner_sig import (
        ACTION_CONFIRM_TX, content_hash_of,
    )
    from minotaur_subnet.orderbook.orderbook import OrderStatus

    ob = _require_orderbook()
    order = ob.get(order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="Order not found")

    if not order.params.get("_user_submit"):
        raise HTTPException(
            status_code=400,
            detail="Order is not marked for user-direct-submit",
        )
    if order.status != OrderStatus.APPROVED:
        raise HTTPException(
            status_code=400,
            detail=f"Order is in {order.status.value}, expected APPROVED",
        )

    body = await request.json()
    tx_hash = body.get("tx_hash", "")
    if not tx_hash:
        raise HTTPException(status_code=400, detail="tx_hash required")

    _enforce_order_owner_sig(
        order, action=ACTION_CONFIRM_TX,
        content_hash=content_hash_of(tx_hash),
        deadline=int(body.get("deadline") or 0),
        signature=body.get("owner_signature", ""),
    )

    ob.update_order(
        order_id,
        status=OrderStatus.FILLED,
        tx_hash=tx_hash,
    )
    if _app_store is not None:
        try:
            _app_store.save_order(ob.get(order_id).to_dict())
        except Exception:
            pass

    return {"order_id": order_id, "status": "filled", "tx_hash": tx_hash}


@router.patch("/orders/{order_id}/signature")
async def attach_signature(order_id: str, request: Request) -> dict:
    """Attach a user EIP-712 signature to an existing order.

    Called by the frontend after order creation: the frontend receives
    the order_id, constructs the EIP-712 typed data using the exact
    order fields, signs with MetaMask, and submits the signature here.
    The on-chain contract verifies the signature at executeIntent time.

    Auth: the EIP-712 ``user_signature`` itself proves caller identity —
    the server ECDSA-recovers the signer from the sig + reconstructs the
    IntentOrder typehash from this order's actual fields, then checks the
    recovered address equals ``order.submitted_by``. That blocks every
    attack the old EIP-191 owner-sig blocked:

      - Garbage bytes don't recover to ``submitted_by`` → 403.
      - A sig the user signed for a different order has a different
        ``orderId``/``paramsHash`` in its typehash, so reconstruction
        against THIS order's fields recovers a different (or no) signer.

    Pre-2026-05-26 the audit added a separate EIP-191 ``owner_signature``
    over an ``AttachSig`` action payload, because the server was skipping
    EIP-712 verification server-side (the comment in ``submit_order``
    explains why — order_id was minted after the sig). Pulling EIP-712
    verification into this route closes the same gap with no second
    wallet prompt. The ``owner_signature`` body field is now ignored when
    present (kept accepted for one-version backward-compat with frontends
    that haven't shipped the single-prompt change yet).

    Body shape: ``{"user_signature": "0x..."}``
    Legacy body fields (``owner_signature``, ``deadline``) are silently
    accepted and ignored.
    """
    ob = _require_orderbook()
    order = ob.get(order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="Order not found")

    body = await request.json()
    sig = body.get("user_signature", "")
    if not sig:
        raise HTTPException(status_code=400, detail="user_signature required")

    owner = (getattr(order, "submitted_by", "") or "").strip()
    if not owner:
        raise HTTPException(
            status_code=400,
            detail="Order has no submitted_by; can't verify ownership",
        )

    # Skip mostly mirrors the legacy EIP-191 escape hatch — same env var,
    # same one-off-incident semantics.
    if os.environ.get("REQUIRE_ORDER_OWNER_SIG", "1").strip().lower() not in (
        "0", "false", "no",
    ):
        from minotaur_subnet.api.routes._signature_verify import (
            verify_user_order_signature,
        )
        if not verify_user_order_signature(order, sig):
            raise HTTPException(
                status_code=403,
                detail=(
                    "user_signature does not recover to order.submitted_by "
                    "(or doesn't match this order's IntentOrder typehash)"
                ),
            )

    ob.update_order(order_id, user_signature=sig)
    if _app_store is not None:
        try:
            _app_store.save_order(ob.get(order_id).to_dict())
        except Exception:
            pass

    return {"order_id": order_id, "signature_attached": True}


def _verify_reader_sig(submitted_by: str, order_id: str, sig_hex: str) -> bool:
    """Verify an EIP-191 reader-sig over ``read-order:{order_id}``.

    Used by GET /orders[/{id}] to decide whether to include the
    sensitive ``user_signature`` field in the response. Reader-sigs are
    deliberately not deadline-bound — they're a "prove you're the
    owner" challenge, not an authorization for a state change. The
    on-chain EIP-712 signature is the authoritative gate at execution
    time, so leaking it via API is graveyard for replay protection but
    not a fund-loss vector.

    Returns True only when the recovered signer matches ``submitted_by``.
    """
    if not submitted_by or not sig_hex or not order_id:
        return False
    try:
        from eth_account import Account
        from eth_account.messages import encode_defunct
    except Exception:
        return False
    try:
        msg = encode_defunct(text=f"read-order:{order_id}")
        sig = sig_hex if sig_hex.startswith("0x") else "0x" + sig_hex
        recovered = Account.recover_message(msg, signature=sig)
    except Exception:
        return False
    return recovered.lower() == submitted_by.lower()


def _strip_user_signature(order_dict: dict) -> dict:
    """Return a copy of ``order_dict`` with ``user_signature`` stripped."""
    if not order_dict:
        return order_dict
    if "user_signature" not in order_dict:
        return order_dict
    out = dict(order_dict)
    out["user_signature"] = ""
    return out


# GET /orders list view. A full order record embeds the execution ``plan``,
# ``consensus_result``, ``plan_assessment`` and the raw ``intent_params_hex``
# calldata blob — ~4 KB per order, so the unbounded full list crossed 1.4 MB
# at ~400 orders. The list is therefore a paginated SUMMARY; the full record
# stays on ``GET /orders/{order_id}``, and ``?full=true`` restores it for the
# follower order-sync (the data is public — size, not secrecy, is the issue).
_LIST_SUMMARY_DROP = ("plan", "consensus_result", "plan_assessment", "user_signature")
_LIST_DEFAULT_LIMIT = 100
_LIST_MAX_LIMIT = 500


def _order_summary(order_dict: dict) -> dict:
    """Slim list-view projection of a full order dict (~4 KB → ~0.6 KB).

    Drops the heavyweight fields and ``params.intent_params_hex``, keeping the
    scalars a list consumer needs: ids, status, the decoded token/amount
    params, timestamps, scores, tx hash.
    """
    out = {k: v for k, v in order_dict.items() if k not in _LIST_SUMMARY_DROP}
    params = out.get("params")
    if isinstance(params, dict) and "intent_params_hex" in params:
        slim = dict(params)
        slim.pop("intent_params_hex")
        out["params"] = slim
    return out


def _created_at(order_dict: dict) -> float:
    """``created_at`` as a float for sorting; stores stringify it, and orders
    persisted before the field existed sort last (0.0)."""
    try:
        return float(order_dict.get("created_at") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _parse_class_csv(value: str | None) -> set[str]:
    """Parse a ``rejection_class`` filter param into a normalized set.

    Accepts a single class or a comma-separated list (``infra,solver``),
    case-insensitive, whitespace-tolerant. Empty / missing → empty set (no
    constraint). Used for both the include (``rejection_class``) and exclude
    (``exclude_rejection_class``) filters.
    """
    if not value:
        return set()
    return {part.strip().lower() for part in value.split(",") if part.strip()}


def _with_rejection_class(order_dict: dict) -> dict:
    """Ensure ``rejection_class`` is present on an order dict.

    In-memory orders carry it from ``Order.to_dict()``, but orders persisted
    before this field existed (the bulk of the historical store) don't. Compute
    it on read from the stored ``status`` + ``error`` so both paths — and every
    historical order — expose the same structured class without a store
    migration. Cheap pure-string work; safe to run on every read.
    """
    if "rejection_class" in order_dict:
        return order_dict
    out = dict(order_dict)
    out["rejection_class"] = classify_rejection(
        out.get("status", ""), out.get("error"),
    )
    return out


@router.get("/orders/{order_id}")
def get_order(
    order_id: str,
    x_reader_sig: str | None = Header(None, alias="X-Reader-Sig"),
) -> dict:
    """Get the status of an order.

    Checks the in-memory OrderBook first, then falls back to the
    persistent store (for orders from before a restart).

    PR-2 (audit M-orders-leak): the response previously included the
    raw EIP-712 ``user_signature``. The on-chain contract treats that
    sig as a one-shot authorization (consumed at execute time), so
    leaking it lets anyone front-run / replay the execution against the
    real owner. The field is now stripped unless the caller proves
    ownership via an EIP-191 ``X-Reader-Sig`` header that recovers to
    the order's ``submitted_by``. Message: ``read-order:{order_id}``.
    """
    ob = _require_orderbook()
    order = ob.get(order_id)
    if order is not None:
        d = order.to_dict()
    else:
        stored = (
            _app_store.get_order(order_id)
            if _app_store is not None and hasattr(_app_store, "get_order")
            else None
        )
        if stored is None:
            raise HTTPException(status_code=404, detail=f"Order not found: {order_id}")
        d = stored

    d = _with_rejection_class(d)  # backfill for historical store rows
    submitted_by = (d.get("submitted_by", "") or "")
    if not _verify_reader_sig(submitted_by, order_id, x_reader_sig or ""):
        d = _strip_user_signature(d)
    return d


@router.get("/orders")
def list_orders(
    app_id: str | None = None,
    status: str | None = None,
    rejection_class: str | None = None,
    exclude_rejection_class: str | None = None,
    limit: int = _LIST_DEFAULT_LIMIT,
    offset: int = 0,
    full: bool = False,
    x_reader_sig: str | None = Header(None, alias="X-Reader-Sig"),
) -> dict:
    """List orders — paginated (newest-first) SUMMARY view.

    Each entry is the :func:`_order_summary` projection (no ``plan`` /
    ``consensus_result`` / ``plan_assessment`` / ``intent_params_hex``); the
    full record is on ``GET /orders/{order_id}``. ``full=true`` returns
    unprojected records — used by the follower order-sync, which needs the
    complete order to rebuild its benchmark corpus. ``limit`` is clamped to
    [1, 500] (default 100); the response carries ``total`` (matches after
    filtering, before paging) so callers can page: ``count``/``limit``/
    ``offset`` describe the returned page.

    Filters:
      - ``app_id`` / ``status`` — passed through to the store (unchanged).
      - ``rejection_class`` — structured terminal-failure class (see
        ``orderbook.rejection``): ``duplicate`` (already served — not a real
        failure), ``user``, ``solver``, ``infra``, ``expired``, ``other``.
        Accepts a single class or a comma-separated allowlist
        (``infra,solver``) — an entry matches if its class is in the list.
      - ``exclude_rejection_class`` — the inverse: a single class or
        comma-separated denylist to drop. The idiomatic "real failures only"
        query is ``?status=rejected&exclude_rejection_class=duplicate`` (the
        ``duplicate`` bucket was already served, so it's not a failure).
        ``include`` is applied first, then ``exclude``.

        These let a dashboard fetch only the ``infra`` failures worth fixing,
        or a clean service-failure denominator, without string-matching
        ``error``.

    Every entry carries ``rejection_class`` (``None`` for non-failures), and the
    response includes ``rejection_class_counts`` — the breakdown over the
    ``app_id``/``status`` match set BEFORE the ``rejection_class`` filter and
    paging — so one call powers both a summary chart and a drill-down page.

    PR-2 (audit M-orders-leak): ``user_signature`` is stripped from
    every entry unless the caller presents an ``X-Reader-Sig`` that
    recovers to that specific order's owner. The list endpoint is more
    aggressive than the single-order GET because a single bulk read
    would otherwise leak signatures for every order at once — so a
    reader-sig only unlocks the orders it actually owns.
    """
    ob = _require_orderbook()
    limit = max(1, min(int(limit), _LIST_MAX_LIMIT))
    offset = max(0, int(offset))
    # The durable store is the source of truth for which orders exist — it
    # survives restarts, whereas the in-memory OrderBook only holds the live
    # working set (and on restart only OPEN orders are reloaded). Source the
    # list from the store, then overlay the OrderBook for the freshest
    # in-flight state. Mirrors the store fallback in get_order(). Without
    # this, terminal orders (filled/rejected) vanish from the list after a
    # restart even though they are persisted.
    merged: dict[str, dict] = {}
    if _app_store is not None and hasattr(_app_store, "list_orders"):
        for d in _app_store.list_orders(app_id=app_id, status=status):
            oid = d.get("order_id")
            if oid:
                merged[oid] = d
    for o in ob.list_orders(app_id=app_id, status=status):
        d = o.to_dict()
        oid = d.get("order_id")
        if oid:
            merged[oid] = d
    # Ensure every record carries a rejection_class (historical store rows
    # predate the field — backfill from status+error on read).
    ordered = [_with_rejection_class(d) for d in merged.values()]
    # Newest first; order_id tie-break keeps pages stable across requests
    # when created_at collides (or is missing on pre-field orders).
    ordered.sort(key=lambda d: (-_created_at(d), str(d.get("order_id") or "")))

    # Breakdown over the app_id/status match set (all classes), computed BEFORE
    # the rejection_class filter so the chart totals stay stable while a caller
    # drills into one class. None (non-failures) is omitted.
    rejection_class_counts: dict[str, int] = {}
    for d in ordered:
        rc = d.get("rejection_class")
        if rc:
            rejection_class_counts[rc] = rejection_class_counts.get(rc, 0) + 1

    # Include (allowlist) first, then exclude (denylist). Both accept a single
    # class or a comma-separated list. "Real failures only" is
    # exclude_rejection_class=duplicate.
    include = _parse_class_csv(rejection_class)
    exclude = _parse_class_csv(exclude_rejection_class)
    if include:
        ordered = [d for d in ordered if (d.get("rejection_class") or "") in include]
    if exclude:
        ordered = [d for d in ordered if (d.get("rejection_class") or "") not in exclude]

    total = len(ordered)
    out = []
    for d in ordered[offset : offset + limit]:
        submitted_by = (d.get("submitted_by", "") or "")
        if not _verify_reader_sig(submitted_by, d.get("order_id", ""), x_reader_sig or ""):
            d = _strip_user_signature(d)
        out.append(d if full else _order_summary(d))
    return {
        "orders": out,
        "count": len(out),
        "total": total,
        "limit": limit,
        "offset": offset,
        "rejection_class_counts": rejection_class_counts,
    }


@router.get("/quotes")
def list_quotes(
    app_id: str | None = None,
    chain_id: int | None = None,
    limit: int = _LIST_DEFAULT_LIMIT,
    offset: int = 0,
    full: bool = False,
) -> dict:
    """List captured quote CASES — the demand corpus (newest-first, paginated).

    A quote case is the trade descriptor of a served ``/quote`` (app_id, chain_id,
    intent_function, params), keyed by a content-addressed ``quote_id``. This
    endpoint powers two consumers:

      * The follower ``QuoteSync`` loop (``full=1`` for the complete params blob),
        which upserts the leader's quotes so every validator's Stage-2 quote draw
        matches — the quote analogue of ``/orders?full=1``.
      * MINERS hunting blind spots: quotes are DEMAND (including pairs the champion
        can't route — the zero-output cases), so this is the "what do users want
        that nobody serves yet" feed. It is served regardless of whether quotes are
        in the scored corpus yet (BENCHMARK_QUOTE_CORPUS), so miners can widen
        coverage ahead of the soak.

    Quote cases are served without a reader-sig gate because they hold only the
    trade descriptor: a quote never had a submitted_by or signature, and capture
    strips identity/derived params (receiver, intent_params_hex, permit, …) via
    ``QUOTE_PARAM_STRIP_FIELDS`` before storage, so the public body carries only
    tokens/amounts/chain. ``limit`` is clamped to [1, 500] (default 100); ``total``
    is the match count before paging.
    """
    if _app_store is None or not hasattr(_app_store, "list_quotes"):
        return {"quotes": [], "count": 0, "total": 0, "limit": limit, "offset": offset}
    limit = max(1, min(int(limit), _LIST_MAX_LIMIT))
    offset = max(0, int(offset))
    rows = _app_store.list_quotes(app_id=app_id, chain_id=chain_id)
    # Newest first; quote_id tie-break keeps pages stable when created_at collides.
    rows.sort(key=lambda d: (-_created_at(d), str(d.get("quote_id") or "")))
    total = len(rows)
    # ``full`` is accepted for parity with /orders (QuoteSync passes full=1) but is
    # currently a no-op: a quote case is only its small trade descriptor, so there
    # is no heavy blob to project away. Kept in the signature so the sync contract
    # and a future slim view stay backward-compatible.
    page = rows[offset : offset + limit]
    return {
        "quotes": page,
        "count": len(page),
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.delete("/orders/{order_id}")
def cancel_order(
    order_id: str,
    submitted_by: str = Query(...),
    owner_signature: str = Query(""),
    deadline: int = Query(0),
) -> dict:
    """Cancel an open order.

    M3 (2026-05-25 audit): previously trusted the client-supplied
    ``submitted_by`` query param. Anyone who could read ``GET /orders/{id}``
    learned the owner address and could cancel the order. Now requires
    an EIP-191 ``owner_signature`` from the order owner over a
    ``Cancel`` action payload with a freshness ``deadline``.

    Args:
        order_id: The order to cancel.
        submitted_by: Wallet address of the order owner (required).
        owner_signature: Hex EIP-191 signature from the owner over the
            canonical Cancel payload (action="Cancel", order_id, "",
            deadline, chain_id). Required unless
            ``REQUIRE_ORDER_OWNER_SIG=0`` (audit-incident-only override).
        deadline: Unix-seconds deadline included in the signed payload.
    """
    from minotaur_subnet.consensus.order_owner_sig import (
        ACTION_CANCEL, verify_order_action,
    )

    ob = _require_orderbook()
    order = ob.get(order_id)
    if order is None:
        raise HTTPException(status_code=404, detail=f"Order not found: {order_id}")

    _enforce_order_owner_sig(
        order, action=ACTION_CANCEL,
        content_hash="", deadline=deadline, signature=owner_signature,
        claimed_owner=submitted_by,
    )

    try:
        success = ob.cancel(order_id, submitted_by=submitted_by)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    if not success:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot cancel order {order_id} (not found or not cancellable)",
        )
    return {"order_id": order_id, "status": "cancelled"}


@router.post(
    "/orders/{order_id}/dry-run",
    dependencies=[Depends(_require_admin_or_signed_miner)],
)
async def dry_run_order(order_id: str, req: DryRunRequest) -> dict:
    """Score a plan against an order without side effects.

    Audit M-dry-run originally admin-gated this endpoint to prevent
    anonymous callers from pinning the validator's worker threads on
    synthetic plans. That fixed the DoS, but also locked out the
    legitimate miner use case the endpoint was built for ("miners use
    this to test their plans before submitting solver code" per the
    service docstring).

    Current gate accepts EITHER:
      - ``X-Admin-Key`` (admin path, unchanged) — full bypass for operators.
      - Bittensor-hotkey-signed headers (miner path) — registered SN112
        miners can call directly. Per-hotkey rate-limited (default 60/hr)
        so DoS protection is preserved. See ``_require_admin_or_signed_miner``
        in routes/apps.py for the signing protocol, and
        scripts/miner_dry_run.py for a reference client.
    """
    from minotaur_subnet.api import services as _tools

    ob = _require_orderbook()
    s = _app_store
    if s is None:
        from minotaur_subnet.api.server import store
        s = store

    return await _tools.dry_run_order(
        s, ob, _js_engine,
        order_id, req.interactions, req.deadline, req.nonce, req.metadata,
    )


@router.post("/apps/{app_id}/quote")
async def get_quote(app_id: str, req: QuoteRequest, request: Request) -> dict:
    """Get an estimated quote for an intent without creating an order.

    Calls the solver's quote() for output/route estimation, then runs a real
    simulation to measure gas and prices the protocol fee HERE (fee_policy) —
    the binding fee is computed by the validator, not the miner-controlled
    solver. No JS scoring, no order created.

    Returns:
        estimated_output: Best output amount the solver found (as string)
        suggested_min_output: estimated_output * (1 - slippage_bps/10000)
        route_summary: Human-readable description of the route
        gas_estimate: Estimated gas units
        platform_fee_wei: Minotaur-computed protocol fee (locked when signed)
        valid_for_seconds: How long this quote is indicative (always 30)
    """
    # Every quote runs a simulation below — rate-limit to bound that cost.
    _quote_rate_limit(request)

    # ── Auto-resolve: token symbols → addresses ──
    req.params = _resolve_token_params(req.params, req.chain_id)

    # Validate app exists and is active
    s = _app_store
    if s is None:
        from minotaur_subnet.api.server import store as _s
        s = _s
    if s is None:
        raise HTTPException(status_code=503, detail="App store not initialized")

    app_def = s.get_app(app_id)
    if app_def is None:
        raise HTTPException(status_code=404, detail=f"App not found: {app_id}")

    # ── Auto-resolve: chain_id (only if not explicitly set by user) ──
    # For cross-chain, prefer the input token's chain (source chain).
    if s is not None and req.chain_id in (0, 1):
        # If CAIP-10 parsing detected a source chain, prefer it
        input_chain = req.params.get("input_chain_id")
        if input_chain:
            req.chain_id = int(input_chain)
        else:
            deployment = s.get_deployment(app_id, chain_id=req.chain_id)
            if deployment is None or not deployment.status.is_operational():
                fallback = s.get_deployment(app_id, chain_id=None)
                if fallback and fallback.status.is_operational():
                    req.chain_id = fallback.chain_id

    # ── Auto-resolve: intent_function ──
    if _js_engine is not None and req.intent_function == "execute":
        manifest = _js_engine.get_manifest(app_id) if hasattr(_js_engine, "get_manifest") else None
        if manifest and "intent_functions" in manifest:
            names = {
                (f.get("name") if isinstance(f, dict) else f)
                for f in manifest["intent_functions"]
            }
            if "execute" not in names and len(names) == 1:
                req.intent_function = next(iter(names))

    # Need a solver
    bl = _block_loop
    if bl is None or bl.solver is None:
        raise HTTPException(
            status_code=503,
            detail="No solver available — submit one via the git-based submission pipeline",
        )

    from minotaur_subnet.shared.types import IntentState

    # Build state (no real contract address for a quote)
    state = IntentState(
        contract_address="",
        chain_id=req.chain_id,
        nonce=0,
        owner="",
        raw_params=req.params,
    )

    # ── Quote ENTIRELY from generate_plan + its simulation ──
    # This endpoint deliberately does NOT call solver.quote(): quote() and
    # generate_plan() are DIFFERENT code paths. quote() is a frozen generic
    # pool-router that returns "0" for many tokens whose routes only the solver's
    # generate_plan() knows — yet generate_plan()'s plan is EXACTLY what the
    # benchmark simulates and settles. So we quote from the settled plan:
    #   estimated_output = the plan's DELIVERED output (measured like the
    #                      benchmark — metadata.raw_output, else output-token
    #                      transfers to the recipient/contract),
    #   gas_estimate     = the sim's measured swap gas + framework overhead,
    #   route_summary    = plan.metadata["route"].
    # solver.quote(), SolverSession, baseline_solver and everything under harness/
    # are UNTOUCHED — the benchmark still calls session.quote() for its own
    # min-floor / sandbag paths; this endpoint is isolated from scoring.
    import inspect as _inspect
    from minotaur_subnet import fee_policy
    from minotaur_subnet.shared.types import QuoteResult as _QuoteResult

    quote_result = _QuoteResult(estimated_output="0")
    _binding_gas_units = 0

    # The anvil-fork plan sim is expensive and now runs on EVERY quote — cache it
    # briefly keyed by (app_id, chain_id, intent_function, params). A sim failure
    # degrades to a structured zero quote (never a 500).
    _cache_key = _quote_plan_cache_key(app_id, req.chain_id, req.intent_function, req.params)
    _cached = _quote_plan_cache_get(_cache_key)
    try:
        from minotaur_subnet.orderbook.orderbook import Order as _Order
        _dep = s.get_deployment(app_id, chain_id=req.chain_id) if s else None
        _deployed = _dep.contract_address if (_dep and _dep.contract_address) else ""
        _sim_runner = getattr(bl, "_simulation_runner", None)
        _has_sim = _sim_runner is not None and getattr(_sim_runner, "simulator", None) is not None

        if _cached is not None:
            quote_result.estimated_output = str(_cached.get("delivered", 0))
            quote_result.route_summary = _cached.get("route", "") or ""
            quote_result.metadata = dict(_cached.get("metadata", {}) or {})
            _binding_gas_units = int(_cached.get("gas_units", 0) or 0)
        else:
            _plan_state = IntentState(
                contract_address=_deployed,
                chain_id=req.chain_id,
                nonce=0,
                owner="0x000000000000000000000000000000000000dEaD",
                raw_params=req.params,
                control={"_intent_function": req.intent_function},
            )
            _plan_call = bl.solver.generate_plan(app_def, _plan_state)
            _plan = await _plan_call if _inspect.isawaitable(_plan_call) else _plan_call
            if _plan is not None:
                _pmeta = _plan.metadata or {}
                quote_result.route_summary = str(_pmeta.get("route", "") or "")
                quote_result.metadata = {
                    _k: _pmeta[_k]
                    for _k in ("route", "dex", "fee_tier", "pool", "data_source", "plan_type")
                    if _pmeta.get(_k) is not None
                }
                if _pmeta.get("cross_chain"):
                    # Cross-chain plans can't be single-fork-simulated here. Derive
                    # a best-effort estimate from plan metadata rather than 0 so
                    # cross-chain quotes don't regress; gas stays at floor.
                    _xc_amt = (
                        _pmeta.get("dst_amount")
                        or _pmeta.get("expected_output")
                        or _pmeta.get("bridge_amount")
                    )
                    if _xc_amt is not None:
                        try:
                            quote_result.estimated_output = str(int(_xc_amt))
                        except (ValueError, TypeError):
                            pass
                elif _deployed and _has_sim:
                    # Run the SAME scoreIntent path the benchmark scores (proxy
                    # deploy, token funding, plan execution, transfer capture) so
                    # exotic / USDC-paired tokens (e.g. DONALDPUMP) quote non-zero.
                    # A bare-interaction sim (intent_order=None) runs each call
                    # independently, captures no meaningful transfers, and returns
                    # 0 for those tokens — the benchmark instead passes an
                    # intent_order built by _build_benchmark_intent_order, so we
                    # mirror it exactly here.
                    #
                    # Revert-avoidance: SimulationRunner.simulate() funds
                    # ``order.submitted_by`` (input-token balance + allowance to the
                    # app contract), while scoreIntent pulls the input via
                    # safeTransferFrom(intent_order["submitted_by"], proxy, ...).
                    # If those two addresses differ the pull reverts → 0 again. So
                    # we derive submitted_by the SAME way the benchmark does
                    # (params["receiver"] or the pre-funded Anvil default account)
                    # and reuse it for BOTH the order and the intent_order — the
                    # funded address then equals scoreIntent's source.
                    from minotaur_subnet.harness.orchestrator import (
                        _build_benchmark_intent_order,
                    )
                    _ANVIL_DEFAULT_ACCOUNT = "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"
                    _bench_receiver = req.params.get("receiver")
                    if (not _bench_receiver
                            or _bench_receiver == "0x0000000000000000000000000000000000000001"):
                        _bench_receiver = _ANVIL_DEFAULT_ACCOUNT
                    _prov_order = _Order(
                        order_id="quote-sim",
                        app_id=app_id,
                        intent_function=req.intent_function,
                        params=dict(req.params),
                        submitted_by=_bench_receiver,
                        chain_id=req.chain_id,
                    )
                    # Build the scoreIntent intent_order exactly like the benchmark,
                    # pinning `receiver` to the funded submitted_by so the encoded
                    # intent and the seeded balances/allowance agree. Degrade to the
                    # bare path (None) if the encoder can't build one — never 500.
                    _intent_order = None
                    try:
                        _bench_manifest = (
                            _js_engine.get_manifest(app_id)
                            if (_js_engine is not None
                                and hasattr(_js_engine, "get_manifest"))
                            else None
                        )
                        if (_bench_manifest is None and _js_engine is not None
                                and getattr(app_def, "js_code", None)):
                            # The engine caches manifests only for intents
                            # something has already SCORED (order scoring and
                            # the blockloop lazy-load on first use). An app
                            # that has never had a live order — e.g. a fresh
                            # V2 deployment — is absent, which silently
                            # degraded its quotes to the bare-interaction sim
                            # (documented to measure 0 delivered for most
                            # plans). Lazy-load it here exactly like
                            # order_service does.
                            try:
                                await _js_engine.load_intent(app_id, app_def.js_code)
                                _bench_manifest = _js_engine.get_manifest(app_id)
                            except Exception as _mexc:
                                logger.warning(
                                    "Quote manifest lazy-load failed for %s: %s",
                                    app_id, _mexc,
                                )
                        _bench_state = IntentState(
                            contract_address=_deployed,
                            chain_id=req.chain_id,
                            nonce=0,
                            owner=_bench_receiver,
                            raw_params={**req.params, "receiver": _bench_receiver},
                            control={"_intent_function": req.intent_function},
                        )
                        _intent_order = _build_benchmark_intent_order(
                            _bench_state, _plan, _bench_manifest,
                        )
                    except Exception as _ioexc:
                        logger.warning(
                            "Quote intent_order build failed for %s: %s", app_id, _ioexc,
                        )
                        _intent_order = None
                    # scoreIntent sim (intent_order set) measures the swap gas AND
                    # captures the delivered output (metadata.raw_output / transfers);
                    # we add the framework wrapper overhead (executeIntent, proxy
                    # deploy, sig verify, fee settle) when pricing the fee below.
                    #
                    # contract_address must be the deployed app when we have an
                    # intent_order: the simulator's scoreIntent branch gates on
                    # `contract_address AND intent_order` (anvil_simulator), so
                    # passing None here silently demoted every quote to the
                    # bare-interaction path (0 delivered for most plans) even
                    # with a perfectly built intent_order. Mirrors
                    # order_processor.py's call shape.
                    _sim = await _sim_runner.simulate(
                        _plan, _prov_order,
                        _deployed if _intent_order else None,
                        _intent_order, False, _deployed,
                    )
                    _swap_gas = int(getattr(_sim, "gas_used", 0) or 0)
                    if _swap_gas > 0:
                        _binding_gas_units = _swap_gas + fee_policy.framework_overhead_gas()
                    _delivered = _quote_delivered_output(_sim, req.params, _deployed)
                    quote_result.estimated_output = str(_delivered)
                    _quote_plan_cache_put(_cache_key, {
                        "delivered": _delivered,
                        "gas_units": _binding_gas_units,
                        "route": quote_result.route_summary,
                        "metadata": quote_result.metadata,
                    })
    except Exception as exc:
        # Never 500 on a sim hiccup — degrade to a structured zero quote.
        logger.warning(
            "Quote plan simulation failed for %s on chain %s: %s",
            app_id, req.chain_id, exc,
        )
        _binding_gas_units = 0

    # ── Minotaur-controlled fee: price the plan's measured gas HERE ──
    # The BINDING fee is computed by us (fee_policy) from the plan sim's measured
    # gas — miners cannot understate the gas of their own plan (the same plan is
    # simulated and executed). On any failure we fall back to the per-chain floor.
    _gas_price = fee_policy.current_gas_price_wei(req.chain_id)
    _binding_fee = fee_policy.protocol_fee_wei(
        req.chain_id, _binding_gas_units, _gas_price,
    )
    from minotaur_subnet.blockchain.tokens import (
        WRAPPED_NATIVE_TOKEN, WRAPPED_NATIVE_SYMBOL,
    )
    quote_result.platform_fee_wei = str(_binding_fee)
    quote_result.platform_fee_token = (
        WRAPPED_NATIVE_TOKEN.get(req.chain_id) or quote_result.platform_fee_token or ""
    )
    quote_result.platform_fee_symbol = (
        WRAPPED_NATIVE_SYMBOL.get(req.chain_id) or quote_result.platform_fee_symbol or ""
    )
    if _binding_gas_units > 0:
        quote_result.gas_estimate = _binding_gas_units

    estimated_output_gross = quote_result.estimated_output
    # Track successful quote (zero output counts as failure)
    if s is not None:
        _ok = estimated_output_gross != "0"
        s.record_quote_attempt(app_id, success=_ok, error="" if _ok else "zero_output")

    # Persist the quote CASE for the demand corpus (Phase-1 soak). Best-effort and
    # fully isolated from the response — capture never fails a quote. The row is
    # keyed by a content-addressed quote_id so exact re-quotes collapse to one row;
    # QuoteSync replicates it leader → follower, and the round-seeded quote draw
    # samples it once BENCHMARK_QUOTE_CORPUS is enabled. A zero-output quote (a pair
    # the champion can't route) is captured too — that's the blind-spot demand we
    # want miners to chase.
    #
    # LEADER-ONLY (consensus-critical): capture is gated on _IS_LEADER_NODE, mirroring
    # the order-submit gate two functions up. /quote is a public read any node can
    # answer, but the benchmark corpus MUST have a single writer — the leader — or a
    # follower that answers even one /quote would persist a leader-absent quote_id
    # that upsert-only QuoteSync never removes, making its store a superset of the
    # leader's and diverging the pack hash the instant BENCHMARK_QUOTE_CORPUS is armed
    # fleet-wide (PACK_HASH_MISMATCH → quorum strands). Followers get quotes ONLY via
    # QuoteSync, which reconciles to an exact mirror of the leader.
    #
    # Identity/derived params (receiver, intent_params_hex, permit, …) are stripped
    # before storage: quote cases are served on the PUBLIC /v1/quotes, so only the
    # trade descriptor is retained. See QUOTE_PARAM_STRIP_FIELDS.
    if (
        _IS_LEADER_NODE
        and s is not None
        and hasattr(s, "save_quote")
        and quote_capture_enabled()
    ):
        try:
            from minotaur_subnet.harness.order_sampler import (
                QUOTE_PARAM_STRIP_FIELDS,
                quote_case_id,
            )
            # ROUND ANCHOR (Phase-2): stamp the quote with the CURRENT round's
            # opened_epoch so the sampling cutoff/retention are pure functions of
            # fleet-uniform round epochs, not wall-clock. The leader owns the round
            # coordinator, so the live round is always resolvable here. If there is
            # NO current round we cannot anchor the quote — SKIP capture rather than
            # store an un-anchored row that the round cutoff can never place.
            _cur_round = None
            try:
                from minotaur_subnet.api.routes import submissions as _subs
                _cur_round = _subs.get_round_store().get_current_round()
            except Exception:
                _cur_round = None
            _cur_epoch = getattr(_cur_round, "opened_epoch", None)
            if _cur_epoch is None:
                logger.debug("quote-case capture skipped: no current round to anchor")
            else:
                _q_params = {
                    k: v for k, v in (req.params or {}).items()
                    if k not in QUOTE_PARAM_STRIP_FIELDS
                }
                _q_id = quote_case_id(
                    app_id, req.chain_id, req.intent_function, _q_params,
                )
                # FIRST-SEEN: a re-quote in a later round must not bump the anchor
                # forward (it must stay monotone). Reuse the existing row's epoch if
                # present; save_quote also COALESCE-preserves it as defense-in-depth.
                _captured_epoch = int(_cur_epoch)
                try:
                    _existing = s.get_quote(_q_id) if hasattr(s, "get_quote") else None
                except Exception:
                    _existing = None
                if _existing and _existing.get("captured_opened_epoch") is not None:
                    _captured_epoch = int(_existing["captured_opened_epoch"])
                s.save_quote({
                    "quote_id": _q_id,
                    "app_id": app_id,
                    "chain_id": req.chain_id,
                    "intent_function": req.intent_function,
                    "params": _q_params,
                    "estimated_output": estimated_output_gross,
                    "created_at": time.time(),
                    "captured_opened_epoch": _captured_epoch,
                })
                _maybe_prune_quotes(s, int(_cur_epoch))
        except Exception:
            logger.debug("quote-case capture failed for %s", app_id, exc_info=True)

    # Net output = gross - platform fee, but ONLY when the fee is
    # denominated in the same token as the output. The fee can come back
    # in WETH (base 18) while the swap output is USDC (base 6) — direct
    # subtraction in that case clamps to 0 and shows users a misleading
    # "0 USDC" estimate (vs CoW/Paraswap showing the real ~10 USDC).
    # When tokens differ, surface gross as the estimate and let the UI
    # render the fee separately (already returned in `platform_fee_wei`
    # + `platform_fee_token` + `platform_fee_symbol`).
    platform_fee_int = 0
    try:
        platform_fee_int = int(quote_result.platform_fee_wei or "0")
    except (ValueError, TypeError):
        pass
    fee_token = (quote_result.platform_fee_token or "").lower()
    output_token = (req.params.get("output_token") or "").lower()
    fee_in_output_token = bool(fee_token) and fee_token == output_token
    try:
        gross_int = int(estimated_output_gross)
        if platform_fee_int > 0 and fee_in_output_token:
            estimated_output = str(max(0, gross_int - platform_fee_int))
        else:
            estimated_output = estimated_output_gross
    except (ValueError, TypeError):
        estimated_output = estimated_output_gross

    # Apply slippage to NET output (what user actually receives)
    slippage_bps = max(0, min(req.slippage_bps, 10000))
    suggested_min_output = "0"
    try:
        est_int = int(estimated_output)
        if est_int > 0:
            suggested_min_output = str(est_int * (10000 - slippage_bps) // 10000)
    except (ValueError, TypeError):
        pass

    # Build computed_params from manifest's quote-sourced param definitions
    # Generic quote outputs an app's manifest can bind to via
    # source:"quote" + quote_field. Apps choose which of these their
    # computational/quoted params reference — nothing here is swap-specific.
    quote_values = {
        "estimated_output": estimated_output,            # net (after protocol fee)
        "estimated_output_gross": estimated_output_gross,
        "suggested_min_output": suggested_min_output,
        "platform_fee_wei": quote_result.platform_fee_wei or "0",
        "gas_estimate": str(quote_result.gas_estimate or 0),
    }
    computed_params: dict[str, str] = dict(quote_result.computed_params)
    manifest = None
    if _js_engine is not None and hasattr(_js_engine, "get_manifest"):
        try:
            manifest = _js_engine.get_manifest(app_id)
        except Exception:
            pass
    if manifest and "intent_functions" in manifest:
        for fn_def in manifest["intent_functions"]:
            if fn_def.get("name") == req.intent_function:
                for param_name, param_def in fn_def.get("params", {}).items():
                    if param_def.get("source") == "quote":
                        qf = param_def.get("quote_field", "")
                        if qf and qf in quote_values:
                            computed_params[param_name] = quote_values[qf]
                break

    # Build interop token fields for response clarity
    from minotaur_subnet.blockchain.tokens import to_interop
    ready = {**req.params, **computed_params}
    input_chain = int(req.params.get("input_chain_id", req.chain_id) or req.chain_id)
    output_chain = int(req.params.get("output_chain_id", req.chain_id) or req.chain_id)
    dest_chain = int(req.params.get("dest_chain_id", 0) or 0) or output_chain

    # Include platform fee in ready_params so it flows into intent encoding
    if quote_result.platform_fee_wei and quote_result.platform_fee_wei != "0":
        ready["platform_fee_wei"] = quote_result.platform_fee_wei

    response = {
        "app_id": app_id,
        "estimated_output": estimated_output,             # net (after platform fee)
        "estimated_output_gross": estimated_output_gross,  # before fees — for debugging
        "suggested_min_output": suggested_min_output,
        "slippage_bps": slippage_bps,
        "route_summary": quote_result.route_summary,
        "gas_estimate": quote_result.gas_estimate,
        "valid_for_seconds": _QUOTE_VALID_SECONDS,
        "chain_id": req.chain_id,
        "intent_function": req.intent_function,
        "computed_params": computed_params,
        "ready_params": ready,
        "platform_fee_wei": quote_result.platform_fee_wei,
        "platform_fee_token": quote_result.platform_fee_token,
        "platform_fee_symbol": quote_result.platform_fee_symbol,
        # Solver-supplied route metadata (DEX, fee tier, pool addresses,
        # data source). Clients use this to display which protocol the
        # quote came from and to debug routing decisions.
        "metadata": getattr(quote_result, "metadata", None) or {},
    }

    # Add interop token addresses for cross-chain clarity
    if ready.get("input_token"):
        response["interop_input_token"] = to_interop(ready["input_token"], input_chain)
    if ready.get("output_token"):
        response["interop_output_token"] = to_interop(ready["output_token"], dest_chain)
    if input_chain != output_chain:
        response["cross_chain"] = True
        response["src_chain_id"] = input_chain
        response["dst_chain_id"] = dest_chain

    # ── Perpetual: required standing allowances (the TWO per-fill pulls) ──
    # A perpetual fills up to max_executions times from ONE signed order. There is
    # NO prefund/escrow — the user's wallet balance + standing allowances ARE the
    # funding, drawn down per fill; a shortfall terminates the perpetual. Two
    # independent pulls must be covered for all fills (app-agnostic — the input
    # token is whatever this app declares, not swap-specific):
    #   1. INPUT/spend token — the token the app safeTransferFroms from the user
    #      each fill (always required for ERC-20 input).
    #   2. Platform FEE (WETH) — required from the USER only in FeeMode.USER;
    #      APP-mode apps (e.g. DexAggregator) settle it from output / the app
    #      float, so the user needs no WETH there. WETH has no EIP-2612 permit, so
    #      a USER-mode fee allowance must be a plain on-chain approve().
    if req.perpetual and req.max_executions > 1:
        n = req.max_executions
        from minotaur_subnet.blockchain.tokens import resolve_spend_token_amount
        spend_token, spend_amount = resolve_spend_token_amount(req.params)
        per_fill_input = spend_amount or 0
        perp: dict[str, Any] = {
            "max_executions": n,
            "cooldown": req.cooldown,
            "per_fill_input_wei": str(per_fill_input),
            "required_input_allowance_wei": str(per_fill_input * n),
            "input_token": spend_token or req.params.get("input_token", ""),
            "per_fill_fee_wei": str(platform_fee_int),
            "fee_token": quote_result.platform_fee_token or "",
            "fee_symbol": quote_result.platform_fee_symbol or "",
            "fee_from_user": False,
        }
        note = (
            "Approve at least required_input_allowance_wei of input_token to the "
            "app contract (or attach an EIP-2612 permit if the input token supports "
            "it). Each fill draws from your wallet; running out of balance or "
            "allowance terminates the perpetual. "
        )
        # Does THIS app pull the fee from the user (FeeMode.USER)? Read on-chain
        # so the quote matches the pre-flight gate. Best-effort — omit the fee
        # requirement (assume deducted-from-output) on any read failure.
        if _deployed and platform_fee_int > 0:
            try:
                from minotaur_subnet.blockchain.chains import get_web3
                from minotaur_subnet.blockchain.token_approval import fee_mode_is_user
                if fee_mode_is_user(get_web3(req.chain_id), _deployed):
                    perp["fee_from_user"] = True
                    perp["required_fee_allowance_wei"] = str(platform_fee_int * n)
                    note += (
                        "This app collects the platform fee from your WETH, so also "
                        "approve required_fee_allowance_wei of fee_token (WETH) — a "
                        "plain on-chain approve(), as WETH has no EIP-2612 permit."
                    )
                else:
                    note += "This app settles the platform fee from its output/float (FeeMode.APP) — no fee-token approval needed."
            except Exception:
                note += "The platform fee is normally settled from the app's output/float — no fee-token approval needed."
        else:
            note += "The platform fee is normally settled from the app's output/float — no fee-token approval needed."
        perp["note"] = note
        response["perpetual"] = perp

    return response


class PreparePermitRequest(BaseModel):
    """Request the EIP-2612 permit digest for a standing-allowance approval."""
    token: str            # ERC-20 to approve (e.g. the perpetual's input or fee token)
    owner: str            # the user's wallet (permit signer)
    value: int            # allowance amount, raw units (e.g. required_*_allowance_wei)
    chain_id: int = 1
    deadline: int = 0     # 0 → default (now + 30 min)


@router.post("/apps/{app_id}/prepare-permit")
def prepare_permit(app_id: str, req: PreparePermitRequest) -> dict:
    """Build the EIP-2612 permit digest the user signs to set a standing allowance.

    Perpetual funding uses the user's wallet balance + a standing ERC-20 allowance
    (no prefund/escrow). Instead of an on-chain ``approve()``, the user can sign a
    gasless permit that the relayer submits to set the allowance (carried on the
    order as ``permit_value``/``permit_deadline``/``permit_v``/``permit_r``/
    ``permit_s``). This endpoint assembles the exact digest for
    ``(token, owner, spender=app contract, value)`` by reading the token's
    ``DOMAIN_SEPARATOR()`` and ``nonces(owner)`` on-chain, so the frontend just
    signs the returned digest and echoes the ``permit_*`` fields back.

    Typical flow: POST /apps/{id}/quote with ``perpetual=true`` → read
    ``perpetual.required_input_allowance_wei`` → call this for the input token
    with that value → sign ``digest`` → submit the order with the ``permit_*``
    params. The platform fee usually needs no approval (deducted from output);
    only a generic WETH-pulling app needs a WETH approve(), and WETH has no
    ERC-2612 permit, so this endpoint returns 400 for it.

    400 if the token doesn't implement ERC-2612 (approve() on-chain instead).
    """
    s = _app_store
    if s is None:
        from minotaur_subnet.api.server import store as _s
        s = _s
    if s is None:
        raise HTTPException(status_code=503, detail="App store not initialized")

    deployment = s.get_deployment(app_id, chain_id=req.chain_id)
    if deployment is None or not deployment.contract_address:
        raise HTTPException(
            status_code=404,
            detail=f"No operational deployment for {app_id} on chain {req.chain_id}",
        )
    spender = deployment.contract_address

    try:
        from minotaur_subnet.blockchain.chains import get_web3
        w3 = get_web3(req.chain_id)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"No web3 for chain {req.chain_id}: {exc}")

    if req.value <= 0:
        raise HTTPException(status_code=400, detail="value must be > 0")

    from minotaur_subnet.blockchain.token_approval import build_permit_digest
    result = build_permit_digest(
        w3, req.token, req.owner, spender, req.value,
        req.deadline if req.deadline > 0 else None,
    )
    if result is None:
        raise HTTPException(
            status_code=400,
            detail=(
                "Token does not support ERC-2612 permit — approve() the app "
                "contract on-chain for the required allowance instead."
            ),
        )

    return {
        "app_id": app_id,
        "chain_id": req.chain_id,
        "token": req.token,
        "owner": req.owner,
        "spender": spender,
        "value": str(req.value),
        "nonce": result["nonce"],
        "deadline": result["deadline"],
        "domain_separator": result["domain_separator"],
        # The final EIP-712 digest (0x1901 prefix already applied). Sign this raw
        # 32-byte hash with the owner wallet; split the 65-byte signature into
        # v/r/s. NB: sign the raw hash, NOT a personal_sign/EIP-191 message.
        "digest": result["digest"],
        # Echo these on the order params after signing (v/r/s from the signature):
        "order_params": {
            "permit_value": str(req.value),
            "permit_deadline": result["deadline"],
        },
        "note": (
            "Sign `digest` (raw 32-byte hash) with the owner wallet, split the "
            "signature into permit_v/permit_r/permit_s, and include those plus "
            "permit_value/permit_deadline in the order params so the relayer sets "
            "the allowance gaslessly. Repeat per token (input + fee)."
        ),
    }


@router.get("/orders/{order_id}/signing-payload")
def get_order_signing_payload(order_id: str) -> dict:
    """Return the EIP-712 payload the user signs to authorize this order.

    ``order_id`` is minted server-side at submit and the signed digest depends on
    it, so signing happens AFTER creation:
    submit → GET signing-payload → sign → PATCH /signature.

    Returns the full typed data (``domain``/``types``/``message`` for
    ``eth_signTypedData_v4``) AND the final ``digest`` (for raw-hash signing),
    built from the SAME fields the server verifies — so the resulting signature
    always passes PATCH /signature. Works for one-shot and perpetual orders alike;
    for a perpetual, ``message`` carries the perpetual/maxExecutions/cooldown terms
    and the uint256-max sentinel ``nonce`` a perpetual must sign with (so a client
    can't accidentally sign a concrete nonce and fail at settlement).
    """
    ob = _require_orderbook()
    order = ob.get(order_id)
    if order is None:
        raise HTTPException(status_code=404, detail=f"Order not found: {order_id}")
    if not order.params.get("app_address"):
        raise HTTPException(
            status_code=409,
            detail=(
                "Order has no app_address yet — its signing payload isn't ready. "
                "Submit through the leader so intent params/app address are resolved."
            ),
        )
    from minotaur_subnet.api.routes._signature_verify import build_order_signing_payload
    payload = build_order_signing_payload(order)
    return {
        "order_id": order_id,
        "submitted_by": order.submitted_by,
        "perpetual": bool(order.perpetual),
        **payload,
        "note": (
            "Sign with eth_signTypedData_v4(domain, types, message) OR sign the raw "
            "32-byte `digest`, then PATCH /orders/{id}/signature with "
            "{user_signature}. Do NOT change message.nonce — perpetuals use the "
            "uint256-max sentinel shown."
        ),
    }


@router.get("/orders/{order_id}/bridge")
def get_bridge_status(order_id: str) -> dict:
    """Get bridge transfer status for a cross-chain order.

    Returns bridge lifecycle data including protocol, chains, and
    current tracking status. Only meaningful for orders in BRIDGING state.
    """
    ob = _require_orderbook()
    order = ob.get(order_id)
    if order is None:
        raise HTTPException(status_code=404, detail=f"Order not found: {order_id}")

    plan = order.plan or {}
    meta = plan.get("metadata", {}) if isinstance(plan, dict) else {}

    if not meta.get("cross_chain"):
        return {
            "order_id": order_id,
            "cross_chain": False,
            "message": "Not a cross-chain order",
        }

    result: dict[str, Any] = {
        "order_id": order_id,
        "cross_chain": True,
        "status": order.status.value,
        "src_chain_id": meta.get("src_chain_id"),
        "dst_chain_id": meta.get("dst_chain_id"),
        "bridge_protocol": meta.get("bridge_protocol"),
        "tx_hash": order.tx_hash,
    }

    # If bridge tracker is available, get tracking info
    if _block_loop is not None and hasattr(_block_loop, "bridge_tracker"):
        tracker = _block_loop.bridge_tracker
        if tracker is not None:
            info = tracker.get_tracking_info(order_id)
            if info:
                result["bridge_tracking"] = info

    return result


@router.get("/blockloop/status")
def blockloop_status() -> dict:
    """Get block loop tick statistics."""
    if _block_loop is None:
        result = {"running": False, "message": "Block loop not initialized"}
    else:
        result = _block_loop.status()
    # `orderbook_stats` from the block loop counts only the in-memory OrderBook
    # working set, so the "total" undercounts historical orders after a restart
    # (and disagrees with the now-durable /orders list). Source it from the
    # store instead. Defensive: a store hiccup must never 500 the status probe.
    if _app_store is not None and hasattr(_app_store, "count_orders_by_status"):
        try:
            result["orderbook_stats"] = _app_store.count_orders_by_status()
        except Exception:
            pass
    return result


class PrepareOrderRequest(BaseModel):
    """Request to resolve all parameters for an order before quoting/submitting."""
    params: dict[str, Any] = {}
    submitted_by: str = ""
    intent_function: str = "execute"
    chain_id: int = 1


@router.post("/apps/{app_id}/prepare")
def prepare_order(app_id: str, req: PrepareOrderRequest) -> dict:
    """Resolve token symbols, chain_id, intent_function, and nonce in one call.

    This is the first step in the recommended agent flow:
    prepare → quote → submit. Each step auto-resolves as much as possible.
    """
    s = _app_store
    if s is None:
        from minotaur_subnet.api.server import store as _s
        s = _s
    if s is None:
        raise HTTPException(status_code=503, detail="App store not initialized")

    app_def = s.get_app(app_id)
    if app_def is None:
        raise HTTPException(status_code=404, detail=f"App not found: {app_id}")

    resolved_chain_id = req.chain_id
    resolved_intent_function = req.intent_function

    # Resolve token symbols → addresses
    resolved_params = _resolve_token_params(req.params, resolved_chain_id)

    # Auto-detect chain_id from deployment
    deployment = s.get_deployment(app_id, chain_id=resolved_chain_id)
    if deployment is None or not deployment.status.is_operational():
        fallback = s.get_deployment(app_id, chain_id=None)
        if fallback and fallback.status.is_operational():
            resolved_chain_id = fallback.chain_id
            deployment = fallback
            # Re-resolve tokens with correct chain
            resolved_params = _resolve_token_params(req.params, resolved_chain_id)

    # Auto-detect intent_function from manifest
    if _js_engine is not None and resolved_intent_function == "execute":
        manifest = _js_engine.get_manifest(app_id) if hasattr(_js_engine, "get_manifest") else None
        if manifest and "intent_functions" in manifest:
            names = {
                (f.get("name") if isinstance(f, dict) else f)
                for f in manifest["intent_functions"]
            }
            if "execute" not in names and len(names) == 1:
                resolved_intent_function = next(iter(names))

    # Auto-fetch nonce for managed wallets
    resolved_nonce = None
    if req.submitted_by and deployment and deployment.contract_address:
        from minotaur_subnet.shared.interop_address import parse_address
        try:
            ia = parse_address(req.submitted_by, default_chain_id=resolved_chain_id)
            wallet = s.get_wallet(ia.address)
            if wallet is None:
                for w in s.list_wallets():
                    if w.address.lower() == ia.address.lower():
                        wallet = w
                        break
            if wallet is not None:
                nonce = _fetch_user_nonce(deployment.contract_address, ia.address, resolved_chain_id)
                if nonce is not None:
                    resolved_nonce = nonce
                    resolved_params["user_nonce"] = nonce
        except ValueError:
            pass

    next_steps = [
        f"get_quote(app_id='{app_id}', params=<resolved_params>, "
        f"intent_function='{resolved_intent_function}', chain_id={resolved_chain_id})",
        "submit_order(app_id=..., params=quote.ready_params, submitted_by=..., "
        f"intent_function='{resolved_intent_function}', chain_id={resolved_chain_id})",
    ]

    return {
        "app_id": app_id,
        "chain_id": resolved_chain_id,
        "intent_function": resolved_intent_function,
        "resolved_params": resolved_params,
        "user_nonce": resolved_nonce,
        "contract_address": deployment.contract_address if deployment else None,
        "app_status": deployment.status.value if deployment else "not deployed",
        "next_steps": next_steps,
    }
