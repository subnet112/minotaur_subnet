"""Order preparation, quoting, and token approval service functions."""

from __future__ import annotations

import os
from typing import Any

from minotaur_subnet.shared.interop_address import parse_address
from minotaur_subnet.store import AppIntentStore

import logging

logger = logging.getLogger(__name__)


async def dry_run_order(
    store: AppIntentStore,
    orderbook: Any,
    js_engine: Any,
    order_id: str,
    interactions: list[dict[str, Any]],
    deadline: int = 0,
    nonce: int = 0,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Score an execution plan against an order without side effects.

    Miners use this to test their plans before submitting solver code.

    Args:
        store:        App intent store.
        orderbook:    The Intent OrderBook.
        js_engine:    JS scoring engine.
        order_id:     The order to score against.
        interactions: List of interaction dicts [{target, value, call_data, chain_id}].
        deadline:     Plan deadline (unix timestamp, 0 = no deadline).
        nonce:        Plan nonce.
        metadata:     Plan metadata dict.

    Returns:
        Dict with score, valid, reason, breakdown.
    """
    if orderbook is None:
        return {"error": "OrderBook not initialized"}

    order = orderbook.get(order_id)
    if order is None:
        return {"error": f"Order not found: {order_id}"}

    app = store.get_app(order.app_id)
    if app is None:
        return {"error": f"App not found: {order.app_id}"}

    from minotaur_subnet.shared.types import ExecutionPlan
    from minotaur_subnet.shared.builders import build_intent_state, parse_interactions
    from minotaur_subnet.shared.simulation import build_mock_simulation

    plan = ExecutionPlan(
        intent_id=order.app_id,
        interactions=parse_interactions(interactions, default_chain_id=order.chain_id),
        deadline=deadline,
        nonce=nonce,
        metadata=metadata or {},
    )

    state = build_intent_state(
        chain_id=order.chain_id,
        owner=order.submitted_by,
        params=order.params,
        intent_function=None,
    )

    simulation = build_mock_simulation(plan, order.params)

    if js_engine is not None:
        try:
            if order.app_id not in js_engine._intents:
                await js_engine.load_intent(order.app_id, app.js_code)
            score_result = await js_engine.score(
                order.app_id, plan, simulation, state,
            )
            return {
                "order_id": order_id,
                "score": score_result.score,
                "valid": score_result.valid,
                "reason": score_result.reason,
                "breakdown": score_result.breakdown,
            }
        except Exception as exc:
            return {"error": f"JS scoring failed: {exc}"}

    # Fallback: mock scoring
    from minotaur_subnet.shared.simulation import compute_mock_score
    mock_score = compute_mock_score(plan, order.params)
    return {
        "order_id": order_id,
        "score": mock_score,
        "valid": True,
        "reason": "mock scoring (no JS engine)",
        "breakdown": {"base": mock_score},
    }


def ensure_token_approval(
    store: AppIntentStore,
    submitted_by: str,
    app_id: str,
    chain_id: int,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Auto-approve token spending for managed wallets before order submission.

    Tries ERC-2612 permit first (gasless), falls back to on-chain approve().
    Returns permit params to merge into the order, or empty dict.

    This is a no-op for non-managed wallets (users handle their own approvals).
    """
    from minotaur_subnet.blockchain.token_approval import (
        check_allowance,
        try_erc2612_permit,
        send_approve_tx,
    )

    # 1. Is this a managed wallet?
    wallet = store.get_wallet(submitted_by)
    if wallet is None:
        # Also try case-insensitive lookup
        for w in store.list_wallets():
            if w.address.lower() == submitted_by.lower():
                wallet = w
                break
    if wallet is None:
        return {}  # Not a managed wallet -- user handles approval

    # 2. Identify the token + amount the app pulls from the user (shared,
    #    app-agnostic convention — the single source of truth).
    from minotaur_subnet.blockchain.tokens import resolve_spend_token_amount
    input_token, amount_int = resolve_spend_token_amount(params)
    if not input_token or not amount_int:
        return {}

    # 3. Get the contract address (= spender) from deployment
    deployment = store.get_deployment(app_id, chain_id=chain_id)
    if deployment is None or not deployment.contract_address:
        logger.debug("No deployment for %s on chain %d -- skipping approval", app_id, chain_id)
        return {}
    spender = deployment.contract_address

    # 4. Get Web3 instance
    try:
        from minotaur_subnet.blockchain.chains import get_web3
        w3 = get_web3(chain_id)
    except (ValueError, Exception) as exc:
        logger.warning("Cannot get Web3 for chain %d: %s", chain_id, exc)
        return {}

    # 5. Check current allowance -- skip if sufficient
    current = check_allowance(w3, input_token, submitted_by, spender)
    if current >= amount_int:
        logger.debug("Allowance already sufficient: %d >= %d", current, amount_int)
        return {}

    # 6. Lit-bridge URL
    bridge_url = os.environ.get("LIT_BRIDGE_URL", "http://localhost:3100")

    # 7. On-chain approve() -- AppIntentBase contracts use safeTransferFrom
    #    which requires a real on-chain approval, not just ERC-2612 permit
    #    params. Skip the gasless permit path entirely.
    try:
        tx_hash = send_approve_tx(
            w3, bridge_url, input_token, submitted_by, spender, amount_int, chain_id,
        )
        logger.info("On-chain approve sent: tx=%s", tx_hash[:16])
        # Wait for the approve tx to be mined before returning.
        # Without this, the relayer may try executeIntent before
        # the allowance is set, causing gas estimation to fail.
        try:
            w3.eth.wait_for_transaction_receipt(
                bytes.fromhex(tx_hash.replace("0x", "")), timeout=15,
            )
        except Exception:
            pass  # Best effort -- approval may still land in time
        return {}  # Approval is on-chain, no permit params needed
    except Exception as exc:
        logger.warning("Auto-approval failed for %s: %s", submitted_by, exc)
        return {}  # Don't block order submission

    # 8. Also approve WETH/WTAO for platform fee (if non-zero)
    platform_fee = int(params.get("platform_fee_wei", 0))
    if platform_fee > 0:
        try:
            from minotaur_subnet.blockchain.tokens import WRAPPED_NATIVE_TOKEN
            weth_addr = WRAPPED_NATIVE_TOKEN.get(chain_id)
            if weth_addr and weth_addr.lower() != input_token.lower():
                weth_allowance = check_allowance(w3, weth_addr, submitted_by, spender)
                if weth_allowance < platform_fee:
                    tx_hash = send_approve_tx(
                        w3, bridge_url, weth_addr, submitted_by, spender, platform_fee, chain_id,
                    )
                    logger.info("WETH platform fee approve sent: tx=%s", tx_hash[:16])
                    try:
                        w3.eth.wait_for_transaction_receipt(
                            bytes.fromhex(tx_hash.replace("0x", "")), timeout=15,
                        )
                    except Exception:
                        pass
        except Exception as exc:
            logger.warning("WETH approval for platform fee failed: %s", exc)

    return {}



def sign_user_order_for_managed_wallet(
    store: AppIntentStore,
    order: Any,
    app_address: str,
    chain_id: int,
) -> str | None:
    """Auto-sign an EIP-712 IntentOrder for a managed wallet.

    Builds the same digest that the on-chain EIP712Verifier.hashOrder() computes,
    then signs via the lit-bridge /sign/hash endpoint.

    Returns hex signature string (with 0x prefix), or None if:
    - Not a managed wallet
    - Missing required order fields
    - Signing fails

    This is a no-op for non-managed wallets.
    """
    submitted_by = order.submitted_by

    # 1. Is this a managed wallet?
    wallet = store.get_wallet(submitted_by)
    if wallet is None:
        for w in store.list_wallets():
            if w.address.lower() == submitted_by.lower():
                wallet = w
                break
    if wallet is None:
        return None

    # 2. Gather order fields matching the encoder's output
    intent_selector_hex = order.params.get("intent_selector", "00000000")
    intent_params_hex = order.params.get("intent_params_hex", "")
    if not intent_params_hex:
        logger.debug("No intent_params_hex -- cannot sign order")
        return None

    try:
        from eth_hash.auto import keccak as _keccak
        from minotaur_subnet.consensus.eip712 import (
            hash_order_struct,
            build_domain_separator,
            _to_typed_data_hash,
        )

        order_id_bytes = _keccak(order.order_id.encode())
        selector_bytes = bytes.fromhex(intent_selector_hex.replace("0x", ""))
        intent_params = bytes.fromhex(intent_params_hex.replace("0x", ""))

        domain_separator = build_domain_separator(chain_id, app_address)

        struct_hash = hash_order_struct(
            order_id=order_id_bytes,
            app=app_address,
            intent_selector=selector_bytes,
            intent_params=intent_params,
            submitted_by=submitted_by,
            chain_id=chain_id,
            deadline=int(order.deadline),
            nonce=int(order.params.get("user_nonce", 0)),
            perpetual=order.perpetual,
            max_executions=order.max_executions,
            cooldown=int(order.cooldown),
        )

        digest = _to_typed_data_hash(domain_separator, struct_hash)

        # 3. Sign via lit-bridge /sign/hash
        bridge_url = os.environ.get("LIT_BRIDGE_URL", "")
        if not bridge_url:
            logger.debug("No LIT_BRIDGE_URL -- cannot auto-sign order")
            return None

        import httpx
        resp = httpx.post(
            f"{bridge_url}/sign/hash",
            json={"address": submitted_by, "hash_hex": digest.hex()},
            timeout=10.0,
        )
        resp.raise_for_status()
        sig = resp.json()["signature"]
        logger.info(
            "Auto-signed EIP-712 order %s for managed wallet %s",
            order.order_id[:16], submitted_by[:10],
        )
        return sig

    except Exception as exc:
        logger.warning("Failed to auto-sign order for %s: %s", submitted_by, exc)
        return None


def _extract_manifest_safely(js_code: str) -> dict[str, Any] | None:
    """Extract a JS manifest using the sandboxed validation path."""
    import asyncio
    import concurrent.futures
    from minotaur_subnet.engine.validation import validate_js_code

    try:
        try:
            asyncio.get_running_loop()
            with concurrent.futures.ThreadPoolExecutor() as pool:
                result = pool.submit(
                    lambda: asyncio.run(validate_js_code(js_code))
                ).result()
        except RuntimeError:
            result = asyncio.run(validate_js_code(js_code))
    except Exception as exc:
        logger.warning("Sandboxed JS manifest extraction failed: %s", exc)
        return None

    return result.js_manifest if result.valid else None


def compute_intent_selector(
    store: AppIntentStore,
    js_engine: Any,
    app_id: str,
    intent_function: str,
) -> str | None:
    """Compute the 4-byte Solidity intent selector for on-chain execution.

    Tries the JS engine manifest cache first, then extracts from the app's
    JS code via the existing JS sandbox/validation path. Returns hex string
    (no 0x prefix) or None.
    """
    from minotaur_subnet.v3.manifest import (
        compute_selector_from_manifest,
        manifest_from_legacy_dict,
    )

    manifest = None

    # 1. Try JS engine cache
    if js_engine is not None and hasattr(js_engine, "get_manifest"):
        try:
            manifest = js_engine.get_manifest(app_id)
        except Exception:
            pass

    # 2. Fallback: extract manifest from app JS using the sandboxed validator.
    if manifest is None:
        app_def = store.get_app(app_id)
        if app_def and app_def.js_code:
            manifest = _extract_manifest_safely(app_def.js_code)

    if not manifest or "intent_functions" not in manifest:
        return None
    typed_manifest = manifest_from_legacy_dict(manifest)
    return compute_selector_from_manifest(typed_manifest, intent_function)
