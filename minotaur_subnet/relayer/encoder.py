"""ABI encoding helpers for the EVM relayer.

Converts Python Order/ExecutionPlan types into Solidity-compatible tuples
for calling AppIntentBase.executeIntent().
"""

from __future__ import annotations

from typing import Any

from eth_hash.auto import keccak
from eth_utils import to_checksum_address

from minotaur_subnet.shared.types import ExecutionPlan, Interaction
from minotaur_subnet.consensus.eip712 import hash_plan_eip712


def _safe_checksum(addr: str | None, default: str = "0x" + "00" * 20) -> str:
    """Normalize an EVM address to checksummed form, tolerating missing values.

    web3.py's contract-call layer is strict about checksummed addresses —
    passing a lowercase or mixed-case hex string raises
    ``Web3.to_checksum_address`` complaints at submit time. We hit this
    live on 2026-05-27: the api was storing ``app_address`` lowercase in
    ``order.params``, the order passed consensus cleanly, then crashed
    at relayer submit on the very first contract call.

    Normalizing here at the encode boundary means downstream code can
    keep using the address as a plain string without each call site
    needing its own checksum guard.

    Empty / falsy input returns the zero-address default rather than
    raising — same posture as the upstream ``.get(..., default)`` pattern.
    """
    if not addr:
        return default
    try:
        # Lowercase FIRST, then checksum. A mixed-case address whose EIP-55
        # checksum is WRONG (but whose 20 hex bytes are perfectly valid) makes
        # web3's strict ``to_checksum_address`` RAISE rather than normalize — it
        # only auto-normalizes all-lower / all-upper input. Solver-produced plan
        # targets routinely arrive mis-cased this way. Without the ``.lower()``
        # this helper caught that raise and returned the bad address UNCHANGED,
        # so the very submit it exists to protect still failed downstream at
        # web3's checksum validation (live: 91 orders rejected on chain 8453
        # with "invalid EIP-55 checksum"). Discarding the bad case bits lets
        # to_checksum_address recompute the canonical checksum; the address
        # encodes to the same 20 bytes either way, so no signed hash changes.
        return to_checksum_address(addr.lower())
    except (ValueError, TypeError, AttributeError):
        # Genuinely malformed hex / wrong length — pass through unchanged so the
        # downstream web3 call raises with its native (more specific) error
        # rather than us swallowing the bad input.
        return addr


def encode_intent_order(order: Any) -> tuple:
    """Convert a Python Order into a Solidity IntentOrder tuple.

    Returns a tuple matching the IntentOrder struct:
        (orderId, app, intentSelector, intentParams, submittedBy,
         chainId, deadline, nonce, perpetual, maxExecutions, cooldown)

    Both ``app`` and ``submittedBy`` are normalized to checksummed form
    via ``_safe_checksum`` so downstream web3 calls don't reject on the
    strict-checksum check.
    """
    order_id_bytes = _str_to_bytes32(order.order_id)
    app_address = _safe_checksum(order.params.get("app_address"))
    intent_selector = bytes.fromhex(
        order.params.get("intent_selector", "00000000")
    )
    intent_params = bytes.fromhex(
        order.params.get("intent_params_hex", "")
    ) if order.params.get("intent_params_hex") else b""

    return (
        order_id_bytes,
        app_address,
        intent_selector,
        intent_params,
        _safe_checksum(order.submitted_by),
        order.chain_id,
        int(order.deadline) if order.deadline else 0,
        _resolve_nonce(order.params.get("user_nonce")),  # nonce (sentinel = skip check)
        order.perpetual,
        order.max_executions,
        int(order.cooldown),
    )


def encode_execution_plan(plan: ExecutionPlan) -> tuple:
    """Convert a Python ExecutionPlan into a Solidity ExecutionPlan tuple.

    Returns a tuple matching the ExecutionPlan struct:
        (calls[], deadline, nonce, metadata)
    """
    calls = []
    for ix in plan.interactions:
        call_data = bytes.fromhex(ix.call_data.replace("0x", "")) if ix.call_data != "0x" else b""
        calls.append((
            # Solver-produced targets arrive with arbitrary casing (seen live
            # 2026-07-07: a wrong-checksum mixed-case target failed every
            # submit for the order on chain 8453). The address encodes to the
            # same 20 bytes either way, so this never changes the signed plan
            # hash — it only satisfies web3's strict checksum validation.
            _safe_checksum(ix.target),
            int(ix.value),
            call_data,
        ))

    metadata = b""
    if plan.metadata:
        import json
        metadata = json.dumps(plan.metadata).encode() if isinstance(plan.metadata, dict) else plan.metadata

    return (
        calls,
        plan.deadline,
        plan.nonce,
        metadata,
    )


_EXECUTE_INTENT_SIG = (
    "executeIntent("
    "(bytes32,address,bytes4,bytes,address,uint256,uint256,uint256,bool,uint256,uint256),"
    "((address,uint256,bytes)[],uint256,uint256,bytes),"
    "bytes,bytes[])"
)
_EXECUTE_INTENT_SELECTOR = keccak(_EXECUTE_INTENT_SIG.encode())[:4]


def encode_execute_intent_calldata(
    order: Any,
    plan: ExecutionPlan,
    user_sig: bytes,
    validator_sigs: list[bytes],
) -> str:
    """Encode the full executeIntent() calldata as a 0x-prefixed hex string.

    This produces the raw bytes that can be sent as tx.data to the
    AppIntentBase contract. Used by the user-direct-submit path where
    the frontend sends the TX itself (for native ETH input) instead of
    routing through the relayer.
    """
    from eth_abi import encode

    order_tuple = encode_intent_order(order)
    plan_tuple = encode_execution_plan(plan)

    encoded_args = encode(
        [
            "(bytes32,address,bytes4,bytes,address,uint256,uint256,uint256,bool,uint256,uint256)",
            "((address,uint256,bytes)[],uint256,uint256,bytes)",
            "bytes",
            "bytes[]",
        ],
        [order_tuple, plan_tuple, user_sig, validator_sigs],
    )
    return "0x" + (_EXECUTE_INTENT_SELECTOR + encoded_args).hex()


def hash_execution_plan(plan: ExecutionPlan) -> str:
    """Compute a deterministic hash of an execution plan.

    Matches EIP712Verifier.hashPlan() on-chain exactly.
    """
    calls = []
    for ix in plan.interactions:
        call_data = bytes.fromhex(ix.call_data.replace("0x", "")) if ix.call_data != "0x" else b""
        # Mirror encode_execution_plan: normalize the (possibly mis-cased)
        # solver target so this hash matches the one built from the encoded
        # plan. Address → 20 bytes is case-agnostic, so this never changes the
        # resulting hash vs a correctly-cased target.
        calls.append((_safe_checksum(ix.target), int(ix.value), call_data))

    metadata = b""
    if plan.metadata:
        import json
        metadata = json.dumps(plan.metadata).encode() if isinstance(plan.metadata, dict) else plan.metadata

    return "0x" + hash_plan_eip712(calls, plan.deadline, plan.nonce, metadata).hex()


_SENTINEL_NONCE = 2**256 - 1  # type(uint256).max — skips nonce verification


def _resolve_nonce(raw_nonce: Any) -> int:
    """Resolve nonce value, defaulting to sentinel for concurrent orders."""
    if raw_nonce is None:
        return _SENTINEL_NONCE
    if isinstance(raw_nonce, str):
        if raw_nonce.startswith("0x"):
            return int(raw_nonce, 16)
        return int(raw_nonce) if raw_nonce else _SENTINEL_NONCE
    return int(raw_nonce)


def encode_bridge_intent_params_hex(
    token_in: str,
    amount_in: int,
    min_bridged: int,
    receiver: str,
    platform_fee_wei: int = 0,
) -> str:
    """ABI-encode bridge intent params for the DexAggregatorApp._bridge function.

    Encoding: abi.encode(address tokenIn, uint256 amountIn, uint256 minBridged,
                         address receiver, uint256 platformFeeWei)
    Returns hex string without 0x prefix.
    """
    from eth_abi import encode as abi_encode
    from web3 import Web3

    encoded = abi_encode(
        ["address", "uint256", "uint256", "address", "uint256"],
        [
            Web3.to_checksum_address(token_in),
            amount_in,
            min_bridged,
            Web3.to_checksum_address(receiver),
            platform_fee_wei,
        ],
    )
    return encoded.hex()


def _str_to_bytes32(s: str) -> bytes:
    """Convert a string to bytes32 (keccak256 hash)."""
    return keccak(s.encode())
