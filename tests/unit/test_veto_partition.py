"""Distributed-veto slice partitioning + order replay hash (Phase 0).

Invariants pinned here:
- The canonical leader draw is NEVER touched: slice partitioning excludes the
  exact ``sample_historical_orders`` membership, and union(draw, slices) covers
  the deduped corpus for the veto chains.
- Determinism from round_id alone; disjointness (draw↔slices and pairwise).
- The replay hash covers exactly the replay identity — stable under every
  post-hoc-mutable / PII / sync-blanked field, sensitive to every replayed one.
"""

from __future__ import annotations

from typing import Any

from minotaur_subnet.harness.order_sampler import (
    VETO_CALIBRATION_ORDERS,
    calibration_overlap,
    order_replay_hash,
    partition_follower_slices,
    sample_historical_orders,
)


class _FakeAppStore:
    def __init__(self, orders: list[dict[str, Any]]) -> None:
        self._orders = orders

    def list_orders(self) -> list[dict[str, Any]]:
        return list(self._orders)


def _make_order(order_id: str, chain_id: int = 8453, status: str = "filled") -> dict:
    return {
        "order_id": order_id,
        "app_id": "app_test",
        "chain_id": chain_id,
        "status": status,
        "intent_function": "swap",
        "block_number": 28000000,
        "submitted_by": "0xUser",
        "user_signature": "0xsig",
        "params": {
            "input_token": "0xWETH",
            # Distinct pair per order_id so the dedup keeps each as its own shape.
            "output_token": f"0xOUT_{order_id}",
            "input_amount": "1000000000000000000",
            "quoted_output": "990000",
        },
        "tx_hash": f"0x{order_id[-6:]}",
    }


def _corpus(n: int = 160) -> list[dict]:
    return [_make_order(f"ord_{i:04d}") for i in range(n)]


# ── partitioning ──────────────────────────────────────────────────────────────

class TestPartition:
    def test_deterministic_from_round_id(self):
        store = _FakeAppStore(_corpus())
        a = partition_follower_slices(store, "round-e1-n1", chain_ids=[8453])
        b = partition_follower_slices(store, "round-e1-n1", chain_ids=[8453])
        assert [[o["order_id"] for o in s] for s in a] == \
               [[o["order_id"] for o in s] for s in b]

    def test_different_round_id_shuffles_differently(self):
        store = _FakeAppStore(_corpus())
        a = partition_follower_slices(store, "round-e1-n1", chain_ids=[8453])
        b = partition_follower_slices(store, "round-e2-n1", chain_ids=[8453])
        assert [o["order_id"] for o in a[0]] != [o["order_id"] for o in b[0]]

    def test_disjoint_from_leader_draw_and_pairwise(self):
        store = _FakeAppStore(_corpus())
        draw_ids = {
            o["order_id"] for o in sample_historical_orders(store, "round-e1-n1")
        }
        slices = partition_follower_slices(store, "round-e1-n1", chain_ids=[8453])
        seen: set[str] = set()
        for s in slices:
            ids = {o["order_id"] for o in s}
            assert not ids & draw_ids, "slice overlaps the canonical draw"
            assert not ids & seen, "slices overlap each other"
            seen |= ids

    def test_union_covers_the_remainder(self):
        store = _FakeAppStore(_corpus(160))
        draw_ids = {
            o["order_id"] for o in sample_historical_orders(store, "round-e1-n1")
        }
        slices = partition_follower_slices(store, "round-e1-n1", chain_ids=[8453])
        slice_ids = {o["order_id"] for s in slices for o in s}
        assert len(draw_ids) == 50
        assert draw_ids | slice_ids == {f"ord_{i:04d}" for i in range(160)}
        # 110 remainder → slices of 50, 50, 10 (short tail kept — partial
        # coverage beats none)
        assert [len(s) for s in slices] == [50, 50, 10]

    def test_chain_filter_and_required_chain_ids(self):
        orders = _corpus(60) + [
            _make_order(f"eth_{i:03d}", chain_id=1) for i in range(30)
        ]
        store = _FakeAppStore(orders)
        slices = partition_follower_slices(store, "round-e1-n1", chain_ids=[8453])
        assert all(o["chain_id"] == 8453 for s in slices for o in s)
        import pytest
        with pytest.raises(ValueError):
            partition_follower_slices(store, "round-e1-n1", chain_ids=[])

    def test_pii_stripped(self):
        store = _FakeAppStore(_corpus())
        slices = partition_follower_slices(store, "round-e1-n1", chain_ids=[8453])
        for s in slices:
            for o in s:
                assert "submitted_by" not in o
                assert "user_signature" not in o

    def test_dedup_representative_never_leaks_across_pipelines(self):
        # Duplicate-shape pairs (filled + rejected copies of one trade): the
        # dedup keeps the filled representative in BOTH the canonical draw and
        # the partition pipeline (chain_id is in the dedup-key prefix, so chain
        # filtering is group-atomic) — no order may appear in both a slice and
        # the draw, and no rejected twin may resurface in a slice.
        orders = []
        for i in range(80):
            filled = _make_order(f"ord_{i:04d}_f", status="filled")
            rejected = _make_order(f"ord_{i:04d}_r", status="rejected")
            # same trade shape: same pair, same amount decade
            rejected["params"] = dict(filled["params"])
            orders.extend([filled, rejected])
        store = _FakeAppStore(orders)
        draw_ids = {
            o["order_id"] for o in sample_historical_orders(store, "round-e1-n1")
        }
        slices = partition_follower_slices(store, "round-e1-n1", chain_ids=[8453])
        slice_ids = {o["order_id"] for s in slices for o in s}
        assert not draw_ids & slice_ids
        # rejected twins were collapsed away everywhere
        assert all(oid.endswith("_f") for oid in draw_ids | slice_ids)

    def test_partition_uses_one_corpus_snapshot(self):
        # The draw-exclusion must derive from the SAME order list the partition
        # candidates came from — a store that grows between two list_orders()
        # calls must not skew the exclusion set (draw membership is corpus-
        # size-sensitive, so skew leaks canonical-draw orders into slices).
        orders = _corpus(160)

        class _GrowingStore(_FakeAppStore):
            def __init__(self, base):
                super().__init__(base)
                self.calls = 0

            def list_orders(self):
                self.calls += 1
                if self.calls == 1:
                    return list(self._orders)
                # every later call sees a bigger corpus
                return list(self._orders) + [
                    _make_order(f"late_{self.calls:02d}")
                ]

        store = _GrowingStore(orders)
        slices = partition_follower_slices(store, "round-e1-n1", chain_ids=[8453])
        # exclusion computed from the FIRST snapshot: disjointness holds against
        # the draw derived from that same snapshot
        frozen = _FakeAppStore(orders)
        draw_ids = {
            o["order_id"] for o in sample_historical_orders(frozen, "round-e1-n1")
        }
        slice_ids = {o["order_id"] for s in slices for o in s}
        assert not draw_ids & slice_ids
        assert not any(oid.startswith("late_") for oid in slice_ids)

    def test_empty_corpus_and_empty_remainder(self):
        assert partition_follower_slices(
            _FakeAppStore([]), "round-e1-n1", chain_ids=[8453],
        ) == []
        # 30 orders: the canonical draw takes all of them → no remainder.
        store = _FakeAppStore(_corpus(30))
        assert partition_follower_slices(store, "round-e1-n1", chain_ids=[8453]) == []


# ── calibration overlap ──────────────────────────────────────────────────────

class TestCalibration:
    def test_subset_of_leader_draw_and_deterministic(self):
        store = _FakeAppStore(_corpus())
        draw_ids = {
            o["order_id"] for o in sample_historical_orders(store, "round-e1-n1")
        }
        a = calibration_overlap(store, "round-e1-n1", chain_ids=[8453])
        b = calibration_overlap(store, "round-e1-n1", chain_ids=[8453])
        assert [o["order_id"] for o in a] == [o["order_id"] for o in b]
        assert len(a) == VETO_CALIBRATION_ORDERS
        assert {o["order_id"] for o in a} <= draw_ids

    def test_small_draw_caps_size(self):
        store = _FakeAppStore(_corpus(3))
        assert len(calibration_overlap(store, "round-e1-n1", chain_ids=[8453])) == 3


# ── replay hash ───────────────────────────────────────────────────────────────

class TestReplayHash:
    def test_stable_under_posthoc_mutable_and_pii_fields(self):
        base = _make_order("ord_x")
        h0 = order_replay_hash(base)
        mutated = dict(base)
        mutated.update({
            "status": "rejected",            # lifecycle transition
            "tx_hash": "0xdeadbeef",         # fill-time metadata
            "block_number": 99999999,
            "user_signature": "",            # blanked by the sync view
            "submitted_by": "0xSomeoneElse",  # PII, stripped for solvers
            "plan": {"x": 1},
            "score": 0.9,
            "consensus_result": {"y": 2},
        })
        assert order_replay_hash(mutated) == h0

    def test_sensitive_to_replay_identity(self):
        base = _make_order("ord_x")
        h0 = order_replay_hash(base)

        p = dict(base)
        p["params"] = dict(base["params"], input_amount="2000000000000000000")
        assert order_replay_hash(p) != h0

        # quoted_output is dedup-volatile but REPLAY-relevant (feeds the
        # IntentState verbatim; gates the on-chain CoW fee) — must be covered.
        q = dict(base)
        q["params"] = dict(base["params"], quoted_output="123")
        assert order_replay_hash(q) != h0

        for key, val in (
            ("order_id", "ord_y"),
            ("app_id", "other_app"),
            ("chain_id", 1),
            ("intent_function", "swap_exact_out"),
        ):
            m = dict(base)
            m[key] = val
            assert order_replay_hash(m) != h0, key

    def test_param_insertion_order_irrelevant(self):
        a = _make_order("ord_x")
        b = dict(a)
        b["params"] = dict(reversed(list(a["params"].items())))
        assert order_replay_hash(a) == order_replay_hash(b)
