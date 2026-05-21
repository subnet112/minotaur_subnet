"""Tests for the on-chain AppRegistry cross-check cache."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from minotaur_subnet.consensus import app_registry_cache as arc

REGISTERED = b"\x00" * 31 + b"\x01"    # bytes32 with non-zero — looks like a real appId
UNREGISTERED = b"\x00" * 32            # bytes32(0) — registry sentinel for "unknown"


@pytest.fixture(autouse=True)
def _clear_cache():
    arc.clear_cache()
    yield
    arc.clear_cache()


def test_off_when_registry_env_unset_returns_true(monkeypatch):
    """No APP_REGISTRY_{chain_id} → enforcement off → always True."""
    monkeypatch.delenv("APP_REGISTRY_8453", raising=False)
    assert arc.enforce_enabled(8453) is False
    assert arc.is_registered_app("0x" + "aa" * 20, 8453) is True


def test_enabled_queries_registry_and_caches(monkeypatch):
    monkeypatch.setenv("APP_REGISTRY_8453", "0x" + "11" * 20)
    monkeypatch.setenv("BASE_RPC_URL", "http://stub")

    w3_instance = MagicMock()
    w3_instance.eth.contract.return_value.functions.appByContract.return_value.call.return_value = REGISTERED

    with patch("web3.Web3") as W3:
        W3.HTTPProvider.return_value = object()
        W3.to_checksum_address = lambda a: a
        W3.return_value = w3_instance

        addr = "0x" + "bb" * 20
        assert arc.is_registered_app(addr, 8453) is True
        # Second call hits cache, no extra Web3() construction.
        assert arc.is_registered_app(addr, 8453) is True
        assert W3.call_count == 1


def test_enabled_returns_false_when_not_registered(monkeypatch):
    monkeypatch.setenv("APP_REGISTRY_8453", "0x" + "11" * 20)
    monkeypatch.setenv("BASE_RPC_URL", "http://stub")

    w3_instance = MagicMock()
    w3_instance.eth.contract.return_value.functions.appByContract.return_value.call.return_value = UNREGISTERED

    with patch("web3.Web3") as W3:
        W3.HTTPProvider.return_value = object()
        W3.to_checksum_address = lambda a: a
        W3.return_value = w3_instance

        assert arc.is_registered_app("0x" + "cc" * 20, 8453) is False


def test_fail_open_when_rpc_missing(monkeypatch):
    """Fail-open on missing RPC — on-chain _requireRegistered is the real gate."""
    monkeypatch.setenv("APP_REGISTRY_8453", "0x" + "11" * 20)
    monkeypatch.delenv("BASE_RPC_URL", raising=False)

    assert arc.is_registered_app("0x" + "dd" * 20, 8453) is True


def test_fail_open_on_rpc_error(monkeypatch):
    monkeypatch.setenv("APP_REGISTRY_8453", "0x" + "11" * 20)
    monkeypatch.setenv("BASE_RPC_URL", "http://stub")

    with patch("web3.Web3") as W3:
        W3.HTTPProvider.return_value = object()
        W3.to_checksum_address = lambda a: a
        bad = MagicMock()
        bad.eth.contract.side_effect = ConnectionError("rpc down")
        W3.return_value = bad

        assert arc.is_registered_app("0x" + "ee" * 20, 8453) is True


def test_empty_contract_address_returns_true(monkeypatch):
    """Defensive: empty string contract address is a no-op (caller mistake,
    not the registry's job to flag)."""
    monkeypatch.setenv("APP_REGISTRY_8453", "0x" + "11" * 20)
    monkeypatch.setenv("BASE_RPC_URL", "http://stub")

    assert arc.is_registered_app("", 8453) is True


def test_cache_segments_by_chain(monkeypatch):
    """Same contract address on different chains must be queried separately."""
    monkeypatch.setenv("APP_REGISTRY_8453", "0x" + "11" * 20)
    monkeypatch.setenv("APP_REGISTRY_964", "0x" + "22" * 20)
    monkeypatch.setenv("BASE_RPC_URL", "http://base")
    monkeypatch.setenv("BITTENSOR_EVM_RPC_URL", "http://btevm")

    def make_w3(rpc_provider):
        w3 = MagicMock()

        def _contract_wrapper(address, abi):
            # Route the mock by which registry address was bound.
            chain_addr = address.lower()
            is_base_registry = chain_addr == ("0x" + "11" * 20).lower()
            response = REGISTERED if is_base_registry else UNREGISTERED
            contract = MagicMock()
            contract.functions.appByContract.return_value.call.return_value = response
            return contract

        w3.eth.contract = _contract_wrapper
        return w3

    with patch("web3.Web3") as W3:
        W3.HTTPProvider.return_value = object()
        W3.to_checksum_address = lambda a: a
        W3.side_effect = lambda _provider, **_kw: make_w3(_provider)

        addr = "0x" + "ff" * 20
        assert arc.is_registered_app(addr, 8453) is True
        assert arc.is_registered_app(addr, 964) is False


def test_cache_expiry_re_queries(monkeypatch):
    """After TTL elapses, a new RPC call must happen."""
    monkeypatch.setenv("APP_REGISTRY_8453", "0x" + "11" * 20)
    monkeypatch.setenv("BASE_RPC_URL", "http://stub")

    w3_instance = MagicMock()
    w3_instance.eth.contract.return_value.functions.appByContract.return_value.call.return_value = REGISTERED

    with patch("web3.Web3") as W3, patch("minotaur_subnet.consensus.app_registry_cache.time.time") as mock_time:
        W3.HTTPProvider.return_value = object()
        W3.to_checksum_address = lambda a: a
        W3.return_value = w3_instance

        mock_time.return_value = 1000.0
        addr = "0x" + "bb" * 20
        assert arc.is_registered_app(addr, 8453) is True
        # Still within TTL
        mock_time.return_value = 1004.0
        assert arc.is_registered_app(addr, 8453) is True
        # Past TTL → re-query
        mock_time.return_value = 1006.0
        assert arc.is_registered_app(addr, 8453) is True
        assert W3.call_count == 2
