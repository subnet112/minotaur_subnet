"""#238 App deployment fee: config + quote + the public-deployment hard gate.

Admin deploys stay free (as today). A PUBLIC/3rd-party deploy is structurally
refused until public deployment is enabled AND the fee is collected — collection
is not wired yet, so public deploys are blocked.
"""
import pytest

from minotaur_subnet.deployment.deploy_fee import (
    DEFAULT_DEPLOY_FEE_TAO,
    RAO_PER_TAO,
    DeploymentFeeRequired,
    deploy_fee_rao,
    deploy_fee_tao,
    public_deployment_enabled,
    quote_deployment,
    require_deployment_authorized,
)


# ── config ──────────────────────────────────────────────────────────────────

def test_default_fee_is_half_tao(monkeypatch):
    monkeypatch.delenv("DEPLOY_FEE_TAO", raising=False)
    assert deploy_fee_tao() == DEFAULT_DEPLOY_FEE_TAO == 0.5
    assert deploy_fee_rao() == int(0.5 * RAO_PER_TAO)


def test_fee_is_env_tunable(monkeypatch):
    monkeypatch.setenv("DEPLOY_FEE_TAO", "2")
    assert deploy_fee_tao() == 2.0
    assert deploy_fee_rao() == 2 * RAO_PER_TAO


def test_bad_fee_env_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("DEPLOY_FEE_TAO", "not-a-number")
    assert deploy_fee_tao() == DEFAULT_DEPLOY_FEE_TAO


def test_negative_fee_clamped_to_zero(monkeypatch):
    monkeypatch.setenv("DEPLOY_FEE_TAO", "-5")
    assert deploy_fee_tao() == 0.0


def test_public_deployment_off_by_default(monkeypatch):
    monkeypatch.delenv("ENABLE_PUBLIC_DEPLOYMENT", raising=False)
    assert public_deployment_enabled() is False
    monkeypatch.setenv("ENABLE_PUBLIC_DEPLOYMENT", "1")
    assert public_deployment_enabled() is True
    monkeypatch.setenv("ENABLE_PUBLIC_DEPLOYMENT", "off")
    assert public_deployment_enabled() is False


# ── quote ───────────────────────────────────────────────────────────────────

def test_quote_lists_gas_per_chain_and_fee(monkeypatch):
    monkeypatch.delenv("DEPLOY_FEE_TAO", raising=False)
    monkeypatch.delenv("DEPLOY_GAS_ESTIMATE", raising=False)
    q = quote_deployment([8453, 1])
    assert set(q["gas"].keys()) == {"8453", "1"}
    assert q["gas"]["8453"]["estimated_gas"] > 0
    assert "gas_cost_wei" not in q["gas"]["8453"]  # no price injected
    assert q["deploy_fee_tao"] == 0.5
    assert q["fee_collection_enabled"] is False


def test_quote_computes_cost_when_gas_price_supplied():
    q = quote_deployment([8453], gas_price_wei_by_chain={8453: 1_000_000_000})
    entry = q["gas"]["8453"]
    assert entry["gas_price_wei"] == 1_000_000_000
    assert entry["gas_cost_wei"] == entry["estimated_gas"] * 1_000_000_000


# ── hard gate ───────────────────────────────────────────────────────────────

def test_admin_deploy_always_allowed(monkeypatch):
    monkeypatch.delenv("ENABLE_PUBLIC_DEPLOYMENT", raising=False)
    require_deployment_authorized(is_admin=True)  # no raise


def test_public_deploy_blocked_when_disabled(monkeypatch):
    monkeypatch.delenv("ENABLE_PUBLIC_DEPLOYMENT", raising=False)
    with pytest.raises(DeploymentFeeRequired, match="disabled"):
        require_deployment_authorized(is_admin=False, fee_paid=True)


def test_public_deploy_blocked_without_fee(monkeypatch):
    monkeypatch.setenv("ENABLE_PUBLIC_DEPLOYMENT", "1")
    with pytest.raises(DeploymentFeeRequired, match="deploy fee"):
        require_deployment_authorized(is_admin=False, fee_paid=False)


def test_public_deploy_allowed_when_enabled_and_paid(monkeypatch):
    monkeypatch.setenv("ENABLE_PUBLIC_DEPLOYMENT", "1")
    require_deployment_authorized(is_admin=False, fee_paid=True)  # no raise


# ── service integration: the gate short-circuits before any deploy work ───────

def test_service_refuses_public_deploy(monkeypatch):
    from minotaur_subnet.api.services.app_service import deploy_app_intent

    monkeypatch.delenv("ENABLE_PUBLIC_DEPLOYMENT", raising=False)
    # is_admin=False with a non-empty app_id: the gate must reject before any
    # store lookup, so a bare object with no methods is enough.
    out = deploy_app_intent(object(), "app_x", is_admin=False)
    assert out.get("deploy_fee_required") is True
    assert "disabled" in out["error"]
