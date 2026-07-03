"""Route-level tests for the app-management signature-auth wiring.

Confirms the endpoints actually enforce authorize() end-to-end: no auth →
403, valid wallet signature → executes, params-mismatch → 403, and the
admin-key bypass still works during rollout.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

os.environ["DISABLE_BENCHMARK_WORKER"] = "1"
os.environ["DISABLE_BLOCK_LOOP"] = "1"
# Don't launch/wire the managed read proxy from this TestClient(app) startup —
# it exports SOLVER_READ_PROXY* via os.environ.setdefault, which leaks into the
# shared pytest process and flips test_benchmark_fail_closed onto the
# deterministic-read path (pre-existing latent ordering fragility).
os.environ["DISABLE_READ_PROXY"] = "1"
os.environ.setdefault("VALIDATOR_REGISTRY_8453", "0x" + "00" * 20)
os.environ.setdefault("VALIDATOR_REGISTRY_964", "0x" + "00" * 20)
os.environ.setdefault("SKIP_CONTRACT_PRESENCE_CHECK", "1")
# Admin gate active (relayer configured) so the bypass path is exercised too.
os.environ.setdefault("ADMIN_API_KEY", "test-admin-key")
os.environ.setdefault("RELAYER_URL", "http://relayer.invalid")

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import pytest
from eth_account import Account
from fastapi.testclient import TestClient

from minotaur_subnet.api.server import app
from minotaur_subnet.api.services import app_auth, developer_auth
from minotaur_subnet.shared.types import AppIntentConfig, AppIntentDefinition
from minotaur_subnet.store import AppIntentStore

OWNER = Account.from_key("0x" + "11" * 32)


@pytest.fixture
def client(tmp_path):
    store = AppIntentStore(store_path=tmp_path / "s.db")
    store.save_app(AppIntentDefinition(
        app_id="app_x", name="dex", version="1.0.0", intent_type="swap",
        js_code="function score(){return 1;}", solidity_code="contract X {}",
        config=AppIntentConfig(supported_chains=[8453]), deployer=OWNER.address,
    ))
    with patch("minotaur_subnet.api.routes.apps._store", return_value=store):
        with TestClient(app) as c:
            yield c, store


def _headers(action, app_id, params_hash, nonce, deadline, signer=OWNER):
    sig = developer_auth.sign_developer_auth(
        signer.key.hex(), action=action, app_id=app_id,
        params_hash=params_hash, nonce=nonce, deadline=deadline,
    )
    return {
        "X-App-Auth-Signer": signer.address,
        "X-App-Auth-Signature": sig,
        "X-App-Auth-Nonce": str(nonce),
        "X-App-Auth-Deadline": str(deadline),
    }


def _future(client):
    # deadline comfortably inside MAX_DEADLINE_FUTURE window
    import time
    return int(time.time()) + 600


def test_withdraw_no_auth_is_403(client):
    c, _ = client
    r = c.post("/v1/apps/app_x/deployments/8453/float/withdraw",
               json={"to": "0x" + "77" * 20, "amount_wei": 5})
    assert r.status_code == 403
    assert "required" in r.json()["detail"].lower()


def test_withdraw_valid_signature_passes_auth(client):
    c, _ = client
    to, amt, dl = "0x" + "77" * 20, 5, _future(c)
    ph = app_auth.params_hash_for(
        developer_auth.ACTION_FLOAT_WITHDRAW, "app_x", 8453, to, amt)
    h = _headers(developer_auth.ACTION_FLOAT_WITHDRAW, "app_x", ph, 1, dl)
    # Relayer not really configured → service returns its own error, but auth
    # PASSED (not a 403). That's the assertion: we got past the gate.
    r = c.post("/v1/apps/app_x/deployments/8453/float/withdraw",
               json={"to": to, "amount_wei": amt}, headers=h)
    # Auth passed (not 403); the service then errors because there's no
    # deployment/relayer in this test — that's past the gate, which is the point.
    assert r.status_code != 403, r.text
    assert "error" in r.json()


def test_withdraw_signature_for_other_recipient_is_403(client):
    """Sign for recipient A, submit body with recipient B → 403 (the drain
    vector). Server recomputes paramsHash from the real body."""
    c, _ = client
    dl = _future(c)
    ph_a = app_auth.params_hash_for(
        developer_auth.ACTION_FLOAT_WITHDRAW, "app_x", 8453, "0x" + "aa" * 20, 5)
    h = _headers(developer_auth.ACTION_FLOAT_WITHDRAW, "app_x", ph_a, 1, dl)
    r = c.post("/v1/apps/app_x/deployments/8453/float/withdraw",
               json={"to": "0x" + "bb" * 20, "amount_wei": 5}, headers=h)
    assert r.status_code == 403


def test_admin_key_bypass_still_works(client):
    c, _ = client
    r = c.post("/v1/apps/app_x/deployments/8453/float/withdraw",
               json={"to": "0x" + "77" * 20, "amount_wei": 5},
               headers={"X-Admin-Key": "test-admin-key"})
    assert r.status_code != 403, r.text


def test_admin_state_read_requires_auth(client):
    c, _ = client
    assert c.get("/v1/apps/app_x/admin-state").status_code == 403
    # admin key works
    r = c.get("/v1/apps/app_x/admin-state", headers={"X-Admin-Key": "test-admin-key"})
    assert r.status_code == 200
    assert r.json()["app_id"] == "app_x"


def test_retire_requires_auth_then_executes(client):
    c, store = client
    # no auth
    assert c.post("/v1/apps/app_x/deployments/8453/retire").status_code == 403
    # admin bypass → executes (returns error only because no deployment exists)
    r = c.post("/v1/apps/app_x/deployments/8453/retire",
               headers={"X-Admin-Key": "test-admin-key"})
    assert r.status_code == 200 and "No deployment" in str(r.json())


# ════════════════════════════════════════════════════════════════════════════
# Deploy auth: wallet-sig gate (exempt = free, non-exempt = fee-required),
# admin-key still free — NO shared admin key needed for a self-serve owner.
# ════════════════════════════════════════════════════════════════════════════
def test_deploy_no_auth_is_403(client):
    c, _ = client
    r = c.post("/v1/apps/app_x/deploy?chain_id=8453", json={})
    assert r.status_code == 403
    assert "required" in r.json()["detail"].lower()


def test_deploy_wallet_sig_nonexempt_is_fee_required(client, monkeypatch):
    monkeypatch.delenv("DEPLOY_FEE_EXEMPT_ADDRESSES", raising=False)
    c, _ = client
    dl = _future(c)
    ph = app_auth.params_hash_for(developer_auth.ACTION_DEPLOY, "app_x", 8453)
    h = _headers(developer_auth.ACTION_DEPLOY, "app_x", ph, 1, dl)
    r = c.post("/v1/apps/app_x/deploy?chain_id=8453", json={}, headers=h)
    # OWNER is the app's deployer → REQUEST authorized (not 403). But a
    # non-exempt deployer with no payment is refused by the #238 fee gate.
    assert r.status_code == 200, r.text
    assert r.json().get("deploy_fee_required") is True


def test_deploy_wallet_sig_exempt_signer_passes_fee_gate(client, monkeypatch):
    monkeypatch.setenv("DEPLOY_FEE_EXEMPT_ADDRESSES", OWNER.address.lower())
    c, _ = client
    dl = _future(c)
    ph = app_auth.params_hash_for(developer_auth.ACTION_DEPLOY, "app_x", 8453)
    h = _headers(developer_auth.ACTION_DEPLOY, "app_x", ph, 1, dl)
    r = c.post("/v1/apps/app_x/deploy?chain_id=8453", json={}, headers=h)
    # Exempt deployer → past BOTH auth and the fee gate, with no admin key.
    assert r.status_code == 200, r.text
    assert r.json().get("deploy_fee_required") is not True


def test_deploy_wrong_params_hash_is_403(client, monkeypatch):
    monkeypatch.setenv("DEPLOY_FEE_EXEMPT_ADDRESSES", OWNER.address.lower())
    c, _ = client
    dl = _future(c)
    # Sign for chain 999 but request 8453 → the route's paramsHash differs →
    # recovered signer ≠ OWNER → 403 (a captured sig can't be re-pointed).
    ph = app_auth.params_hash_for(developer_auth.ACTION_DEPLOY, "app_x", 999)
    h = _headers(developer_auth.ACTION_DEPLOY, "app_x", ph, 1, dl)
    r = c.post("/v1/apps/app_x/deploy?chain_id=8453", json={}, headers=h)
    assert r.status_code == 403


def test_deploy_admin_key_still_free(client):
    c, _ = client
    r = c.post("/v1/apps/app_x/deploy?chain_id=8453", json={},
               headers={"X-Admin-Key": "test-admin-key"})
    assert r.status_code == 200, r.text
    assert r.json().get("deploy_fee_required") is not True


# ════════════════════════════════════════════════════════════════════════════
# Create auth: admin key OR a self-serve owner_signature (no shared secret).
# ════════════════════════════════════════════════════════════════════════════
_CJS = (
    "const manifest = { intent_functions: [{ name: 'swap', params: [] }] };\n"
    "function score() { return 1; }\n"
    "module.exports = { score, manifest };\n"
)
_CSOL = "contract X {}"


def _create_body(**kw):
    b = dict(name="dex2", description="", supported_chains=[8453],
             js_code=_CJS, solidity_code=_CSOL)
    b.update(kw)
    return b


def test_create_no_auth_is_403(client):
    c, _ = client
    r = c.post("/v1/apps/", json=_create_body())
    assert r.status_code == 403
    d = r.json()["detail"].lower()
    assert "owner_signature" in d or "admin" in d


def test_create_admin_key_passes_gate(client):
    c, _ = client
    r = c.post("/v1/apps/", json=_create_body(),
               headers={"X-Admin-Key": "test-admin-key"})
    assert r.status_code == 200, r.text  # past the gate; create_app_intent runs


def test_create_owner_signature_no_admin_binds_owner(client):
    c, _ = client
    dl = _future(c)
    ph = app_auth.create_owner_binding_hash(_CJS.strip(), _CSOL.strip())
    sig = developer_auth.sign_developer_auth(
        OWNER.key.hex(), action=developer_auth.ACTION_CREATE_APP,
        app_id="", params_hash=ph, nonce=0, deadline=dl,
    )
    r = c.post("/v1/apps/", json=_create_body(owner_signature=sig, owner_deadline=dl))
    assert r.status_code == 200, r.text
    out = r.json()
    assert "app_id" in out, out
    # Ownership PROVEN by key + bound to the signer — no admin key used.
    assert out.get("deployer", "").lower() == OWNER.address.lower()
