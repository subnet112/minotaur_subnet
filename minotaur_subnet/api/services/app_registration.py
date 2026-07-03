"""App registration moderation: permissionless deploy, gated activation.

The boundary this enforces: anyone may create + deploy an app and OWN it
(deployer = their wallet, all lifecycle actions gated to them by signature),
but an app only enters the LIVE order-routing set once it is registered in
the AppRegistry — and registration is now an admin-approved step, not an
automatic side effect of deploy. Registration is exactly the moderation
checkpoint, because the on-chain ``_requireRegistered()`` gate is what lets
an app serve real orders; an unapproved app is deployed but inert for live
traffic.

Flow:
  1. owner deploys (unregistered)      → registration_status "unrequested"
  2. owner POSTs request-registration  → "requested"        (owner-signed)
  3. admin POSTs approve-registration  → "approved" + on-chain register
                                          (ADMIN-ONLY: signer ∈ APP_ADMIN_SIGNERS)
     admin POSTs reject-registration   → "rejected"

Only ``approved`` (and legacy ``""``) apps auto-register on (re)deploy — see
``registration_allows_autoregister`` used by ``deploy_app_intent``.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# Registration moderation states.
REG_UNREQUESTED = "unrequested"
REG_REQUESTED = "requested"
REG_APPROVED = "approved"
REG_REJECTED = "rejected"

# Empty (legacy record predating the field) is grandfathered as approved so
# existing live apps keep auto-registering on redeploy (e.g. the dex
# aggregator V2 migration) without a manual approval step.
_AUTOREGISTER_OK = {REG_APPROVED, ""}


def registration_allows_autoregister(status: str | None) -> bool:
    """Whether an app in this registration state may auto-register at deploy."""
    return (status or "") in _AUTOREGISTER_OK


def _effective_status(definition: Any) -> str:
    raw = (getattr(definition, "registration_status", "") or "").strip()
    return raw or REG_APPROVED  # legacy "" presents as approved


def _has_deployment(store: Any, app_id: str) -> bool:
    return bool(store.get_deployments(app_id))


def request_registration(store: Any, app_id: str, note: str = "") -> dict[str, Any]:
    """Owner submits the app for registration review (unrequested → requested).

    Requires a deployment to exist — you request registration for a deployed
    contract, which also gives the request skin-in-the-game (deploy gas).
    """
    definition = store.get_app(app_id)
    if definition is None:
        return {"error": f"App not found: {app_id}"}
    if not _has_deployment(store, app_id):
        return {"error": "Deploy the app before requesting registration"}

    status = _effective_status(definition)
    if status == REG_APPROVED:
        return {"app_id": app_id, "registration_status": REG_APPROVED,
                "changed": False, "note": "already approved"}
    if status == REG_REQUESTED:
        return {"app_id": app_id, "registration_status": REG_REQUESTED, "changed": False}

    definition.registration_status = REG_REQUESTED
    meta = dict(definition.policy_metadata or {})
    reg = dict(meta.get("registration") or {})
    if note:
        reg["request_note"] = note[:500]
    meta["registration"] = reg
    definition.policy_metadata = meta
    store.save_app(definition)
    return {"app_id": app_id, "registration_status": REG_REQUESTED, "changed": True}


def approve_registration(store: Any, app_id: str, reviewer: str = "") -> dict[str, Any]:
    """ADMIN approves: mark approved and register every deployed contract in
    the AppRegistry (best-effort per chain, same path as deploy-time
    auto-registration). The route restricts this to APP_ADMIN_SIGNERS."""
    from .app_lifecycle import auto_register_deployment

    definition = store.get_app(app_id)
    if definition is None:
        return {"error": f"App not found: {app_id}"}
    deployments = store.get_deployments(app_id) or {}
    if not deployments:
        return {"error": "App has no deployment to register"}

    definition.registration_status = REG_APPROVED
    meta = dict(definition.policy_metadata or {})
    reg = dict(meta.get("registration") or {})
    if reviewer:
        reg["approved_by"] = reviewer
    meta["registration"] = reg
    definition.policy_metadata = meta
    store.save_app(definition)

    # Register each deployed contract (revoke stale mapping + allowlist +
    # registerApp). Never raises — surfaced per chain for the frontend.
    registry: dict[int, Any] = {}
    for chain_id, dep in deployments.items():
        addr = getattr(dep, "contract_address", None)
        if addr:
            registry[chain_id] = auto_register_deployment(store, app_id, chain_id, addr)
    return {"app_id": app_id, "registration_status": REG_APPROVED, "registry": registry}


def reject_registration(store: Any, app_id: str, reason: str = "", reviewer: str = "") -> dict[str, Any]:
    """ADMIN rejects the registration request (→ rejected). On-chain state is
    untouched; the app stays deployed but out of the live routing set."""
    definition = store.get_app(app_id)
    if definition is None:
        return {"error": f"App not found: {app_id}"}
    definition.registration_status = REG_REJECTED
    meta = dict(definition.policy_metadata or {})
    reg = dict(meta.get("registration") or {})
    if reason:
        reg["reject_reason"] = reason[:500]
    if reviewer:
        reg["rejected_by"] = reviewer
    meta["registration"] = reg
    definition.policy_metadata = meta
    store.save_app(definition)
    return {"app_id": app_id, "registration_status": REG_REJECTED}
