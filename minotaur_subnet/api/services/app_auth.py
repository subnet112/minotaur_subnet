"""Wallet-signature authorization for app-management actions.

Why this exists: the app-lifecycle endpoints (float withdraw/deposit, config,
retire, solidity update, allow-developer) are executed with the LEADER's
relayer key — the API is a standing proxy for a key that can move real funds
and redirect fees. Behind only the shared ``X-Admin-Key`` that is one secret,
and one a browser frontend would have to hold. This layer lets the operator
(or an app's real owner) authorize each action by SIGNING it with their
wallet instead, so:

- the caller proves control of an allowed EVM key (no shared secret in a UI);
- the signature is bound to the exact parameters (recipient + amount for a
  withdraw, the fee bps for a config change, ...), so a captured or MITM'd
  request can't be re-pointed;
- a single-use nonce + short deadline make it non-replayable.

It reuses the EIP-712 ``developer_auth`` primitive (same domain/type, wallet
signs ``eth_signTypedData_v4``) and the per-``(app, signer)`` nonce store.

Allowed signers = the app's own ``deployer`` ∪ ``APP_ADMIN_SIGNERS`` (env, the
operator's own wallet addresses). During rollout the admin key still works as
a bypass; set ``REQUIRE_APP_ACTION_SIGNATURE=1`` to make a signature mandatory
(the switch that lets the admin key be retired safely).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from eth_hash.auto import keccak

from minotaur_subnet.api.services import developer_auth


@dataclass
class AuthBlock:
    """Parsed wallet-auth request headers (all optional; empty = absent)."""
    signer: str = ""
    signature: str = ""
    nonce: int = 0
    deadline: int = 0


def signature_required() -> bool:
    """Whether a wallet signature is MANDATORY (admin-key bypass disabled)."""
    return os.environ.get("REQUIRE_APP_ACTION_SIGNATURE", "0").strip().lower() in (
        "1", "true", "yes",
    )


def admin_signers() -> list[str]:
    """Operator wallet addresses allowed to manage ANY app (env, lowercased)."""
    raw = os.environ.get("APP_ADMIN_SIGNERS", "")
    return [a.strip().lower() for a in raw.split(",") if a.strip()]


def allowed_signers(store: Any, app_id: str, *, admin_only: bool = False) -> list[str]:
    """Addresses that may authorize actions for ``app_id`` (lowercased): the
    operator admin set, plus — unless ``admin_only`` — the app's own deployer.

    ``admin_only=True`` is for actions an owner must NOT self-authorize (the
    registration approval gate): only ``APP_ADMIN_SIGNERS`` qualify, never the
    app's deployer."""
    out = list(admin_signers())
    if admin_only:
        return out
    definition = store.get_app(app_id)
    dep = (getattr(definition, "deployer", "") or "").strip().lower()
    if dep and dep not in out:
        out.append(dep)
    return out


# ── parameter binding ────────────────────────────────────────────────────
#
# paramsHash = keccak256(utf8(canonical)). The canonical string pins every
# security-relevant parameter so a signature authorizes THAT operation and
# nothing else. A frontend reproduces it with
# ``keccak256(toUtf8Bytes(canonical))`` (ethers) before signing. Fields are
# joined with "|", addresses lowercased, ints decimal, bools "true"/"false".


def _canonical(action: str, app_id: str, chain_id: int | None, *parts: Any) -> str:
    head = [action, app_id]
    if chain_id is not None:
        head.append(str(int(chain_id)))
    return "|".join(head + [_part(p) for p in parts])


def _part(p: Any) -> str:
    if isinstance(p, bool):
        return "true" if p else "false"
    if isinstance(p, str) and p.startswith("0x") and len(p) == 42:
        return p.lower()
    return str(p)


def params_hash_for(action: str, app_id: str, chain_id: int | None, *parts: Any) -> bytes:
    """keccak of the canonical binding string for a lifecycle action."""
    return keccak(_canonical(action, app_id, chain_id, *parts).encode())


# Separator that cannot appear in Solidity/JS source (a control char), so the
# js/solidity concatenation is unambiguous.
_CREATE_SEP = "␟"


def create_owner_binding_hash(js_code: str, solidity_code: str) -> bytes:
    """paramsHash the app owner signs at CREATE time (action="create_app").

    Binds the app's code so a captured signature authorizes creating THAT app
    and nothing else. Frontend parity (ethers):
        keccak256(toUtf8Bytes(js_code + "\\u241f" + solidity_code))
    """
    return keccak(((js_code or "") + _CREATE_SEP + (solidity_code or "")).encode())


def canonical_string(action: str, app_id: str, chain_id: int | None, *parts: Any) -> str:
    """Expose the exact canonical string (for responses / frontend parity)."""
    return _canonical(action, app_id, chain_id, *parts)


# ── authorization ────────────────────────────────────────────────────────


def authorize(
    store: Any,
    app_id: str,
    *,
    action: str,
    params_hash: bytes,
    auth: AuthBlock,
    admin_ok: bool,
    consume_nonce: bool = True,
    admin_only: bool = False,
    now: int | None = None,
) -> tuple[bool, str, str]:
    """Authorize a lifecycle action. Returns ``(ok, error, signer)``.

    Rules, in order:
      1. A provided signature is ALWAYS verified (and its nonce consumed when
         ``consume_nonce``), regardless of ``admin_ok`` — a wallet signature is
         the strongest proof, so we never silently ignore one.
      2. No signature + ``admin_ok`` + signatures not mandated → allow
         (``signer="admin"``), the back-compat path during rollout.
      3. Otherwise → deny.

    ``consume_nonce=False`` is for read actions (admin-state): the signature
    is still parameter- and deadline-bound, but reads don't burn a nonce.
    ``admin_only=True`` restricts the signature path to ``APP_ADMIN_SIGNERS``
    (the app owner cannot self-authorize) — for the registration approval
    gate. The admin-key bypass still counts as admin authority.
    """
    sig = (auth.signature or "").strip()
    if sig:
        allowed = allowed_signers(store, app_id, admin_only=admin_only)
        signer = (auth.signer or "").strip().lower()
        if not signer:
            # Default to the app's own deployer when the caller didn't say.
            definition = store.get_app(app_id)
            signer = (getattr(definition, "deployer", "") or "").strip().lower()
        if not signer:
            return False, "no signer given and app has no deployer", ""
        if signer not in allowed:
            return False, f"signer {signer[:10]}… is not an allowed signer for this app", ""

        ok, err = developer_auth.verify_developer_auth(
            expected_deployer=signer,
            action=action,
            app_id=app_id,
            params_hash=params_hash,
            nonce=auth.nonce,
            deadline=auth.deadline,
            signature=sig,
            now=now,
        )
        if not ok:
            return False, err, ""
        if consume_nonce:
            consumed, cerr = store.consume_developer_nonce(app_id, signer, auth.nonce)
            if not consumed:
                return False, cerr, ""
        return True, "", signer

    if admin_ok and not signature_required():
        return True, "", "admin"

    if signature_required():
        return False, "wallet signature required (admin-key bypass disabled)", ""
    return False, "admin key or wallet signature required", ""
