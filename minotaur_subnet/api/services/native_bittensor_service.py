"""Native Bittensor delegated permissions service functions."""

from __future__ import annotations

import time
import uuid
from dataclasses import asdict
from typing import Any

from minotaur_subnet.shared.types import (
    NativeBittensorAction,
    NativeBittensorExecutionRecord,
    NativeBittensorExecutionStatus,
    NativeBittensorPermission,
    NativeBittensorPermissionStatus,
    PolicyTier,
)
from minotaur_subnet.store import AppIntentStore

import logging

logger = logging.getLogger(__name__)


def _policy_tier_from_input(value: str | PolicyTier | None) -> PolicyTier | None:
    if isinstance(value, PolicyTier):
        return value
    if value is None:
        return PolicyTier.STRICT
    raw = str(value).strip().lower()
    for member in PolicyTier:
        if member.value == raw:
            return member
    return None


def _native_permission_status_from_input(
    value: str | NativeBittensorPermissionStatus | None,
) -> NativeBittensorPermissionStatus | None:
    if value is None or value == "":
        return None
    if isinstance(value, NativeBittensorPermissionStatus):
        return value
    raw = str(value).strip().lower()
    for member in NativeBittensorPermissionStatus:
        if member.value == raw:
            return member
    return None


def _native_execution_status_from_input(
    value: str | NativeBittensorExecutionStatus | None,
) -> NativeBittensorExecutionStatus | None:
    if value is None or value == "":
        return None
    if isinstance(value, NativeBittensorExecutionStatus):
        return value
    raw = str(value).strip().lower()
    for member in NativeBittensorExecutionStatus:
        if member.value == raw:
            return member
    return None


def _serialize_native_permission(permission: NativeBittensorPermission) -> dict[str, Any]:
    data = asdict(permission)
    data["status"] = permission.status.value
    data["policy_tier"] = permission.policy_tier.value
    data["enabled_actions"] = [action.value for action in permission.enabled_actions]
    return data


def _serialize_native_execution(record: NativeBittensorExecutionRecord) -> dict[str, Any]:
    data = asdict(record)
    data["action"] = record.action.value
    data["status"] = record.status.value
    return data


def _serialize_proxy_verification(result: Any) -> dict[str, Any]:
    if result is None:
        return {}
    if hasattr(result, "__dataclass_fields__"):
        return asdict(result)
    if isinstance(result, dict):
        return dict(result)
    return {"valid": bool(getattr(result, "valid", False))}


def _resolve_delegate_ss58(
    store: AppIntentStore,
    owner_ss58: str,
    delegate_ss58: str = "",
) -> tuple[str, str]:
    from ._state import _native_bittensor_delegate_allocator

    explicit = delegate_ss58.strip()
    if explicit:
        return explicit, "explicit"

    existing = store.list_native_permissions(owner_ss58=owner_ss58)
    preferred = [
        permission
        for permission in existing
        if permission.delegate_ss58
        and permission.status is not NativeBittensorPermissionStatus.REVOKED
    ]
    if preferred:
        return preferred[0].delegate_ss58, "existing"
    if existing and existing[0].delegate_ss58:
        return existing[0].delegate_ss58, "existing"

    if _native_bittensor_delegate_allocator is not None:
        allocated = _native_bittensor_delegate_allocator(owner_ss58)
        if allocated:
            return str(allocated), "allocator"

    return "", ""


def _sync_native_permission_status(
    store: AppIntentStore,
    permission: NativeBittensorPermission,
) -> tuple[NativeBittensorPermission, Any]:
    from ._state import _native_bittensor_executor

    verification = None
    now = time.time()
    if permission.expires_at is not None and now > permission.expires_at:
        permission.status = NativeBittensorPermissionStatus.EXPIRED
    elif (
        _native_bittensor_executor is not None
        and permission.status
        not in (
            NativeBittensorPermissionStatus.REVOKED,
            NativeBittensorPermissionStatus.DISABLED,
        )
    ):
        verification = _native_bittensor_executor.verify_proxy(permission)
        permission.status = (
            NativeBittensorPermissionStatus.ACTIVE
            if getattr(verification, "valid", False)
            else NativeBittensorPermissionStatus.PENDING
        )
    permission.updated_at = now
    store.save_native_permission(permission)
    return permission, verification


def create_native_bittensor_permission(
    store: AppIntentStore,
    *,
    owner_ss58: str,
    allowed_netuids: list[int],
    allowed_hotkeys: list[str] | None = None,
    delegate_ss58: str = "",
    max_rao_per_action: int | None = None,
    max_rao_per_day: int | None = None,
    max_slippage_bps: int | None = None,
    cooldown_seconds: int | None = None,
    expires_at: float | None = None,
    enable_remove_stake: bool = False,
    policy_tier: str | PolicyTier | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create a policy-bounded native Bittensor delegated permission."""
    from ._state import _native_bittensor_executor

    owner = owner_ss58.strip()
    if not owner:
        return {"error": "owner_ss58 is required"}
    if not allowed_netuids:
        return {"error": "allowed_netuids must be a non-empty list"}
    if any(not isinstance(netuid, int) or netuid < 0 for netuid in allowed_netuids):
        return {"error": "allowed_netuids must contain non-negative integers only"}

    tier = _policy_tier_from_input(policy_tier)
    if tier is None:
        return {"error": f"Invalid policy_tier: {policy_tier}"}

    resolved_delegate, delegate_source = _resolve_delegate_ss58(
        store,
        owner,
        delegate_ss58=delegate_ss58,
    )
    if not resolved_delegate:
        return {
            "error": "No delegate_ss58 provided and no delegate allocator is configured",
        }

    if expires_at is not None and expires_at <= time.time():
        return {"error": "expires_at must be in the future"}
    if max_rao_per_action is not None and max_rao_per_action <= 0:
        return {"error": "max_rao_per_action must be positive when provided"}
    if max_rao_per_day is not None and max_rao_per_day <= 0:
        return {"error": "max_rao_per_day must be positive when provided"}

    actions = [
        NativeBittensorAction.ADD_STAKE,
        NativeBittensorAction.MOVE_STAKE,
    ]
    if enable_remove_stake:
        actions.append(NativeBittensorAction.REMOVE_STAKE)

    now = time.time()
    permission = NativeBittensorPermission(
        permission_id=f"native_perm_{uuid.uuid4().hex[:16]}",
        owner_ss58=owner,
        delegate_ss58=resolved_delegate,
        status=NativeBittensorPermissionStatus.PENDING,
        enabled_actions=actions,
        allowed_netuids=list(dict.fromkeys(allowed_netuids)),
        allowed_hotkeys=list(dict.fromkeys(allowed_hotkeys or [])),
        max_rao_per_action=max_rao_per_action,
        max_rao_per_day=max_rao_per_day,
        max_slippage_bps=max_slippage_bps,
        cooldown_seconds=cooldown_seconds,
        expires_at=expires_at,
        policy_tier=tier,
        created_at=now,
        updated_at=now,
        metadata=dict(metadata or {}),
    )

    verification = None
    if _native_bittensor_executor is not None:
        verification = _native_bittensor_executor.verify_proxy(permission)
        if getattr(verification, "valid", False):
            permission.status = NativeBittensorPermissionStatus.ACTIVE

    store.save_native_permission(permission)
    response = _serialize_native_permission(permission)
    response["delegate_source"] = delegate_source
    if verification is not None:
        response["proxy_verification"] = _serialize_proxy_verification(verification)
    return response


def get_native_bittensor_permission(
    store: AppIntentStore,
    permission_id: str,
) -> dict[str, Any]:
    """Return a native Bittensor delegated permission by ID."""
    permission = store.get_native_permission(permission_id)
    if permission is None:
        return {"error": f"Native permission not found: {permission_id}"}
    return _serialize_native_permission(permission)


def activate_native_bittensor_permission(
    store: AppIntentStore,
    permission_id: str,
) -> dict[str, Any]:
    """Activate a permission after on-chain proxy.addProxy() is confirmed.

    Called by the frontend after the user signs the proxy extrinsic.
    """
    permission = store.get_native_permission(permission_id)
    if permission is None:
        return {"error": f"Native permission not found: {permission_id}"}
    permission.status = NativeBittensorPermissionStatus.ACTIVE
    store.save_native_permission(permission)
    return {"permission_id": permission_id, "status": "active"}


def list_native_bittensor_permissions(
    store: AppIntentStore,
    *,
    owner_ss58: str | None = None,
    status: str | NativeBittensorPermissionStatus | None = None,
) -> dict[str, Any]:
    """List native Bittensor delegated permissions."""
    status_filter = _native_permission_status_from_input(status)
    if status not in (None, "") and status_filter is None:
        return {"error": f"Invalid native permission status: {status}"}

    permissions = store.list_native_permissions(
        owner_ss58=owner_ss58 or None,
        status=status_filter,
    )
    return {
        "permissions": [_serialize_native_permission(permission) for permission in permissions],
        "count": len(permissions),
    }


def refresh_native_bittensor_permission(
    store: AppIntentStore,
    permission_id: str,
) -> dict[str, Any]:
    """Refresh a native permission against current on-chain proxy state."""
    from ._state import _native_bittensor_executor

    if _native_bittensor_executor is None:
        return {"error": "Native Bittensor executor is not configured"}

    permission = store.get_native_permission(permission_id)
    if permission is None:
        return {"error": f"Native permission not found: {permission_id}"}

    permission, verification = _sync_native_permission_status(store, permission)
    response = _serialize_native_permission(permission)
    if verification is not None:
        response["proxy_verification"] = _serialize_proxy_verification(verification)
    return response


def revoke_native_bittensor_permission(
    store: AppIntentStore,
    permission_id: str,
    *,
    reason: str = "",
) -> dict[str, Any]:
    """Soft-revoke a native Bittensor delegated permission in Minotaur."""
    permission = store.get_native_permission(permission_id)
    if permission is None:
        return {"error": f"Native permission not found: {permission_id}"}

    permission.status = NativeBittensorPermissionStatus.REVOKED
    permission.updated_at = time.time()
    if reason:
        permission.metadata = {
            **permission.metadata,
            "revocation_reason": reason,
            "revoked_at": permission.updated_at,
        }
    store.save_native_permission(permission)

    response = _serialize_native_permission(permission)
    response["warning"] = (
        "This is a Minotaur-side revoke only. Remove the on-chain proxy as well "
        "to enforce a hard stop."
    )
    return response


def list_native_bittensor_executions(
    store: AppIntentStore,
    *,
    permission_id: str | None = None,
    owner_ss58: str | None = None,
    status: str | NativeBittensorExecutionStatus | None = None,
) -> dict[str, Any]:
    """List audit records for native Bittensor delegated executions."""
    status_filter = _native_execution_status_from_input(status)
    if status not in (None, "") and status_filter is None:
        return {"error": f"Invalid native execution status: {status}"}

    records = store.list_native_executions(
        permission_id=permission_id or None,
        owner_ss58=owner_ss58 or None,
        status=status_filter,
    )
    return {
        "executions": [_serialize_native_execution(record) for record in records],
        "count": len(records),
    }


def execute_native_bittensor_add_stake(
    store: AppIntentStore,
    permission_id: str,
    *,
    netuid: int,
    hotkey_ss58: str,
    amount_rao: int,
    reason: str = "",
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Execute a proxied native `add_stake` using an active delegated permission."""
    from ._state import _native_bittensor_executor

    if _native_bittensor_executor is None:
        return {"error": "Native Bittensor executor is not configured"}

    permission = store.get_native_permission(permission_id)
    if permission is None:
        return {"error": f"Native permission not found: {permission_id}"}

    permission, verification = _sync_native_permission_status(store, permission)
    if permission.status is not NativeBittensorPermissionStatus.ACTIVE:
        response = _serialize_native_permission(permission)
        response["error"] = "Native permission is not active"
        if verification is not None:
            response["proxy_verification"] = _serialize_proxy_verification(verification)
        return response

    recent = store.list_native_executions(permission_id=permission_id)
    record = _native_bittensor_executor.execute_add_stake(
        permission,
        netuid=netuid,
        hotkey_ss58=hotkey_ss58,
        amount_rao=amount_rao,
        reason=reason,
        metadata=metadata or {},
        recent_executions=recent,
    )
    store.save_native_execution(record)
    return _serialize_native_execution(record)


def execute_native_bittensor_move_stake(
    store: AppIntentStore,
    permission_id: str,
    *,
    origin_netuid: int,
    origin_hotkey_ss58: str,
    destination_netuid: int,
    destination_hotkey_ss58: str,
    amount_rao: int,
    reason: str = "",
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Execute a proxied native `move_stake` using an active delegated permission."""
    from ._state import _native_bittensor_executor

    if _native_bittensor_executor is None:
        return {"error": "Native Bittensor executor is not configured"}

    permission = store.get_native_permission(permission_id)
    if permission is None:
        return {"error": f"Native permission not found: {permission_id}"}

    permission, verification = _sync_native_permission_status(store, permission)
    if permission.status is not NativeBittensorPermissionStatus.ACTIVE:
        response = _serialize_native_permission(permission)
        response["error"] = "Native permission is not active"
        if verification is not None:
            response["proxy_verification"] = _serialize_proxy_verification(verification)
        return response

    recent = store.list_native_executions(permission_id=permission_id)
    record = _native_bittensor_executor.execute_move_stake(
        permission,
        origin_netuid=origin_netuid,
        origin_hotkey_ss58=origin_hotkey_ss58,
        destination_netuid=destination_netuid,
        destination_hotkey_ss58=destination_hotkey_ss58,
        amount_rao=amount_rao,
        reason=reason,
        metadata=metadata or {},
        recent_executions=recent,
    )
    store.save_native_execution(record)
    return _serialize_native_execution(record)
