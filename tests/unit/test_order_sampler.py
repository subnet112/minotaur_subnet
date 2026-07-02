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
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "order_id": order_id,
        "app_id": "app_test",
        "chain_id": chain_id,
        "status": status,
        "block_number": block_number,
        "submitted_by": submitted_by,
        # Distinct pair per order_id — sampling tests need each order to be its
        # own trade shape, or the pre-draw dedup collapses them all to one.
        "params": params or {
            "input_token": "0xWETH",
            "output_token": f"0xOUT_{order_id}",
            "input_amount": "1000000000000000000",
        },
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


class TestSharedCorpus:
    """#242: one round-seeded SHARED draw — identical on every validator, no seed param."""

    def test_signature_has_no_per_validator_params(self):
        import inspect
        params = set(inspect.signature(sample_historical_orders).parameters)
        assert {"validator_seed", "validator_index", "validator_count"}.isdisjoint(params)

    def test_default_n_per_chain_is_50(self):
        import inspect
        assert inspect.signature(sample_historical_orders).parameters["n_per_chain"].default == 50

    def test_every_validator_derives_the_identical_subset(self):
        # The draw depends ONLY on round_id — independent of who runs it. Sampling
        # the same store + round_id any number of times yields the identical subset,
        # so the fleet shares one corpus (the basis for a meaningful quorum).
        orders = [_make_order(f"ord_{i:03d}") for i in range(100)]
        store = _FakeAppStore(orders)
        draws = [
            [o["order_id"] for o in sample_historical_orders(store, "round-42", n_per_chain=10)]
            for _ in range(5)
        ]
        assert all(d == draws[0] for d in draws)


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


class TestDedup:
    """Duplicate demand collapses BEFORE the draw — slots go to distinct trades."""

    def test_exact_resubmissions_collapse_to_one(self):
        # The live failure mode: one 0.01-WETH swap submitted 23 times. All copies
        # are one candidate; the volatile quote fields don't split them.
        orders = [
            _make_order(
                f"ord_{i:03d}",
                status="rejected",
                params={
                    "input_token": "0xWETH",
                    "output_token": "0xTOK",
                    "input_amount": "10000000000000000",
                    "quoted_output": str(1000 + i),  # varies per submission
                    "platform_fee_wei": str(i),
                },
            )
            for i in range(23)
        ]
        store = _FakeAppStore(orders)
        sample = sample_historical_orders(store, "round-1", n_per_chain=50)
        assert len(sample) == 1

    def test_near_dups_same_pair_same_decade_collapse(self):
        # 1 USDC vs 2 USDC on the same pair = one scenario; the slippage guard
        # scales with the amount and must not defeat the bucket.
        def usdc_swap(oid, amount, min_out):
            return _make_order(oid, params={
                "input_token": "0xUSDC", "output_token": "0xTOK",
                "input_amount": amount, "min_output_amount": min_out,
            })
        orders = [
            usdc_swap("ord_a", "1000000", "990000"),
            usdc_swap("ord_b", "2000000", "1980000"),
        ]
        sample = sample_historical_orders(_FakeAppStore(orders), "round-1", n_per_chain=50)
        assert len(sample) == 1

    def test_different_decade_pair_or_direction_survive(self):
        def swap(oid, inp, out, amount):
            return _make_order(oid, params={
                "input_token": inp, "output_token": out, "input_amount": amount,
            })
        orders = [
            swap("ord_1usdc", "0xUSDC", "0xTOK", "1000000"),
            swap("ord_10usdc", "0xUSDC", "0xTOK", "10000000"),   # next decade
            swap("ord_pair", "0xUSDC", "0xOTHER", "1000000"),    # different pair
            swap("ord_rev", "0xTOK", "0xUSDC", "1000000"),       # reverse direction
        ]
        sample = sample_historical_orders(_FakeAppStore(orders), "round-1", n_per_chain=50)
        assert len(sample) == 4

    def test_extra_meaningful_params_never_collapse(self):
        # An app param outside the bucketed swap fields (e.g. recipient) keeps
        # orders distinct even on the same pair/decade.
        def swap(oid, recipient):
            return _make_order(oid, params={
                "input_token": "0xUSDC", "output_token": "0xTOK",
                "input_amount": "1000000", "recipient": recipient,
            })
        orders = [swap("ord_a", "0xAAA"), swap("ord_b", "0xBBB")]
        sample = sample_historical_orders(_FakeAppStore(orders), "round-1", n_per_chain=50)
        assert len(sample) == 2

    def test_same_trade_on_different_chain_survives(self):
        params = {"input_token": "0xUSDC", "output_token": "0xTOK", "input_amount": "1000000"}
        orders = [
            _make_order("ord_base", chain_id=8453, params=dict(params)),
            _make_order("ord_eth", chain_id=1, params=dict(params)),
        ]
        sample = sample_historical_orders(_FakeAppStore(orders), "round-1", n_per_chain=50)
        assert len(sample) == 2

    def test_same_trade_on_different_app_or_function_survives(self):
        # Dedup identity is scoped (app_id, intent_function, chain_id) — the same
        # pair/amount on another app or intent function is a different scenario.
        params = {"input_token": "0xUSDC", "output_token": "0xTOK", "input_amount": "1000000"}
        base = _make_order("ord_a", params=dict(params))
        other_app = _make_order("ord_b", params=dict(params))
        other_app["app_id"] = "app_other"
        other_fn = _make_order("ord_c", params=dict(params))
        other_fn["intent_function"] = "limit_swap"
        orders = [base, other_app, other_fn]
        sample = sample_historical_orders(_FakeAppStore(orders), "round-1", n_per_chain=50)
        assert len(sample) == 3

    def test_dedup_runs_within_each_chain_pool_independently(self):
        # Per-chain sampling is unchanged: after collapsing each chain's dups,
        # every chain still fills its own n_per_chain quota from its own pool.
        def swap(oid, chain_id, out_token):
            return _make_order(oid, chain_id=chain_id, params={
                "input_token": "0xUSDC", "output_token": out_token,
                "input_amount": "1000000",
            })
        orders = (
            # base: 3 distinct trades, each duplicated 3x
            [swap(f"base_{t}_{i}", 8453, f"0xT{t}") for t in range(3) for i in range(3)]
            # eth: 2 distinct trades, each duplicated 2x
            + [swap(f"eth_{t}_{i}", 1, f"0xT{t}") for t in range(2) for i in range(2)]
        )
        sample = sample_historical_orders(_FakeAppStore(orders), "round-1", n_per_chain=50)
        base = [o for o in sample if o["chain_id"] == 8453]
        eth = [o for o in sample if o["chain_id"] == 1]
        assert len(base) == 3
        assert len(eth) == 2

    def test_representative_prefers_filled_then_lowest_order_id(self):
        params = {"input_token": "0xUSDC", "output_token": "0xTOK", "input_amount": "1000000"}
        orders = [
            _make_order("ord_c", status="rejected", params=dict(params)),
            _make_order("ord_b", status="filled", params=dict(params)),
            _make_order("ord_a", status="filled", params=dict(params)),
        ]
        sample = sample_historical_orders(_FakeAppStore(orders), "round-1", n_per_chain=50)
        assert [o["order_id"] for o in sample] == ["ord_a"]

    def test_representative_is_order_independent(self):
        # The chosen representative (hence the corpus) must not depend on store
        # iteration order — validators' stores may list in different orders.
        params = {"input_token": "0xUSDC", "output_token": "0xTOK", "input_amount": "1000000"}
        orders = [
            _make_order("ord_a", status="filled", params=dict(params)),
            _make_order("ord_b", status="rejected", params=dict(params)),
            _make_order("ord_c", status="filled", params=dict(params)),
        ]
        ids_fwd = [o["order_id"] for o in
                   sample_historical_orders(_FakeAppStore(orders), "round-1", n_per_chain=50)]
        ids_rev = [o["order_id"] for o in
                   sample_historical_orders(_FakeAppStore(list(reversed(orders))), "round-1", n_per_chain=50)]
        assert ids_fwd == ids_rev == ["ord_a"]

    def test_non_swap_params_fall_back_to_exact_identity(self):
        # No input/output/amount triple → exact-shape dedup only.
        orders = [
            _make_order("ord_a", params={"target": "0xX", "calldata": "0x01"}),
            _make_order("ord_b", params={"target": "0xX", "calldata": "0x01"}),
            _make_order("ord_c", params={"target": "0xX", "calldata": "0x02"}),
        ]
        sample = sample_historical_orders(_FakeAppStore(orders), "round-1", n_per_chain=50)
        assert {o["order_id"] for o in sample} == {"ord_a", "ord_c"}


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
