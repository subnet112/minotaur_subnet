"""Order-ownership signature helpers (M3 + M4 from 2026-05-25 audit).

Three order-modifying endpoints — ``DELETE /orders/{id}``,
``PATCH /orders/{id}/tx-confirmed``, ``PATCH /orders/{id}/signature`` —
previously trusted client-supplied identity (``submitted_by`` query param)
or no identity at all (anyone could PATCH any order). The audit flagged
this as Medium severity: not fund-loss because on-chain EIP-712 still
gates execution, but grief-able (overwrite a real user's pending sig
with garbage; cancel orders you don't own; spam fake FILLED states).

This module provides a uniform EIP-191 sig check that proves the caller
controls the order's ``submitted_by`` address. Each action signs over a
domain-separated, content-bound payload with a deadline:

    digest = keccak(abi_encode(
        bytes32(domain="Minotaur Order Action v1"),
        bytes32(action),     # "Cancel" / "ConfirmTx" / "AttachSig"
        bytes(order_id),
        bytes32(content_hash),  # keccak of the new tx_hash / signature, "" for cancel
        uint64(deadline),
        uint256(chain_id),
    ))

Signed with ``personal_sign`` (EIP-191). Server recovers signer, checks
freshness, checks signer == order.submitted_by.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Final

from eth_abi import encode as abi_encode
from eth_account import Account
from eth_account.messages import encode_defunct
from eth_hash.auto import keccak


DOMAIN_TAG: Final[bytes] = b"Minotaur Order Action v1\x00\x00\x00\x00\x00\x00\x00\x00"
assert len(DOMAIN_TAG) == 32

# Actions are 32-byte ASCII tags. Keeping them short means humans can
# read them in a wallet's signing prompt — important UX.
ACTION_CANCEL: Final[bytes] = b"Cancel\x00" * 1 + b"\x00" * 25
ACTION_CONFIRM_TX: Final[bytes] = b"ConfirmTx\x00" + b"\x00" * 22
ACTION_ATTACH_SIG: Final[bytes] = b"AttachSig\x00" + b"\x00" * 22
for _t in (ACTION_CANCEL, ACTION_CONFIRM_TX, ACTION_ATTACH_SIG):
    assert len(_t) == 32, f"Action tag must be 32 bytes: {_t!r}"


# Max age of the deadline (relative to ``now()``) before we reject. The
# frontend typically signs with a deadline ~5 minutes ahead so the user
# has time to confirm in their wallet. We don't accept deadlines further
# than this in the future either — protects against signed-then-saved
# replay months later.
MAX_DEADLINE_FUTURE_SECONDS = 24 * 3600  # 24h


@dataclass(frozen=True)
class OrderActionPayload:
    """Canonical signable struct for an order-modifying action."""

    action: bytes        # 32-byte tag from ACTION_* constants
    order_id: str        # The order being modified
    content_hash: str    # 0x-prefixed 32-byte hex of action-specific content
    deadline: int        # Unix seconds; the sig is invalid after this
    chain_id: int        # Operational chain the order lives on


def _digest(p: OrderActionPayload) -> bytes:
    """Compute the keccak256 digest the personal_sign covers."""
    order_id_bytes = p.order_id.encode("utf-8")
    ch = p.content_hash[2:] if p.content_hash.startswith("0x") else p.content_hash
    if not ch:
        ch_bytes = b"\x00" * 32
    else:
        ch_bytes = bytes.fromhex(ch)
    if len(ch_bytes) != 32:
        raise ValueError(f"content_hash must be 32 bytes, got {len(ch_bytes)}")
    encoded = abi_encode(
        ["bytes32", "bytes32", "bytes", "bytes32", "uint64", "uint256"],
        [DOMAIN_TAG, p.action, order_id_bytes, ch_bytes, int(p.deadline), int(p.chain_id)],
    )
    return keccak(encoded)


def content_hash_of(content: str) -> str:
    """Hash an arbitrary payload (tx_hash, user_signature, "") for binding
    into an action payload. Empty content hashes to all-zeros."""
    if not content:
        return "0x" + ("0" * 64)
    return "0x" + keccak(content.encode("utf-8")).hex()


def sign_order_action(
    private_key: str,
    *,
    action: bytes,
    order_id: str,
    content_hash: str = "",
    deadline: int | None = None,
    chain_id: int = 0,
) -> str:
    """Sign an order-action payload. Returns the hex signature.

    Used in tests + by SDK helpers. Frontends construct the payload
    client-side and pass through MetaMask's ``personal_sign``.
    """
    if not content_hash:
        content_hash = "0x" + ("0" * 64)
    if deadline is None:
        deadline = int(time.time()) + 300  # 5min default
    payload = OrderActionPayload(
        action=action, order_id=order_id, content_hash=content_hash,
        deadline=int(deadline), chain_id=int(chain_id),
    )
    msg = encode_defunct(primitive=_digest(payload))
    signed = Account.sign_message(msg, private_key=private_key)
    return signed.signature.hex()


def verify_order_action(
    *,
    expected_owner: str,
    action: bytes,
    order_id: str,
    content_hash: str,
    deadline: int,
    chain_id: int,
    signature_hex: str,
    now: int | None = None,
) -> tuple[bool, str]:
    """Verify an action-sig was produced by ``expected_owner`` and is fresh.

    Returns ``(ok, error)``. On accept, ``error`` is empty.
    """
    if not expected_owner:
        return False, "expected_owner is empty"
    if not signature_hex:
        return False, "owner_signature is required for this action"

    now_ts = int(now if now is not None else time.time())
    try:
        deadline_i = int(deadline)
    except (TypeError, ValueError):
        return False, f"invalid deadline: {deadline!r}"

    if deadline_i <= now_ts:
        return False, (
            f"signature deadline expired ({deadline_i} <= now {now_ts}); "
            "re-sign with a fresh deadline"
        )
    if deadline_i - now_ts > MAX_DEADLINE_FUTURE_SECONDS:
        return False, (
            f"deadline too far in the future "
            f"({deadline_i - now_ts}s > {MAX_DEADLINE_FUTURE_SECONDS}s)"
        )

    payload = OrderActionPayload(
        action=action, order_id=order_id,
        content_hash=content_hash or ("0x" + "00" * 32),
        deadline=deadline_i, chain_id=int(chain_id),
    )
    try:
        msg = encode_defunct(primitive=_digest(payload))
        sig = signature_hex if signature_hex.startswith("0x") else "0x" + signature_hex
        recovered = Account.recover_message(msg, signature=sig)
    except Exception as exc:
        return False, f"owner_signature malformed: {exc}"

    if recovered.lower() != expected_owner.lower():
        return False, (
            f"owner_signature does not match order owner: signer is "
            f"{recovered[:10]}..., owner is {expected_owner[:10]}..."
        )
    return True, ""
