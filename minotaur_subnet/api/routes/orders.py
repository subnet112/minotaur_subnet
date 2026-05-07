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

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

router = APIRouter()

# Module-level references set by server.py at startup
_orderbook = None
_block_loop = None
_app_store = None
_js_engine = None


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


def _require_orderbook():
    if _orderbook is None:
        raise HTTPException(
            status_code=503,
            detail="OrderBook not initialized",
        )
    return _orderbook


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


@router.post("/apps/{app_id}/orders", status_code=201)
def submit_order(app_id: str, req: SubmitOrderRequest) -> dict:
    """Submit a new order to the Intent OrderBook."""
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
        if "user_nonce" not in req.params and deployment and deployment.contract_address:
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

        # Build ABI-encoded intentParams for on-chain execution.
        # Swap intents use the fixed 11-field layout that DexAggregator._swap
        # expects; the generic manifest encoder only emits fields the caller
        # sent, which silently truncates the layout. Never fall back for swap.
        intent_hex = _svc.build_swap_intent_params_hex(req.params, ia.address)
        if not intent_hex and (req.intent_function or "").lower() == "swap":
            raise HTTPException(
                status_code=400,
                detail=(
                    "Swap intents require input_token, output_token, "
                    "input_amount, and min_output_amount. Ensure the frontend "
                    "passes min_output_amount derived from the quote."
                ),
            )
        if not intent_hex:
            # Generic manifest-based encoding for non-swap intents (DCA, yield, etc.)
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

    # Default deadline: 1 hour from now (on-chain rejects deadline=0)
    import time as _time
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

    Only valid for orders that were submitted with ``_user_submit = True``
    and are currently in APPROVED state — guards against a random client
    fabricating FILLED states for someone else's order.
    """
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
    """
    ob = _require_orderbook()
    order = ob.get(order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="Order not found")

    body = await request.json()
    sig = body.get("user_signature", "")
    if not sig:
        raise HTTPException(status_code=400, detail="user_signature required")

    ob.update_order(order_id, user_signature=sig)
    if _app_store is not None:
        try:
            _app_store.save_order(ob.get(order_id).to_dict())
        except Exception:
            pass

    return {"order_id": order_id, "signature_attached": True}


@router.get("/orders/{order_id}")
def get_order(order_id: str) -> dict:
    """Get the status of an order.

    Checks the in-memory OrderBook first, then falls back to the
    persistent store (for orders from before a restart).
    """
    ob = _require_orderbook()
    order = ob.get(order_id)
    if order is not None:
        return order.to_dict()
    # Fall back to persistent store
    stored = _app_store.get_order(order_id) if _app_store is not None and hasattr(_app_store, "get_order") else None
    if stored is not None:
        return stored
    raise HTTPException(status_code=404, detail=f"Order not found: {order_id}")


@router.get("/orders")
def list_orders(
    app_id: str | None = None,
    status: str | None = None,
) -> dict:
    """List orders with optional filters."""
    ob = _require_orderbook()
    orders = ob.list_orders(app_id=app_id, status=status)
    return {
        "orders": [o.to_dict() for o in orders],
        "count": len(orders),
    }


@router.delete("/orders/{order_id}")
def cancel_order(order_id: str, submitted_by: str = Query(...)) -> dict:
    """Cancel an open order.

    Args:
        order_id: The order to cancel.
        submitted_by: Wallet address of the order owner (required).
            Only the original submitter can cancel their own order.
    """
    ob = _require_orderbook()
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


@router.post("/orders/{order_id}/dry-run")
async def dry_run_order(order_id: str, req: DryRunRequest) -> dict:
    """Score a plan against an order without side effects."""
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
async def get_quote(app_id: str, req: QuoteRequest) -> dict:
    """Get an estimated quote for an intent without creating an order.

    Calls the solver's quote() method for fast, pure-math quoting from
    snapshot pool state. No simulation, no JS scoring, no order created.

    Returns:
        estimated_output: Best output amount the solver found (as string)
        suggested_min_output: estimated_output * (1 - slippage_bps/10000)
        route_summary: Human-readable description of the route
        gas_estimate: Estimated gas units
        valid_for_seconds: How long this quote is indicative (always 30)
    """
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

    # Solver builds its own data from RPC; no snapshot needed
    # Call solver.quote() — supports both sync (BaselineSwapSolver) and
    # async (DockerRuntimeSolver) solvers.
    try:
        import inspect
        quote_call = bl.solver.quote(app_def, state)
        if inspect.isawaitable(quote_call):
            quote_result = await quote_call
        else:
            quote_result = quote_call
    except NotImplementedError:
        if s is not None:
            s.record_quote_attempt(app_id, success=False, error="not_implemented")
        raise HTTPException(
            status_code=501,
            detail="Solver does not support quoting",
        )
    except ValueError as exc:
        if s is not None:
            s.record_quote_attempt(app_id, success=False, error=str(exc))
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        if s is not None:
            s.record_quote_attempt(app_id, success=False, error=f"error: {exc}")
        raise HTTPException(
            status_code=500,
            detail=f"Solver quote failed: {exc}",
        )

    # Legacy solver images may not implement the quote command — the Docker
    # session logs "Unknown command: quote" and returns None. Treat this the
    # same as NotImplementedError so the caller can fall back cleanly instead
    # of crashing on attribute access.
    if quote_result is None:
        if s is not None:
            s.record_quote_attempt(app_id, success=False, error="not_implemented")
        raise HTTPException(
            status_code=501,
            detail="Solver does not support quoting (legacy image — rebuild champion)",
        )

    estimated_output_gross = quote_result.estimated_output
    # Track successful quote (zero output counts as failure)
    if s is not None:
        _ok = estimated_output_gross != "0"
        s.record_quote_attempt(app_id, success=_ok, error="" if _ok else "zero_output")

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
    quote_values = {
        "estimated_output": estimated_output,
        "suggested_min_output": suggested_min_output,
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

    return response


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
        return {"running": False, "message": "Block loop not initialized"}
    return _block_loop.status()


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
