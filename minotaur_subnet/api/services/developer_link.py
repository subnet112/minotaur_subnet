"""Bind an app's EVM deployer to a Bittensor SS58 coldkey (dual-signed).

The app deployer identity is an EVM (secp256k1) address (see ``developer_auth``).
To collect the deploy fee as native TAO on finney, a validator must know which
SS58 coldkey speaks for that deployer — but the two are different keypairs, with
no link today. This records that link only when BOTH sides attest:

  * the EVM deployer signs an EIP-712 ``link_ss58`` authorization
    (``developer_auth``), committing to the exact SS58 it is linking; and
  * the SS58 coldkey signs a canonical message committing to
    ``(app_id, deployer)``, verified with the substrate ``Keypair``.

Requiring both directions stops either side from unilaterally claiming the
other's address — e.g. a deployer naming someone else's coldkey to free-ride on
their TAO transfer to the fee collector. The single-use developer-auth nonce
makes the EVM authorization replayable-once (and binds the SS58 message to the
same nonce).

This mirrors the EVM↔hotkey attestation in ``consensus/identity.py`` and the
substrate signature checks already used in the apps / submissions routes.
"""

from __future__ import annotations

from typing import Any

from minotaur_subnet.api.services import developer_auth


def link_message(app_id: str, deployer: str, nonce: int) -> str:
    """Canonical message the SS58 coldkey signs to consent to the link.

    Binds the app, the EVM deployer (lower-cased), and the single-use nonce, so
    a coldkey signature can't be replayed for another app/deployer or reused
    after the nonce advances.
    """
    return f"MinotaurLinkSS58:{app_id}:{(deployer or '').strip().lower()}:{int(nonce)}"


def verify_ss58_signature(ss58: str, message: str, signature: str) -> tuple[bool, str]:
    """Verify a substrate ``Keypair`` signature over ``message`` by ``ss58``.

    Returns ``(ok, error)``. Mirrors the hotkey checks in the apps / submissions
    routes (``Keypair(ss58_address=...).verify(...)``), so sr25519 and ed25519
    coldkeys both work.
    """
    try:
        from bittensor_wallet.keypair import Keypair
    except Exception:  # pragma: no cover - fallback import path
        try:
            from bittensor import Keypair
        except Exception as exc:  # pragma: no cover - substrate libs absent
            return False, f"substrate keypair unavailable: {exc}"

    raw = signature[2:] if signature.startswith("0x") else signature
    try:
        sig_bytes = bytes.fromhex(raw)
    except ValueError:
        return False, "ss58_signature must be hex"

    try:
        keypair = Keypair(ss58_address=ss58)
        ok = bool(keypair.verify(message.encode("utf-8"), sig_bytes))
    except Exception as exc:
        return False, f"ss58 signature verification failed: {exc}"
    if not ok:
        return False, "ss58 signature does not match the coldkey"
    return True, ""


def link_payer_ss58(
    store: Any,
    app_id: str,
    ss58: str,
    *,
    nonce: int,
    deadline: int,
    evm_signature: str,
    ss58_signature: str,
    now: int | None = None,
) -> tuple[bool, str]:
    """Record the ``(deployer EVM ↔ coldkey SS58)`` link for ``app_id``.

    Gated by BOTH signatures; the developer-auth nonce is consumed once, only on
    full success (so a failure on either side never burns a nonce). Returns
    ``(ok, error)``.
    """
    definition = store.get_app(app_id)
    if definition is None:
        return False, f"app not found: {app_id}"
    deployer = (getattr(definition, "deployer", "") or "").strip()
    if not deployer:
        return False, "app has no deployer; nothing to link"
    ss58 = (ss58 or "").strip()
    if not ss58:
        return False, "ss58 is required"

    # 1. EVM side: the deployer authorizes linking THIS ss58 (EIP-712 + nonce).
    ok, err = developer_auth.verify_developer_auth(
        expected_deployer=deployer,
        action=developer_auth.ACTION_LINK_SS58,
        app_id=app_id,
        params_hash=developer_auth.params_hash(ss58.encode()),
        nonce=nonce,
        deadline=deadline,
        signature=evm_signature,
        now=now,
    )
    if not ok:
        return False, f"deployer authorization invalid: {err}"

    # 2. SS58 side: the coldkey consents to (app_id, deployer) for this nonce.
    sok, serr = verify_ss58_signature(
        ss58, link_message(app_id, deployer, nonce), ss58_signature,
    )
    if not sok:
        return False, serr

    # 3. Both sides attested — consume the nonce and persist the link.
    consumed, cerr = store.consume_developer_nonce(app_id, deployer.lower(), nonce)
    if not consumed:
        return False, cerr
    store.set_payer_ss58(app_id, deployer.lower(), ss58)
    return True, ""
