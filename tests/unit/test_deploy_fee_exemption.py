"""Deploy-fee exemption allowlist (DEPLOY_FEE_EXEMPT_ADDRESSES): whitelisted
deployers skip the 0.5 TAO fee."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from minotaur_subnet.deployment.deploy_fee import (
    DeploymentFeeRequired,
    is_deploy_fee_exempt,
    quote_deployment,
    require_deployment_authorized,
)
from minotaur_subnet.shared.types import (
    AppIntentConfig,
    AppIntentDefinition,
    AppStatus,
    DeploymentResult,
)
from minotaur_subnet.store.app_intent_store import AppIntentStore

EXEMPT = "0x" + "63" * 20
OTHER = "0x" + "99" * 20


# ── the allowlist primitive ──────────────────────────────────────────────


def test_is_exempt_matches_case_insensitively(monkeypatch):
    monkeypatch.setenv("DEPLOY_FEE_EXEMPT_ADDRESSES", f"{EXEMPT.upper()}, 0xAbC")
    assert is_deploy_fee_exempt(EXEMPT.lower()) is True
    assert is_deploy_fee_exempt(EXEMPT.upper()) is True
    assert is_deploy_fee_exempt(OTHER) is False
    assert is_deploy_fee_exempt("") is False


def test_empty_env_exempts_nobody(monkeypatch):
    monkeypatch.delenv("DEPLOY_FEE_EXEMPT_ADDRESSES", raising=False)
    assert is_deploy_fee_exempt(EXEMPT) is False


# ── the gate ─────────────────────────────────────────────────────────────


def test_gate_passes_for_exempt_even_when_public_off(monkeypatch):
    monkeypatch.delenv("ENABLE_PUBLIC_DEPLOYMENT", raising=False)
    # Non-admin, unpaid, public OFF — normally refused; fee_exempt lets it pass.
    require_deployment_authorized(is_admin=False, fee_paid=False, fee_exempt=True)


def test_gate_still_refuses_non_exempt(monkeypatch):
    monkeypatch.delenv("ENABLE_PUBLIC_DEPLOYMENT", raising=False)
    try:
        require_deployment_authorized(is_admin=False, fee_paid=False, fee_exempt=False)
    except DeploymentFeeRequired:
        pass
    else:
        raise AssertionError("expected DeploymentFeeRequired")


# ── quote surfaces the waiver ────────────────────────────────────────────


def test_quote_waives_fee_for_exempt_deployer(monkeypatch):
    monkeypatch.setenv("DEPLOY_FEE_EXEMPT_ADDRESSES", EXEMPT)
    q = quote_deployment([8453], deployer=EXEMPT)
    assert q["fee_waived"] is True
    assert q["deploy_fee_tao"] == 0.0 and q["deploy_fee_rao"] == 0

    q2 = quote_deployment([8453], deployer=OTHER)
    assert q2["fee_waived"] is False
    assert q2["deploy_fee_tao"] == 0.5


# ── end-to-end through deploy_app_intent ─────────────────────────────────


def _store_with_app(tmp_path, deployer):
    s = AppIntentStore(store_path=tmp_path / "s.db")
    s.save_app(AppIntentDefinition(
        app_id="app_x", name="dex", version="1.0.0", intent_type="swap",
        js_code="x", solidity_code="x",
        config=AppIntentConfig(supported_chains=[8453]),
        deployer=deployer, registration_status="approved",
    ))
    return s


def _deploy_svc():
    svc = MagicMock()
    async def _fake(defn, chain):
        return DeploymentResult(app_id="app_x", status=AppStatus.SOLVING,
                                js_code_hash="x", chain_id=8453,
                                contract_address="0x" + "22" * 20)
    svc.deploy = _fake
    return svc


def test_exempt_deployer_deploys_without_payment(tmp_path, monkeypatch):
    from minotaur_subnet.api.services.app_service import deploy_app_intent

    monkeypatch.setenv("DEPLOY_FEE_EXEMPT_ADDRESSES", EXEMPT)
    monkeypatch.delenv("ENABLE_PUBLIC_DEPLOYMENT", raising=False)
    s = _store_with_app(tmp_path, deployer=EXEMPT)

    with patch("minotaur_subnet.api.services._state._deploy_service", _deploy_svc()), \
         patch("minotaur_subnet.api.services.app_lifecycle.auto_register_deployment",
               return_value={"registered": True}):
        # Non-admin (public), NO payment — allowed only because EXEMPT is waived.
        out = deploy_app_intent(s, "app_x", chain_id=8453, is_admin=False)

    assert not out.get("error"), out
    assert out.get("fee_exempt") is True
    assert out.get("contract_address")


def test_non_exempt_public_deploy_still_blocked(tmp_path, monkeypatch):
    from minotaur_subnet.api.services.app_service import deploy_app_intent

    monkeypatch.setenv("DEPLOY_FEE_EXEMPT_ADDRESSES", EXEMPT)
    monkeypatch.delenv("ENABLE_PUBLIC_DEPLOYMENT", raising=False)
    s = _store_with_app(tmp_path, deployer=OTHER)  # not exempt

    out = deploy_app_intent(s, "app_x", chain_id=8453, is_admin=False)
    assert out.get("deploy_fee_required") is True
