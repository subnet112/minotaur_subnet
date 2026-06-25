"""Tests for deploy-fee payment authorization (deploy_payment) and its gate
integration in deploy_app_intent. The on-chain payment check is stubbed with a
mock verifier; the default DisabledPaymentVerifier keeps the #238 gate closed."""

from __future__ import annotations

import contextlib
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

# Ensure repo root is importable
_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from eth_account import Account

from minotaur_subnet.api.services import app_service
from minotaur_subnet.api.services import deploy_payment as dp
from minotaur_subnet.api.services import developer_auth as da
from minotaur_subnet.deployment.deploy_fee import deploy_fee_rao
from minotaur_subnet.shared.types import AppIntentConfig, AppIntentDefinition
from minotaur_subnet.store.app_intent_store import AppIntentStore

DEPLOYER_PK = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
DEPLOYER = Account.from_key(DEPLOYER_PK).address
OTHER_PK = "0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d"
CHAIN = 964  # Bittensor EVM (wTAO)


class _OkVerifier:
    """Stands in for a real on-chain payment verifier that confirms the fee."""

    def verify(self, **_):
        return True, ""


@contextlib.contextmanager
def _collection_enabled():
    with patch.dict(os.environ, {"ENABLE_PUBLIC_DEPLOYMENT": "1"}):
        yield


def _sign_payment(app_id, ref, nonce, deadline, *, pk=DEPLOYER_PK, chain=CHAIN, amount=None):
    amount = deploy_fee_rao() if amount is None else amount
    ph = dp.deploy_fee_params_hash(ref, chain, amount)
    return da.sign_developer_auth(
        pk, action=da.ACTION_PAY_DEPLOY_FEE, app_id=app_id,
        params_hash=ph, nonce=nonce, deadline=deadline,
    )


class TestDeployFeePaymentAuth(unittest.TestCase):
    """The verify_deploy_fee_payment authorization core."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = AppIntentStore(Path(self._tmp.name) / "s.db")
        self.app_id = "app-pay"
        self.ref = "0xdeadbeef"
        self.deadline = int(time.time()) + 300

    def tearDown(self):
        self._tmp.cleanup()

    def _seed(self, deployer=DEPLOYER, chains=(CHAIN,)):
        self.store.save_app(AppIntentDefinition(
            app_id=self.app_id, name="P", version="1.0.0", intent_type="swap",
            js_code="x", deployer=deployer,
            config=AppIntentConfig(supported_chains=list(chains)),
        ))
        return self.store.get_app(self.app_id)

    def _payment(self, nonce=1, deadline=None, **sign_over):
        deadline = self.deadline if deadline is None else deadline
        sig = _sign_payment(self.app_id, self.ref, nonce, deadline, **sign_over)
        return dp.DeployFeePayment(
            payment_ref=self.ref, nonce=nonce, deadline=deadline, signature=sig,
        )

    def _verify(self, defn, *, chain_id=CHAIN, payment=None, verifier=_OkVerifier()):
        return dp.verify_deploy_fee_payment(
            self.store, defn, chain_id=chain_id,
            payment=payment if payment is not None else self._payment(),
            verifier=verifier,
        )

    def test_collection_off_blocks_without_burning_nonce(self):
        defn = self._seed()
        ok, err = self._verify(defn)  # ENABLE_PUBLIC_DEPLOYMENT not set
        self.assertFalse(ok)
        self.assertIn("not live", err)
        self.assertEqual(self.store.get_developer_nonce(self.app_id, DEPLOYER), 0)

    def test_valid_payment_authorized_consumes_nonce(self):
        defn = self._seed()
        with _collection_enabled():
            ok, err = self._verify(defn)
        self.assertTrue(ok, err)
        self.assertEqual(self.store.get_developer_nonce(self.app_id, DEPLOYER), 1)

    def test_default_disabled_verifier_refuses(self):
        defn = self._seed()
        with _collection_enabled():
            ok, err = dp.verify_deploy_fee_payment(
                self.store, defn, chain_id=CHAIN, payment=self._payment(),
            )  # no verifier → DisabledPaymentVerifier
        self.assertFalse(ok)
        self.assertIn("not configured", err)
        self.assertEqual(self.store.get_developer_nonce(self.app_id, DEPLOYER), 0)

    def test_no_deployer_rejected(self):
        defn = self._seed(deployer="")
        with _collection_enabled():
            ok, err = self._verify(defn)
        self.assertFalse(ok)
        self.assertIn("no deployer", err)

    def test_missing_ref_or_signature_rejected(self):
        defn = self._seed()
        with _collection_enabled():
            ok1, _ = dp.verify_deploy_fee_payment(
                self.store, defn, chain_id=CHAIN, verifier=_OkVerifier(),
                payment=dp.DeployFeePayment(payment_ref="", nonce=1, deadline=self.deadline, signature="x"),
            )
            ok2, _ = dp.verify_deploy_fee_payment(
                self.store, defn, chain_id=CHAIN, verifier=_OkVerifier(),
                payment=dp.DeployFeePayment(payment_ref=self.ref, nonce=1, deadline=self.deadline, signature=""),
            )
        self.assertFalse(ok1)
        self.assertFalse(ok2)

    def test_wrong_signer_rejected_nonce_untouched(self):
        defn = self._seed()
        with _collection_enabled():
            ok, _ = self._verify(defn, payment=self._payment(pk=OTHER_PK))
        self.assertFalse(ok)
        self.assertEqual(self.store.get_developer_nonce(self.app_id, DEPLOYER), 0)

    def test_chain_binding_mismatch_rejected(self):
        defn = self._seed(chains=(CHAIN, 1))
        # Signed for CHAIN (964) but verified for chain 1 → params_hash mismatch.
        with _collection_enabled():
            ok, _ = self._verify(defn, chain_id=1, payment=self._payment(chain=CHAIN))
        self.assertFalse(ok)

    def test_amount_binding_mismatch_rejected(self):
        defn = self._seed()
        with _collection_enabled():
            ok, _ = self._verify(defn, payment=self._payment(amount=deploy_fee_rao() + 1))
        self.assertFalse(ok)

    def test_expired_deadline_rejected(self):
        defn = self._seed()
        with _collection_enabled():
            ok, err = self._verify(defn, payment=self._payment(deadline=int(time.time()) - 1))
        self.assertFalse(ok)
        self.assertIn("expired", err)

    def test_replay_rejected(self):
        defn = self._seed()
        with _collection_enabled():
            self.assertTrue(self._verify(defn, payment=self._payment(nonce=1))[0])
            ok, _ = self._verify(defn, payment=self._payment(nonce=1))
        self.assertFalse(ok)


class TestDeployAppIntentFeeGate(unittest.TestCase):
    """deploy_app_intent: admin path stays free; payment path is gated."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = AppIntentStore(Path(self._tmp.name) / "s.db")
        self.app_id = "app-deploy"
        self.store.save_app(AppIntentDefinition(
            app_id=self.app_id, name="D", version="1.0.0", intent_type="swap",
            js_code="x", deployer=DEPLOYER,
            config=AppIntentConfig(supported_chains=[CHAIN]),
        ))
        self.deadline = int(time.time()) + 300
        # Ensure no real deploy service so a gate-pass lands on the no-relayer path.
        p = patch("minotaur_subnet.api.services._state._deploy_service", None)
        p.start()
        self.addCleanup(p.stop)

    def tearDown(self):
        self._tmp.cleanup()

    def _payment(self, nonce, *, pk=DEPLOYER_PK):
        ref = "0xref"
        sig = _sign_payment(self.app_id, ref, nonce, self.deadline, pk=pk)
        return dp.DeployFeePayment(payment_ref=ref, nonce=nonce, deadline=self.deadline, signature=sig)

    def test_admin_deploy_no_payment_reaches_deploy(self):
        res = app_service.deploy_app_intent(self.store, self.app_id, chain_id=CHAIN)
        self.assertNotIn("deploy_fee_required", res)
        self.assertIn("relayer", res.get("error", "").lower())

    def test_payment_backed_blocked_when_collection_off(self):
        res = app_service.deploy_app_intent(
            self.store, self.app_id, chain_id=CHAIN, payment=self._payment(1),
        )
        self.assertTrue(res.get("deploy_fee_required"))
        self.assertEqual(self.store.get_developer_nonce(self.app_id, DEPLOYER), 0)

    def test_payment_backed_passes_gate_when_authorized(self):
        with _collection_enabled(), patch(
            "minotaur_subnet.api.services.deploy_payment.get_payment_verifier",
            return_value=_OkVerifier(),
        ):
            res = app_service.deploy_app_intent(
                self.store, self.app_id, chain_id=CHAIN, payment=self._payment(1),
            )
        # Past the fee gate → fails only for lack of a relayer; nonce is spent.
        self.assertNotIn("deploy_fee_required", res)
        self.assertIn("relayer", res.get("error", "").lower())
        self.assertEqual(self.store.get_developer_nonce(self.app_id, DEPLOYER), 1)

    def test_payment_backed_wrong_signer_blocked(self):
        with _collection_enabled(), patch(
            "minotaur_subnet.api.services.deploy_payment.get_payment_verifier",
            return_value=_OkVerifier(),
        ):
            res = app_service.deploy_app_intent(
                self.store, self.app_id, chain_id=CHAIN, payment=self._payment(1, pk=OTHER_PK),
            )
        self.assertTrue(res.get("deploy_fee_required"))


if __name__ == "__main__":
    unittest.main()
