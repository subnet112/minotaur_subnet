"""Leader-signed submission wrapper — EIP-191 envelope around a quorum bundle.

The validator EIP-712 approvals already attest to the plan + scores. The
wrapper adds two additional bindings the validators do NOT cover:

  - **Caller identity**: the address that gets to call the relayer's
    ``/v1/submit-plan`` endpoint, so an anonymous attacker who observed
    a valid quorum bundle on-chain can't replay it. Only the wrapper's
    signer (which must be in the on-chain ``ValidatorRegistry``) can
    re-submit.
  - **Freshness**: a timestamp + monotonic nonce per signer. Limits the
    replay window to ``MAX_WRAPPER_AGE_SECONDS`` and rejects out-of-order
    re-submissions even within that window.

This lives outside the validator approval typehash on purpose — no
``AppIntentBase`` contract change is needed. The wrapper is checked
off-chain by the relayer; on-chain verification continues to operate
solely on the (still-unchanged) quorum sig set.

Format:

    WrapperPayload = (
        plan_hash: bytes32,
        submission_nonce: uint64,
        timestamp: uint64,
        chain_id: uint256,
    )

Signed with EIP-191 personal_sign over keccak256(abi_encode(...)).
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from eth_abi import encode as abi_encode
from eth_account import Account
from eth_account.messages import encode_defunct
from eth_hash.auto import keccak


# Wrappers older than this are rejected. Bounds the replay window even
# if every other layer fails. Long enough for normal RTT + clock skew.
MAX_WRAPPER_AGE_SECONDS = 30

# Clock skew tolerance for wrappers timestamped slightly in the future.
MAX_WRAPPER_FUTURE_SKEW_SECONDS = 5


@dataclass(frozen=True)
class WrapperPayload:
    """Canonical signable struct for the leader's submission wrapper."""

    plan_hash: str           # 0x-prefixed 32-byte hex
    submission_nonce: int    # Monotonically increasing per signer
    timestamp: int           # Unix seconds at sign time
    chain_id: int            # Chain the wrapped submission targets


def _digest(payload: WrapperPayload) -> bytes:
    """Compute the keccak256 digest the wrapper signature commits to."""
    plan_hash_bytes = bytes.fromhex(payload.plan_hash[2:] if payload.plan_hash.startswith("0x") else payload.plan_hash)
    if len(plan_hash_bytes) != 32:
        raise ValueError(f"plan_hash must be 32 bytes, got {len(plan_hash_bytes)}")
    encoded = abi_encode(
        ["bytes32", "uint64", "uint64", "uint256"],
        [plan_hash_bytes, int(payload.submission_nonce), int(payload.timestamp), int(payload.chain_id)],
    )
    return keccak(encoded)


def sign_wrapper(
    private_key: str,
    *,
    plan_hash: str,
    submission_nonce: int,
    chain_id: int,
    timestamp: int | None = None,
) -> tuple[WrapperPayload, str]:
    """Sign a wrapper around a quorum bundle.

    Returns (payload, signature_hex). The api includes both in its POST
    to the relayer's /v1/submit-plan endpoint.

    Args:
        private_key: Hex EVM key (typically the api's VALIDATOR_PRIVATE_KEY —
            same key that signs validator approvals).
        plan_hash: 0x-prefixed 32-byte hex of the plan being submitted.
        submission_nonce: Caller's monotonic counter. The relayer rejects
            any nonce <= last_seen for this signer.
        chain_id: Operational chain for the bundle.
        timestamp: Unix seconds. Defaults to now.
    """
    if timestamp is None:
        timestamp = int(time.time())
    payload = WrapperPayload(
        plan_hash=plan_hash,
        submission_nonce=int(submission_nonce),
        timestamp=int(timestamp),
        chain_id=int(chain_id),
    )
    msg = encode_defunct(primitive=_digest(payload))
    signed = Account.sign_message(msg, private_key=private_key)
    return payload, signed.signature.hex()


def recover_wrapper_signer(payload: WrapperPayload, signature_hex: str) -> str:
    """Recover the signer address from a wrapper sig.

    Raises ValueError on malformed signature. Returns the checksummed
    EVM address.
    """
    sig_bytes_hex = signature_hex
    if not sig_bytes_hex.startswith("0x"):
        sig_bytes_hex = "0x" + sig_bytes_hex
    msg = encode_defunct(primitive=_digest(payload))
    return Account.recover_message(msg, signature=sig_bytes_hex)


def is_wrapper_fresh(
    payload: WrapperPayload,
    *,
    now: int | None = None,
    max_age: int = MAX_WRAPPER_AGE_SECONDS,
    max_skew: int = MAX_WRAPPER_FUTURE_SKEW_SECONDS,
) -> tuple[bool, str]:
    """Check the wrapper's timestamp is within the accepted freshness
    window. Returns ``(ok, reason)`` — ``reason`` is empty when ok.
    """
    now = int(now if now is not None else time.time())
    age = now - int(payload.timestamp)
    if age > max_age:
        return False, f"wrapper too old: {age}s > {max_age}s window"
    if -age > max_skew:
        return False, f"wrapper timestamp too far in the future: {-age}s > {max_skew}s skew"
    return True, ""
