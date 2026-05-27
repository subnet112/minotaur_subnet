"""Signed-payload authentication for intra-host validator↔api RPC.

When the EpochManager (running in the api process) needs to hand a
per-miner weight ranking to the validator daemon (which owns the only
``bt.Wallet`` that can call ``subtensor.set_weights``), it does so by
POSTing to ``http://validator:9100/internal/weights/queue``. Both
containers run on the same Docker host and share the same internal
network, but that network is NOT inherently authenticated — any process
that can reach the validator container can otherwise call its emit
queue.

Rather than introducing a new shared-secret env var (more state for
operators to manage, more ways to misconfigure), we reuse the
``VALIDATOR_PRIVATE_KEY`` that both containers already hold. The api
signs every internal request with that key; the validator daemon
derives the expected signer address from the same key at startup and
rejects requests whose recovered signer doesn't match.

This is the same EIP-191 envelope used by ``consensus.leader_wrapper``:
keccak256 over abi-encoded (method, path, body, timestamp), wrapped in
``encode_defunct`` so the signature is indistinguishable from any other
personal_sign that the key produces. Includes a timestamp + max-age
gate to bound the replay window.

Headers (case-insensitive, sent on every internal request):

  X-Internal-Timestamp: <unix seconds, integer>
  X-Internal-Signature: 0x<65-byte hex>

Verification on the receiver:

    verify_request(
        method=request.method,
        path=request.path,
        body=await request.read(),
        timestamp=int(request.headers["X-Internal-Timestamp"]),
        signature_hex=request.headers["X-Internal-Signature"],
        expected_address=ctx.internal_signer_address,
    )

Raises ``InvalidSignature`` for any mismatch (signer, timestamp out of
window, malformed signature). Receivers should map that to HTTP 403.
"""

from __future__ import annotations

import time

from eth_abi import encode as abi_encode
from eth_account import Account
from eth_account.messages import encode_defunct
from eth_hash.auto import keccak


# Reject signed requests older than this. Bounded replay window even
# if every other layer fails. 30s is the same envelope leader_wrapper
# uses for off-host quorum bundles; intra-host RTT is sub-millisecond
# so 30s is comfortably loose for clock skew.
MAX_REQUEST_AGE_SECONDS = 30

# Clock skew tolerance for timestamps slightly in the future.
MAX_REQUEST_FUTURE_SKEW_SECONDS = 5


class InvalidSignature(Exception):
    """Raised when verify_request rejects a signed request."""


def _digest(method: str, path: str, body: bytes, timestamp: int) -> bytes:
    """Compute the keccak256 digest the signature commits to.

    The four fields together pin down exactly which request was signed:

      - method+path: a sig for ``POST /internal/weights/queue`` cannot
        be replayed against ``POST /internal/metagraph/state``.
      - body: the exact bytes; any tamper invalidates.
      - timestamp: freshness; receiver enforces max age.

    Note we don't include the host/port — they're implicit in the
    transport and both ends already agree on them via env config.
    """
    encoded = abi_encode(
        ["string", "string", "bytes", "uint64"],
        [method.upper(), path, body, int(timestamp)],
    )
    return keccak(encoded)


def sign_request(
    private_key: str,
    *,
    method: str,
    path: str,
    body: bytes,
    timestamp: int | None = None,
) -> tuple[int, str]:
    """Sign an internal HTTP request.

    Args:
        private_key: Hex EVM key (the same ``VALIDATOR_PRIVATE_KEY`` the
            api uses for EIP-712 consensus approvals).
        method: HTTP method, e.g. ``"POST"``. Case-normalized to upper.
        path: HTTP path with leading slash, no query string (the auth
            digest binds only the path; query params, if any, go in the
            body for signed requests).
        body: Exact request body bytes. Pass ``b""`` for GETs.
        timestamp: Unix seconds at sign time. Defaults to ``time.time()``.

    Returns:
        ``(timestamp, signature_hex)`` — caller stamps both into request
        headers as ``X-Internal-Timestamp`` / ``X-Internal-Signature``.
    """
    if timestamp is None:
        timestamp = int(time.time())
    msg = encode_defunct(primitive=_digest(method, path, body, timestamp))
    signed = Account.sign_message(msg, private_key=private_key)
    sig_hex = signed.signature.hex()
    if not sig_hex.startswith("0x"):
        sig_hex = "0x" + sig_hex
    return int(timestamp), sig_hex


def verify_request(
    *,
    method: str,
    path: str,
    body: bytes,
    timestamp: int,
    signature_hex: str,
    expected_address: str,
    max_age_seconds: int = MAX_REQUEST_AGE_SECONDS,
    now: float | None = None,
) -> None:
    """Verify a signed internal HTTP request.

    Args:
        method, path, body, timestamp, signature_hex: Mirror the values
            ``sign_request`` was called with by the sender.
        expected_address: The 0x-prefixed EVM address the receiver
            expects the signature to recover to. Derived once at
            validator startup from ``VALIDATOR_PRIVATE_KEY``.
        max_age_seconds: How old a request may be before rejection.
            Defaults to ``MAX_REQUEST_AGE_SECONDS`` (30s).
        now: Override the current time for testing.

    Raises:
        InvalidSignature: any mismatch — bad sig, wrong signer, stale
            timestamp, or future skew past the tolerance. The receiver
            maps this to HTTP 403 without leaking which check failed.
    """
    current = time.time() if now is None else now
    age = current - timestamp
    if age > max_age_seconds:
        raise InvalidSignature(
            f"request too old: timestamp {timestamp} is {age:.0f}s in the past "
            f"(max {max_age_seconds}s)"
        )
    if age < -MAX_REQUEST_FUTURE_SKEW_SECONDS:
        raise InvalidSignature(
            f"request timestamp {timestamp} is {-age:.0f}s in the future "
            f"(max skew {MAX_REQUEST_FUTURE_SKEW_SECONDS}s)"
        )

    sig = signature_hex if signature_hex.startswith("0x") else "0x" + signature_hex
    msg = encode_defunct(primitive=_digest(method, path, body, int(timestamp)))
    try:
        recovered = Account.recover_message(msg, signature=sig)
    except Exception as exc:
        raise InvalidSignature(f"signature recovery failed: {exc}") from exc

    if recovered.lower() != expected_address.lower():
        raise InvalidSignature(
            f"signer mismatch: expected {expected_address}, got {recovered}"
        )


def derive_signer_address(private_key: str) -> str:
    """Compute the checksummed EVM address of a private key.

    Used by the receiver at startup: it derives the expected signer once
    and caches it, then compares every incoming signature's recovered
    address against the cached value. The sender doesn't share this —
    both sides hold the same ``VALIDATOR_PRIVATE_KEY`` env so both can
    derive it independently.
    """
    return Account.from_key(private_key).address
