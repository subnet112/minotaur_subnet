"""Deploy gas fields: type-2 (EIP-1559) with per-chain tip floors + timeouts.

The old path used a legacy `gasPrice = w3.eth.gas_price`, which on Base/ETH is
near the base fee and left deploys stuck (the 0.006-gwei bug). These lock in:
type-2 on base-fee chains, a tip FLOOR so a ~0 node tip can't underprice, and
a maxFee buffer for base-fee rises.
"""
import pytest

from minotaur_subnet.relayer.evm_relayer import (
    _build_deploy_gas_fields,
    _deploy_receipt_timeout,
    _DEPLOY_BASE_FEE_BUFFER,
)


class _Eth:
    def __init__(self, base_fee, tip, gas_price=None):
        self._base = base_fee
        self._tip = tip
        self._gp = gas_price

    def get_block(self, _):
        return {"baseFeePerGas": self._base} if self._base is not None else {}

    @property
    def max_priority_fee(self):
        if self._tip is None:
            raise RuntimeError("no eth_maxPriorityFeePerGas")
        return self._tip

    @property
    def gas_price(self):
        return self._gp


class _W3:
    def __init__(self, **kw):
        self.eth = _Eth(**kw)


def test_ethereum_is_type2_with_1gwei_tip_floor():
    # ETH base 0.077 gwei, node tip ~0.0001 gwei → floored to 1 gwei, type-2.
    w3 = _W3(base_fee=77_000_000, tip=100_000)
    g = _build_deploy_gas_fields(w3, 1)
    assert g["type"] == 2
    assert "gasPrice" not in g, "Ethereum must NOT use a legacy gasPrice"
    assert g["maxPriorityFeePerGas"] == 1_000_000_000  # 1 gwei floor
    assert g["maxFeePerGas"] == 77_000_000 * _DEPLOY_BASE_FEE_BUFFER + 1_000_000_000


def test_base_type2_tip_floor_beats_near_zero_node_tip():
    # Base base 0.005 gwei, node tip 0.001 gwei → floored to 0.005 gwei.
    w3 = _W3(base_fee=5_000_000, tip=1_000_000)
    g = _build_deploy_gas_fields(w3, 8453)
    assert g["type"] == 2
    assert g["maxPriorityFeePerGas"] == 5_000_000  # 0.005 gwei floor > node tip
    assert g["maxFeePerGas"] == 5_000_000 * _DEPLOY_BASE_FEE_BUFFER + 5_000_000


def test_btevm_type2_uses_high_base_fee():
    # BT-EVM base 10 gwei, node tip 0 → floored to 0.5 gwei.
    w3 = _W3(base_fee=10_000_000_000, tip=0)
    g = _build_deploy_gas_fields(w3, 964)
    assert g["type"] == 2
    assert g["maxPriorityFeePerGas"] == 500_000_000
    assert g["maxFeePerGas"] == 10_000_000_000 * _DEPLOY_BASE_FEE_BUFFER + 500_000_000


def test_node_tip_used_when_above_floor():
    # A healthy node tip (2 gwei) on ETH is kept as-is (above the 1-gwei floor).
    w3 = _W3(base_fee=50_000_000, tip=2_000_000_000)
    g = _build_deploy_gas_fields(w3, 1)
    assert g["maxPriorityFeePerGas"] == 2_000_000_000


def test_legacy_fallback_when_no_base_fee():
    # A chain that reports no base fee → legacy gasPrice with a 1-gwei floor.
    w3 = _W3(base_fee=None, tip=None, gas_price=200_000_000)  # 0.2 gwei
    g = _build_deploy_gas_fields(w3, 12345)
    assert g == {"gasPrice": 1_000_000_000}  # floored to 1 gwei (0.2*1.25 < 1)
    assert "type" not in g


def test_receipt_timeout_per_chain_and_env(monkeypatch):
    monkeypatch.delenv("DEPLOY_RECEIPT_TIMEOUT_SECONDS", raising=False)
    assert _deploy_receipt_timeout(1) == 110   # Ethereum longer
    assert _deploy_receipt_timeout(8453) == 90
    assert _deploy_receipt_timeout(999) == 90   # default
    monkeypatch.setenv("DEPLOY_RECEIPT_TIMEOUT_SECONDS", "200")
    assert _deploy_receipt_timeout(1) == 115    # clamped under http/nginx 120s
    monkeypatch.setenv("DEPLOY_RECEIPT_TIMEOUT_SECONDS", "10")
    assert _deploy_receipt_timeout(1) == 30     # floored
