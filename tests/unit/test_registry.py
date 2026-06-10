"""AppRegistry resolution for the scoring lab — app_id -> contractAddr from chain.

The lab never hardcodes app addresses; it resolves them from the on-chain
AppRegistry like production. These tests cover the resolver logic + the bytes32
appId coercion with a lightweight fake w3 (no chain).
"""
import pytest
from web3 import Web3

from minotaur_subnet.harness.scoring_lab.registry import (
    APP_REGISTRY_ABI,
    is_registered,
    resolve_contract,
    to_bytes32,
)

_DEV = "0x" + "11" * 20
_CONTRACT = Web3.to_checksum_address("0x" + "ab" * 20)
_ZERO = "0x" + "00" * 20


class _Call:
    def __init__(self, v):
        self._v = v

    def call(self):
        return self._v


class _Fns:
    def __init__(self, getapp=None, byc=None):
        self._g, self._b = getapp, byc

    def getApp(self, _appid):
        return _Call(self._g)

    def appByContract(self, _addr):
        return _Call(self._b)


class _W3:
    """Minimal web3 stand-in: returns a contract whose functions are pre-baked."""
    def __init__(self, getapp=None, byc=None):
        self._fns = _Fns(getapp, byc)
        self.eth = self

    def contract(self, address=None, abi=None):
        return type("C", (), {"functions": self._fns})()

    @staticmethod
    def to_checksum_address(a):
        return Web3.to_checksum_address(a)


def test_to_bytes32_accepts_hex_and_pads():
    assert to_bytes32("0x" + "ab" * 32) == bytes.fromhex("ab" * 32)
    assert to_bytes32("0x1234") == bytes.fromhex("1234") + b"\x00" * 30
    assert len(to_bytes32(b"\x01\x02")) == 32


def test_to_bytes32_rejects_non_hex_string():
    with pytest.raises(ValueError):
        to_bytes32("dex")  # must be the on-chain bytes32, not a friendly label


def test_resolve_contract_returns_registered_address():
    # AppRecord tuple = (developer, manifestHash, contractAddr, registeredAt)
    rec = (_DEV, b"\x00" * 32, _CONTRACT, 1700000000)
    w3 = _W3(getapp=rec)
    assert resolve_contract(w3, "0x" + "cc" * 20, "0x" + "de" * 32) == _CONTRACT


def test_resolve_contract_raises_when_unregistered():
    rec = (_DEV, b"\x00" * 32, _ZERO, 0)   # contractAddr == 0 => not registered
    w3 = _W3(getapp=rec)
    with pytest.raises(ValueError, match="not registered"):
        resolve_contract(w3, "0x" + "cc" * 20, "0x" + "de" * 32)


def test_is_registered_reverse_lookup():
    assert is_registered(_W3(byc=b"\x01" + b"\x00" * 31), "0x" + "cc" * 20, _CONTRACT) is True
    assert is_registered(_W3(byc=b"\x00" * 32), "0x" + "cc" * 20, _CONTRACT) is False


def test_abi_is_well_formed():
    # building a contract with the ABI must not raise (validates the ABI shape)
    Web3().eth.contract(abi=APP_REGISTRY_ABI)
