"""Attest gas fields (EIP-1559 + legacy fallback) and the low-balance alert that
surfaces a draining relayer wallet before it freezes champion adoption."""
import logging
from minotaur_subnet.relayer import solver_repo as s

GWEI = 10**9


class _Eth:
    def __init__(self, base=None, tip=0, gas_price=10*GWEI, balance=10**18):
        self._base, self._tip, self._gp, self._bal = base, tip, gas_price, balance
    def get_block(self, _): return {"baseFeePerGas": self._base}
    @property
    def max_priority_fee(self): return self._tip
    @property
    def gas_price(self): return self._gp
    def get_balance(self, _): return self._bal


class _W3:
    def __init__(self, **kw): self.eth = _Eth(**kw)


def test_eip1559_fields_use_node_tip_when_above_floor():
    f = s._attest_gas_fields(_W3(base=100*GWEI, tip=1*GWEI))
    assert f["maxPriorityFeePerGas"] == 1*GWEI
    assert f["maxFeePerGas"] == 100*GWEI*4 + 1*GWEI
    assert "gasPrice" not in f


def test_eip1559_tip_floored():
    f = s._attest_gas_fields(_W3(base=10*GWEI, tip=0))
    assert f["maxPriorityFeePerGas"] == s._ATTEST_TIP_FLOOR_WEI


def test_legacy_fallback_without_basefee():
    f = s._attest_gas_fields(_W3(base=None, gas_price=7*GWEI))
    assert f == {"gasPrice": 7*GWEI}


def test_low_balance_warns(caplog):
    # 0.001 TAO vs a 0.005 TAO reserve (500k @ 10gwei) → well under 3x → warn
    with caplog.at_level(logging.WARNING, logger="minotaur_subnet.relayer.solver_repo"):
        s._warn_if_low_attest_balance(_W3(balance=10**15), "0xrelayer", {"gasPrice": 10*GWEI})
    assert any("LOW ATTEST BALANCE" in r.message for r in caplog.records)


def test_sufficient_balance_no_warn(caplog):
    with caplog.at_level(logging.WARNING, logger="minotaur_subnet.relayer.solver_repo"):
        s._warn_if_low_attest_balance(_W3(balance=10**18), "0xrelayer", {"gasPrice": 10*GWEI})
    assert not any("LOW ATTEST BALANCE" in r.message for r in caplog.records)
