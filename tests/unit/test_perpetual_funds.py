"""Unit tests for the perpetual pre-flight funds gate + gasless permit (#1/#3).

The scoring fork fabricates the user's balance, so a broke perpetual would
otherwise pass scoring+quorum+relay every cooldown cycle and only revert at
settlement. ``_perpetual_funds_check`` reads the LIVE balance+allowance and
terminates an unfundable perpetual immediately — unless a carried EIP-2612
permit can cure an allowance shortfall gaslessly.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import minotaur_subnet.blockchain.chains as chains_mod
import minotaur_subnet.blockchain.token_approval as ta_mod
from minotaur_subnet.blockloop.order_processor import OrderProcessor
from minotaur_subnet.orderbook.orderbook import OrderStatus

SPENDER = "0x00000000000000000000000000000000000000A0"
TOKEN = "0x00000000000000000000000000000000000000B0"
USER = "0x00000000000000000000000000000000000000C0"


def _processor(relayer=None):
    p = OrderProcessor.__new__(OrderProcessor)
    p.orderbook = MagicMock()
    p.app_store = MagicMock()
    p.order_persistence = MagicMock()
    p.relayer = relayer or MagicMock()
    return p


def _order(params=None):
    return SimpleNamespace(
        order_id="ord_1", app_id="app_1", perpetual=True, chain_id=8453,
        submitted_by=USER,
        params={"input_token": TOKEN, "input_amount": "1000", "platform_fee_wei": 0,
                **(params or {})},
    )


def _patch_reads(monkeypatch, reading):
    """Patch web3 + the balance/allowance reader to a fixed result."""
    monkeypatch.setattr(chains_mod, "get_web3", lambda cid: MagicMock())
    monkeypatch.setattr(ta_mod, "read_balance_and_allowance",
                        lambda w3, token, owner, spender: reading)


def test_sufficient_funds_proceeds(monkeypatch):
    _patch_reads(monkeypatch, (5000, 5000))
    p = _processor()
    ok = asyncio.run(p._perpetual_funds_check(_order(), SPENDER))
    assert ok is True
    p.orderbook.update_order.assert_not_called()


def test_balance_shortfall_terminates(monkeypatch):
    _patch_reads(monkeypatch, (500, 10_000))  # balance < 1000 required
    p = _processor()
    ok = asyncio.run(p._perpetual_funds_check(_order(), SPENDER))
    assert ok is False
    kwargs = p.orderbook.update_order.call_args.kwargs
    assert kwargs["status"] == OrderStatus.REJECTED
    assert "balance" in kwargs["error"].lower()
    p.app_store.record_execution.assert_not_called()  # blameless — no miner debit


def test_allowance_shortfall_without_permit_terminates(monkeypatch):
    _patch_reads(monkeypatch, (5000, 100))  # allowance < 1000 required, no permit
    p = _processor()
    ok = asyncio.run(p._perpetual_funds_check(_order(), SPENDER))
    assert ok is False
    kwargs = p.orderbook.update_order.call_args.kwargs
    assert kwargs["status"] == OrderStatus.REJECTED
    assert "allowance" in kwargs["error"].lower()
    p.app_store.record_execution.assert_not_called()


def test_allowance_shortfall_cured_by_permit_proceeds(monkeypatch):
    # First read: allowance short. Permit submits OK. Re-read: allowance now ample.
    readings = iter([(5000, 100), (5000, 10_000)])
    monkeypatch.setattr(chains_mod, "get_web3", lambda cid: MagicMock())
    monkeypatch.setattr(ta_mod, "read_balance_and_allowance",
                        lambda *a: next(readings))
    relayer = MagicMock()
    relayer.call_contract_function = AsyncMock(return_value="0xtx")
    p = _processor(relayer)
    order = _order({
        "permit_deadline": 9999999999, "permit_v": 27,
        "permit_r": "0x" + "11" * 32, "permit_s": "0x" + "22" * 32,
    })
    ok = asyncio.run(p._perpetual_funds_check(order, SPENDER))
    assert ok is True
    relayer.call_contract_function.assert_awaited_once()
    # Not terminated
    assert p.orderbook.update_order.call_count == 0


def test_read_failure_fails_open(monkeypatch):
    _patch_reads(monkeypatch, None)  # RPC hiccup — must NOT terminate
    p = _processor()
    ok = asyncio.run(p._perpetual_funds_check(_order(), SPENDER))
    assert ok is True
    p.orderbook.update_order.assert_not_called()


def test_native_input_skips_input_leg(monkeypatch):
    # Native input has no ERC-20 allowance; with fee=0 there is nothing to read.
    called = {"n": 0}

    def _read(*a):
        called["n"] += 1
        return (0, 0)
    monkeypatch.setattr(chains_mod, "get_web3", lambda cid: MagicMock())
    monkeypatch.setattr(ta_mod, "read_balance_and_allowance", _read)
    p = _processor()
    order = _order({"_input_token_is_native": True})
    ok = asyncio.run(p._perpetual_funds_check(order, SPENDER))
    assert ok is True
    assert called["n"] == 0  # no legs read


def test_user_fee_mode_checks_weth_and_terminates(monkeypatch):
    # FeeMode.USER: input OK, but the WETH fee allowance is short → terminate.
    def _read(w3, token, owner, spender):
        return (10_000, 10_000) if token == TOKEN else (10_000, 5)  # WETH short
    monkeypatch.setattr(chains_mod, "get_web3", lambda cid: MagicMock())
    monkeypatch.setattr(ta_mod, "read_balance_and_allowance", _read)
    monkeypatch.setattr(ta_mod, "fee_mode_is_user", lambda w3, c: True)
    p = _processor()
    ok = asyncio.run(p._perpetual_funds_check(_order({"platform_fee_wei": 100}), SPENDER))
    assert ok is False
    kwargs = p.orderbook.update_order.call_args.kwargs
    assert kwargs["status"] == OrderStatus.REJECTED
    p.app_store.record_execution.assert_not_called()


def test_app_fee_mode_skips_weth_leg(monkeypatch):
    # FeeMode.APP (DexAggregator): WETH would read broke, but the fee is deducted
    # from output — the leg must be SKIPPED so the perpetual isn't falsely killed.
    def _read(w3, token, owner, spender):
        return (10_000, 10_000) if token == TOKEN else (0, 0)  # WETH ignored
    monkeypatch.setattr(chains_mod, "get_web3", lambda cid: MagicMock())
    monkeypatch.setattr(ta_mod, "read_balance_and_allowance", _read)
    monkeypatch.setattr(ta_mod, "fee_mode_is_user", lambda w3, c: False)
    p = _processor()
    ok = asyncio.run(p._perpetual_funds_check(_order({"platform_fee_wei": 100}), SPENDER))
    assert ok is True
    p.orderbook.update_order.assert_not_called()


def test_fee_mode_is_user_reads_view():
    from minotaur_subnet.blockchain.token_approval import fee_mode_is_user
    w3u = MagicMock(); w3u.eth.call.return_value = (0).to_bytes(32, "big")
    w3a = MagicMock(); w3a.eth.call.return_value = (1).to_bytes(32, "big")
    w3e = MagicMock(); w3e.eth.call.side_effect = Exception("revert")
    assert fee_mode_is_user(w3u, TOKEN) is True     # 0 = USER
    assert fee_mode_is_user(w3a, TOKEN) is False    # 1 = APP
    assert fee_mode_is_user(w3e, TOKEN) is False    # error → treat as APP (safe)


def test_submit_permit_absent_params_returns_false():
    p = _processor()
    ok = asyncio.run(p._submit_order_permit(_order(), TOKEN, SPENDER))
    assert ok is False


def test_terminate_unfunded_no_miner_debit():
    p = _processor()
    p._terminate_perpetual_unfunded(_order(), "insufficient balance")
    kwargs = p.orderbook.update_order.call_args.kwargs
    assert kwargs["status"] == OrderStatus.REJECTED
    assert "fund" in kwargs["error"].lower()
    p.app_store.record_execution.assert_not_called()
