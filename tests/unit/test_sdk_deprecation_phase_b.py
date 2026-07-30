"""SDK v2 Phase B — MarketSnapshot deprecation shim.

The contract: reading prices/pool_states/dex_config warns ONCE per field per
process (both warnings and logging channels), values and behaviour are
otherwise bit-identical, and non-deprecated fields never warn.
"""

from __future__ import annotations

import logging
import warnings

import pytest

from minotaur_subnet.sdk import intent_solver as mod
from minotaur_subnet.sdk.intent_solver import MarketSnapshot
from minotaur_subnet.sdk.version import SDK_VERSION


@pytest.fixture(autouse=True)
def _reset_warned_set():
    mod._snapshot_deprecation_warned.clear()
    yield
    mod._snapshot_deprecation_warned.clear()


def _snap() -> MarketSnapshot:
    return MarketSnapshot(
        chain_id=8453, block_number=1, timestamp=2,
        prices={"ETH/USD": 1850.0},
        pool_states={"0xpool": {"liquidity": 1}},
        dex_config={"router": "0xr"},
        balances={"0xt": "5"},
    )


def test_deprecated_fields_warn_and_return_the_value(caplog):
    snap = _snap()
    with caplog.at_level(logging.WARNING):
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            assert snap.pool_states == {"0xpool": {"liquidity": 1}}
            assert snap.prices == {"ETH/USD": 1850.0}
            assert snap.dex_config == {"router": "0xr"}
    deps = [x for x in w if issubclass(x.category, DeprecationWarning)]
    assert len(deps) == 3
    assert "2026-09-01" in str(deps[0].message)
    assert sum("deprecated" in r.message for r in caplog.records) == 3


def test_warns_once_per_field_per_process(caplog):
    a, b = _snap(), _snap()
    with caplog.at_level(logging.WARNING):
        for _ in range(3):
            _ = a.pool_states
            _ = b.pool_states  # different instance, same process
    assert sum("pool_states" in r.message for r in caplog.records) == 1


def test_non_deprecated_fields_never_warn(caplog):
    snap = _snap()
    with caplog.at_level(logging.WARNING):
        _ = snap.chain_id
        _ = snap.balances
        _ = snap.raw_state
        _ = snap.timestamp
    assert not any("deprecated" in r.message for r in caplog.records)


def test_empty_constructor_does_not_warn(caplog):
    with caplog.at_level(logging.WARNING):
        MarketSnapshot.empty(chain_id=1)
    assert not any("deprecated" in r.message for r in caplog.records)


def test_sdk_version_bumped_to_deprecation_generation():
    assert SDK_VERSION == "1.1.0"
