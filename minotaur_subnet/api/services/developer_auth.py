"""EIP-712 developer-auth: replay-safe owner authorization for app actions.

A single typed-data primitive proving an app's ``deployer`` (an EVM address)
authorized a *specific* action — updating scoring JS today, deploying / paying
the deploy fee tomorrow. Every authorization is bound to
``(action, app_id, paramsHash)`` and carries a monotonic ``nonce`` (consumed
once per ``(app, deployer)`` — see ``AppIntentStore.consume_developer_nonce``)
and a ``deadline``. That binding closes a real gap in the previous scheme,
which signed only ``keccak(app_id, sha256(js))`` with no nonce: a captured
signature stayed valid forever, so anyone who replayed an old scoring update
could roll an app's JS back to a prior version. The nonce + deadline make a
captured signature single-use and short-lived, and the ``action`` field gives
domain separation so a signature for one action can't be replayed as another.

Real EIP-712 (``eth_account.encode_typed_data``) so a developer signs with a
standard wallet (``eth_signTypedData_v4``); the server recovers the signer and
compares it to the app's ``deployer``. This is the *developer* trust domain
(an app owner's EVM key) and is intentionally separate from the *validator*
identity/consensus signatures (a validator hotkey) in ``consensus/``.
"""

from __future__ import annotations

import time
from typing import Final

from eth_account import Account
from eth_account.messages import encode_typed_data
from eth_hash.auto import keccak

# ── EIP-712 domain + type ────────────────────────────────────────────────────
#
# No chainId / verifyingContract: this authorization is verified off-chain by
# the validator API, not by a contract, and binds to ``app_id`` (unique per
# environment) — so a fixed name+version domain is sufficient and keeps the
# signature environment-agnostic. ``version`` is the migration lever.
_DOMAIN: Final[dict] = {"name": "MinotaurDeveloperAuth", "version": "1"}

_TYPES: Final[dict] = {
    "DeveloperAuth": [
        {"name": "action", "type": "string"},
        {"name": "appId", "type": "string"},
        {"name": "paramsHash", "type": "bytes32"},
        {"name": "nonce", "type": "uint256"},
        {"name": "deadline", "type": "uint256"},
    ],
}

# Action tags — domain separation across auth-gated actions. Keep them short
# and human-readable so they render legibly in a wallet's signing prompt.
ACTION_UPDATE_SCORING: Final[str] = "update_scoring"
ACTION_DEPLOY: Final[str] = "deploy"
ACTION_PAY_DEPLOY_FEE: Final[str] = "pay_deploy_fee"
ACTION_LINK_SS58: Final[str] = "link_ss58"
# Create-time owner binding: the developer signs the app content, and the API
# records the RECOVERED signer as the app's deployer — so ownership is proven
# by a key, not a claimed address. appId is "" (the app doesn't exist yet); the
# paramsHash binds the code, the deadline bounds the signature. See
# api/services/app_auth.create_owner_binding_hash + create_app_intent.
ACTION_CREATE_APP: Final[str] = "create_app"
# App-management lifecycle actions (see api/services/app_auth.py). Each gates
# a relayer-key-signed operation, so the caller must prove owner authority by
# signing the exact parameters — otherwise the API's relayer key would move
# real funds / redirect fees on any unauthenticated caller's word.
ACTION_UPDATE_SOLIDITY: Final[str] = "update_solidity"
ACTION_RETIRE_DEPLOYMENT: Final[str] = "retire_deployment"
ACTION_FLOAT_DEPOSIT: Final[str] = "float_deposit"
ACTION_FLOAT_WITHDRAW: Final[str] = "float_withdraw"
ACTION_SET_CONFIG: Final[str] = "set_app_config"
ACTION_ALLOW_DEVELOPER: Final[str] = "allow_developer"
ACTION_ADMIN_STATE: Final[str] = "admin_state"
# Registration moderation (permissionless deploy, gated activation):
# request is owner-signed; approve/reject are ADMIN-ONLY (signer must be in
# APP_ADMIN_SIGNERS, never the app's own deployer). See app_registration.py.
ACTION_REQUEST_REGISTRATION: Final[str] = "request_registration"
ACTION_APPROVE_REGISTRATION: Final[str] = "approve_registration"
ACTION_REJECT_REGISTRATION: Final[str] = "reject_registration"
# Testing lever (solving → active skips the benchmark proof): ADMIN-ONLY like
# the registration moderation actions. ACTIVE is legacy-equivalent to SOLVED
# for order-readiness, so this never gates a normal go-live.
ACTION_ACTIVATE_APP: Final[str] = "activate_app"

# Reject deadlines further out than this — caps how long a signed-but-unused
# authorization can sit before replay, even though the nonce already makes it
# single-use.
MAX_DEADLINE_FUTURE_SECONDS: Final[int] = 24 * 3600  # 24h


def params_hash(data: bytes) -> bytes:
    """keccak256 of an action's bound content (e.g. the new JS source), bytes32.

    For ``update_scoring`` the bound content is the exact new JS code, so a
    signature authorizes *that* code and nothing else.
    """
    return keccak(data)


def _to_bytes32(value: bytes | str) -> bytes:
    """Normalize a paramsHash given as bytes or 0x-hex into 32 raw bytes."""
    if isinstance(value, str):
        raw = bytes.fromhex(value[2:] if value.startswith("0x") else value)
    else:
        raw = bytes(value)
    if len(raw) != 32:
        raise ValueError(f"paramsHash must be 32 bytes, got {len(raw)}")
    return raw


def _signable(action: str, app_id: str, params_hash_b: bytes, nonce: int, deadline: int):
    message = {
        "action": action,
        "appId": app_id,
        "paramsHash": params_hash_b,
        "nonce": int(nonce),
        "deadline": int(deadline),
    }
    return encode_typed_data(_DOMAIN, _TYPES, message)


def sign_developer_auth(
    private_key: str,
    *,
    action: str,
    app_id: str,
    params_hash: bytes | str,
    nonce: int,
    deadline: int,
) -> str:
    """Produce a developer-auth signature (hex). Used by tests + SDK helpers;
    frontends build the same typed data and sign via ``eth_signTypedData_v4``.
    """
    signable = _signable(action, app_id, _to_bytes32(params_hash), int(nonce), int(deadline))
    signed = Account.sign_message(signable, private_key=private_key)
    sig = signed.signature.hex()
    return sig if sig.startswith("0x") else "0x" + sig


def verify_developer_auth(
    *,
    expected_deployer: str,
    action: str,
    app_id: str,
    params_hash: bytes | str,
    nonce: int,
    deadline: int,
    signature: str,
    now: int | None = None,
) -> tuple[bool, str]:
    """Verify a developer-auth signature was produced by ``expected_deployer``,
    is fresh, and binds the exact ``(action, app_id, paramsHash, nonce)``.

    Returns ``(ok, error)``; on accept ``error`` is empty. Nonce *consumption*
    is the caller's job (atomic, via the store) — this only checks the binding.
    """
    if not expected_deployer:
        return False, "expected_deployer is empty"
    if not signature:
        return False, "signature is required for this action"

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

    try:
        signable = _signable(action, app_id, _to_bytes32(params_hash), int(nonce), deadline_i)
        sig = signature if signature.startswith("0x") else "0x" + signature
        recovered = Account.recover_message(signable, signature=sig)
    except Exception as exc:
        return False, f"signature malformed: {exc}"

    if recovered.lower() != expected_deployer.strip().lower():
        return False, (
            f"signature does not match deployer: signer {recovered[:10]}..., "
            f"deployer {expected_deployer.strip().lower()[:10]}..."
        )
    return True, ""


def recover_developer_auth(
    *,
    action: str,
    app_id: str,
    params_hash: bytes | str,
    nonce: int,
    deadline: int,
    signature: str,
    now: int | None = None,
) -> tuple[str | None, str]:
    """Recover the signer of a developer-auth signature (EIP-55 address) after
    checking freshness + binding — WITHOUT comparing to an expected address.

    For actions where the signer identity is being ESTABLISHED rather than
    checked — create-time owner binding, where the app has no deployer yet.
    Returns ``(address, "")`` on success or ``(None, error)``.
    """
    if not signature:
        return None, "signature is required"

    now_ts = int(now if now is not None else time.time())
    try:
        deadline_i = int(deadline)
    except (TypeError, ValueError):
        return None, f"invalid deadline: {deadline!r}"
    if deadline_i <= now_ts:
        return None, (
            f"signature deadline expired ({deadline_i} <= now {now_ts}); "
            "re-sign with a fresh deadline"
        )
    if deadline_i - now_ts > MAX_DEADLINE_FUTURE_SECONDS:
        return None, (
            f"deadline too far in the future "
            f"({deadline_i - now_ts}s > {MAX_DEADLINE_FUTURE_SECONDS}s)"
        )

    try:
        signable = _signable(action, app_id, _to_bytes32(params_hash), int(nonce), deadline_i)
        sig = signature if signature.startswith("0x") else "0x" + signature
        recovered = Account.recover_message(signable, signature=sig)
    except Exception as exc:
        return None, f"signature malformed: {exc}"
    return recovered, ""
