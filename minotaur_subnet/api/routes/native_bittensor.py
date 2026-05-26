"""Routes for native Bittensor delegated permissions and executions."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from minotaur_subnet.api import services as _tools
from minotaur_subnet.api.routes.apps import _debug_rate_limit, _require_admin

router = APIRouter(tags=["native-bittensor"])


def _store():
    from minotaur_subnet.api.server import store

    return store


class CreateNativePermissionRequest(BaseModel):
    owner_ss58: str = Field(..., description="User's safe Bittensor ss58 account")
    allowed_netuids: list[int] = Field(..., description="Subnet netuids this permission may touch")
    allowed_hotkeys: list[str] = Field(default_factory=list, description="Allowed validator hotkeys")
    delegate_ss58: str = Field("", description="Optional dedicated delegate ss58 to bind")
    max_rao_per_action: int | None = Field(None, description="Max native amount per action in RAO")
    max_rao_per_day: int | None = Field(None, description="Max native amount per rolling day in RAO")
    max_slippage_bps: int | None = Field(None, description="Max tolerated slippage in basis points")
    cooldown_seconds: int | None = Field(None, description="Minimum delay between delegated actions")
    expires_at: float | None = Field(None, description="Unix timestamp when permission expires")
    enable_remove_stake: bool = Field(False, description="Opt-in to delegated remove_stake")
    policy_tier: str = Field("strict", description="Policy tier: strict/hybrid/expert")
    metadata: dict[str, Any] = Field(default_factory=dict, description="Extra metadata")


class RevokeNativePermissionRequest(BaseModel):
    reason: str = Field("", description="Optional soft-revoke reason for audit history")


class AddStakeRequest(BaseModel):
    netuid: int = Field(..., description="Target subnet netuid")
    hotkey_ss58: str = Field(..., description="Validator hotkey to stake to")
    amount_rao: int = Field(..., description="Stake amount in RAO")
    reason: str = Field("", description="Human-readable execution reason")
    metadata: dict[str, Any] = Field(default_factory=dict, description="Extra execution metadata")


class MoveStakeRequest(BaseModel):
    origin_netuid: int = Field(..., description="Origin subnet netuid")
    origin_hotkey_ss58: str = Field(..., description="Origin validator hotkey")
    destination_netuid: int = Field(..., description="Destination subnet netuid")
    destination_hotkey_ss58: str = Field(..., description="Destination validator hotkey")
    amount_rao: int = Field(..., description="Stake amount in RAO")
    reason: str = Field("", description="Human-readable execution reason")
    metadata: dict[str, Any] = Field(default_factory=dict, description="Extra execution metadata")


@router.post("/native-bittensor/permissions")
def create_native_permission(body: CreateNativePermissionRequest) -> dict[str, Any]:
    """Create a policy-bounded native Bittensor delegated permission."""
    return _tools.create_native_bittensor_permission(
        _store(),
        owner_ss58=body.owner_ss58,
        allowed_netuids=body.allowed_netuids,
        allowed_hotkeys=body.allowed_hotkeys,
        delegate_ss58=body.delegate_ss58,
        max_rao_per_action=body.max_rao_per_action,
        max_rao_per_day=body.max_rao_per_day,
        max_slippage_bps=body.max_slippage_bps,
        cooldown_seconds=body.cooldown_seconds,
        expires_at=body.expires_at,
        enable_remove_stake=body.enable_remove_stake,
        policy_tier=body.policy_tier,
        metadata=body.metadata,
    )


@router.get("/native-bittensor/permissions")
def list_native_permissions(
    owner_ss58: str = "",
    status: str = "",
) -> dict[str, Any]:
    """List native Bittensor delegated permissions."""
    return _tools.list_native_bittensor_permissions(
        _store(),
        owner_ss58=owner_ss58 or None,
        status=status or None,
    )


@router.get("/native-bittensor/permissions/{permission_id}")
def get_native_permission(permission_id: str) -> dict[str, Any]:
    """Get a native Bittensor delegated permission by ID."""
    return _tools.get_native_bittensor_permission(_store(), permission_id)


@router.post("/native-bittensor/permissions/{permission_id}/activate")
def activate_native_permission(permission_id: str) -> dict[str, Any]:
    """Activate a permission after on-chain proxy.addProxy() is confirmed."""
    return _tools.activate_native_bittensor_permission(_store(), permission_id)


@router.post("/native-bittensor/permissions/{permission_id}/refresh")
def refresh_native_permission(permission_id: str) -> dict[str, Any]:
    """Refresh a permission against current on-chain proxy state."""
    return _tools.refresh_native_bittensor_permission(_store(), permission_id)


@router.post("/native-bittensor/permissions/{permission_id}/revoke")
def revoke_native_permission(
    permission_id: str,
    body: RevokeNativePermissionRequest,
) -> dict[str, Any]:
    """Soft-revoke a delegated permission inside Minotaur."""
    return _tools.revoke_native_bittensor_permission(
        _store(),
        permission_id,
        reason=body.reason,
    )


@router.get("/native-bittensor/executions")
def list_native_executions(
    permission_id: str = "",
    owner_ss58: str = "",
    status: str = "",
) -> dict[str, Any]:
    """List native Bittensor execution audit records."""
    return _tools.list_native_bittensor_executions(
        _store(),
        permission_id=permission_id or None,
        owner_ss58=owner_ss58 or None,
        status=status or None,
    )


@router.post("/native-bittensor/permissions/{permission_id}/actions/add-stake")
def execute_add_stake(permission_id: str, body: AddStakeRequest) -> dict[str, Any]:
    """Execute a proxied native add_stake request."""
    return _tools.execute_native_bittensor_add_stake(
        _store(),
        permission_id,
        netuid=body.netuid,
        hotkey_ss58=body.hotkey_ss58,
        amount_rao=body.amount_rao,
        reason=body.reason,
        metadata=body.metadata,
    )


@router.post("/native-bittensor/permissions/{permission_id}/actions/move-stake")
def execute_move_stake(permission_id: str, body: MoveStakeRequest) -> dict[str, Any]:
    """Execute a proxied native move_stake request."""
    return _tools.execute_native_bittensor_move_stake(
        _store(),
        permission_id,
        origin_netuid=body.origin_netuid,
        origin_hotkey_ss58=body.origin_hotkey_ss58,
        destination_netuid=body.destination_netuid,
        destination_hotkey_ss58=body.destination_hotkey_ss58,
        amount_rao=body.amount_rao,
        reason=body.reason,
        metadata=body.metadata,
    )


class SimSwapRequest(BaseModel):
    """Simulate a TAO ↔ Alpha swap to get the expected output."""
    origin_netuid: int = Field(..., description="Source netuid (0=TAO)")
    destination_netuid: int = Field(..., description="Dest netuid (0=TAO)")
    amount_rao: int = Field(..., description="Input amount in RAO")


@router.post("/native-bittensor/sim-swap", dependencies=[Depends(_require_admin)])
def sim_swap(body: SimSwapRequest, request: Request) -> dict[str, Any]:
    """Simulate a TAO ↔ Alpha swap and return expected output.

    Admin-gated + per-IP rate-limited (1 req/min) as of PR-2 (audit H6):
    each call opens a fresh subtensor RPC connection to
    ``entrypoint-finney.opentensor.ai`` (or the configured SUBTENSOR_URL).
    Anonymous abuse burns this validator's per-IP RPC quota on the
    upstream entrypoint and can get the validator's source IP
    rate-limited or banned by Opentensor — silently breaking on-chain
    weight emission for hours.
    """
    _debug_rate_limit(request, per_minute=1)
    try:
        import os
        import bittensor as bt
        sub = bt.Subtensor(network=os.environ.get("SUBTENSOR_URL", "ws://subtensor:9944"))
        result = sub.sim_swap(
            origin_netuid=body.origin_netuid,
            destination_netuid=body.destination_netuid,
            amount=bt.Balance.from_rao(body.amount_rao),
        )
        return {
            "tao_amount": result.tao_amount.rao if hasattr(result.tao_amount, 'rao') else 0,
            "alpha_amount": result.alpha_amount.rao if hasattr(result.alpha_amount, 'rao') else 0,
            "tao_fee": result.tao_fee.rao if hasattr(result.tao_fee, 'rao') else 0,
            "alpha_fee": result.alpha_fee.rao if hasattr(result.alpha_fee, 'rao') else 0,
            "origin_netuid": body.origin_netuid,
            "destination_netuid": body.destination_netuid,
            "input_amount_rao": body.amount_rao,
        }
    except Exception as exc:
        return {"error": str(exc)}


# DirectStakeRequest + /native-bittensor/stake handler moved to
# ``routes/local_testnet.py`` (2026-05-25 audit). The stake bypass was
# documented as a "testnet shortcut" but mounted unconditionally on prod —
# now only registered when ``LOCAL_TESTNET=1``. Production stake operations
# go through the permission-system path (``/permissions/{id}/actions/add-stake``).
