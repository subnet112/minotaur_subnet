"""Benchmark must fund the app fee paymaster's wrapped-native float.

Regression for the residual empty-revert class observed on the prod lead's
shadow-vote: ``DexAggregatorApp`` runs in ``FeeMode.APP`` and settles the
protocol fee in WETH — from the swap output when ``tokenOut == WETH``,
otherwise pulled from ``appPaymaster`` via ``safeTransferFrom``. On a fork
the paymaster has no WETH (and no allowance to the fork-deployed contract),
so every ``tokenOut != WETH`` scenario reverts with empty data
(``('execution reverted', '0x')``) at the fee step — scoring 0 regardless of
solver quality. The simulator must fund + approve the paymaster, mirroring
the user-input funding it already does.
"""
from __future__ import annotations

from unittest.mock import MagicMock

from minotaur_subnet.simulator.anvil_simulator import AnvilSimulator

PAYMASTER = "0x" + "11" * 20
WETH = "0x4200000000000000000000000000000000000006"
CONTRACT = "0x" + "22" * 20
RELAYER = "0x" + "33" * 20
ZERO = "0x" + "00" * 20


def _sim() -> AnvilSimulator:
    # Web3 HTTPProvider is lazy — no live Anvil needed for these unit tests.
    return AnvilSimulator(rpc_url="http://anvil.invalid:8545")


def test_fund_app_paymaster_deals_and_approves_weth(monkeypatch) -> None:
    sim = _sim()
    views = {b"appPaymaster()": PAYMASTER, b"wrappedNativeToken()": WETH}
    monkeypatch.setattr(sim, "_read_view_address", lambda target, sig: views.get(sig))
    deal, allow, imp = MagicMock(return_value=True), MagicMock(), MagicMock()
    monkeypatch.setattr(sim, "_deal_erc20", deal)
    monkeypatch.setattr(sim, "_set_erc20_allowance", allow)
    monkeypatch.setattr(sim, "_impersonate", imp)

    sim._fund_app_paymaster(CONTRACT, RELAYER)

    # WETH dealt to the PAYMASTER (not the user or contract), positive amount.
    deal.assert_called_once()
    tok, to, amt = deal.call_args.args[:3]
    assert tok == WETH and to == PAYMASTER and amt > 0
    # THIS fork-deployed contract approved to pull the paymaster's WETH.
    allow.assert_called_once()
    a_tok, a_owner, a_spender = allow.call_args.args[:3]
    assert a_tok == WETH and a_owner == PAYMASTER and a_spender == CONTRACT
    # Relayer re-impersonated for the subsequent scoreIntent tx.
    imp.assert_called_with(RELAYER)


def test_fund_app_paymaster_noop_without_paymaster(monkeypatch) -> None:
    # appPaymaster() reverts (non-fee / USER-mode app) -> None -> skip.
    sim = _sim()
    monkeypatch.setattr(sim, "_read_view_address", lambda target, sig: None)
    deal, allow = MagicMock(), MagicMock()
    monkeypatch.setattr(sim, "_deal_erc20", deal)
    monkeypatch.setattr(sim, "_set_erc20_allowance", allow)

    sim._fund_app_paymaster(CONTRACT, RELAYER)

    deal.assert_not_called()
    allow.assert_not_called()


def test_fund_app_paymaster_noop_on_zero_address(monkeypatch) -> None:
    sim = _sim()
    views = {b"appPaymaster()": ZERO, b"wrappedNativeToken()": WETH}
    monkeypatch.setattr(sim, "_read_view_address", lambda target, sig: views.get(sig))
    deal = MagicMock()
    monkeypatch.setattr(sim, "_deal_erc20", deal)
    monkeypatch.setattr(sim, "_set_erc20_allowance", MagicMock())

    sim._fund_app_paymaster(CONTRACT, RELAYER)

    deal.assert_not_called()


def test_read_view_address_parses_address() -> None:
    sim = _sim()
    sim.w3 = MagicMock()
    sim.w3.eth.call.return_value = bytes(12) + bytes.fromhex(PAYMASTER[2:])
    out = sim._read_view_address(CONTRACT, b"appPaymaster()")
    assert out is not None and out.lower() == PAYMASTER.lower()


def test_read_view_address_returns_none_on_revert() -> None:
    sim = _sim()
    sim.w3 = MagicMock()
    sim.w3.eth.call.side_effect = Exception("execution reverted")
    assert sim._read_view_address(CONTRACT, b"appPaymaster()") is None


def test_read_view_address_returns_none_on_short_result() -> None:
    sim = _sim()
    sim.w3 = MagicMock()
    sim.w3.eth.call.return_value = b"\x00" * 4  # too short to hold an address word
    assert sim._read_view_address(CONTRACT, b"appPaymaster()") is None
