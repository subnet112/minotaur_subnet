"""API tests for native Bittensor delegated permission endpoints."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

os.environ["DISABLE_BENCHMARK_WORKER"] = "1"
os.environ["DISABLE_BLOCK_LOOP"] = "1"
# Required by api/startup env_check (added 2026-05). Set non-empty stubs
# + skip the contract-presence check so TestClient lifespan reaches
# the routes under test rather than tripping the registry guards.
os.environ.setdefault("VALIDATOR_REGISTRY_8453", "0x" + "00" * 20)
os.environ.setdefault("VALIDATOR_REGISTRY_964", "0x" + "00" * 20)
os.environ.setdefault("SKIP_CONTRACT_PRESENCE_CHECK", "1")

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from fastapi.testclient import TestClient

from minotaur_subnet.api import services
import minotaur_subnet.api.server as api_server
from minotaur_subnet.api.server import app
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
    def __init__(self):
        self.add_calls: list[dict[str, object]] = []
        self.move_calls: list[dict[str, object]] = []

    def verify_proxy(self, permission):
        return FakeVerification(
            valid=True,
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
            execution_id="exec_api_add_1",
            permission_id=permission.permission_id,
            action=NativeBittensorAction.ADD_STAKE,
            owner_ss58=permission.owner_ss58,
            delegate_ss58=permission.delegate_ss58,
            amount_rao=amount_rao,
            status=NativeBittensorExecutionStatus.CONFIRMED,
            netuid=netuid,
            hotkey_ss58=hotkey_ss58,
            reason=reason,
            extrinsic_hash="0xapiadd",
            submitted_at=123.0,
            finalized_at=124.0,
            metadata=metadata or {},
        )

    def execute_move_stake(
        self,
        permission,
        *,
        origin_netuid: int,
        origin_hotkey_ss58: str,
        destination_netuid: int,
        destination_hotkey_ss58: str,
        amount_rao: int,
        reason: str = "",
        metadata=None,
        recent_executions=None,
    ):
        self.move_calls.append(
            {
                "permission_id": permission.permission_id,
                "origin_netuid": origin_netuid,
                "destination_netuid": destination_netuid,
                "amount_rao": amount_rao,
            }
        )
        return NativeBittensorExecutionRecord(
            execution_id="exec_api_move_1",
            permission_id=permission.permission_id,
            action=NativeBittensorAction.MOVE_STAKE,
            owner_ss58=permission.owner_ss58,
            delegate_ss58=permission.delegate_ss58,
            amount_rao=amount_rao,
            status=NativeBittensorExecutionStatus.CONFIRMED,
            origin_netuid=origin_netuid,
            origin_hotkey_ss58=origin_hotkey_ss58,
            destination_netuid=destination_netuid,
            destination_hotkey_ss58=destination_hotkey_ss58,
            reason=reason,
            extrinsic_hash="0xapimove",
            submitted_at=125.0,
            finalized_at=126.0,
            metadata=metadata or {},
        )


def teardown_function(_name):
    services.set_native_bittensor_executor(None)
    services.set_native_bittensor_delegate_allocator(None)


def test_native_permission_routes_create_list_refresh_revoke(tmp_path):
    store = AppIntentStore(store_path=tmp_path / "store.json")
    executor = FakeNativeExecutor()

    with patch.object(api_server, "store", store):
        with TestClient(app, raise_server_exceptions=False) as client:
            services.set_native_bittensor_executor(executor)
            services.set_native_bittensor_delegate_allocator(None)

            create_resp = client.post(
                "/v1/native-bittensor/permissions",
                json={
                    "owner_ss58": "5Owner111",
                    "delegate_ss58": "5Delegate111",
                    "allowed_netuids": [11],
                    "allowed_hotkeys": ["5HotkeyA"],
                    "max_rao_per_action": 1500,
                },
            )
            assert create_resp.status_code == 200
            created = create_resp.json()
            assert created["status"] == "active"
            permission_id = created["permission_id"]

            list_resp = client.get("/v1/native-bittensor/permissions?owner_ss58=5Owner111")
            assert list_resp.status_code == 200
            assert list_resp.json()["count"] == 1

            refresh_resp = client.post(f"/v1/native-bittensor/permissions/{permission_id}/refresh")
            assert refresh_resp.status_code == 200
            assert refresh_resp.json()["status"] == "active"

            revoke_resp = client.post(
                f"/v1/native-bittensor/permissions/{permission_id}/revoke",
                json={"reason": "user requested stop"},
            )
            assert revoke_resp.status_code == 200
            revoked = revoke_resp.json()
            assert revoked["status"] == "revoked"
            assert "warning" in revoked


def test_native_permission_routes_execute_and_list_audit_records(tmp_path):
    store = AppIntentStore(store_path=tmp_path / "store.json")
    executor = FakeNativeExecutor()

    with patch.object(api_server, "store", store):
        with TestClient(app, raise_server_exceptions=False) as client:
            services.set_native_bittensor_executor(executor)
            services.set_native_bittensor_delegate_allocator(None)

            create_resp = client.post(
                "/v1/native-bittensor/permissions",
                json={
                    "owner_ss58": "5Owner111",
                    "delegate_ss58": "5Delegate111",
                    "allowed_netuids": [11, 12],
                    "allowed_hotkeys": ["5HotkeyA", "5HotkeyB"],
                    "max_rao_per_action": 3000,
                },
            )
            permission_id = create_resp.json()["permission_id"]

            add_resp = client.post(
                f"/v1/native-bittensor/permissions/{permission_id}/actions/add-stake",
                json={
                    "netuid": 11,
                    "hotkey_ss58": "5HotkeyA",
                    "amount_rao": 2000,
                    "reason": "route into subnet",
                },
            )
            assert add_resp.status_code == 200
            added = add_resp.json()
            assert added["status"] == "confirmed"
            assert added["action"] == "add_stake"

            move_resp = client.post(
                f"/v1/native-bittensor/permissions/{permission_id}/actions/move-stake",
                json={
                    "origin_netuid": 11,
                    "origin_hotkey_ss58": "5HotkeyA",
                    "destination_netuid": 12,
                    "destination_hotkey_ss58": "5HotkeyB",
                    "amount_rao": 1000,
                    "reason": "rotate validators",
                },
            )
            assert move_resp.status_code == 200
            moved = move_resp.json()
            assert moved["status"] == "confirmed"
            assert moved["action"] == "move_stake"

            executions_resp = client.get(
                f"/v1/native-bittensor/executions?permission_id={permission_id}"
            )
            assert executions_resp.status_code == 200
            payload = executions_resp.json()
            assert payload["count"] == 2
            assert {item["action"] for item in payload["executions"]} == {
                "add_stake",
                "move_stake",
            }
