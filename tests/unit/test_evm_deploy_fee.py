"""Tests for the EVM (WTAO) deploy-fee verifier and once-per-app plumbing.

The fee is 0.5 TAO paid in WTAO on BT EVM (964): a direct ERC-20 Transfer
from the app deployer to the collector, confirmed, worth >= the fee, used
once. RAO (9 dp) → WTAO wei (18 dp) is ×1e9, so 0.5 TAO = 5e17 wei.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from eth_hash.auto import keccak

from minotaur_subnet.api.services.evm_payment import EvmDeployFeeVerifier

DEPLOYER = "0x" + "11" * 20
COLLECTOR = "0x" + "cc" * 20
WTAO = "0x9Dc08C6e2BF0F1eeD1E00670f80Df39145529F81"
TX = "0x" + "ab" * 32
FEE_RAO = 500_000_000            # 0.5 TAO in RAO (9 dp)
FEE_WEI = FEE_RAO * 10**9        # 0.5 TAO in WTAO wei (18 dp) = 5e17
TRANSFER_TOPIC = "0x" + keccak(b"Transfer(address,address,uint256)").hex()


def _topic(addr: str) -> str:
    return "0x" + addr[2:].lower().rjust(64, "0")


def _transfer_log(frm, to, value, token=WTAO):
    return {
        "address": token,
        "topics": [TRANSFER_TOPIC, _topic(frm), _topic(to)],
        "data": "0x" + value.to_bytes(32, "big").hex(),
    }


def _w3(*, logs, status=1, block=100, head=200, decimals=18):
    w3 = MagicMock()
    w3.eth.get_transaction_receipt.return_value = {
        "status": status, "blockNumber": block, "logs": logs,
    }
    w3.eth.block_number = head
    w3.eth.call.return_value = decimals.to_bytes(32, "big")  # decimals()
    return w3


def _verify(w3, *, store=None, amount_rao=FEE_RAO, deployer=DEPLOYER, monkeypatch=None):
    if monkeypatch is not None:
        monkeypatch.setenv("DEPLOY_FEE_COLLECTOR_EVM", COLLECTOR)
    if store is None:
        store = MagicMock()
        store.consume_payment_ref.return_value = (True, "")
    v = EvmDeployFeeVerifier(get_web3=lambda cid: w3)
    return v.verify(store=store, app_id="app_x", deployer=deployer,
                    payment_ref=TX, chain_id=964, amount_rao=amount_rao), store


# ── happy path ───────────────────────────────────────────────────────────


def test_valid_wtao_payment_accepted(monkeypatch):
    w3 = _w3(logs=[_transfer_log(DEPLOYER, COLLECTOR, FEE_WEI)])
    (ok, err), store = _verify(w3, monkeypatch=monkeypatch)
    assert ok, err
    store.consume_payment_ref.assert_called_once_with(TX, "app_x")


def test_overpayment_accepted(monkeypatch):
    w3 = _w3(logs=[_transfer_log(DEPLOYER, COLLECTOR, FEE_WEI * 2)])
    (ok, err), _ = _verify(w3, monkeypatch=monkeypatch)
    assert ok, err


# ── rejections ───────────────────────────────────────────────────────────


def test_no_collector_configured_refuses(monkeypatch):
    monkeypatch.delenv("DEPLOY_FEE_COLLECTOR_EVM", raising=False)
    w3 = _w3(logs=[_transfer_log(DEPLOYER, COLLECTOR, FEE_WEI)])
    v = EvmDeployFeeVerifier(get_web3=lambda cid: w3)
    ok, err = v.verify(store=MagicMock(), app_id="app_x", deployer=DEPLOYER,
                       payment_ref=TX, chain_id=964, amount_rao=FEE_RAO)
    assert not ok and "collector" in err.lower()


def test_underpayment_rejected(monkeypatch):
    w3 = _w3(logs=[_transfer_log(DEPLOYER, COLLECTOR, FEE_WEI - 1)])
    (ok, err), store = _verify(w3, monkeypatch=monkeypatch)
    assert not ok and "wtao transfer" in err.lower()
    store.consume_payment_ref.assert_not_called()


def test_wrong_recipient_rejected(monkeypatch):
    w3 = _w3(logs=[_transfer_log(DEPLOYER, "0x" + "99" * 20, FEE_WEI)])
    (ok, _), _ = _verify(w3, monkeypatch=monkeypatch)
    assert not ok


def test_wrong_payer_rejected(monkeypatch):
    """Transfer from someone other than the app deployer must not count —
    stops claiming a stranger's payment."""
    w3 = _w3(logs=[_transfer_log("0x" + "77" * 20, COLLECTOR, FEE_WEI)])
    (ok, _), _ = _verify(w3, monkeypatch=monkeypatch)
    assert not ok


def test_wrong_token_rejected(monkeypatch):
    w3 = _w3(logs=[_transfer_log(DEPLOYER, COLLECTOR, FEE_WEI, token="0x" + "de" * 20)])
    (ok, _), _ = _verify(w3, monkeypatch=monkeypatch)
    assert not ok


def test_reverted_tx_rejected(monkeypatch):
    w3 = _w3(logs=[_transfer_log(DEPLOYER, COLLECTOR, FEE_WEI)], status=0)
    (ok, err), _ = _verify(w3, monkeypatch=monkeypatch)
    assert not ok and "revert" in err.lower()


def test_unconfirmed_tx_rejected(monkeypatch):
    # block 199, head 200 → 2 confirmations < default 6
    w3 = _w3(logs=[_transfer_log(DEPLOYER, COLLECTOR, FEE_WEI)], block=199, head=200)
    (ok, err), _ = _verify(w3, monkeypatch=monkeypatch)
    assert not ok and "confirm" in err.lower()


def test_missing_tx_rejected(monkeypatch):
    monkeypatch.setenv("DEPLOY_FEE_COLLECTOR_EVM", COLLECTOR)
    w3 = MagicMock()
    w3.eth.get_transaction_receipt.return_value = None
    v = EvmDeployFeeVerifier(get_web3=lambda cid: w3)
    ok, err = v.verify(store=MagicMock(), app_id="app_x", deployer=DEPLOYER,
                       payment_ref=TX, chain_id=964, amount_rao=FEE_RAO)
    assert not ok and "not found" in err.lower()


def test_replayed_ref_rejected(monkeypatch):
    w3 = _w3(logs=[_transfer_log(DEPLOYER, COLLECTOR, FEE_WEI)])
    store = MagicMock()
    store.consume_payment_ref.return_value = (False, "payment already used")
    (ok, err), _ = _verify(w3, store=store, monkeypatch=monkeypatch)
    assert not ok and "already used" in err.lower()


def test_decimals_scaling_9dp_token(monkeypatch):
    """A 9-decimal fee token: fee_wei == amount_rao (no ×1e9 scale). Amount at
    the 18-dp scale would be rejected as an overpay boundary check."""
    # 9-dp token: fee = FEE_RAO wei exactly.
    w3 = _w3(logs=[_transfer_log(DEPLOYER, COLLECTOR, FEE_RAO)], decimals=9)
    (ok, err), _ = _verify(w3, monkeypatch=monkeypatch)
    assert ok, err


# ── once-per-app plumbing in deploy_app_intent ───────────────────────────


def test_deploy_charges_fee_once_per_app(tmp_path, monkeypatch):
    """A paid deploy records deploy_fee.paid; a second chain deploy of the SAME
    app with a payment body is accepted WITHOUT re-verifying/re-charging."""
    from minotaur_subnet.api.services.app_service import (
        _app_deploy_fee_paid, deploy_app_intent,
    )
    from minotaur_subnet.api.services.deploy_payment import DeployFeePayment
    from minotaur_subnet.shared.types import (
        AppIntentConfig, AppIntentDefinition, AppStatus, DeploymentResult,
    )
    from minotaur_subnet.store.app_intent_store import AppIntentStore
    from unittest.mock import patch

    s = AppIntentStore(store_path=tmp_path / "s.db")
    s.save_app(AppIntentDefinition(
        app_id="app_x", name="dex", version="1.0.0", intent_type="swap",
        js_code="x", solidity_code="x",
        config=AppIntentConfig(supported_chains=[8453, 964]),
        deployer=DEPLOYER, registration_status="approved",
    ))
    # Pre-mark the fee paid (simulates a prior paid deploy on chain 8453).
    d = s.get_app("app_x")
    d.policy_metadata = {"deploy_fee": {"paid": True, "payment_ref": TX}}
    s.save_app(d)
    assert _app_deploy_fee_paid(s.get_app("app_x")) is True

    deploy_svc = MagicMock()
    async def _fake_deploy(defn, chain):
        return DeploymentResult(app_id="app_x", status=AppStatus.SOLVING,
                                js_code_hash="x", chain_id=964,
                                contract_address="0x" + "22" * 20)
    deploy_svc.deploy = _fake_deploy
    pay = DeployFeePayment(payment_ref=TX, nonce=1, deadline=0, signature="0xsig")

    with patch("minotaur_subnet.api.services._state._deploy_service", deploy_svc), \
         patch("minotaur_subnet.api.services.deploy_payment.verify_deploy_fee_payment") as vf, \
         patch("minotaur_subnet.api.services.app_lifecycle.auto_register_deployment",
               return_value={"registered": True}):
        out = deploy_app_intent(s, "app_x", chain_id=964, is_admin=False, payment=pay)

    assert not out.get("error"), out          # DeploymentResult.error is None on success
    assert out.get("contract_address")
    # Fee already paid → verification NOT re-run for the second chain.
    vf.assert_not_called()
