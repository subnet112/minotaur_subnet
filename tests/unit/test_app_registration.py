"""Tests for app registration moderation (permissionless deploy, gated
activation): request/approve/reject + the deploy-time auto-register gate."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from minotaur_subnet.api.services import app_registration as reg
from minotaur_subnet.api.services.app_registration import (
    REG_APPROVED,
    REG_REJECTED,
    REG_REQUESTED,
    REG_UNREQUESTED,
    approve_registration,
    registration_allows_autoregister,
    reject_registration,
    request_registration,
)
from minotaur_subnet.shared.types import (
    AppIntentConfig,
    AppIntentDefinition,
    AppStatus,
    DeploymentResult,
)
from minotaur_subnet.store.app_intent_store import AppIntentStore

APP_ADDR = "0x" + "22" * 20
OWNER = "0x" + "44" * 20


def _store(tmp_path, *, status="unrequested", deployed=True) -> AppIntentStore:
    s = AppIntentStore(store_path=tmp_path / "s.db")
    s.save_app(AppIntentDefinition(
        app_id="app_x", name="dex", version="1.0.0", intent_type="swap",
        js_code="function score(){return 1;}", solidity_code="contract X {}",
        config=AppIntentConfig(supported_chains=[8453]),
        deployer=OWNER, registration_status=status,
    ))
    if deployed:
        s.save_deployment(DeploymentResult(
            app_id="app_x", status=AppStatus.SOLVING, js_code_hash="x",
            chain_id=8453, contract_address=APP_ADDR,
        ))
    return s


# ── the auto-register gate ───────────────────────────────────────────────


def test_autoregister_gate():
    assert registration_allows_autoregister(REG_APPROVED) is True
    assert registration_allows_autoregister("") is True          # legacy grandfathered
    assert registration_allows_autoregister(REG_UNREQUESTED) is False
    assert registration_allows_autoregister(REG_REQUESTED) is False
    assert registration_allows_autoregister(REG_REJECTED) is False


# ── request ──────────────────────────────────────────────────────────────


def test_request_flips_unrequested_to_requested(tmp_path):
    s = _store(tmp_path, status="unrequested")
    out = request_registration(s, "app_x", note="please review")
    assert out["registration_status"] == REG_REQUESTED and out["changed"] is True
    d = s.get_app("app_x")
    assert d.registration_status == REG_REQUESTED
    assert d.policy_metadata["registration"]["request_note"] == "please review"


def test_request_requires_a_deployment(tmp_path):
    s = _store(tmp_path, status="unrequested", deployed=False)
    out = request_registration(s, "app_x")
    assert "error" in out and "Deploy" in out["error"]
    assert s.get_app("app_x").registration_status == REG_UNREQUESTED


def test_request_on_approved_is_noop(tmp_path):
    s = _store(tmp_path, status=REG_APPROVED)
    out = request_registration(s, "app_x")
    assert out["changed"] is False and out["registration_status"] == REG_APPROVED


# ── approve → registers ──────────────────────────────────────────────────


def test_approve_marks_approved_and_registers_each_chain(tmp_path):
    s = _store(tmp_path, status=REG_REQUESTED)
    with patch(
        "minotaur_subnet.api.services.app_lifecycle.auto_register_deployment",
        return_value={"registered": True},
    ) as areg:
        out = approve_registration(s, "app_x", reviewer="0xADMIN")
    assert out["registration_status"] == REG_APPROVED
    assert out["registry"][8453] == {"registered": True}
    areg.assert_called_once_with(s, "app_x", 8453, APP_ADDR)
    d = s.get_app("app_x")
    assert d.registration_status == REG_APPROVED
    assert d.policy_metadata["registration"]["approved_by"] == "0xADMIN"


def test_approve_without_deployment_errors(tmp_path):
    s = _store(tmp_path, status=REG_REQUESTED, deployed=False)
    assert "error" in approve_registration(s, "app_x")


def test_reject_marks_rejected(tmp_path):
    s = _store(tmp_path, status=REG_REQUESTED)
    out = reject_registration(s, "app_x", reason="malicious solidity", reviewer="0xADMIN")
    assert out["registration_status"] == REG_REJECTED
    d = s.get_app("app_x")
    assert d.registration_status == REG_REJECTED
    assert d.policy_metadata["registration"]["reject_reason"] == "malicious solidity"


# ── deploy-time gating (the boundary) ────────────────────────────────────


def test_deploy_of_unapproved_app_does_not_autoregister(tmp_path):
    from minotaur_subnet.api.services.app_service import deploy_app_intent

    s = _store(tmp_path, status=REG_UNREQUESTED, deployed=False)
    deploy_svc = MagicMock()
    deploy_svc.deploy = MagicMock()
    # Make _deploy_service.deploy return a successful DeploymentResult.
    async def _fake_deploy(defn, chain):
        return DeploymentResult(app_id="app_x", status=AppStatus.SOLVING,
                                js_code_hash="x", chain_id=8453,
                                contract_address=APP_ADDR)
    deploy_svc.deploy = _fake_deploy

    with patch("minotaur_subnet.api.services._state._deploy_service", deploy_svc), \
         patch("minotaur_subnet.api.services.app_lifecycle.auto_register_deployment") as areg:
        out = deploy_app_intent(s, "app_x", chain_id=8453)

    assert out.get("registry", {}).get("pending_approval") is True
    areg.assert_not_called()


def test_deploy_of_approved_app_autoregisters(tmp_path):
    from minotaur_subnet.api.services.app_service import deploy_app_intent

    s = _store(tmp_path, status=REG_APPROVED, deployed=False)
    deploy_svc = MagicMock()
    async def _fake_deploy(defn, chain):
        return DeploymentResult(app_id="app_x", status=AppStatus.SOLVING,
                                js_code_hash="x", chain_id=8453,
                                contract_address=APP_ADDR)
    deploy_svc.deploy = _fake_deploy

    with patch("minotaur_subnet.api.services._state._deploy_service", deploy_svc), \
         patch("minotaur_subnet.api.services.app_lifecycle.auto_register_deployment",
               return_value={"registered": True}) as areg:
        out = deploy_app_intent(s, "app_x", chain_id=8453)

    assert out.get("registry") == {"registered": True}
    areg.assert_called_once()
