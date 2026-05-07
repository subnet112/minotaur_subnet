"""Unit tests for proxy-backed native Bittensor execution scaffolding."""

from __future__ import annotations

from dataclasses import dataclass

from minotaur_subnet.blockchain.bittensor_proxy_executor import BittensorProxyExecutor
from minotaur_subnet.shared.types import (
    NativeBittensorAction,
    NativeBittensorActionRequest,
    NativeBittensorExecutionRecord,
    NativeBittensorExecutionStatus,
    NativeBittensorPermission,
    NativeBittensorPermissionStatus,
)
from minotaur_subnet.store import AppIntentStore


@dataclass
class FakeCall:
    call_module: str
    call_function: str
    call_params: dict[str, object]

    @property
    def data(self) -> bytes:
        payload = f"{self.call_module}:{self.call_function}:{sorted(self.call_params.items())}"
        return payload.encode()


@dataclass
class FakeResponse:
    success: bool = True
    extrinsic_hash: str = "0xabc123"
    error: str = ""
    message: str = "Success"


class FakeSubtensor:
    def __init__(self, *, proxy_entries=None, response: FakeResponse | None = None):
        self.proxy_entries = proxy_entries or []
        self.response = response or FakeResponse()
        self.composed_calls: list[FakeCall] = []
        self.proxy_calls: list[dict[str, object]] = []

    def get_proxies_for_real_account(self, owner_ss58: str):
        return self.proxy_entries

    def compose_call(self, *, call_module: str, call_function: str, call_params: dict[str, object]):
        call = FakeCall(call_module=call_module, call_function=call_function, call_params=call_params)
        self.composed_calls.append(call)
        return call

    def proxy(self, *, wallet, real_account_ss58: str, force_proxy_type: str, call: FakeCall):
        self.proxy_calls.append(
            {
                "wallet": wallet,
                "real_account_ss58": real_account_ss58,
                "force_proxy_type": force_proxy_type,
                "call": call,
            }
        )
        return self.response


def _make_permission(**overrides) -> NativeBittensorPermission:
    base = {
        "permission_id": "perm_1",
        "owner_ss58": "5Owner111",
        "delegate_ss58": "5Delegate111",
        "status": NativeBittensorPermissionStatus.ACTIVE,
        "enabled_actions": [
            NativeBittensorAction.ADD_STAKE,
            NativeBittensorAction.MOVE_STAKE,
        ],
        "allowed_netuids": [11, 12],
        "allowed_hotkeys": ["5HotkeyA", "5HotkeyB"],
        "max_rao_per_action": 5_000,
        "max_rao_per_day": 10_000,
        "cooldown_seconds": 0,
        "created_at": 100.0,
        "updated_at": 100.0,
    }
    base.update(overrides)
    return NativeBittensorPermission(**base)


def test_store_round_trip_native_permission_and_execution(tmp_path):
    store = AppIntentStore(store_path=tmp_path / "store.json")
    permission = _make_permission()
    record = NativeBittensorExecutionRecord(
        execution_id="exec_1",
        permission_id=permission.permission_id,
        action=NativeBittensorAction.ADD_STAKE,
        owner_ss58=permission.owner_ss58,
        delegate_ss58=permission.delegate_ss58,
        amount_rao=1_000,
        status=NativeBittensorExecutionStatus.CONFIRMED,
        netuid=11,
        hotkey_ss58="5HotkeyA",
        call_hash="0xcall",
        extrinsic_hash="0xextrinsic",
        submitted_at=123.0,
        finalized_at=124.0,
    )

    store.save_native_permission(permission)
    store.save_native_execution(record)

    reloaded = AppIntentStore(store_path=tmp_path / "store.json")
    loaded_permission = reloaded.get_native_permission(permission.permission_id)
    assert loaded_permission is not None
    assert loaded_permission.status is NativeBittensorPermissionStatus.ACTIVE
    assert loaded_permission.enabled_actions == permission.enabled_actions

    loaded_records = reloaded.list_native_executions(permission_id=permission.permission_id)
    assert len(loaded_records) == 1
    assert loaded_records[0].status is NativeBittensorExecutionStatus.CONFIRMED
    assert loaded_records[0].extrinsic_hash == "0xextrinsic"


def test_executor_rejects_action_outside_policy():
    subtensor = FakeSubtensor(
        proxy_entries=[
            {"delegate": "5Delegate111", "proxy_type": "Staking", "delay": 0},
        ]
    )
    permission = _make_permission(enabled_actions=[NativeBittensorAction.ADD_STAKE])
    executor = BittensorProxyExecutor(
        subtensor=subtensor,
        wallet_loader=lambda ss58: {"delegate": ss58},
        clock=lambda: 200.0,
    )
    request = NativeBittensorActionRequest(
        permission_id=permission.permission_id,
        action=NativeBittensorAction.REMOVE_STAKE,
        owner_ss58=permission.owner_ss58,
        delegate_ss58=permission.delegate_ss58,
        amount_rao=100,
        netuid=11,
        hotkey_ss58="5HotkeyA",
    )

    record = executor.execute_request(permission, request)

    assert record.status is NativeBittensorExecutionStatus.REJECTED
    assert "Action not allowed" in record.error
    assert subtensor.proxy_calls == []


def test_executor_rejects_when_daily_cap_exceeded():
    subtensor = FakeSubtensor(
        proxy_entries=[
            {"delegate": "5Delegate111", "proxy_type": "Staking", "delay": 0},
        ]
    )
    permission = _make_permission(max_rao_per_day=1_000)
    prior = NativeBittensorExecutionRecord(
        execution_id="exec_prior",
        permission_id=permission.permission_id,
        action=NativeBittensorAction.ADD_STAKE,
        owner_ss58=permission.owner_ss58,
        delegate_ss58=permission.delegate_ss58,
        amount_rao=900,
        status=NativeBittensorExecutionStatus.CONFIRMED,
        submitted_at=50.0,
    )
    executor = BittensorProxyExecutor(
        subtensor=subtensor,
        wallet_loader=lambda ss58: {"delegate": ss58},
        clock=lambda: 100.0,
    )

    record = executor.execute_add_stake(
        permission,
        netuid=11,
        hotkey_ss58="5HotkeyA",
        amount_rao=200,
        recent_executions=[prior],
    )

    assert record.status is NativeBittensorExecutionStatus.REJECTED
    assert "daily policy cap" in record.error
    assert subtensor.proxy_calls == []


def test_executor_executes_add_stake_via_proxy():
    subtensor = FakeSubtensor(
        proxy_entries=[
            {"delegate": "5Delegate111", "proxy_type": "Staking", "delay": 0},
        ]
    )
    permission = _make_permission()
    executor = BittensorProxyExecutor(
        subtensor=subtensor,
        wallet_loader=lambda ss58: {"delegate": ss58},
        clock=lambda: 300.0,
    )

    record = executor.execute_add_stake(
        permission,
        netuid=11,
        hotkey_ss58="5HotkeyA",
        amount_rao=1_500,
        reason="rebalance inflow",
    )

    assert record.status is NativeBittensorExecutionStatus.CONFIRMED
    assert record.extrinsic_hash == "0xabc123"
    assert record.call_hash.startswith("0x")
    assert subtensor.proxy_calls[0]["real_account_ss58"] == permission.owner_ss58
    assert subtensor.proxy_calls[0]["force_proxy_type"] == "Staking"
    call = subtensor.proxy_calls[0]["call"]
    assert call.call_function == "add_stake"
    assert call.call_params["amount_staked"] == 1_500
    assert call.call_params["netuid"] == 11


def test_executor_executes_move_stake_via_proxy():
    subtensor = FakeSubtensor(
        proxy_entries=[
            {"delegate": "5Delegate111", "proxy_type": "Staking", "delay": 0},
        ]
    )
    permission = _make_permission()
    executor = BittensorProxyExecutor(
        subtensor=subtensor,
        wallet_loader=lambda ss58: {"delegate": ss58},
        clock=lambda: 400.0,
    )

    record = executor.execute_move_stake(
        permission,
        origin_netuid=11,
        origin_hotkey_ss58="5HotkeyA",
        destination_netuid=12,
        destination_hotkey_ss58="5HotkeyB",
        amount_rao=2_000,
        reason="validator rotation",
    )

    assert record.status is NativeBittensorExecutionStatus.CONFIRMED
    call = subtensor.proxy_calls[0]["call"]
    assert call.call_function == "move_stake"
    assert call.call_params["origin_netuid"] == 11
    assert call.call_params["destination_netuid"] == 12
    assert call.call_params["alpha_amount"] == 2_000
