"""The relayer's send paths must invalidate the local nonce on a failed send.

get_and_increment advances a per-(chain,wallet) counter WITHOUT an RPC call.
If the broadcast/receipt then fails and the counter is left advanced, every
later tx from that wallet is nonce-gapped and stuck in the mempool. The escrow /
admin / registry calls route through _broadcast_and_confirm, which invalidates
on any failure (matching the submit_plan / deploy_contract guards).
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from minotaur_subnet.relayer.evm_relayer import EvmRelayer, NonceManager

_KEY = "1:0xwallet"


def _relayer():
    r = EvmRelayer.__new__(EvmRelayer)  # bypass __init__ (needs chain config)
    r._nonce_manager = NonceManager()
    r._nonce_manager._nonces[_KEY] = 42  # a stale, already-advanced counter
    return r


def _w3(*, send_raises=False, receipt_status=1):
    w3 = MagicMock()
    if send_raises:
        w3.eth.send_raw_transaction.side_effect = RuntimeError("dropped send")
    else:
        th = MagicMock()
        th.hex.return_value = "0xdeadbeef"
        w3.eth.send_raw_transaction.return_value = th
        w3.eth.wait_for_transaction_receipt.return_value = {"status": receipt_status}
    return w3


class TestNonceInvalidation:
    def test_send_failure_invalidates_nonce(self):
        r = _relayer()
        with pytest.raises(RuntimeError):
            r._broadcast_and_confirm(
                _w3(send_raises=True), MagicMock(), 1, "0xwallet", desc="x",
            )
        assert _KEY not in r._nonce_manager._nonces  # re-syncs next use

    def test_revert_invalidates_nonce(self):
        r = _relayer()
        with pytest.raises(RuntimeError):
            r._broadcast_and_confirm(
                _w3(receipt_status=0), MagicMock(), 1, "0xwallet", desc="x",
            )
        assert _KEY not in r._nonce_manager._nonces

    def test_success_keeps_nonce_and_returns_hex(self):
        r = _relayer()
        out = r._broadcast_and_confirm(
            _w3(receipt_status=1), MagicMock(), 1, "0xwallet", desc="x",
        )
        assert out == "0xdeadbeef"
        assert r._nonce_manager._nonces.get(_KEY) == 42  # untouched on success

    def test_require_success_false_does_not_raise_on_revert(self):
        # register_intent / sync_validators preserved their prior no-status-check
        # behaviour: a reverted receipt is not an error, so the nonce stands.
        r = _relayer()
        out = r._broadcast_and_confirm(
            _w3(receipt_status=0), MagicMock(), 1, "0xwallet",
            desc="x", require_success=False,
        )
        assert out == "0xdeadbeef"
        assert r._nonce_manager._nonces.get(_KEY) == 42
