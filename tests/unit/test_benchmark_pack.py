"""Tests for benchmark pack hash — the consensus primitive for round scoring."""

from __future__ import annotations

from minotaur_subnet.harness.benchmark_pack import compute_pack_hash


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
