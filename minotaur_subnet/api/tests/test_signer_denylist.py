"""Tests for the compromised-signer / banned-app_id denylist.

Regression guard for the 2026-07-18 breach: two leaked operator EOAs
(0x63AeEF52 "MinoDeployer", 0xD4cF78 old owner) were reused by an external
attacker to re-sign a credential-exfil scoring payload against the public
App-management API (which authorizes by allowed-signer wallet signature, no
admin key). These keys must never authorize an action or be an app deployer,
and the hard-deleted malicious app_id must never be re-persisted.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from minotaur_subnet.api.services import app_auth
from minotaur_subnet.shared import signer_denylist
from minotaur_subnet.shared.types import AppIntentDefinition
from minotaur_subnet.store import AppIntentStore

DENY_63A = "0x63AeEF526406be8d1aF89023422A455b4d8e130B"  # MinoDeployer (leaked)
DENY_D4 = "0xD4cF78059243fAED77350f2dD7e73d5300465D70"   # old owner (compromised)
GOOD = "0x7dC30109A32764f808823095C576A0355b7978d6"      # rotated good owner
BANNED_APP = "app_da6c96b84c60"                           # purged exfil app


class TestDenylistModule(unittest.TestCase):
    def test_compromised_signers_denylisted_any_case(self):
        for a in (DENY_63A, DENY_63A.lower(), DENY_63A.upper(), DENY_D4, DENY_D4.lower()):
            self.assertTrue(signer_denylist.is_signer_denylisted(a), a)

    def test_good_owner_not_denylisted(self):
        self.assertFalse(signer_denylist.is_signer_denylisted(GOOD))
        self.assertFalse(signer_denylist.is_signer_denylisted(""))
        self.assertFalse(signer_denylist.is_signer_denylisted(None))

    def test_banned_app_id_any_case(self):
        self.assertTrue(signer_denylist.is_app_id_banned(BANNED_APP))
        self.assertTrue(signer_denylist.is_app_id_banned(BANNED_APP.upper()))
        self.assertFalse(signer_denylist.is_app_id_banned("app_0867cdd4effd"))

    def test_env_extends_signer_denylist(self):
        extra = "0x00000000000000000000000000000000deadbeef"
        self.assertFalse(signer_denylist.is_signer_denylisted(extra))
        with patch.dict("os.environ", {"SIGNER_DENYLIST": f"{extra},0xabc"}):
            self.assertTrue(signer_denylist.is_signer_denylisted(extra))
            self.assertTrue(signer_denylist.is_signer_denylisted(extra.upper()))
        # Hardcoded set survives regardless of env.
        self.assertTrue(signer_denylist.is_signer_denylisted(DENY_63A))

    def test_env_extends_app_id_ban(self):
        with patch.dict("os.environ", {"APP_ID_DENYLIST": "app_evil"}):
            self.assertTrue(signer_denylist.is_app_id_banned("app_evil"))


class TestAllowedSignersFiltering(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = AppIntentStore(Path(self._tmp.name) / "store.db")

    def tearDown(self):
        self._tmp.cleanup()

    def _save(self, app_id: str, deployer: str) -> None:
        # NOTE: goes through save_app, so deployer must not be denylisted here.
        self.store.save_app(AppIntentDefinition(
            app_id=app_id, name="n", version="1.0.0", intent_type="swap",
            js_code="module.exports={score:()=>({score:1,valid:true})}",
            deployer=deployer,
        ))

    def test_denylisted_admin_signer_is_filtered_out(self):
        self._save("app1", GOOD)
        with patch.dict("os.environ", {"APP_ADMIN_SIGNERS": f"{GOOD},{DENY_D4}"}):
            allowed = app_auth.allowed_signers(self.store, "app1")
        self.assertIn(GOOD.lower(), allowed)
        self.assertNotIn(DENY_D4.lower(), allowed)


class TestAuthorizeRejectsDenylistedSigner(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = AppIntentStore(Path(self._tmp.name) / "store.db")
        # App owned by the GOOD key; attacker tries to sign as a denylisted key.
        self.store.save_app(AppIntentDefinition(
            app_id="app1", name="n", version="1.0.0", intent_type="swap",
            js_code="module.exports={score:()=>({score:1,valid:true})}",
            deployer=GOOD,
        ))

    def tearDown(self):
        self._tmp.cleanup()

    def test_denylisted_signer_rejected_before_nonce_consumed(self):
        auth = app_auth.AuthBlock(
            signer=DENY_63A, signature="0x" + "11" * 65, nonce=1, deadline=9_999_999_999,
        )
        ok, err, signer = app_auth.authorize(
            self.store, "app1", action="update_scoring",
            params_hash=b"x" * 32, auth=auth, admin_ok=False,
        )
        self.assertFalse(ok)
        self.assertIn("denylisted", err)
        # Nonce must NOT have advanced (rejected before consume).
        self.assertEqual(self.store.get_developer_nonce("app1", DENY_63A), 0)


class TestSaveAppBackstop(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = AppIntentStore(Path(self._tmp.name) / "store.db")

    def tearDown(self):
        self._tmp.cleanup()

    def _def(self, app_id: str, deployer: str) -> AppIntentDefinition:
        return AppIntentDefinition(
            app_id=app_id, name="n", version="1.0.0", intent_type="swap",
            js_code="module.exports={score:()=>({score:1,valid:true})}",
            deployer=deployer,
        )

    def test_banned_app_id_rejected(self):
        with self.assertRaises(ValueError):
            self.store.save_app(self._def(BANNED_APP, GOOD))

    def test_denylisted_deployer_rejected(self):
        with self.assertRaises(ValueError):
            self.store.save_app(self._def("app_ok", DENY_63A))

    def test_clean_app_persists(self):
        self.store.save_app(self._def("app_ok", GOOD))
        self.assertIsNotNone(self.store.get_app("app_ok"))


if __name__ == "__main__":
    unittest.main()
