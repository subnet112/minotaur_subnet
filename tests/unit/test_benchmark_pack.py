"""Tests for benchmark pack hash — the consensus primitive for round scoring."""

from __future__ import annotations

from minotaur_subnet.harness.benchmark_pack import compute_pack_hash
from minotaur_subnet.harness.rpc_budget_proxy.cost_table import compute_budget_record
from minotaur_subnet.harness.rpc_budget_proxy.rewrite_table import rewrite_table_record


class TestComputeBudgetFold:
    """The deterministic-budget {budget, cost_table} record folds into the pack
    hash ONLY when active — inert by default (backward compatible)."""

    _S = [{"app_id": "a", "name": "n", "params": {"x": 1}}]
    _H = ["ord_a", "ord_b"]

    def test_none_is_backward_compatible(self):
        # No budget → byte-identical to the pre-budget pack hash.
        assert compute_pack_hash("r", self._S, self._H, compute_budget=None) == \
            compute_pack_hash("r", self._S, self._H)

    def test_active_budget_changes_hash_and_is_deterministic(self):
        base = compute_pack_hash("r", self._S, self._H)
        h1 = compute_pack_hash("r", self._S, self._H, compute_budget=compute_budget_record(5000))
        h2 = compute_pack_hash("r", self._S, self._H, compute_budget=compute_budget_record(5000))
        assert h1 != base                      # folding it in is consensus-breaking
        assert h1 == h2                        # same budget+table → same hash (fleet agrees)

    def test_different_budget_or_table_diverges(self):
        h_a = compute_pack_hash("r", self._S, self._H, compute_budget=compute_budget_record(5000))
        h_b = compute_pack_hash("r", self._S, self._H, compute_budget=compute_budget_record(6000))
        assert h_a != h_b                      # a different budget can't reach quorum
        rec_t = compute_budget_record(5000)
        rec_t["cost_table"]["methods"]["eth_call"] = 99   # a tampered cost table also diverges
        assert compute_pack_hash("r", self._S, self._H, compute_budget=rec_t) != h_a


class TestBlockRewriteFold:
    """The block-pin rewrite-table record folds into the pack hash ONLY when the
    solver-read proxy routes — inert by default (backward compatible)."""

    _S = [{"app_id": "a", "name": "n", "params": {"x": 1}}]
    _H = ["ord_a", "ord_b"]

    def test_none_is_backward_compatible(self):
        assert compute_pack_hash("r", self._S, self._H, block_rewrite=None) == \
            compute_pack_hash("r", self._S, self._H)

    def test_active_rewrite_changes_hash_and_is_deterministic(self):
        base = compute_pack_hash("r", self._S, self._H)
        h1 = compute_pack_hash("r", self._S, self._H, block_rewrite=rewrite_table_record())
        h2 = compute_pack_hash("r", self._S, self._H, block_rewrite=rewrite_table_record())
        assert h1 != base   # routing through the proxy is consensus-breaking
        assert h1 == h2     # same rewrite table → same hash (fleet agrees)

    def test_tampered_rewrite_table_diverges(self):
        h_a = compute_pack_hash("r", self._S, self._H, block_rewrite=rewrite_table_record())
        rec = rewrite_table_record()
        rec["block_param_index"]["eth_call"] = 99   # a different pinning rule
        assert compute_pack_hash("r", self._S, self._H, block_rewrite=rec) != h_a

    def test_composes_with_compute_budget(self):
        # both folds active → distinct from either alone (orthogonal, composable)
        bud = compute_budget_record(5000)
        rw = rewrite_table_record()
        both = compute_pack_hash("r", self._S, self._H, compute_budget=bud, block_rewrite=rw)
        assert both != compute_pack_hash("r", self._S, self._H, compute_budget=bud)
        assert both != compute_pack_hash("r", self._S, self._H, block_rewrite=rw)


class TestDeterminism:
    def test_same_inputs_same_hash(self):
        scenarios = [
            {"app_id": "app_1", "name": "swap_small", "params": {"a": 1}},
            {"app_id": "app_2", "name": "swap_large", "params": {"b": 2}},
        ]
        h1 = compute_pack_hash("round-1", scenarios, ["ord_a", "ord_b"])
        h2 = compute_pack_hash("round-1", scenarios, ["ord_a", "ord_b"])
        assert h1 == h2
        assert h1.startswith("0x")
        assert len(h1) == 66  # 0x + 64 hex chars

    def test_order_of_scenarios_doesnt_matter(self):
        s1 = [
            {"app_id": "app_1", "name": "A"},
            {"app_id": "app_2", "name": "B"},
        ]
        s2 = [
            {"app_id": "app_2", "name": "B"},
            {"app_id": "app_1", "name": "A"},
        ]
        h1 = compute_pack_hash("r1", s1, [])
        h2 = compute_pack_hash("r1", s2, [])
        assert h1 == h2

    def test_order_of_historical_ids_doesnt_matter(self):
        h1 = compute_pack_hash("r1", [], ["ord_1", "ord_2", "ord_3"])
        h2 = compute_pack_hash("r1", [], ["ord_3", "ord_1", "ord_2"])
        assert h1 == h2


class TestSensitivity:
    def test_different_round_id_different_hash(self):
        h1 = compute_pack_hash("round-1", [{"app_id": "a", "name": "x"}], [])
        h2 = compute_pack_hash("round-2", [{"app_id": "a", "name": "x"}], [])
        assert h1 != h2

    def test_different_scenarios_different_hash(self):
        s1 = [{"app_id": "a", "name": "x"}]
        s2 = [{"app_id": "a", "name": "y"}]
        h1 = compute_pack_hash("r1", s1, [])
        h2 = compute_pack_hash("r1", s2, [])
        assert h1 != h2

    def test_different_params_different_hash(self):
        s1 = [{"app_id": "a", "name": "x", "params": {"amount": "1000"}}]
        s2 = [{"app_id": "a", "name": "x", "params": {"amount": "2000"}}]
        h1 = compute_pack_hash("r1", s1, [])
        h2 = compute_pack_hash("r1", s2, [])
        assert h1 != h2

    def test_different_order_ids_different_hash(self):
        h1 = compute_pack_hash("r1", [], ["ord_1", "ord_2"])
        h2 = compute_pack_hash("r1", [], ["ord_1", "ord_3"])
        assert h1 != h2


class TestCanonical:
    def test_empty_pack(self):
        h = compute_pack_hash("round-1", [], [])
        assert h.startswith("0x")
        assert len(h) == 66

    def test_ignores_extra_fields(self):
        """Comments, descriptions, and other non-canonical fields don't affect hash."""
        s1 = [{"app_id": "a", "name": "x", "params": {}, "description": "First version"}]
        s2 = [{"app_id": "a", "name": "x", "params": {}, "description": "Different text"}]
        h1 = compute_pack_hash("r1", s1, [])
        h2 = compute_pack_hash("r1", s2, [])
        assert h1 == h2  # description is not canonical
