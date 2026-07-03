"""Benchmark must fund APP-mode protocol-fee settlement for BOTH contract
generations.

Regression for the residual empty-revert class observed on the prod lead's
shadow-vote, extended for V2 (issue #521):

- **V1** ``DexAggregatorApp`` (AppIntentBase) settles the APP-mode fee in
  WETH pulled from ``appPaymaster`` via ``safeTransferFrom``. On a fork the
  paymaster has no WETH (and no allowance to the fork instance), so every
  ``tokenOut != WETH`` scenario reverts with empty data at the fee step.
- **V2** ``DexAggregatorAppV2`` (AppIntentBaseV2) pays the fee via
  ``safeTransfer`` from a WETH float held by the APP CONTRACT itself. On a
  fresh fork that balance is 0, so EVERY nonzero-fee order reverts at
  ``_verifyFeeSettlementPost`` — same benchmark-signal starvation one hop
  over. V2 still exposes ``appPaymaster()`` (informational), so the app
  float must be funded regardless of what that view returns — in
  particular for our own deployer, which passes ``appPaymaster = 0x0``.

The simulator funds both models unconditionally: an account the app never
draws from is inert.
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


def _dealt_to(deal: MagicMock) -> dict[str, int]:
    """{recipient: amount} across all _deal_erc20 calls (all must be WETH)."""
    out: dict[str, int] = {}
    for call in deal.call_args_list:
        tok, to, amt = call.args[:3]
        assert tok == WETH
        out[to] = amt
    return out


def test_funds_paymaster_and_app_float(monkeypatch) -> None:
    """App with a real paymaster (V1 wiring, or V2 keeping the informational
    pointer): BOTH the paymaster (V1 pull) and the app itself (V2 float) get
    WETH; only the paymaster needs an allowance."""
    sim = _sim()
    views = {b"appPaymaster()": PAYMASTER, b"wrappedNativeToken()": WETH}
    monkeypatch.setattr(sim, "_read_view_address", lambda target, sig: views.get(sig))
    deal, allow, imp = MagicMock(return_value=True), MagicMock(), MagicMock()
    monkeypatch.setattr(sim, "_deal_erc20", deal)
    monkeypatch.setattr(sim, "_set_erc20_allowance", allow)
    monkeypatch.setattr(sim, "_impersonate", imp)

    sim._fund_app_paymaster(CONTRACT, RELAYER)

    dealt = _dealt_to(deal)
    assert dealt.keys() == {PAYMASTER, CONTRACT}
    assert all(amt > 0 for amt in dealt.values())
    # THIS fork-deployed contract approved to pull the paymaster's WETH.
    allow.assert_called_once()
    a_tok, a_owner, a_spender = allow.call_args.args[:3]
    assert a_tok == WETH and a_owner == PAYMASTER and a_spender == CONTRACT
    # Relayer re-impersonated for the subsequent scoreIntent tx.
    imp.assert_called_with(RELAYER)


def test_funds_app_float_when_paymaster_zero(monkeypatch) -> None:
    """The deployer passes ``appPaymaster = 0x0`` — a V2 app deployed that way
    still needs its float funded (this was the early-return hole in the
    pre-#521 code: paymaster==0 skipped ALL funding)."""
    sim = _sim()
    views = {b"appPaymaster()": ZERO, b"wrappedNativeToken()": WETH}
    monkeypatch.setattr(sim, "_read_view_address", lambda target, sig: views.get(sig))
    deal, allow, imp = MagicMock(return_value=True), MagicMock(), MagicMock()
    monkeypatch.setattr(sim, "_deal_erc20", deal)
    monkeypatch.setattr(sim, "_set_erc20_allowance", allow)
    monkeypatch.setattr(sim, "_impersonate", imp)

    sim._fund_app_paymaster(CONTRACT, RELAYER)

    assert _dealt_to(deal).keys() == {CONTRACT}
    allow.assert_not_called()  # nobody to approve from
    imp.assert_called_with(RELAYER)


def test_funds_app_float_when_paymaster_view_absent(monkeypatch) -> None:
    """A fee app without an ``appPaymaster()`` getter at all (view reverts →
    None) still gets its float."""
    sim = _sim()
    views = {b"wrappedNativeToken()": WETH}
    monkeypatch.setattr(sim, "_read_view_address", lambda target, sig: views.get(sig))
    deal, allow = MagicMock(return_value=True), MagicMock()
    monkeypatch.setattr(sim, "_deal_erc20", deal)
    monkeypatch.setattr(sim, "_set_erc20_allowance", allow)
    monkeypatch.setattr(sim, "_impersonate", MagicMock())

    sim._fund_app_paymaster(CONTRACT, RELAYER)

    assert _dealt_to(deal).keys() == {CONTRACT}
    allow.assert_not_called()


def test_noop_without_wrapped_native(monkeypatch) -> None:
    """No ``wrappedNativeToken()`` (non-fee app) -> nothing to fund."""
    sim = _sim()
    monkeypatch.setattr(sim, "_read_view_address", lambda target, sig: None)
    deal, allow = MagicMock(), MagicMock()
    monkeypatch.setattr(sim, "_deal_erc20", deal)
    monkeypatch.setattr(sim, "_set_erc20_allowance", allow)
    monkeypatch.setattr(sim, "_impersonate", MagicMock())

    sim._fund_app_paymaster(CONTRACT, RELAYER)

    deal.assert_not_called()
    allow.assert_not_called()


def test_noop_on_zero_wrapped_native(monkeypatch) -> None:
    sim = _sim()
    views = {b"appPaymaster()": PAYMASTER, b"wrappedNativeToken()": ZERO}
    monkeypatch.setattr(sim, "_read_view_address", lambda target, sig: views.get(sig))
    deal = MagicMock()
    monkeypatch.setattr(sim, "_deal_erc20", deal)
    monkeypatch.setattr(sim, "_set_erc20_allowance", MagicMock())
    monkeypatch.setattr(sim, "_impersonate", MagicMock())

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
