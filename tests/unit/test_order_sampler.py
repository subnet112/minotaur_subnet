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


class TestDiverseSubsets:
    """Per-validator seed: each validator draws a different subset (cross-validation)."""

    def test_none_seed_matches_legacy_round_only_draw(self):
        # Backward compat: validator_seed=None must be byte-for-byte the old draw.
        orders = [_make_order(f"ord_{i:03d}") for i in range(50)]
        store = _FakeAppStore(orders)
        legacy = sample_historical_orders(store, "round-123", n_per_chain=10)
        explicit_none = sample_historical_orders(
            store, "round-123", n_per_chain=10, validator_seed=None
        )
        assert [o["order_id"] for o in legacy] == [o["order_id"] for o in explicit_none]

    def test_same_validator_seed_is_deterministic(self):
        orders = [_make_order(f"ord_{i:03d}") for i in range(50)]
        store = _FakeAppStore(orders)
        a = sample_historical_orders(store, "round-1", n_per_chain=10, validator_seed="0xVALA")
        b = sample_historical_orders(store, "round-1", n_per_chain=10, validator_seed="0xVALA")
        assert [o["order_id"] for o in a] == [o["order_id"] for o in b]

    def test_different_validators_draw_different_subsets(self):
        # Same round, different validator identities → different (diverse) subsets.
        orders = [_make_order(f"ord_{i:03d}") for i in range(50)]
        store = _FakeAppStore(orders)
        a = sample_historical_orders(store, "round-1", n_per_chain=10, validator_seed="0xVALA")
        b = sample_historical_orders(store, "round-1", n_per_chain=10, validator_seed="0xVALB")
        ids_a = set(o["order_id"] for o in a)
        ids_b = set(o["order_id"] for o in b)
        assert ids_a != ids_b
        # ...but each is still a valid full-size draw from the same pool.
        assert len(ids_a) == 10 and len(ids_b) == 10


class TestFiltering:
    def test_includes_terminal_demand_excludes_inflight(self):
        # #228: terminal demand (filled + the champion's failures rejected/expired)
        # is sampled; in-flight (open) and user-cancelled are not — not solver signal.
        orders = [
            _make_order("ord_filled", status="filled"),
            _make_order("ord_rejected", status="rejected"),
            _make_order("ord_expired", status="expired"),
            _make_order("ord_open", status="open"),
            _make_order("ord_cancelled", status="cancelled"),
        ]
        store = _FakeAppStore(orders)
        sample = sample_historical_orders(store, "round-1", n_per_chain=10)
        assert {o["order_id"] for o in sample} == {"ord_filled", "ord_rejected", "ord_expired"}

    def test_includes_unfilled_orders_without_block_number(self):
        # #228: block_number is NOT required — the benchmark forks at the round/
        # live-head pin, not the order's block, so unfilled demand (no fill block)
        # replays against current state just like a filled order.
        orders = [
            _make_order("ord_filled", status="filled", block_number=28000000),
            _make_order("ord_rejected_noblock", status="rejected", block_number=None),
        ]
        store = _FakeAppStore(orders)
        sample = sample_historical_orders(store, "round-1", n_per_chain=10)
        assert {o["order_id"] for o in sample} == {"ord_filled", "ord_rejected_noblock"}

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
