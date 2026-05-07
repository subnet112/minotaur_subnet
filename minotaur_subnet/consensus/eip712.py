"""EIP-712 typed-data signing utilities matching the Solidity EIP712Verifier.

Produces byte-identical hashes and signatures that the AppIntentBase contract
accepts on-chain. Uses eth_abi for ABI encoding and eth_hash for keccak256.

Key Solidity references:
  - EIP712Verifier.sol: INTENT_ORDER_TYPEHASH, PLAN_APPROVAL_TYPEHASH, hashPlan()
  - AppIntentBase.sol: DOMAIN_SEPARATOR construction (constructor line 57-63)
"""

from __future__ import annotations

from eth_abi import encode as abi_encode
from eth_account import Account
from eth_hash.auto import keccak


# ── EIP-712 type hashes (must match Solidity constants) ─────────────────────

INTENT_ORDER_TYPEHASH = keccak(
    b"IntentOrder(bytes32 orderId,address app,bytes4 intentSelector,"
    b"bytes32 paramsHash,address submittedBy,uint256 chainId,uint256 deadline,"
    b"uint256 nonce,bool perpetual,uint256 maxExecutions,uint256 cooldown)"
)

PLAN_APPROVAL_TYPEHASH = keccak(
    b"PlanApproval(bytes32 orderId,bytes32 planHash,uint256 score)"
)

EIP712_DOMAIN_TYPEHASH = keccak(
    b"EIP712Domain(string name,string version,uint256 chainId,"
    b"address verifyingContract)"
)


# ── Domain separator ────────────────────────────────────────────────────────


def build_domain_separator(
    chain_id: int,
    contract_address: str,
    name: str = "MinotaurAppIntent",
    version: str = "1",
) -> bytes:
    """Compute the EIP-712 domain separator matching AppIntentBase constructor.

    Solidity equivalent:
        keccak256(abi.encode(
            EIP712_DOMAIN_TYPEHASH,
            keccak256("MinotaurAppIntent"),
            keccak256("1"),
            block.chainid,
            address(this)
        ))
    """
    return keccak(abi_encode(
        ["bytes32", "bytes32", "bytes32", "uint256", "address"],
        [
            EIP712_DOMAIN_TYPEHASH,
            keccak(name.encode()),
            keccak(version.encode()),
            chain_id,
            contract_address,
        ],
    ))


# ── Plan hashing (matches EIP712Verifier.hashPlan) ─────────────────────────


def hash_plan_eip712(
    calls: list[tuple[str, int, bytes]],
    deadline: int,
    nonce: int,
    metadata: bytes = b"",
) -> bytes:
    """Hash an execution plan to match EIP712Verifier.hashPlan() exactly.

    Args:
        calls: List of (target_address, value, calldata_bytes) tuples.
        deadline: Plan deadline (uint256).
        nonce: Plan nonce (uint256).
        metadata: Raw metadata bytes.

    Returns:
        bytes32 plan hash.

    Solidity equivalent:
        for each call: keccak(abi.encode(target, value, keccak(callData)))
        keccak(abi.encode(
            keccak(abi.encodePacked(callHashes)),
            deadline, nonce, keccak(metadata)
        ))
    """
    call_hashes: list[bytes] = []
    for target, value, call_data in calls:
        h = keccak(abi_encode(
            ["address", "uint256", "bytes32"],
            [target, value, keccak(call_data)],
        ))
        call_hashes.append(h)

    # abi.encodePacked(bytes32[]) = concatenation of 32-byte values
    packed = b"".join(call_hashes)

    return keccak(abi_encode(
        ["bytes32", "uint256", "uint256", "bytes32"],
        [keccak(packed), deadline, nonce, keccak(metadata)],
    ))


# ── User order signing (EIP-712 IntentOrder) ───────────────────────────────


def hash_order_struct(
    order_id: bytes,
    app: str,
    intent_selector: bytes,
    intent_params: bytes,
    submitted_by: str,
    chain_id: int,
    deadline: int,
    nonce: int,
    perpetual: bool,
    max_executions: int,
    cooldown: int,
) -> bytes:
    """Compute the EIP-712 struct hash for an IntentOrder.

    Matches EIP712Verifier.hashOrder() structHash computation.
    Note: intentSelector is bytes4, padded to bytes32 in abi.encode.
    """
    # paramsHash = keccak256(intentParams) — matches Solidity line 32
    params_hash = keccak(intent_params)

    return keccak(abi_encode(
        [
            "bytes32",   # INTENT_ORDER_TYPEHASH
            "bytes32",   # orderId
            "address",   # app
            "bytes32",   # intentSelector (bytes4 padded right in abi.encode)
            "bytes32",   # paramsHash
            "address",   # submittedBy
            "uint256",   # chainId
            "uint256",   # deadline
            "uint256",   # nonce
            "bool",      # perpetual
            "uint256",   # maxExecutions
            "uint256",   # cooldown
        ],
        [
            INTENT_ORDER_TYPEHASH,
            order_id,
            app,
            intent_selector.ljust(32, b"\x00"),  # bytes4 → bytes32 right-padded
            params_hash,
            submitted_by,
            chain_id,
            deadline,
            nonce,
            perpetual,
            max_executions,
            cooldown,
        ],
    ))


def sign_user_order(
    private_key: str,
    order_id: bytes,
    app: str,
    intent_selector: bytes,
    intent_params: bytes,
    submitted_by: str,
    chain_id: int,
    deadline: int,
    nonce: int,
    perpetual: bool,
    max_executions: int,
    cooldown: int,
    domain_separator: bytes,
) -> bytes:
    """Sign an IntentOrder with EIP-712 typed data.

    Returns a 65-byte ECDSA signature (r, s, v).
    """
    struct_hash = hash_order_struct(
        order_id, app, intent_selector, intent_params, submitted_by,
        chain_id, deadline, nonce, perpetual, max_executions, cooldown,
    )
    digest = _to_typed_data_hash(domain_separator, struct_hash)
    signed = Account.unsafe_sign_hash(digest, private_key=private_key)
    return signed.signature


# ── Validator plan approval signing (EIP-712 PlanApproval) ──────────────────


def hash_plan_approval_struct(
    order_id: bytes,
    plan_hash: bytes,
    score_bps: int,
) -> bytes:
    """Compute the EIP-712 struct hash for a PlanApproval.

    Matches EIP712Verifier.hashPlanApproval() structHash computation.
    """
    return keccak(abi_encode(
        ["bytes32", "bytes32", "bytes32", "uint256"],
        [PLAN_APPROVAL_TYPEHASH, order_id, plan_hash, score_bps],
    ))


def sign_plan_approval_eip712(
    private_key: str,
    order_id: bytes,
    plan_hash: bytes,
    score_bps: int,
    domain_separator: bytes,
) -> bytes:
    """Sign a PlanApproval with EIP-712 typed data.

    Args:
        private_key: Hex-encoded private key.
        order_id: bytes32 order ID.
        plan_hash: bytes32 plan hash from hash_plan_eip712().
        score_bps: Score in basis points (0-10000). Contract uses scoreThreshold.
        domain_separator: EIP-712 domain separator.

    Returns:
        65-byte ECDSA signature.
    """
    struct_hash = hash_plan_approval_struct(order_id, plan_hash, score_bps)
    digest = _to_typed_data_hash(domain_separator, struct_hash)
    signed = Account.unsafe_sign_hash(digest, private_key=private_key)
    return signed.signature


def verify_plan_approval_eip712(
    address: str,
    signature: bytes,
    order_id: bytes,
    plan_hash: bytes,
    score_bps: int,
    domain_separator: bytes,
) -> bool:
    """Verify a validator's EIP-712 PlanApproval signature.

    Returns True if the signature was produced by the given address.
    """
    struct_hash = hash_plan_approval_struct(order_id, plan_hash, score_bps)
    digest = _to_typed_data_hash(domain_separator, struct_hash)
    recovered = Account._recover_hash(digest, signature=signature)
    return recovered.lower() == address.lower()


# ── Helpers ─────────────────────────────────────────────────────────────────


def _to_typed_data_hash(domain_separator: bytes, struct_hash: bytes) -> bytes:
    """Compute EIP-712 digest: keccak256(\\x19\\x01 || domainSeparator || structHash).

    Matches OpenZeppelin MessageHashUtils.toTypedDataHash().
    """
    return keccak(b"\x19\x01" + domain_separator + struct_hash)


def address_from_key(private_key: str) -> str:
    """Derive the checksummed Ethereum address from a private key."""
    return Account.from_key(private_key).address
