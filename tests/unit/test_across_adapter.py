"""Tests for the Across Protocol bridge adapter (Base ↔ Ethereum).

Mocking policy (matches test_cross_chain_primitive.py):
  - Real adapter, real ABI encoding — no mocking
  - Mock: the Across REST API (suggested-fees, deposit/status)
  - Mock: RPC (receipt lookup for depositId extraction)
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.cross_chain

from eth_abi import decode as abi_decode

from minotaur_subnet.bridge.across import (
    ACROSS_API,
    AcrossAdapter,
    DEPOSIT_V3_SELECTOR,
    FILL_DEADLINE_S,
    SPOKE_POOLS,
    TOKEN_MAP,
    V3_FUNDS_DEPOSITED_TOPIC,
    ZERO_ADDRESS,
)
from minotaur_subnet.bridge.base import BridgeQuote, BridgeStatusEnum
from minotaur_subnet.bridge.registry import BridgeRegistry
from minotaur_subnet.shared.types import _BRIDGE_CALL_SELECTORS

WETH_ETH = TOKEN_MAP["WETH"][1]
WETH_BASE = TOKEN_MAP["WETH"][8453]
USDC_ETH = TOKEN_MAP["USDC"][1]
USDC_BASE = TOKEN_MAP["USDC"][8453]
USER = "0x" + "aa" * 20
AMOUNT = 10**18
QUOTE_TS = 1_753_000_000


def _run(coro):
    return asyncio.run(coro)


def _api_fees_response(**overrides) -> dict[str, Any]:
    base = {
        "totalRelayFee": {"total": str(AMOUNT // 2500), "pct": "0.0004"},
        "timestamp": str(QUOTE_TS),
        "isAmountTooLow": False,
        "spokePoolAddress": SPOKE_POOLS[1],
    }
    base.update(overrides)
    return base


def _quoted(adapter: AcrossAdapter, **fee_overrides) -> BridgeQuote:
    """Get a quote with the fees API mocked."""
    async def _fake_fetch(*args, **kwargs):
        return _api_fees_response(**fee_overrides)

    with patch.object(adapter, "_fetch_suggested_fees", _fake_fetch):
        return _run(adapter.quote(WETH_ETH, AMOUNT, 1, 8453))


# ═══════════════════════════════════════════════════════════════════════════
#  Routes and pair resolution
# ═══════════════════════════════════════════════════════════════════════════


class TestRoutes:
    def test_supported_routes_both_directions(self):
        routes = AcrossAdapter().supported_routes()
        assert (1, 8453) in routes
        assert (8453, 1) in routes
        assert len(routes) == 2

    def test_resolve_pair_by_address(self):
        adapter = AcrossAdapter()
        sym, src, dst = adapter._resolve_pair(WETH_ETH.lower(), 1, 8453)
        assert (sym, src, dst) == ("WETH", WETH_ETH, WETH_BASE)
        sym, src, dst = adapter._resolve_pair(USDC_BASE, 8453, 1)
        assert (sym, src, dst) == ("USDC", USDC_BASE, USDC_ETH)

    def test_resolve_pair_by_symbol(self):
        adapter = AcrossAdapter()
        assert adapter._resolve_pair("weth", 1, 8453)[0] == "WETH"

    def test_resolve_pair_unknown(self):
        adapter = AcrossAdapter()
        assert adapter._resolve_pair("0x" + "12" * 20, 1, 8453) is None
        # Unsupported chain pair
        assert adapter._resolve_pair(WETH_ETH, 1, 42161) is None


# ═══════════════════════════════════════════════════════════════════════════
#  Quoting
# ═══════════════════════════════════════════════════════════════════════════


class TestQuote:
    def test_quote_fields(self):
        adapter = AcrossAdapter()
        q = _quoted(adapter)
        fee = AMOUNT // 2500
        assert q.protocol == "across"
        assert q.token_in == WETH_ETH
        assert q.token_out == WETH_BASE
        assert q.amount_in == AMOUNT
        assert q.fee == fee
        assert q.estimated_output == AMOUNT - fee
        assert q.metadata["spoke_pool"] == SPOKE_POOLS[1]
        assert q.metadata["quote_timestamp"] == QUOTE_TS
        assert q.metadata["fill_deadline"] == QUOTE_TS + FILL_DEADLINE_S

    def test_quote_amount_too_low_raises(self):
        adapter = AcrossAdapter()
        with pytest.raises(ValueError, match="below route minimum"):
            _quoted(adapter, isAmountTooLow=True)

    def test_quote_fee_exceeds_amount_raises(self):
        adapter = AcrossAdapter()
        with pytest.raises(ValueError, match="consumes the whole amount"):
            _quoted(adapter, totalRelayFee={"total": str(AMOUNT * 2)})

    def test_quote_unknown_route_raises(self):
        adapter = AcrossAdapter()
        with pytest.raises(ValueError, match="No Across route"):
            _run(adapter.quote("0x" + "12" * 20, AMOUNT, 1, 8453))

    def test_quote_api_failure_propagates(self):
        adapter = AcrossAdapter()

        async def _boom(*args, **kwargs):
            raise ValueError("Across suggested-fees HTTP 500")

        with patch.object(adapter, "_fetch_suggested_fees", _boom):
            with pytest.raises(ValueError, match="HTTP 500"):
                _run(adapter.quote(WETH_ETH, AMOUNT, 1, 8453))

    def test_registry_routes_to_across(self):
        reg = BridgeRegistry()
        adapter = AcrossAdapter()
        reg.register(adapter)

        async def _fake_fetch(*args, **kwargs):
            return _api_fees_response()

        with patch.object(adapter, "_fetch_suggested_fees", _fake_fetch):
            q = _run(reg.best_quote(WETH_ETH, AMOUNT, 1, 8453))
        assert q is not None and q.protocol == "across"
        assert reg.find_bridge(1, 8453) == [adapter]


# ═══════════════════════════════════════════════════════════════════════════
#  Interaction building
# ═══════════════════════════════════════════════════════════════════════════


class TestBuildInteractions:
    def test_approve_and_deposit(self):
        adapter = AcrossAdapter()
        q = _quoted(adapter)
        ixs = adapter.build_bridge_interactions(q, USER)
        assert len(ixs) == 2

        approve, deposit = ixs
        # 1. approve(spokePool, amount) on the input token
        assert approve.target == WETH_ETH
        assert approve.chain_id == 1
        assert approve.call_data.startswith("0x095ea7b3")
        spender, amount = abi_decode(
            ["address", "uint256"], bytes.fromhex(approve.call_data[10:]),
        )
        assert spender.lower() == SPOKE_POOLS[1].lower()
        assert amount == AMOUNT

        # 2. depositV3 on the SpokePool
        assert deposit.target == SPOKE_POOLS[1]
        assert deposit.chain_id == 1
        assert deposit.value == "0"
        assert deposit.call_data[2:10] == DEPOSIT_V3_SELECTOR.hex()
        args = abi_decode(
            [
                "address", "address", "address", "address",
                "uint256", "uint256", "uint256", "address",
                "uint32", "uint32", "uint32", "bytes",
            ],
            bytes.fromhex(deposit.call_data[10:]),
        )
        (depositor, recipient, tok_in, tok_out, amt_in, amt_out,
         dst_chain, excl_relayer, quote_ts, fill_deadline, excl_deadline,
         message) = args
        # depositor AND recipient pinned to the platform-provided sender —
        # refund-on-expiry and destination fill both go there.
        assert depositor.lower() == USER.lower()
        assert recipient.lower() == USER.lower()
        assert tok_in.lower() == WETH_ETH.lower()
        assert tok_out.lower() == WETH_BASE.lower()
        assert amt_in == AMOUNT
        assert amt_out == q.estimated_output
        assert dst_chain == 8453
        assert excl_relayer == ZERO_ADDRESS
        assert quote_ts == QUOTE_TS
        assert fill_deadline == QUOTE_TS + FILL_DEADLINE_S
        assert excl_deadline == 0
        assert message == b""

    def test_mock_config_replaces_deposit_only(self):
        adapter = AcrossAdapter()
        q = _quoted(adapter)
        cfg = adapter.mock_config(q)
        assert cfg["selectors"] == [DEPOSIT_V3_SELECTOR.hex()]
        assert cfg["mock_type"] == "erc20_transfer"
        assert cfg["mock_token"] == WETH_ETH
        assert cfg["mock_amount"] == AMOUNT

    def test_deposit_selector_in_solver_ban_list(self):
        # The compiler bans bridge selectors in solver legs; the constant in
        # shared/types.py must stay in sync with the adapter's real selector.
        assert DEPOSIT_V3_SELECTOR.hex() in _BRIDGE_CALL_SELECTORS


# ═══════════════════════════════════════════════════════════════════════════
#  Status checks
# ═══════════════════════════════════════════════════════════════════════════


def _receipt(status: int = 1, with_event: bool = True, deposit_id: int = 42):
    logs = []
    if with_event:
        logs.append({
            "topics": [
                bytes.fromhex(V3_FUNDS_DEPOSITED_TOPIC),
                (8453).to_bytes(32, "big"),        # destinationChainId
                deposit_id.to_bytes(32, "big"),     # depositId
            ],
        })
    return {"status": status, "logs": logs}


def _mock_w3(receipt):
    w3 = MagicMock()
    w3.eth.get_transaction_receipt.return_value = receipt
    return w3


class TestCheckStatus:
    TX = "0x" + "ab" * 32

    def _status_with(self, receipt, api_status: str | None):
        """Run check_status with the RPC mocked and, when api_status is set,
        the deposit/status HTTP call stubbed to that verdict."""
        adapter = AcrossAdapter()

        async def _fake_api(tx_hash, src_chain_id, deposit_id):
            return self._parse(api_status, tx_hash)

        with patch(
            "minotaur_subnet.blockchain.chains.get_web3",
            return_value=_mock_w3(receipt),
        ):
            if api_status is None:
                return _run(adapter.check_status(self.TX, 1, 8453))
            with patch.object(adapter, "_check_deposit_status", _fake_api):
                return _run(adapter.check_status(self.TX, 1, 8453))

    @staticmethod
    def _parse(api_status, tx_hash):
        # Mirror of the adapter's verdict mapping, used to stub the HTTP layer.
        from minotaur_subnet.bridge.base import BridgeStatus
        if api_status == "filled":
            return BridgeStatus(
                status=BridgeStatusEnum.COMPLETED, src_tx_hash=tx_hash,
                dst_tx_hash="0xfill",
            )
        if api_status == "expired":
            return BridgeStatus(
                status=BridgeStatusEnum.FAILED, src_tx_hash=tx_hash,
                error="expired",
            )
        return BridgeStatus(
            status=BridgeStatusEnum.IN_TRANSIT, src_tx_hash=tx_hash,
        )

    def test_extract_deposit_id(self):
        adapter = AcrossAdapter()
        with patch(
            "minotaur_subnet.blockchain.chains.get_web3",
            return_value=_mock_w3(_receipt(deposit_id=1337)),
        ):
            assert adapter._extract_deposit_id(self.TX, 1) == 1337

    def test_extract_deposit_id_string_topics(self):
        adapter = AcrossAdapter()
        receipt = {
            "status": 1,
            "logs": [{
                "topics": [
                    "0x" + V3_FUNDS_DEPOSITED_TOPIC,
                    "0x" + (8453).to_bytes(32, "big").hex(),
                    "0x" + (7).to_bytes(32, "big").hex(),
                ],
            }],
        }
        with patch(
            "minotaur_subnet.blockchain.chains.get_web3",
            return_value=_mock_w3(receipt),
        ):
            assert adapter._extract_deposit_id(self.TX, 1) == 7

    def test_failed_source_tx(self):
        result = self._status_with(_receipt(status=0), api_status=None)
        assert result.status == BridgeStatusEnum.FAILED

    def test_no_deposit_event(self):
        result = self._status_with(_receipt(with_event=False), api_status=None)
        assert result.status == BridgeStatusEnum.FAILED

    def test_filled(self):
        result = self._status_with(_receipt(), api_status="filled")
        assert result.status == BridgeStatusEnum.COMPLETED
        assert result.dst_tx_hash == "0xfill"

    def test_expired_is_failed(self):
        result = self._status_with(_receipt(), api_status="expired")
        assert result.status == BridgeStatusEnum.FAILED

    def test_pending_fill(self):
        result = self._status_with(_receipt(), api_status="pending")
        assert result.status == BridgeStatusEnum.IN_TRANSIT

    def test_empty_tx_hash_pending(self):
        adapter = AcrossAdapter()
        result = _run(adapter.check_status("", 1, 8453))
        assert result.status == BridgeStatusEnum.PENDING

    def test_rpc_error_degrades_to_pending(self):
        adapter = AcrossAdapter()
        w3 = MagicMock()
        w3.eth.get_transaction_receipt.side_effect = RuntimeError("rpc down")
        with patch(
            "minotaur_subnet.blockchain.chains.get_web3", return_value=w3,
        ):
            result = _run(adapter.check_status(self.TX, 1, 8453))
        assert result.status == BridgeStatusEnum.PENDING
