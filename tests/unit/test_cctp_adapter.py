"""Tests for the Circle CCTP v2 bridge adapter (USDC Base ↔ Ethereum).

Mocking policy: real adapter + real ABI encoding; the Iris API is mocked.
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.cross_chain

from eth_abi import decode as abi_decode

from minotaur_subnet.bridge.base import BridgeQuote, BridgeStatusEnum
from minotaur_subnet.bridge.cctp import (
    CCTP_DOMAINS,
    CCTPAdapter,
    DEPOSIT_FOR_BURN_SELECTOR,
    FAST_FINALITY_THRESHOLD,
    MAX_FEE_HEADROOM_PCT,
    MESSAGE_TRANSMITTER_V2,
    RECEIVE_MESSAGE_SELECTOR,
    TOKEN_MESSENGER_V2,
    USDC,
    cctp_enabled,
)
from minotaur_subnet.shared.types import _BRIDGE_CALL_SELECTORS

USER = "0x" + "aa" * 20
AMOUNT = 1_000_000_000  # 1000 USDC (6 decimals)
FAST_FEE_BPS = 1


def _run(coro):
    return asyncio.run(coro)


def _quoted(adapter: CCTPAdapter, src=1, dst=8453, amount=AMOUNT) -> BridgeQuote:
    async def _fake_fee(*args, **kwargs):
        return FAST_FEE_BPS

    with patch.object(adapter, "_fetch_fast_fee_bps", _fake_fee):
        return _run(adapter.quote(USDC[src], amount, src, dst))


class TestQuote:
    def test_routes(self):
        assert set(CCTPAdapter().supported_routes()) == {(1, 8453), (8453, 1)}

    def test_quote_fields(self):
        q = _quoted(CCTPAdapter())
        fee = AMOUNT * FAST_FEE_BPS // 10_000
        assert q.protocol == "cctp"
        assert q.token_in == USDC[1]
        assert q.token_out == USDC[8453]
        assert q.fee == fee
        assert q.estimated_output == AMOUNT - fee
        assert q.metadata["src_domain"] == CCTP_DOMAINS[1]
        assert q.metadata["dst_domain"] == CCTP_DOMAINS[8453]
        assert q.metadata["min_finality_threshold"] == FAST_FINALITY_THRESHOLD
        # maxFee carries headroom over the quoted fee
        assert q.metadata["max_fee"] == AMOUNT * FAST_FEE_BPS * MAX_FEE_HEADROOM_PCT // (10_000 * 100)
        assert q.metadata["max_fee"] > fee

    def test_non_usdc_rejected(self):
        adapter = CCTPAdapter()
        with pytest.raises(ValueError, match="USDC only"):
            _run(adapter.quote("0x" + "12" * 20, AMOUNT, 1, 8453))

    def test_unknown_route_rejected(self):
        adapter = CCTPAdapter()
        with pytest.raises(ValueError, match="No CCTP route"):
            _run(adapter.quote(USDC[1], AMOUNT, 1, 42161))

    def test_fee_api_failure_propagates(self):
        adapter = CCTPAdapter()

        async def _boom(*args, **kwargs):
            raise ValueError("Iris fees HTTP 500")

        with patch.object(adapter, "_fetch_fast_fee_bps", _boom):
            with pytest.raises(ValueError, match="HTTP 500"):
                _run(adapter.quote(USDC[1], AMOUNT, 1, 8453))


class TestBuildInteractions:
    def test_approve_and_burn(self):
        adapter = CCTPAdapter()
        q = _quoted(adapter)
        ixs = adapter.build_bridge_interactions(q, USER)
        assert len(ixs) == 2

        approve, burn = ixs
        assert approve.target == USDC[1]
        spender, amount = abi_decode(
            ["address", "uint256"], bytes.fromhex(approve.call_data[10:]),
        )
        assert spender.lower() == TOKEN_MESSENGER_V2.lower()
        assert amount == AMOUNT

        assert burn.target == TOKEN_MESSENGER_V2
        assert burn.call_data[2:10] == DEPOSIT_FOR_BURN_SELECTOR.hex()
        (amt, dst_domain, mint_recipient, burn_token, dst_caller,
         max_fee, finality) = abi_decode(
            ["uint256", "uint32", "bytes32", "address", "bytes32", "uint256", "uint32"],
            bytes.fromhex(burn.call_data[10:]),
        )
        assert amt == AMOUNT
        assert dst_domain == CCTP_DOMAINS[8453]
        # mintRecipient = left-padded sender — the unredirectable recipient
        assert mint_recipient == b"\x00" * 12 + bytes.fromhex(USER[2:])
        assert burn_token.lower() == USDC[1].lower()
        assert dst_caller == b"\x00" * 32  # open: anyone can submit the mint
        assert max_fee == q.metadata["max_fee"]
        assert finality == FAST_FINALITY_THRESHOLD

    def test_mint_interactions(self):
        adapter = CCTPAdapter()
        ixs = adapter.build_mint_interactions("0xdeadbeef", "0xc0ffee", 8453)
        assert len(ixs) == 1
        assert ixs[0].target == MESSAGE_TRANSMITTER_V2
        assert ixs[0].chain_id == 8453
        assert ixs[0].call_data[2:10] == RECEIVE_MESSAGE_SELECTOR.hex()
        message, attestation = abi_decode(
            ["bytes", "bytes"], bytes.fromhex(ixs[0].call_data[10:]),
        )
        assert message == bytes.fromhex("deadbeef")
        assert attestation == bytes.fromhex("c0ffee")

    def test_mock_config(self):
        adapter = CCTPAdapter()
        q = _quoted(adapter)
        cfg = adapter.mock_config(q)
        assert cfg["selectors"] == [DEPOSIT_FOR_BURN_SELECTOR.hex()]
        assert cfg["mock_type"] == "erc20_transfer"
        assert cfg["mock_amount"] == AMOUNT

    def test_burn_selector_in_solver_ban_list(self):
        assert DEPOSIT_FOR_BURN_SELECTOR.hex() in _BRIDGE_CALL_SELECTORS


class TestCheckStatus:
    TX = "0x" + "ab" * 32

    def _status(self, iris_response):
        adapter = CCTPAdapter()

        async def _fake_fetch(src_domain, tx_hash):
            if isinstance(iris_response, Exception):
                raise iris_response
            return iris_response

        with patch.object(adapter, "_fetch_messages", _fake_fetch):
            return _run(adapter.check_status(self.TX, 1, 8453))

    def test_complete_status_is_ready_to_mint_not_completed(self):
        # Iris "complete" = attestation ready, NOT minted. The adapter never
        # returns COMPLETED (the tracker completes after it mints).
        result = self._status({"messages": [{
            "status": "complete", "message": "0xdead", "attestation": "0xc0ffee",
        }]})
        assert result.status == BridgeStatusEnum.IN_TRANSIT
        assert result.metadata["ready_to_mint"] is True
        assert result.metadata["attestation"] == "0xc0ffee"

    def test_complete_without_attestation_stays_pending(self):
        # "complete" with no attestation value can't be minted yet.
        result = self._status({"messages": [{"status": "complete"}]})
        assert result.status == BridgeStatusEnum.IN_TRANSIT
        assert not result.metadata

    def test_attested_surfaces_mint_payload(self):
        result = self._status({"messages": [{
            "status": "pending_confirmations",
            "message": "0xdeadbeef",
            "attestation": "0xc0ffee",
        }]})
        assert result.status == BridgeStatusEnum.IN_TRANSIT
        assert result.metadata["ready_to_mint"] is True
        assert result.metadata["message"] == "0xdeadbeef"
        assert result.metadata["attestation"] == "0xc0ffee"

    def test_pending_attestation(self):
        result = self._status({"messages": [{
            "status": "pending_confirmations", "attestation": "PENDING",
        }]})
        assert result.status == BridgeStatusEnum.IN_TRANSIT
        assert not result.metadata

    def test_no_messages_yet(self):
        result = self._status({"messages": []})
        assert result.status == BridgeStatusEnum.PENDING

    def test_api_error_degrades_to_pending(self):
        result = self._status(ValueError("iris down"))
        assert result.status == BridgeStatusEnum.PENDING
        assert "iris down" in (result.error or "")

    def test_empty_tx_hash(self):
        adapter = CCTPAdapter()
        result = _run(adapter.check_status("", 1, 8453))
        assert result.status == BridgeStatusEnum.PENDING


class TestEnablement:
    def test_default_off(self, monkeypatch):
        monkeypatch.delenv("CCTP_ENABLED", raising=False)
        assert cctp_enabled() is False

    def test_enabled(self, monkeypatch):
        monkeypatch.setenv("CCTP_ENABLED", "1")
        assert cctp_enabled() is True
