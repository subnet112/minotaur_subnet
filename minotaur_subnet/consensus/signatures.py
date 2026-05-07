"""Cryptographic helpers for validator plan approval signing.

Uses EIP-712 typed-data signatures matching the on-chain EIP712Verifier.
Delegates to eip712.py for all hashing and signing operations.
"""

from __future__ import annotations

from typing import Any

from eth_hash.auto import keccak

from minotaur_subnet.shared.types import ExecutionPlan
from .eip712 import (
    hash_plan_eip712,
    sign_plan_approval_eip712,
    verify_plan_approval_eip712,
    build_domain_separator,
    address_from_key,
)


def hash_plan(plan: ExecutionPlan) -> str:
    """Deterministic keccak256 hash of an execution plan.

    Matches EIP712Verifier.hashPlan() on-chain for consistency.
    Returns hex string with 0x prefix.
    """
    calls = []
    for ix in plan.interactions:
        call_data = bytes.fromhex(ix.call_data.replace("0x", "")) if ix.call_data != "0x" else b""
        calls.append((ix.target, int(ix.value), call_data))

    metadata = b""
    if plan.metadata:
        import json
        metadata = json.dumps(plan.metadata).encode() if isinstance(plan.metadata, dict) else plan.metadata

    return "0x" + hash_plan_eip712(calls, plan.deadline, plan.nonce, metadata).hex()


def sign_plan_approval(
    private_key: str,
    order_id: str,
    plan_hash: str,
    score: float,
    *,
    domain_separator: bytes | None = None,
    chain_id: int = 31337,
    contract_address: str = "0x" + "00" * 20,
    score_bps: int | None = None,
) -> str:
    """Sign a plan approval message using EIP-712.

    Args:
        private_key: Hex-encoded private key (with or without 0x prefix).
        order_id: The order being approved (hex string or plain string).
        plan_hash: Keccak256 hash of the execution plan (0x-prefixed hex).
        score: The JS score (0.0-1.0). Converted to BPS unless score_bps given.
        domain_separator: Pre-computed EIP-712 domain separator (optional).
        chain_id: Chain ID for domain computation (if no domain_separator).
        contract_address: Contract address for domain (if no domain_separator).
        score_bps: Override BPS score directly (0-10000).

    Returns:
        Hex-encoded signature (no 0x prefix).
    """
    if domain_separator is None:
        domain_separator = build_domain_separator(chain_id, contract_address)

    order_id_bytes = _str_to_bytes32(order_id)
    plan_hash_bytes = _str_to_bytes32(plan_hash)

    if score_bps is None:
        score_bps = int(score * 10000)

    sig = sign_plan_approval_eip712(
        private_key, order_id_bytes, plan_hash_bytes, score_bps, domain_separator,
    )
    return sig.hex()


def verify_plan_approval(
    public_key: str,
    signature: str,
    order_id: str,
    plan_hash: str,
    score: float,
    *,
    domain_separator: bytes | None = None,
    chain_id: int = 31337,
    contract_address: str = "0x" + "00" * 20,
    score_bps: int | None = None,
) -> bool:
    """Verify a validator's plan approval EIP-712 signature.

    Args:
        public_key: Expected signer address (0x-prefixed).
        signature: Hex-encoded signature to verify.
        order_id: The order that was approved.
        plan_hash: Keccak256 hash of the execution plan.
        score: The score (0.0-1.0). Converted to BPS unless score_bps given.
        domain_separator: Pre-computed domain separator (optional).
        chain_id: Chain ID for domain (if no domain_separator).
        contract_address: Contract address for domain (if no domain_separator).
        score_bps: Override BPS score directly.

    Returns:
        True if the signature is valid and from the expected signer.
    """
    if domain_separator is None:
        domain_separator = build_domain_separator(chain_id, contract_address)

    order_id_bytes = _str_to_bytes32(order_id)
    plan_hash_bytes = _str_to_bytes32(plan_hash)
    sig_bytes = bytes.fromhex(signature.replace("0x", ""))

    if score_bps is None:
        score_bps = int(score * 10000)

    return verify_plan_approval_eip712(
        public_key, sig_bytes, order_id_bytes, plan_hash_bytes,
        score_bps, domain_separator,
    )


def _keccak256(data: bytes) -> str:
    """Compute keccak256 hash, returning hex string with 0x prefix."""
    return "0x" + keccak(data).hex()


def _str_to_bytes32(s: str) -> bytes:
    """Convert a string to bytes32. If it looks like hex, decode it; otherwise keccak hash it."""
    s_clean = s.replace("0x", "")
    if len(s_clean) == 64:
        try:
            return bytes.fromhex(s_clean)
        except ValueError:
            pass
    return keccak(s.encode())
