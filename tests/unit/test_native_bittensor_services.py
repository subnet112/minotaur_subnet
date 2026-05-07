"""Unit tests for native Bittensor API service helpers."""

from __future__ import annotations

from dataclasses import dataclass

from minotaur_subnet.api import services
from minotaur_subnet.shared.types import (
    NativeBittensorAction,
    NativeBittensorExecutionRecord,
    NativeBittensorExecutionStatus,
)
from minotaur_subnet.store import AppIntentStore


@dataclass
class FakeVerification:
    valid: bool
    matched_delegate: bool = True
    matched_proxy_type: bool = True
    matched_delay: bool = True
    entries: list[dict[str, object]] | None = None
    error: str = ""


class FakeNativeExecutor:
    def __init__(self, *, proxy_valid: bool = True):
        self.proxy_valid = proxy_valid
        self.add_calls: list[dict[str, object]] = []
        self.move_calls: list[dict[str, object]] = []

    def verify_proxy(self, permission):
        return FakeVerification(
            valid=self.proxy_valid,
            entries=[
                {
                    "delegate_ss58": permission.delegate_ss58,
                    "proxy_type": permission.proxy_type,
                    "delay_blocks": permission.proxy_delay_blocks,
                }
            ],
        )

    def execute_add_stake(
        self,
        permission,
        *,
        netuid: int,
        hotkey_ss58: str,
        amount_rao: int,
        reason: str = "",
        metadata=None,
        recent_executions=None,
    ):
        self.add_calls.append(
            {
                "permission_id": permission.permission_id,
                "netuid": netuid,
                "hotkey_ss58": hotkey_ss58,
                "amount_rao": amount_rao,
            }
        )
        return NativeBittensorExecutionRecord(
            execution_id="exec_add_1",
            permission_id=permission.permission_id,
            action=NativeBittensorAction.ADD_STAKE,
            owner_ss58=permission.owner_ss58,
            delegate_ss58=permission.delegate_ss58,
            amount_rao=amount_rao,
            status=NativeBittensorExecutionStatus.CONFIRMED,
            netuid=netuid,
            hotkey_ss58=hotkey_ss58,
            reason=reason,
            extrinsic_hash="0xadd",
            submitted_at=123.0,
            finalized_at=124.0,
            metadata=metadata or {},
        )

    def execute_move_stake(self, *args, **kwargs):
        raise AssertionError("move_stake not expected in this test")


def teardown_function(_name):
    services.set_native_bittensor_executor(None)
    services.set_native_bittensor_delegate_allocator(None)


def test_create_native_permission_uses_explicit_delegate_and_becomes_active(tmp_path):
    store = AppIntentStore(store_path=tmp_path / "store.json")
    services.set_native_bittensor_executor(FakeNativeExecutor(proxy_valid=True))

    result = services.create_native_bittensor_permission(
        store,
        owner_ss58="5Owner111",
        delegate_ss58="5Delegate111",
        allowed_netuids=[11],
        allowed_hotkeys=["5HotkeyA"],
        max_rao_per_action=1_000,
    )

    assert result["delegate_ss58"] == "5Delegate111"
    assert result["delegate_source"] == "explicit"
    assert result["status"] == "active"
    assert result["enabled_actions"] == ["add_stake", "move_stake"]
    assert result["proxy_verification"]["valid"] is True


def test_execute_native_add_stake_persists_execution_record(tmp_path):
    store = AppIntentStore(store_path=tmp_path / "store.json")
    services.set_native_bittensor_executor(FakeNativeExecutor(proxy_valid=True))

    created = services.create_native_bittensor_permission(
        store,
        owner_ss58="5Owner111",
        delegate_ss58="5Delegate111",
        allowed_netuids=[11],
        allowed_hotkeys=["5HotkeyA"],
        max_rao_per_action=2_000,
        max_rao_per_day=5_000,
    )
    permission_id = created["permission_id"]

    result = services.execute_native_bittensor_add_stake(
        store,
        permission_id,
        netuid=11,
        hotkey_ss58="5HotkeyA",
        amount_rao=1_500,
        reason="auto-rebalance",
    )

    assert result["status"] == "confirmed"
    assert result["action"] == "add_stake"
    assert result["extrinsic_hash"] == "0xadd"

    executions = store.list_native_executions(permission_id=permission_id)
    assert len(executions) == 1
    assert executions[0].status is NativeBittensorExecutionStatus.CONFIRMED
