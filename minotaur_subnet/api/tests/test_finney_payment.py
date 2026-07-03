"""Tests for the finney deploy-fee verifier: the policy (fake reader), the
consume-once store, rail selection, the adapter's pure parsing, and the full
verify_deploy_fee_payment integration."""

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

from minotaur_subnet.api.services import deploy_payment as dp
from minotaur_subnet.api.services import developer_auth as da
from minotaur_subnet.api.services import finney_payment as fp
from minotaur_subnet.deployment.deploy_fee import deploy_fee_rao
from minotaur_subnet.shared.types import AppIntentConfig, AppIntentDefinition
from minotaur_subnet.store.app_intent_store import AppIntentStore

PAYER = "5PayerColdkeyAddressAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
COLLECTOR = "5CollectorColdkeyAddressBBBBBBBBBBBBBBBBBBBBBBBB"
DEPLOYER_PK = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
DEPLOYER = Account.from_key(DEPLOYER_PK).address
CHAIN = 2  # finney (substrate) — chain_id is bound into the signature only
REF = "0xblockhash:3"


class _FakeReader:
    def __init__(self, record=None, raise_exc=None):
        self._record = record
        self._raise = raise_exc

    def find_transfer(self, *, payment_ref):
        if self._raise is not None:
            raise self._raise
        return self._record


def _record(*, frm=PAYER, to=COLLECTOR, amount=None, finalized=True):
    return fp.TransferRecord(
        from_ss58=frm, to_ss58=to,
        amount_rao=deploy_fee_rao() if amount is None else amount,
        finalized=finalized,
    )


@contextlib.contextmanager
def _collector_set(value=COLLECTOR):
    with patch.dict(os.environ, {"DEPLOY_FEE_COLLECTOR_SS58": value}):
        yield


class TestFinneyPaymentVerifier(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = AppIntentStore(Path(self._tmp.name) / "s.db")
        self.app_id = "app-fin"
        self.store.set_payer_ss58(self.app_id, DEPLOYER, PAYER)

    def tearDown(self):
        self._tmp.cleanup()

    def _verify(self, record=None, *, reader=None):
        v = fp.FinneyPaymentVerifier(reader=reader or _FakeReader(record))
        return v.verify(
            store=self.store, app_id=self.app_id, deployer=DEPLOYER,
            payment_ref=REF, chain_id=CHAIN, amount_rao=deploy_fee_rao(),
        )

    def test_happy_path_confirms_and_consumes(self):
        with _collector_set():
            ok, err = self._verify(_record())
        self.assertTrue(ok, err)
        # payment is now consumed → not reusable
        self.assertFalse(self.store.consume_payment_ref(REF, self.app_id)[0])

    def test_overpayment_ok(self):
        with _collector_set():
            ok, err = self._verify(_record(amount=deploy_fee_rao() + 5))
        self.assertTrue(ok, err)

    def test_no_link_rejected(self):
        self.store.set_payer_ss58(self.app_id, DEPLOYER, "")  # clear link
        # set_payer_ss58 with "" stores empty; get returns "" → treated as unlinked
        with _collector_set():
            ok, err = self._verify(_record())
        self.assertFalse(ok)
        self.assertIn("link", err.lower())

    def test_collector_not_configured_rejected(self):
        # DEPLOY_FEE_COLLECTOR_SS58 unset
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DEPLOY_FEE_COLLECTOR_SS58", None)
            ok, err = self._verify(_record())
        self.assertFalse(ok)
        self.assertIn("collector", err.lower())

    def test_no_transfer_found_rejected(self):
        with _collector_set():
            ok, err = self._verify(None)
        self.assertFalse(ok)
        self.assertIn("no transfer", err.lower())

    def test_not_finalized_rejected(self):
        with _collector_set():
            ok, err = self._verify(_record(finalized=False))
        self.assertFalse(ok)
        self.assertIn("finalized", err.lower())

    def test_wrong_payer_rejected(self):
        with _collector_set():
            ok, err = self._verify(_record(frm="5SomeoneElse"))
        self.assertFalse(ok)
        self.assertIn("linked coldkey", err.lower())

    def test_wrong_collector_rejected(self):
        with _collector_set():
            ok, err = self._verify(_record(to="5WrongDest"))
        self.assertFalse(ok)
        self.assertIn("collector", err.lower())

    def test_insufficient_amount_rejected(self):
        with _collector_set():
            ok, err = self._verify(_record(amount=deploy_fee_rao() - 1))
        self.assertFalse(ok)
        self.assertIn("below", err.lower())

    def test_replay_rejected(self):
        with _collector_set():
            self.assertTrue(self._verify(_record())[0])
            ok, err = self._verify(_record())  # same REF again
        self.assertFalse(ok)
        self.assertIn("already used", err.lower())

    def test_reader_exception_rejected(self):
        with _collector_set():
            ok, err = self._verify(reader=_FakeReader(raise_exc=RuntimeError("rpc down")))
        self.assertFalse(ok)
        self.assertIn("lookup failed", err.lower())

    def test_unconfirmed_payment_not_consumed(self):
        # A rejected verification must not consume the payment ref.
        with _collector_set():
            self._verify(_record(amount=deploy_fee_rao() - 1))
        self.assertTrue(self.store.consume_payment_ref(REF, self.app_id)[0])


class TestConsumePaymentRefStore(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = AppIntentStore(Path(self._tmp.name) / "s.db")

    def tearDown(self):
        self._tmp.cleanup()

    def test_consume_once(self):
        ok, _ = self.store.consume_payment_ref("ref-1", "a")
        self.assertTrue(ok)
        ok2, err = self.store.consume_payment_ref("ref-1", "a")
        self.assertFalse(ok2)
        self.assertIn("already used", err)

    def test_empty_ref_rejected(self):
        ok, _ = self.store.consume_payment_ref("", "a")
        self.assertFalse(ok)

    def test_distinct_refs_independent(self):
        self.assertTrue(self.store.consume_payment_ref("ref-1", "a")[0])
        self.assertTrue(self.store.consume_payment_ref("ref-2", "a")[0])


class TestRailSelection(unittest.TestCase):
    def test_default_rail_is_evm(self):
        # Default rail is EVM (WTAO on BT EVM) — the developer's own wallet pays.
        from minotaur_subnet.api.services.evm_payment import EvmDeployFeeVerifier

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DEPLOY_FEE_RAIL", None)
            self.assertIsInstance(dp.get_payment_verifier(), EvmDeployFeeVerifier)

    def test_finney_rail_selectable(self):
        with patch.dict(os.environ, {"DEPLOY_FEE_RAIL": "finney"}):
            self.assertIsInstance(dp.get_payment_verifier(), fp.FinneyPaymentVerifier)


class TestSubstrateReaderPure(unittest.TestCase):
    """The adapter's parsing + payment_ref guard (no node needed)."""

    def test_parse_transfer_list_and_dict(self):
        r = fp.SubstrateInterfaceTransferReader
        self.assertEqual(r._parse_transfer(["5A", "5B", 7]), ("5A", "5B", 7))
        self.assertEqual(
            r._parse_transfer({"from": "5A", "to": "5B", "amount": 7}), ("5A", "5B", 7)
        )
        self.assertIsNone(r._parse_transfer("garbage"))
        self.assertIsNone(r._parse_transfer(["only", "two"]))

    def test_malformed_payment_ref_returns_none_without_connecting(self):
        reader = fp.SubstrateInterfaceTransferReader(url="ws://unused")
        # No "block:index" → returns None before any connection attempt.
        self.assertIsNone(reader.find_transfer(payment_ref="no-colon"))
        self.assertIsNone(reader.find_transfer(payment_ref="0xhash:notint"))


class TestVerifyDeployFeePaymentFinneyIntegration(unittest.TestCase):
    """End-to-end through verify_deploy_fee_payment with a finney verifier."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = AppIntentStore(Path(self._tmp.name) / "s.db")
        self.app_id = "app-int"
        self.store.save_app(AppIntentDefinition(
            app_id=self.app_id, name="I", version="1.0.0", intent_type="swap",
            js_code="x", deployer=DEPLOYER,
            config=AppIntentConfig(supported_chains=[CHAIN]),
        ))
        self.store.set_payer_ss58(self.app_id, DEPLOYER, PAYER)
        self.deadline = int(time.time()) + 300

    def tearDown(self):
        self._tmp.cleanup()

    def _payment(self, nonce=1):
        ph = dp.deploy_fee_params_hash(REF, CHAIN, deploy_fee_rao())
        sig = da.sign_developer_auth(
            DEPLOYER_PK, action=da.ACTION_PAY_DEPLOY_FEE, app_id=self.app_id,
            params_hash=ph, nonce=nonce, deadline=self.deadline,
        )
        return dp.DeployFeePayment(payment_ref=REF, nonce=nonce, deadline=self.deadline, signature=sig)

    def test_full_finney_authorization(self):
        verifier = fp.FinneyPaymentVerifier(reader=_FakeReader(_record()))
        # The fee binds the payment chain; pin it to this test's CHAIN so the
        # signature (built over CHAIN) matches the server's recomputed hash.
        with patch.dict(os.environ, {
            "ENABLE_PUBLIC_DEPLOYMENT": "1",
            "DEPLOY_FEE_COLLECTOR_SS58": COLLECTOR,
            "DEPLOY_FEE_PAYMENT_CHAIN_ID": str(CHAIN),
        }):
            ok, err = dp.verify_deploy_fee_payment(
                self.store, self.store.get_app(self.app_id),
                payment=self._payment(), verifier=verifier,
            )
        self.assertTrue(ok, err)
        # both the nonce and the payment are now consumed
        self.assertEqual(self.store.get_developer_nonce(self.app_id, DEPLOYER), 1)
        self.assertFalse(self.store.consume_payment_ref(REF, self.app_id)[0])


if __name__ == "__main__":
    unittest.main()
