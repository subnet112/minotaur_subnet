"""Tests for the dual-signed EVM-deployer ↔ SS58-coldkey link (developer_link):
the service, the store, and the HTTP route."""

from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

# Ensure repo root is importable
_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from bittensor_wallet.keypair import Keypair
from eth_account import Account

from minotaur_subnet.api.services import developer_auth as da
from minotaur_subnet.api.services import developer_link as dl
from minotaur_subnet.shared.types import AppIntentDefinition
from minotaur_subnet.store.app_intent_store import AppIntentStore

DEPLOYER_PK = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
DEPLOYER = Account.from_key(DEPLOYER_PK).address
OTHER_PK = "0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d"


def _evm_sign(app_id, ss58, nonce, deadline, *, pk=DEPLOYER_PK):
    return da.sign_developer_auth(
        pk, action=da.ACTION_LINK_SS58, app_id=app_id,
        params_hash=da.params_hash(ss58.encode()), nonce=nonce, deadline=deadline,
    )


def _ss58_sign(keypair, app_id, deployer, nonce):
    return keypair.sign(dl.link_message(app_id, deployer, nonce).encode("utf-8")).hex()


class TestDeveloperLinkService(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = AppIntentStore(Path(self._tmp.name) / "s.db")
        self.app_id = "app-link"
        self.deadline = int(time.time()) + 300
        self.cold = Keypair.create_from_uri("//Alice")
        self.ss58 = self.cold.ss58_address

    def tearDown(self):
        self._tmp.cleanup()

    def _seed(self, deployer=DEPLOYER):
        self.store.save_app(AppIntentDefinition(
            app_id=self.app_id, name="L", version="1.0.0", intent_type="swap",
            js_code="x", deployer=deployer,
        ))

    def _link(self, *, ss58=None, nonce=1, deadline=None, evm_sig=None, ss58_sig=None,
              evm_pk=DEPLOYER_PK, ss58_keypair=None, ss58_for_evm=None):
        ss58 = self.ss58 if ss58 is None else ss58
        deadline = self.deadline if deadline is None else deadline
        kp = ss58_keypair or self.cold
        evm_sig = evm_sig if evm_sig is not None else _evm_sign(
            self.app_id, ss58_for_evm or ss58, nonce, deadline, pk=evm_pk)
        ss58_sig = ss58_sig if ss58_sig is not None else _ss58_sign(
            kp, self.app_id, DEPLOYER, nonce)
        return dl.link_payer_ss58(
            self.store, self.app_id, ss58, nonce=nonce, deadline=deadline,
            evm_signature=evm_sig, ss58_signature=ss58_sig,
        )

    def test_happy_path_links_and_consumes_nonce(self):
        self._seed()
        ok, err = self._link()
        self.assertTrue(ok, err)
        self.assertEqual(self.store.get_payer_ss58(self.app_id), self.ss58)
        self.assertEqual(self.store.get_developer_nonce(self.app_id, DEPLOYER), 1)

    def test_evm_wrong_signer_rejected_nonce_untouched(self):
        self._seed()
        ok, _ = self._link(evm_pk=OTHER_PK)
        self.assertFalse(ok)
        self.assertEqual(self.store.get_developer_nonce(self.app_id, DEPLOYER), 0)
        self.assertEqual(self.store.get_payer_ss58(self.app_id), "")

    def test_evm_signed_for_different_ss58_rejected(self):
        # EVM authorizes Bob's ss58 but the request links Alice's → params mismatch.
        self._seed()
        bob = Keypair.create_from_uri("//Bob")
        ok, _ = self._link(ss58_for_evm=bob.ss58_address)
        self.assertFalse(ok)

    def test_ss58_wrong_coldkey_rejected_nonce_untouched(self):
        # A different coldkey signs while the request claims Alice's ss58.
        self._seed()
        bob = Keypair.create_from_uri("//Bob")
        ok, _ = self._link(ss58_keypair=bob)
        self.assertFalse(ok)
        self.assertEqual(self.store.get_developer_nonce(self.app_id, DEPLOYER), 0)

    def test_ss58_tampered_message_rejected(self):
        # Coldkey signs over the wrong nonce → verification fails.
        self._seed()
        bad_sig = _ss58_sign(self.cold, self.app_id, DEPLOYER, 999)
        ok, _ = self._link(ss58_sig=bad_sig)
        self.assertFalse(ok)

    def test_malformed_ss58_signature_rejected_gracefully(self):
        self._seed()
        ok, err = self._link(ss58_sig="not-hex")
        self.assertFalse(ok)
        self.assertIn("hex", err)

    def test_evm_expired_deadline_rejected(self):
        self._seed()
        past = int(time.time()) - 1
        ok, err = self._link(deadline=past)
        self.assertFalse(ok)

    def test_replay_rejected(self):
        self._seed()
        self.assertTrue(self._link(nonce=1)[0])
        ok, _ = self._link(nonce=1)  # same nonce reused
        self.assertFalse(ok)

    def test_no_deployer_rejected(self):
        self._seed(deployer="")
        ok, err = self._link()
        self.assertFalse(ok)
        self.assertIn("deployer", err)

    def test_missing_ss58_rejected(self):
        self._seed()
        ok, err = dl.link_payer_ss58(
            self.store, self.app_id, "", nonce=1, deadline=self.deadline,
            evm_signature="x", ss58_signature="y",
        )
        self.assertFalse(ok)


class TestPayerSS58Store(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = AppIntentStore(Path(self._tmp.name) / "s.db")

    def tearDown(self):
        self._tmp.cleanup()

    def test_default_empty(self):
        self.assertEqual(self.store.get_payer_ss58("nope"), "")

    def test_set_get_and_relink(self):
        self.store.set_payer_ss58("a", DEPLOYER, "5Alice")
        self.assertEqual(self.store.get_payer_ss58("a"), "5Alice")
        self.store.set_payer_ss58("a", DEPLOYER, "5Bob")  # re-link replaces
        self.assertEqual(self.store.get_payer_ss58("a"), "5Bob")


class TestLinkSS58Route(unittest.TestCase):
    def setUp(self):
        self._env_prev = {
            k: os.environ.get(k) for k in ("LOCAL_TESTNET", "RELAYER_URL", "ADMIN_API_KEY")
        }
        self.addCleanup(self._restore_env)
        os.environ["LOCAL_TESTNET"] = "1"
        os.environ.pop("RELAYER_URL", None)
        os.environ.pop("ADMIN_API_KEY", None)
        from fastapi.testclient import TestClient
        from minotaur_subnet.api.server import app, store as server_store
        self.client = TestClient(app, raise_server_exceptions=False)
        self.store = server_store
        import uuid
        self.app_id = f"link-route-{uuid.uuid4().hex[:8]}"
        self.store.save_app(AppIntentDefinition(
            app_id=self.app_id, name="L", version="1.0.0", intent_type="swap",
            js_code="x", deployer=DEPLOYER,
        ))
        self.cold = Keypair.create_from_uri("//Alice")
        self.deadline = int(time.time()) + 300

    def _restore_env(self):
        for k, v in self._env_prev.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_link_route_dual_signed_then_read(self):
        ss58 = self.cold.ss58_address
        body = {
            "ss58": ss58, "nonce": 1, "deadline": self.deadline,
            "evm_signature": _evm_sign(self.app_id, ss58, 1, self.deadline),
            "ss58_signature": _ss58_sign(self.cold, self.app_id, DEPLOYER, 1),
        }
        resp = self.client.post(f"/v1/apps/{self.app_id}/link-ss58", json=body)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json().get("status"), "linked", resp.json())

        got = self.client.get(f"/v1/apps/{self.app_id}/payer-ss58")
        self.assertEqual(got.json().get("payer_ss58"), ss58)

    def test_link_route_bad_ss58_sig_errors(self):
        ss58 = self.cold.ss58_address
        bob = Keypair.create_from_uri("//Bob")
        body = {
            "ss58": ss58, "nonce": 1, "deadline": self.deadline,
            "evm_signature": _evm_sign(self.app_id, ss58, 1, self.deadline),
            "ss58_signature": _ss58_sign(bob, self.app_id, DEPLOYER, 1),  # wrong coldkey
        }
        resp = self.client.post(f"/v1/apps/{self.app_id}/link-ss58", json=body)
        self.assertIn("Link failed", resp.json().get("error", ""))


if __name__ == "__main__":
    unittest.main()
