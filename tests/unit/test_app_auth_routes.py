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
