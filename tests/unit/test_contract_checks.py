"""Tests for startup contract-presence verification."""

from __future__ import annotations

from unittest.mock import patch, MagicMock

import pytest

from minotaur_subnet.api.contract_checks import (
    ContractPresenceError,
    verify_required_contracts,
)


def _w3_with_code(code: bytes):
    w3 = MagicMock()
    w3.eth.get_code.return_value = code
    return w3


def test_all_unset_returns_empty_and_does_not_call_rpc(monkeypatch):
    # Clear every env var this check inspects.
    for var in (
        "VALIDATOR_REGISTRY_8453", "VALIDATOR_REGISTRY_1", "VALIDATOR_REGISTRY_964",
        "CHAMPION_REGISTRY_964",
        "APP_INTENT_BASE_8453", "APP_INTENT_BASE_1", "APP_INTENT_BASE_964",
    ):
        monkeypatch.delenv(var, raising=False)

    with patch("web3.Web3") as W3:
        assert verify_required_contracts() == []
        W3.assert_not_called()


def test_configured_address_with_code_passes(monkeypatch):
    monkeypatch.setenv("VALIDATOR_REGISTRY_8453", "0x" + "11" * 20)
    monkeypatch.setenv("BASE_RPC_URL", "http://stub")
    for var in (
        "VALIDATOR_REGISTRY_1", "VALIDATOR_REGISTRY_964",
        "CHAMPION_REGISTRY_964",
        "APP_INTENT_BASE_8453", "APP_INTENT_BASE_1", "APP_INTENT_BASE_964",
    ):
        monkeypatch.delenv(var, raising=False)

    with patch("web3.Web3") as W3:
        W3.is_address.return_value = True
        W3.to_checksum_address = lambda a: a
        W3.return_value = _w3_with_code(b"\x60\x80\x60\x40\x52")

        verified = verify_required_contracts()
        assert verified == ["ValidatorRegistry (Base) @ 0x" + "11" * 20]


def test_empty_bytecode_raises(monkeypatch):
    monkeypatch.setenv("CHAMPION_REGISTRY_964", "0x" + "22" * 20)
    monkeypatch.setenv("BITTENSOR_EVM_RPC_URL", "http://stub")
    for var in (
        "VALIDATOR_REGISTRY_8453", "VALIDATOR_REGISTRY_1", "VALIDATOR_REGISTRY_964",
        "APP_INTENT_BASE_8453", "APP_INTENT_BASE_1", "APP_INTENT_BASE_964",
    ):
        monkeypatch.delenv(var, raising=False)

    with patch("web3.Web3") as W3:
        W3.is_address.return_value = True
        W3.to_checksum_address = lambda a: a
        W3.return_value = _w3_with_code(b"")

        with pytest.raises(ContractPresenceError) as exc:
            verify_required_contracts()
        assert "ChampionRegistry (BT EVM)" in str(exc.value)
        assert "has no bytecode" in str(exc.value)


def test_invalid_address_raises(monkeypatch):
    monkeypatch.setenv("VALIDATOR_REGISTRY_8453", "not-an-address")
    monkeypatch.setenv("BASE_RPC_URL", "http://stub")
    for var in (
        "VALIDATOR_REGISTRY_1", "VALIDATOR_REGISTRY_964",
        "CHAMPION_REGISTRY_964",
        "APP_INTENT_BASE_8453", "APP_INTENT_BASE_1", "APP_INTENT_BASE_964",
    ):
        monkeypatch.delenv(var, raising=False)

    with patch("web3.Web3") as W3:
        W3.is_address.return_value = False

        with pytest.raises(ContractPresenceError) as exc:
            verify_required_contracts()
        assert "is not a valid EVM address" in str(exc.value)


def test_missing_rpc_for_configured_address(monkeypatch):
    monkeypatch.setenv("CHAMPION_REGISTRY_964", "0x" + "33" * 20)
    monkeypatch.delenv("BITTENSOR_EVM_RPC_URL", raising=False)
    monkeypatch.delenv("BITTENSOR_EVM_FORK_RPC_URL", raising=False)
    for var in (
        "VALIDATOR_REGISTRY_8453", "VALIDATOR_REGISTRY_1", "VALIDATOR_REGISTRY_964",
        "APP_INTENT_BASE_8453", "APP_INTENT_BASE_1", "APP_INTENT_BASE_964",
    ):
        monkeypatch.delenv(var, raising=False)

    with patch("web3.Web3") as W3:
        W3.is_address.return_value = True

        with pytest.raises(ContractPresenceError) as exc:
            verify_required_contracts()
        assert "no RPC is configured for chain 964" in str(exc.value)


def test_rpc_error_reported_not_swallowed(monkeypatch):
    monkeypatch.setenv("CHAMPION_REGISTRY_964", "0x" + "44" * 20)
    monkeypatch.setenv("BITTENSOR_EVM_RPC_URL", "http://stub")
    for var in (
        "VALIDATOR_REGISTRY_8453", "VALIDATOR_REGISTRY_1", "VALIDATOR_REGISTRY_964",
        "APP_INTENT_BASE_8453", "APP_INTENT_BASE_1", "APP_INTENT_BASE_964",
    ):
        monkeypatch.delenv(var, raising=False)

    with patch("web3.Web3") as W3:
        W3.is_address.return_value = True
        W3.to_checksum_address = lambda a: a
        bad = MagicMock()
        bad.eth.get_code.side_effect = ConnectionError("rpc down")
        W3.return_value = bad

        with pytest.raises(ContractPresenceError) as exc:
            verify_required_contracts()
        assert "rpc down" in str(exc.value)


def test_multiple_errors_all_reported(monkeypatch):
    # Two distinct failures should both surface.
    monkeypatch.setenv("CHAMPION_REGISTRY_964", "not-an-address")
    monkeypatch.setenv("VALIDATOR_REGISTRY_8453", "0x" + "55" * 20)
    monkeypatch.setenv("BASE_RPC_URL", "http://stub")
    for var in (
        "VALIDATOR_REGISTRY_1", "VALIDATOR_REGISTRY_964",
        "APP_INTENT_BASE_8453", "APP_INTENT_BASE_1", "APP_INTENT_BASE_964",
    ):
        monkeypatch.delenv(var, raising=False)

    with patch("web3.Web3") as W3:
        W3.is_address.side_effect = lambda a: a.startswith("0x")
        W3.to_checksum_address = lambda a: a
        W3.return_value = _w3_with_code(b"")

        with pytest.raises(ContractPresenceError) as exc:
            verify_required_contracts()
        msg = str(exc.value)
        assert "ChampionRegistry" in msg
        assert "ValidatorRegistry" in msg
