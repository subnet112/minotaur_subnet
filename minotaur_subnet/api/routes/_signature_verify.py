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


def verify_user_order_signature(order: "Order", signature_hex: str) -> bool:
    """Verify an EIP-712 user order signature.

    Returns True if the recovered signer matches ``order.submitted_by``.
    Returns False on mismatch or any crypto error.
    """
    if not signature_hex:
        return True  # No signature = no validation

    try:
        from eth_account import Account
        from eth_abi import encode as abi_encode
        from eth_hash.auto import keccak

        # Get fields from order params
        app_address = order.params.get("app_address", "0x" + "00" * 20)
        intent_params_hex = order.params.get("intent_params_hex", "")
        intent_selector_hex = order.params.get("intent_selector", "")
        user_nonce = order.params.get("user_nonce", 0)

        # orderId: keccak256 of the string order_id (matches frontend)
        order_id_bytes = keccak(order.order_id.encode())

        # intentSelector: 4-byte selector
        if intent_selector_hex:
            intent_selector = bytes.fromhex(
                intent_selector_hex.replace("0x", "")
            )[:4]
        else:
            intent_selector = keccak(
                b"swap(address,address,uint256,uint256,address)"
            )[:4]

        # paramsHash: keccak256 of ABI-encoded intent params
        if intent_params_hex:
            intent_params_bytes = bytes.fromhex(
                intent_params_hex.replace("0x", "")
            )
        else:
            intent_params_bytes = b""
        params_hash = keccak(intent_params_bytes)

        # Nonce: sentinel value or integer
        if isinstance(user_nonce, str) and user_nonce.startswith("0x"):
            nonce = int(user_nonce, 16)
        else:
            nonce = int(user_nonce or 0)

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

        # EIP-712 digest
        digest = keccak(b"\x19\x01" + domain_sep + struct_hash)

        # Recover signer
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
