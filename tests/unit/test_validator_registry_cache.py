"""Tests for the on-chain ValidatorRegistry cross-check cache."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from minotaur_subnet.consensus import validator_registry_cache as vrc


@pytest.fixture(autouse=True)
def _clear_cache():
    vrc.clear_cache()
    yield
    vrc.clear_cache()


def test_off_by_default_returns_true(monkeypatch):
    """When enforcement is off, the function is a no-op (True)."""
    monkeypatch.delenv("CONSENSUS_ENFORCE_ONCHAIN_REGISTRY", raising=False)
    assert vrc.enforce_enabled() is False
    assert vrc.is_on_chain_validator("0x" + "aa" * 20, 8453) is True


def test_enabled_queries_registry_and_caches(monkeypatch):
    monkeypatch.setenv("CONSENSUS_ENFORCE_ONCHAIN_REGISTRY", "1")
    monkeypatch.setenv("VALIDATOR_REGISTRY_8453", "0x" + "11" * 20)
    monkeypatch.setenv("BASE_RPC_URL", "http://stub")

    w3_instance = MagicMock()
    w3_instance.eth.contract.return_value.functions.isValidator.return_value.call.return_value = True

    with patch("web3.Web3") as W3:
        W3.HTTPProvider.return_value = object()
        W3.to_checksum_address = lambda a: a
        W3.return_value = w3_instance

        signer = "0x" + "bb" * 20
        assert vrc.is_on_chain_validator(signer, 8453) is True
        # Second call hits the cache, no extra Web3() construction.
        assert vrc.is_on_chain_validator(signer, 8453) is True
        assert W3.call_count == 1


def test_enabled_returns_false_when_not_registered(monkeypatch):
    monkeypatch.setenv("CONSENSUS_ENFORCE_ONCHAIN_REGISTRY", "1")
    monkeypatch.setenv("VALIDATOR_REGISTRY_8453", "0x" + "11" * 20)
    monkeypatch.setenv("BASE_RPC_URL", "http://stub")

    w3_instance = MagicMock()
    w3_instance.eth.contract.return_value.functions.isValidator.return_value.call.return_value = False

    with patch("web3.Web3") as W3:
        W3.HTTPProvider.return_value = object()
        W3.to_checksum_address = lambda a: a
        W3.return_value = w3_instance

        assert vrc.is_on_chain_validator("0x" + "cc" * 20, 8453) is False


def test_fail_open_when_rpc_missing(monkeypatch):
    """Fail-open on missing RPC to avoid deadlocking consensus on outages."""
    monkeypatch.setenv("CONSENSUS_ENFORCE_ONCHAIN_REGISTRY", "1")
    monkeypatch.setenv("VALIDATOR_REGISTRY_8453", "0x" + "11" * 20)
    monkeypatch.delenv("BASE_RPC_URL", raising=False)

    assert vrc.is_on_chain_validator("0x" + "dd" * 20, 8453) is True


def test_fail_open_on_rpc_error(monkeypatch):
    monkeypatch.setenv("CONSENSUS_ENFORCE_ONCHAIN_REGISTRY", "1")
    monkeypatch.setenv("VALIDATOR_REGISTRY_8453", "0x" + "11" * 20)
    monkeypatch.setenv("BASE_RPC_URL", "http://stub")

    with patch("web3.Web3") as W3:
        W3.HTTPProvider.return_value = object()
        W3.to_checksum_address = lambda a: a
        bad = MagicMock()
        bad.eth.contract.side_effect = ConnectionError("rpc down")
        W3.return_value = bad

        assert vrc.is_on_chain_validator("0x" + "ee" * 20, 8453) is True


def test_cache_segments_by_chain(monkeypatch):
    """Same signer address on different chains must be queried separately."""
    monkeypatch.setenv("CONSENSUS_ENFORCE_ONCHAIN_REGISTRY", "1")
    monkeypatch.setenv("VALIDATOR_REGISTRY_8453", "0x" + "11" * 20)
    monkeypatch.setenv("VALIDATOR_REGISTRY_964", "0x" + "22" * 20)
    monkeypatch.setenv("BASE_RPC_URL", "http://base")
    monkeypatch.setenv("BITTENSOR_EVM_RPC_URL", "http://btevm")

    results = {8453: True, 964: False}

    def make_w3(rpc_provider):
        # rpc_provider is the object from HTTPProvider — we look up which chain
        # by inspecting the recent URL the test passed in via env.
        w3 = MagicMock()

        def _call_wrapper(signer):
            # Route the mock based on whichever registry address was bound.
            call_mock = MagicMock()
            # Pick the chain based on the most recent address in the contract
            # registration — return True for 8453, False for 964 via side effect.
            call_mock.call.return_value = results[w3._chain_id]
            return call_mock

        def _contract_wrapper(address, abi):
            # Stash which chain this is so isValidator returns the right thing.
            w3._chain_id = 8453 if address.lower() == ("0x" + "11" * 20).lower() else 964
            contract = MagicMock()
            contract.functions.isValidator = _call_wrapper
            return contract

        w3.eth.contract = _contract_wrapper
        return w3

    with patch("web3.Web3") as W3:
        W3.HTTPProvider.return_value = object()
        W3.to_checksum_address = lambda a: a
        W3.side_effect = lambda _provider, **_kw: make_w3(_provider)

        signer = "0x" + "ff" * 20
        assert vrc.is_on_chain_validator(signer, 8453) is True
        assert vrc.is_on_chain_validator(signer, 964) is False
