"""Async deploy (#609): the deploy route returns immediately; clients poll.

The synchronous deploy held the HTTP connection for the full compile →
relayer tx → confirmation chain (~85s on mainnet) — longer than proxy and
client timeouts, so browsers saw a dropped connection while the deploy
succeeded server-side. background=True dispatches the chain to a daemon
thread after guards + fee auth and returns a ``deploying`` acknowledgement.
"""
from __future__ import annotations

import threading
import time

from unittest.mock import MagicMock, patch

from minotaur_subnet.api.services.app_service import deploy_app_intent
from minotaur_subnet.shared.types import (
    AppIntentConfig,
    AppIntentDefinition,
    AppStatus,
    DeploymentResult,
)
from minotaur_subnet.store.app_intent_store import AppIntentStore

APP_ADDR = "0x" + "22" * 20


def _store(tmp_path) -> AppIntentStore:
    s = AppIntentStore(store_path=tmp_path / "s.db")
    s.save_app(AppIntentDefinition(
        app_id="app_x", name="dex", version="1.0.0", intent_type="swap",
        js_code="function score(){return 1;}", solidity_code="contract X {}",
        config=AppIntentConfig(supported_chains=[8453]),
        deployer="0x" + "44" * 20,
    ))
    return s


def _wait_for_status(store, want: AppStatus, deadline: float = 5.0) -> DeploymentResult:
    end = time.monotonic() + deadline
    while time.monotonic() < end:
        dep = store.get_deployment("app_x", chain_id=8453)
        if dep is not None and dep.status == want:
            return dep
        time.sleep(0.01)
    raise AssertionError(
        f"deployment never reached {want}; last = "
        f"{store.get_deployment('app_x', chain_id=8453)}"
    )


def test_background_deploy_returns_immediately_then_persists_success(tmp_path):
    s = _store(tmp_path)
    release = threading.Event()

    svc = MagicMock()

    async def _slow_deploy(defn, chain):
        release.wait(timeout=5)
        return DeploymentResult(
            app_id="app_x", status=AppStatus.SOLVING, js_code_hash="x",
            chain_id=8453, contract_address=APP_ADDR,
        )

    svc.deploy = _slow_deploy

    with patch("minotaur_subnet.api.services._state._deploy_service", svc):
        out = deploy_app_intent(s, "app_x", chain_id=8453, background=True)

        # Immediate acknowledgement, not the deploy result
        assert out["status"] == "deploying"
        assert out["chain_id"] == 8453
        assert out["poll"] == "/v1/apps/app_x/status"
        assert "contract_address" not in out

        # Record is DEPLOYING while the worker is still in flight
        dep = s.get_deployment("app_x", chain_id=8453)
        assert dep.status == AppStatus.DEPLOYING

        # A concurrent deploy attempt is refused while in flight
        second = deploy_app_intent(s, "app_x", chain_id=8453, background=True)
        assert "already deploying" in second.get("error", "")

        release.set()
        dep = _wait_for_status(s, AppStatus.SOLVING)
        assert dep.contract_address == APP_ADDR


def test_background_deploy_failure_rolls_back_to_draft(tmp_path):
    s = _store(tmp_path)
    svc = MagicMock()

    async def _boom(defn, chain):
        raise RuntimeError("relayer rejected: no ValidatorRegistry on chain")

    svc.deploy = _boom

    with patch("minotaur_subnet.api.services._state._deploy_service", svc):
        out = deploy_app_intent(s, "app_x", chain_id=8453, background=True)
        assert out["status"] == "deploying"

        dep = _wait_for_status(s, AppStatus.DRAFT)
        assert "relayer rejected" in (dep.error or "")


def test_sync_path_unchanged_by_default(tmp_path):
    s = _store(tmp_path)
    svc = MagicMock()

    async def _ok(defn, chain):
        return DeploymentResult(
            app_id="app_x", status=AppStatus.SOLVING, js_code_hash="x",
            chain_id=8453, contract_address=APP_ADDR,
        )

    svc.deploy = _ok

    with patch("minotaur_subnet.api.services._state._deploy_service", svc), \
         patch(
             "minotaur_subnet.api.services.app_lifecycle.auto_register_deployment",
             return_value={"registered": True},
         ):
        out = deploy_app_intent(s, "app_x", chain_id=8453)  # background defaults False

    # Full synchronous result, exactly as before
    assert out["contract_address"] == APP_ADDR
    assert out["status"] == AppStatus.SOLVING
    assert s.get_deployment("app_x", chain_id=8453).status == AppStatus.SOLVING


def test_background_guard_failures_stay_synchronous(tmp_path):
    s = _store(tmp_path)
    # Unknown chain must be rejected in-request, never handed to the thread
    svc = MagicMock()
    with patch("minotaur_subnet.api.services._state._deploy_service", svc):
        out = deploy_app_intent(s, "app_x", chain_id=999, background=True)
    assert "not in app's supported_chains" in out["error"]
    assert s.get_deployment("app_x", chain_id=999) is None
