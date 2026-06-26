"""Tests for EIP-712 developer-auth: the primitive, the nonce store, and the
``update_scoring`` integration that replaced the nonce-less EIP-191 scheme."""

from __future__ import annotations

import contextlib
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

# Ensure repo root is importable
_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from eth_account import Account

from minotaur_subnet.api.services import app_service
from minotaur_subnet.api.services import developer_auth as da
from minotaur_subnet.shared.types import AppIntentDefinition
from minotaur_subnet.store.app_intent_store import AppIntentStore

# Deterministic test key (Anvil account #0) and a second, unrelated key.
DEPLOYER_PK = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
DEPLOYER = Account.from_key(DEPLOYER_PK).address
OTHER_PK = "0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d"


def _sign(app_id, new_js, nonce, deadline, *, pk=DEPLOYER_PK, action=da.ACTION_UPDATE_SCORING):
    return da.sign_developer_auth(
        pk, action=action, app_id=app_id,
        params_hash=da.params_hash(new_js.encode()), nonce=nonce, deadline=deadline,
    )


class TestDeveloperAuthPrimitive(unittest.TestCase):
    """The pure EIP-712 sign/verify primitive."""

    def setUp(self):
        self.app_id = "app-123"
        self.js = "module.exports = { score: () => ({score:0.9, valid:true}) }"
        self.ph = da.params_hash(self.js.encode())
        self.deadline = int(time.time()) + 300
        self.sig = _sign(self.app_id, self.js, 1, self.deadline)

    def _verify(self, **over):
        kw = dict(
            expected_deployer=DEPLOYER, action=da.ACTION_UPDATE_SCORING,
            app_id=self.app_id, params_hash=self.ph, nonce=1,
            deadline=self.deadline, signature=self.sig,
        )
        kw.update(over)
        return da.verify_developer_auth(**kw)

    def test_valid_roundtrip(self):
        ok, err = self._verify()
        self.assertTrue(ok, err)

    def test_wrong_signer_rejected(self):
        ok, _ = self._verify(expected_deployer=Account.from_key(OTHER_PK).address)
        self.assertFalse(ok)

    def test_tampered_fields_rejected(self):
        for label, over in [
            ("nonce", {"nonce": 2}),
            ("action", {"action": da.ACTION_DEPLOY}),
            ("app_id", {"app_id": "app-999"}),
            ("params_hash", {"params_hash": da.params_hash(b"other")}),
        ]:
            with self.subTest(field=label):
                ok, _ = self._verify(**over)
                self.assertFalse(ok, f"tampered {label} should be rejected")

    def test_expired_deadline_rejected(self):
        ok, err = self._verify(deadline=int(time.time()) - 1)
        self.assertFalse(ok)
        self.assertIn("expired", err)

    def test_far_future_deadline_rejected(self):
        far = int(time.time()) + da.MAX_DEADLINE_FUTURE_SECONDS + 100
        sig = _sign(self.app_id, self.js, 1, far)
        ok, err = self._verify(deadline=far, signature=sig)
        self.assertFalse(ok)
        self.assertIn("future", err)

    def test_missing_signature_rejected(self):
        ok, err = self._verify(signature="")
        self.assertFalse(ok)
        self.assertIn("required", err)

    def test_empty_deployer_rejected(self):
        ok, _ = self._verify(expected_deployer="")
        self.assertFalse(ok)

    def test_signature_accepts_no_0x_prefix(self):
        ok, err = self._verify(signature=self.sig[2:])
        self.assertTrue(ok, err)


class TestDeveloperNonceStore(unittest.TestCase):
    """Monotonic, single-use nonce accounting in AppIntentStore."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = AppIntentStore(Path(self._tmp.name) / "store.db")

    def tearDown(self):
        self._tmp.cleanup()

    def test_fresh_nonce_is_zero_next_is_one(self):
        self.assertEqual(self.store.get_developer_nonce("a", DEPLOYER), 0)

    def test_monotonic_consume(self):
        ok, err = self.store.consume_developer_nonce("a", DEPLOYER, 1)
        self.assertTrue(ok, err)
        self.assertEqual(self.store.get_developer_nonce("a", DEPLOYER), 1)
        ok, err = self.store.consume_developer_nonce("a", DEPLOYER, 2)
        self.assertTrue(ok, err)

    def test_replay_same_nonce_rejected(self):
        self.assertTrue(self.store.consume_developer_nonce("a", DEPLOYER, 1)[0])
        ok, err = self.store.consume_developer_nonce("a", DEPLOYER, 1)
        self.assertFalse(ok)
        self.assertIn("expected 2", err)

    def test_gap_rejected(self):
        ok, err = self.store.consume_developer_nonce("a", DEPLOYER, 3)
        self.assertFalse(ok)
        self.assertIn("expected 1", err)

    def test_isolation_across_app_and_deployer(self):
        self.assertTrue(self.store.consume_developer_nonce("a", DEPLOYER, 1)[0])
        # Same deployer, different app → independent counter.
        self.assertEqual(self.store.get_developer_nonce("b", DEPLOYER), 0)
        self.assertTrue(self.store.consume_developer_nonce("b", DEPLOYER, 1)[0])
        # Same app, different deployer → independent counter.
        other = Account.from_key(OTHER_PK).address
        self.assertEqual(self.store.get_developer_nonce("a", other), 0)
        self.assertTrue(self.store.consume_developer_nonce("a", other, 1)[0])

    def test_deployer_case_insensitive(self):
        self.assertTrue(self.store.consume_developer_nonce("a", DEPLOYER.upper(), 1)[0])
        # Lowercased form shares the same counter.
        self.assertEqual(self.store.get_developer_nonce("a", DEPLOYER.lower()), 1)


@contextlib.contextmanager
def _mock_js_validation():
    """Stub JS validation so update_scoring's auth path is tested in isolation."""
    valid = SimpleNamespace(valid=True, errors=[], warnings=[], js_manifest=None)
    with patch(
        "minotaur_subnet.engine.validation.validate_js_code",
        new=AsyncMock(return_value=valid),
    ), patch.object(
        app_service, "_validate_manifest_semantics_for_response",
        return_value=([], [], None),
    ):
        yield


class TestUpdateScoringDeveloperAuth(unittest.TestCase):
    """Service-level update_scoring with the new EIP-712 + nonce authorization."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = AppIntentStore(Path(self._tmp.name) / "store.db")
        self.app_id = "app-under-test"
        self.new_js = "module.exports = { score: () => ({score:0.95, valid:true}) }"

    def tearDown(self):
        self._tmp.cleanup()

    def _save_app(self, deployer):
        self.store.save_app(AppIntentDefinition(
            app_id=self.app_id, name="T", version="1.0.0", intent_type="swap",
            js_code="module.exports = { score: () => ({score:0.5, valid:true}) }",
            deployer=deployer,
        ))

    def _update(self, **kw):
        with _mock_js_validation():
            return app_service.update_scoring(self.store, self.app_id, self.new_js, **kw)

    def test_authorized_update_consumes_nonce(self):
        self._save_app(DEPLOYER)
        deadline = int(time.time()) + 300
        sig = _sign(self.app_id, self.new_js, 1, deadline)
        res = self._update(signature=sig, nonce=1, deadline=deadline)
        self.assertEqual(res.get("status"), "updated", res)
        self.assertEqual(self.store.get_developer_nonce(self.app_id, DEPLOYER), 1)

    def test_replay_rejected_after_success(self):
        self._save_app(DEPLOYER)
        deadline = int(time.time()) + 300
        sig = _sign(self.app_id, self.new_js, 1, deadline)
        self.assertEqual(self._update(signature=sig, nonce=1, deadline=deadline).get("status"), "updated")
        # Same signed request again — nonce already consumed.
        res = self._update(signature=sig, nonce=1, deadline=deadline)
        self.assertIn("Unauthorized", res.get("error", ""))

    def test_wrong_signer_rejected_and_nonce_untouched(self):
        self._save_app(DEPLOYER)
        deadline = int(time.time()) + 300
        sig = _sign(self.app_id, self.new_js, 1, deadline, pk=OTHER_PK)
        res = self._update(signature=sig, nonce=1, deadline=deadline)
        self.assertIn("Unauthorized", res.get("error", ""))
        self.assertEqual(self.store.get_developer_nonce(self.app_id, DEPLOYER), 0)

    def test_expired_deadline_rejected(self):
        self._save_app(DEPLOYER)
        past = int(time.time()) - 1
        sig = _sign(self.app_id, self.new_js, 1, past)
        res = self._update(signature=sig, nonce=1, deadline=past)
        self.assertIn("Unauthorized", res.get("error", ""))

    def test_missing_signature_rejected(self):
        self._save_app(DEPLOYER)
        res = self._update(signature="", nonce=1, deadline=int(time.time()) + 300)
        self.assertIn("Unauthorized", res.get("error", ""))

    def test_no_deployer_allows_open_update(self):
        self._save_app("")  # no deployer → open (backward compat)
        res = self._update()
        self.assertEqual(res.get("status"), "updated", res)


if __name__ == "__main__":
    unittest.main()
