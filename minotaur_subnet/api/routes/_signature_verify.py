"""EIP-712 user order signature verification for the REST API (WAL-1, WAL-6).

Recovers the signer from the signature and checks it matches ``order.submitted_by``.
Signature is optional — empty string means no validation (backward compat).

The EIP-712 structure matches the on-chain EIP712Verifier.sol exactly:
- Domain: MinotaurAppIntent v1, chainId, verifyingContract (app contract)
- Type: IntentOrder with paramsHash = keccak256(ABI-encoded intentParams)
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from minotaur_subnet.orderbook.orderbook import Order

logger = logging.getLogger(__name__)


def build_order_signing_payload(order: "Order") -> dict:
    """Build the EIP-712 payload a client signs to authorize an order.

    Returns the domain, types, message and the final ``0x1901`` digest — all a
    frontend needs to call ``eth_signTypedData_v4`` OR sign the raw digest —
    derived from the SAME fields :func:`verify_user_order_signature` checks, so a
    signature produced from this payload always verifies (single source of truth).

    Because ``order_id`` is minted server-side at submit and the digest depends on
    it, this must be built AFTER the order exists. Includes the perpetual fields
    (``perpetual``/``maxExecutions``/``cooldown``) and the resolved nonce — the
    uint256-max sentinel when the order pins none, as perpetuals do — so the
    client can't accidentally sign with the wrong nonce. uint256 values are
    returned as decimal strings (the sentinel exceeds JS safe-integer range).
    """
    from eth_abi import encode as abi_encode
    from eth_hash.auto import keccak
    # Resolve nonce via the same helper the relayer uses so the reconstruction
    # lines up with whatever the relayer encodes into calldata at executeIntent
    # time. Default is the uint256-max sentinel (skip the on-chain nonce check),
    # which is exactly what a perpetual signs with.
    from minotaur_subnet.relayer.encoder import _resolve_nonce

    app_address = order.params.get("app_address", "0x" + "00" * 20)
    intent_params_hex = order.params.get("intent_params_hex", "")
    intent_selector_hex = order.params.get("intent_selector", "")

    # orderId: keccak256 of the string order_id (matches frontend)
    order_id_bytes = keccak(order.order_id.encode())

    # intentSelector: 4-byte selector
    if intent_selector_hex:
        intent_selector = bytes.fromhex(intent_selector_hex.replace("0x", ""))[:4]
    else:
        intent_selector = keccak(b"swap(address,address,uint256,uint256,address)")[:4]

    # paramsHash: keccak256 of ABI-encoded intent params
    intent_params_bytes = (
        bytes.fromhex(intent_params_hex.replace("0x", "")) if intent_params_hex else b""
    )
    params_hash = keccak(intent_params_bytes)

    nonce = _resolve_nonce(order.params.get("user_nonce"))

    # EIP-712 type hash (matches EIP712Verifier.sol)
    INTENT_ORDER_TYPEHASH = keccak(
        b"IntentOrder(bytes32 orderId,address app,bytes4 intentSelector,"
        b"bytes32 paramsHash,address submittedBy,uint256 chainId,"
        b"uint256 deadline,uint256 nonce,bool perpetual,"
        b"uint256 maxExecutions,uint256 cooldown)"
    )
    struct_hash = keccak(abi_encode(
        ["bytes32", "bytes32", "address", "bytes4", "bytes32",
         "address", "uint256", "uint256", "uint256", "bool",
         "uint256", "uint256"],
        [INTENT_ORDER_TYPEHASH, order_id_bytes, app_address,
         intent_selector, params_hash, order.submitted_by,
         order.chain_id, int(order.deadline), nonce,
         order.perpetual, order.max_executions, int(order.cooldown)],
    ))

    # Domain separator (matches contract constructor)
    DOMAIN_TYPEHASH = keccak(
        b"EIP712Domain(string name,string version,uint256 chainId,"
        b"address verifyingContract)"
    )
    domain_sep = keccak(abi_encode(
        ["bytes32", "bytes32", "bytes32", "uint256", "address"],
        [DOMAIN_TYPEHASH, keccak(b"MinotaurAppIntent"),
         keccak(b"1"), order.chain_id, app_address],
    ))
    digest = keccak(b"\x19\x01" + domain_sep + struct_hash)

    return {
        "digest": "0x" + digest.hex(),
        "domain_separator": "0x" + domain_sep.hex(),
        "struct_hash": "0x" + struct_hash.hex(),
        "primaryType": "IntentOrder",
        "domain": {
            "name": "MinotaurAppIntent",
            "version": "1",
            "chainId": order.chain_id,
            "verifyingContract": app_address,
        },
        "types": {
            "EIP712Domain": [
                {"name": "name", "type": "string"},
                {"name": "version", "type": "string"},
                {"name": "chainId", "type": "uint256"},
                {"name": "verifyingContract", "type": "address"},
            ],
            "IntentOrder": [
                {"name": "orderId", "type": "bytes32"},
                {"name": "app", "type": "address"},
                {"name": "intentSelector", "type": "bytes4"},
                {"name": "paramsHash", "type": "bytes32"},
                {"name": "submittedBy", "type": "address"},
                {"name": "chainId", "type": "uint256"},
                {"name": "deadline", "type": "uint256"},
                {"name": "nonce", "type": "uint256"},
                {"name": "perpetual", "type": "bool"},
                {"name": "maxExecutions", "type": "uint256"},
                {"name": "cooldown", "type": "uint256"},
            ],
        },
        "message": {
            "orderId": "0x" + order_id_bytes.hex(),
            "app": app_address,
            "intentSelector": "0x" + intent_selector.hex(),
            "paramsHash": "0x" + params_hash.hex(),
            "submittedBy": order.submitted_by,
            "chainId": str(order.chain_id),
            "deadline": str(int(order.deadline)),
            "nonce": str(nonce),
            "perpetual": bool(order.perpetual),
            "maxExecutions": str(order.max_executions),
            "cooldown": str(int(order.cooldown)),
        },
    }


def verify_user_order_signature(order: "Order", signature_hex: str) -> bool:
    """Verify an EIP-712 user order signature.

    Returns True if the recovered signer matches ``order.submitted_by``.
    Returns False on mismatch or any crypto error. The digest is built by
    :func:`build_order_signing_payload`, the SAME payload handed to the client to
    sign, so a signature over that payload always verifies here.
    """
    if not signature_hex:
        return True  # No signature = no validation

    try:
        from eth_account import Account

        digest = bytes.fromhex(build_order_signing_payload(order)["digest"][2:])
        sig_bytes = bytes.fromhex(
            signature_hex[2:] if signature_hex.startswith("0x") else signature_hex
        )
        recovered = Account._recover_hash(digest, signature=sig_bytes)
        ok = recovered.lower() == order.submitted_by.lower()
        if not ok:
            logger.warning(
                "EIP-712 signature mismatch: recovered=%s expected=%s",
                recovered, order.submitted_by,
            )
        return ok

    except Exception as exc:
        logger.warning("Signature verification failed: %s", exc)
        return False
