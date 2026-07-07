"""A failed deploy must not leave a permanent DEPLOYING record.

Live incident 2026-07-07 (Base): the relayer's deploy tx timed out unmined,
deploy_app_intent's exception path returned an error but left the stored
deployment record at DEPLOYING — the already-deployed guard then refused
every redeploy and retire_deployment refuses mid-deploy, wedging the app
with no API-level recovery.
"""
from __future__ import annotations

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
OWNER = "0x" + "44" * 20


def _store(tmp_path) -> AppIntentStore:
    s = AppIntentStore(store_path=tmp_path / "s.db")
    s.save_app(AppIntentDefinition(
        app_id="app_x", name="dex", version="1.0.0", intent_type="swap",
        js_code="function score(){return 1;}", solidity_code="contract X {}",
        config=AppIntentConfig(supported_chains=[8453]),
        deployer=OWNER,
    ))
    return s


def _failing_deploy_svc(exc: Exception):
    svc = MagicMock()

    async def _fake_deploy(defn, chain):
        raise exc

    svc.deploy = _fake_deploy
    return svc


def test_deploy_exception_rolls_record_back_to_draft(tmp_path):
    s = _store(tmp_path)
    svc = _failing_deploy_svc(RuntimeError("Relayer deploy failed: 500"))

    with patch("minotaur_subnet.api.services._state._deploy_service", svc):
        out = deploy_app_intent(s, "app_x", chain_id=8453)

    assert out["status"] == "draft"
    assert "Deploy failed" in out["error"]
    dep = s.get_deployment("app_x", chain_id=8453)
    assert dep is not None
    assert dep.status == AppStatus.DRAFT
    assert "Relayer deploy failed" in (dep.error or "")


def test_failed_deploy_can_be_retried(tmp_path):
    s = _store(tmp_path)
    svc = _failing_deploy_svc(RuntimeError("tx not in the chain after 90 seconds"))

    with patch("minotaur_subnet.api.services._state._deploy_service", svc):
        first = deploy_app_intent(s, "app_x", chain_id=8453)
    assert "error" in first

    # Second attempt must get past the already-deployed guard and reach the
    # deploy service again (here: succeed).
    ok_svc = MagicMock()

    async def _ok_deploy(defn, chain):
        return DeploymentResult(
            app_id="app_x", status=AppStatus.SOLVING, js_code_hash="x",
            chain_id=8453, contract_address=APP_ADDR,
        )

    ok_svc.deploy = _ok_deploy
    with patch("minotaur_subnet.api.services._state._deploy_service", ok_svc), \
         patch(
             "minotaur_subnet.api.services.app_lifecycle.auto_register_deployment",
             return_value={"registered": True},
         ):
        second = deploy_app_intent(s, "app_x", chain_id=8453)

    assert second.get("error") is None
    assert s.get_deployment("app_x", chain_id=8453).status == AppStatus.SOLVING


def test_mid_deploy_guard_still_blocks_concurrent_deploys(tmp_path):
    s = _store(tmp_path)
    # Simulate a genuinely in-flight deploy record
    s.save_deployment(DeploymentResult(
        app_id="app_x", status=AppStatus.DEPLOYING, js_code_hash="x",
        chain_id=8453,
    ))
    out = deploy_app_intent(s, "app_x", chain_id=8453)
    assert "already deploying" in out.get("error", "")


def test_boot_reconcile_flips_orphaned_deploying_to_draft(tmp_path):
    s = _store(tmp_path)
    s.save_deployment(DeploymentResult(
        app_id="app_x", status=AppStatus.DEPLOYING, js_code_hash="x",
        chain_id=8453,
    ))
    # An operational record on another chain must be untouched
    s.save_deployment(DeploymentResult(
        app_id="app_x", status=AppStatus.SOLVING, js_code_hash="x",
        chain_id=1, contract_address=APP_ADDR,
    ))

    flipped = s.reconcile_stale_deploying()

    assert flipped == [("app_x", 8453)]
    dep = s.get_deployment("app_x", chain_id=8453)
    assert dep.status == AppStatus.DRAFT
    assert "stale mid-deploy" in (dep.error or "")
    assert s.get_deployment("app_x", chain_id=1).status == AppStatus.SOLVING
    # Idempotent
    assert s.reconcile_stale_deploying() == []
