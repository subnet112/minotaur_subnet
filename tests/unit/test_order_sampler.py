"""Tests for the deterministic historical order sampler (Stage 2 of benchmarking)."""

from __future__ import annotations

from typing import Any

from minotaur_subnet.harness.order_sampler import sample_historical_orders


class _FakeAppStore:
    """Mock app store returning a fixed order list."""

    def __init__(self, orders: list[dict[str, Any]]) -> None:
        self._orders = orders

    def list_orders(self) -> list[dict[str, Any]]:
        return list(self._orders)


def _make_order(
    order_id: str,
    chain_id: int = 8453,
    status: str = "filled",
    block_number: int | None = 28000000,
    submitted_by: str = "0xUser",
) -> dict[str, Any]:
    return {
        "order_id": order_id,
        "app_id": "app_test",
        "chain_id": chain_id,
        "status": status,
        "block_number": block_number,
        "submitted_by": submitted_by,
        "params": {"input_token": "0xWETH", "output_token": "0xUSDC", "input_amount": "1e18"},
        "tx_hash": f"0x{order_id[-8:]}",
    }


class TestDeterminism:
    def test_same_round_id_yields_same_sample(self):
        orders = [_make_order(f"ord_{i:03d}") for i in range(50)]
        store = _FakeAppStore(orders)
        sample1 = sample_historical_orders(store, "round-123", n_per_chain=10)
        sample2 = sample_historical_orders(store, "round-123", n_per_chain=10)
        ids1 = [o["order_id"] for o in sample1]
        ids2 = [o["order_id"] for o in sample2]
        assert ids1 == ids2
        assert len(ids1) == 10

    def test_different_round_id_yields_different_sample(self):
        orders = [_make_order(f"ord_{i:03d}") for i in range(50)]
        store = _FakeAppStore(orders)
        sample1 = sample_historical_orders(store, "round-123", n_per_chain=10)
        sample2 = sample_historical_orders(store, "round-456", n_per_chain=10)
        ids1 = set(o["order_id"] for o in sample1)
        ids2 = set(o["order_id"] for o in sample2)
        assert ids1 != ids2


class TestFiltering:
    def test_excludes_non_filled_orders(self):
        orders = [
            _make_order("ord_filled", status="filled"),
            _make_order("ord_rejected", status="rejected"),
            _make_order("ord_open", status="open"),
        ]
        store = _FakeAppStore(orders)
        sample = sample_historical_orders(store, "round-1", n_per_chain=10)
        assert len(sample) == 1
        assert sample[0]["order_id"] == "ord_filled"

    def test_excludes_orders_without_block_number(self):
        orders = [
            _make_order("ord_with_block", block_number=28000000),
            _make_order("ord_no_block", block_number=None),
        ]
        store = _FakeAppStore(orders)
        sample = sample_historical_orders(store, "round-1", n_per_chain=10)
        assert len(sample) == 1
        assert sample[0]["order_id"] == "ord_with_block"

    def test_filters_by_chain_ids(self):
        orders = [
            _make_order("ord_base", chain_id=8453),
            _make_order("ord_eth", chain_id=1),
        ]
        store = _FakeAppStore(orders)
        sample = sample_historical_orders(store, "round-1", chain_ids=[8453], n_per_chain=10)
        assert len(sample) == 1
        assert sample[0]["chain_id"] == 8453

    def test_samples_per_chain_independently(self):
        orders = (
            [_make_order(f"base_{i}", chain_id=8453) for i in range(20)]
            + [_make_order(f"eth_{i}", chain_id=1) for i in range(20)]
        )
        store = _FakeAppStore(orders)
        sample = sample_historical_orders(store, "round-1", n_per_chain=5)
        base = [o for o in sample if o["chain_id"] == 8453]
        eth = [o for o in sample if o["chain_id"] == 1]
        assert len(base) == 5
        assert len(eth) == 5


class TestPII:
    def test_strips_submitted_by(self):
        orders = [_make_order("ord_1", submitted_by="0xUserPII")]
        store = _FakeAppStore(orders)
        sample = sample_historical_orders(store, "round-1", n_per_chain=10)
        assert "submitted_by" not in sample[0]
        assert sample[0]["params"] is not None
        assert sample[0]["block_number"] is not None


class TestEmpty:
    def test_empty_store_returns_empty_list(self):
        store = _FakeAppStore([])
        sample = sample_historical_orders(store, "round-1", n_per_chain=10)
        assert sample == []

    def test_fewer_orders_than_requested(self):
        orders = [_make_order(f"ord_{i}") for i in range(3)]
        store = _FakeAppStore(orders)
        sample = sample_historical_orders(store, "round-1", n_per_chain=10)
        assert len(sample) == 3
