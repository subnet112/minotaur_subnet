"""Route-level tests for registration moderation auth: request is owner-
signable, approve/reject are ADMIN-ONLY (the owner cannot approve their own
app)."""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from unittest.mock import patch

os.environ["DISABLE_BENCHMARK_WORKER"] = "1"
os.environ["DISABLE_BLOCK_LOOP"] = "1"
os.environ["DISABLE_READ_PROXY"] = "1"
os.environ.setdefault("VALIDATOR_REGISTRY_8453", "0x" + "00" * 20)
os.environ.setdefault("VALIDATOR_REGISTRY_964", "0x" + "00" * 20)
os.environ.setdefault("SKIP_CONTRACT_PRESENCE_CHECK", "1")
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
from minotaur_subnet.shared.types import (
    AppIntentConfig,
    AppIntentDefinition,
    AppStatus,
    DeploymentResult,
)
from minotaur_subnet.store import AppIntentStore

OWNER = Account.from_key("0x" + "11" * 32)
ADMIN = Account.from_key("0x" + "22" * 32)


@pytest.fixture
def client(tmp_path, monkeypatch):
    # ADMIN wallet is the only APP_ADMIN_SIGNER; OWNER is the app deployer.
    monkeypatch.setenv("APP_ADMIN_SIGNERS", ADMIN.address)
    store = AppIntentStore(store_path=tmp_path / "s.db")
    store.save_app(AppIntentDefinition(
        app_id="app_x", name="dex", version="1.0.0", intent_type="swap",
        js_code="function score(){return 1;}", solidity_code="contract X {}",
        config=AppIntentConfig(supported_chains=[8453]),
        deployer=OWNER.address, registration_status="unrequested",
    ))
    store.save_deployment(DeploymentResult(
        app_id="app_x", status=AppStatus.SOLVING, js_code_hash="x",
        chain_id=8453, contract_address="0x" + "22" * 20,
    ))
    with patch("minotaur_subnet.api.routes.apps._store", return_value=store):
        with TestClient(app) as c:
            yield c, store


def _sig_headers(wallet, action, app_id, *parts, nonce=0, chain=None):
    ph = app_auth.params_hash_for(action, app_id, chain, *parts)
    deadline = int(time.time()) + 600
    sig = developer_auth.sign_developer_auth(
        wallet.key.hex(), action=action, app_id=app_id,
        params_hash=ph, nonce=nonce, deadline=deadline)
    h = {"X-App-Auth-Signer": wallet.address, "X-App-Auth-Signature": sig,
         "X-App-Auth-Deadline": str(deadline)}
    if nonce:
        h["X-App-Auth-Nonce"] = str(nonce)
    return h


def _nonce(client, addr):
    c, _ = client
    return c.get(f"/v1/apps/app_x/auth-nonce?deployer={addr}").json()["next_nonce"]


# ── request: owner-signable ──────────────────────────────────────────────


def test_owner_can_request_registration(client):
    c, store = client
    n = _nonce(client, OWNER.address)
    h = _sig_headers(OWNER, developer_auth.ACTION_REQUEST_REGISTRATION,
                     "app_x", "", nonce=n)
    r = c.post("/v1/apps/app_x/registration/request", json={"note": ""}, headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["registration_status"] == "requested"


def test_request_without_auth_is_403(client):
    c, _ = client
    r = c.post("/v1/apps/app_x/registration/request", json={"note": ""})
    assert r.status_code == 403


# ── approve/reject: ADMIN ONLY ───────────────────────────────────────────


def test_owner_cannot_approve_own_app(client):
    """The core property: an app owner's signature does NOT authorize approval
    — only APP_ADMIN_SIGNERS can approve."""
    c, _ = client
    n = _nonce(client, OWNER.address)
    h = _sig_headers(OWNER, developer_auth.ACTION_APPROVE_REGISTRATION,
                     "app_x", nonce=n)
    r = c.post("/v1/apps/app_x/registration/approve", headers=h)
    assert r.status_code == 403
    assert "allowed signer" in r.json()["detail"].lower()


def test_admin_signer_can_approve(client):
    c, store = client
    n = _nonce(client, ADMIN.address)
    h = _sig_headers(ADMIN, developer_auth.ACTION_APPROVE_REGISTRATION,
                     "app_x", nonce=n)
    with patch("minotaur_subnet.api.services.app_lifecycle.auto_register_deployment",
               return_value={"registered": True}):
        r = c.post("/v1/apps/app_x/registration/approve", headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["registration_status"] == "approved"
    assert store.get_app("app_x").registration_status == "approved"


def test_admin_key_can_approve(client):
    c, _ = client
    with patch("minotaur_subnet.api.services.app_lifecycle.auto_register_deployment",
               return_value={"registered": True}):
        r = c.post("/v1/apps/app_x/registration/approve",
                   headers={"X-Admin-Key": "test-admin-key"})
    assert r.status_code == 200, r.text


def test_admin_can_reject(client):
    c, store = client
    r = c.post("/v1/apps/app_x/registration/reject",
               json={"reason": "bad"}, headers={"X-Admin-Key": "test-admin-key"})
    assert r.status_code == 200
    assert store.get_app("app_x").registration_status == "rejected"


def test_owner_cannot_reject(client):
    c, _ = client
    n = _nonce(client, OWNER.address)
    h = _sig_headers(OWNER, developer_auth.ACTION_REJECT_REGISTRATION,
                     "app_x", "", nonce=n)
    r = c.post("/v1/apps/app_x/registration/reject", json={"reason": ""}, headers=h)
    assert r.status_code == 403
