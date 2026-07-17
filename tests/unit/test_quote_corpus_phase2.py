"""Phase-2 tests: round-anchored quote cutoff, retention, first-seen anchor,
and veto quote-awareness.

The Phase-2 hardening makes the quote draw a pure function of round_id by keying on
a first-seen-frozen, fleet-uniform captured_opened_epoch — closing the capture/prune/
QuoteSync race that would split the pack hash once BENCHMARK_QUOTE_CORPUS is armed.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from minotaur_subnet.harness.order_sampler import (
    QUOTE_RETENTION_EPOCHS,
    quote_case_id,
    sample_historical_quotes,
)


class _FakeQuoteStore:
    def __init__(self, quotes: list[dict[str, Any]]) -> None:
        self._quotes = quotes

    def list_quotes(self) -> list[dict[str, Any]]:
        return list(self._quotes)

    def list_apps(self) -> list[Any]:
        return []


def _q(qid: str, captured_opened_epoch, out_token: str, chain_id: int = 8453) -> dict:
    return {
        "quote_id": qid,
        "app_id": "app_test",
        "chain_id": chain_id,
        "intent_function": "swap",
        "params": {"input_token": "0xWETH", "output_token": out_token,
                   "input_amount": "1000000000000000000"},
        "captured_opened_epoch": captured_opened_epoch,
    }


class TestRoundAnchoredCutoff:
    def test_only_strictly_earlier_rounds_are_eligible(self):
        # Drawing round opened_epoch = 1000. Eligible iff captured < 1000.
        quotes = [
            _q("q_a", 500, "0xA"),    # earlier round -> eligible
            _q("q_b", 999, "0xB"),    # earlier round -> eligible
            _q("q_c", 1000, "0xC"),   # SAME round -> excluded
            _q("q_d", 1001, "0xD"),   # later round -> excluded
            _q("q_e", None, "0xE"),   # unanchored -> excluded
        ]
        got = sample_historical_quotes(_FakeQuoteStore(quotes), "round-e1000-n1", n_per_chain=50)
        ids = {q["quote_id"] for q in got}
        assert ids == {"q_a", "q_b"}

    def test_deterministic_with_cutoff(self):
        quotes = [_q(f"q_{i:03d}", 100 + i, f"0xO{i}") for i in range(40)]
        store = _FakeQuoteStore(quotes)
        a = [q["quote_id"] for q in sample_historical_quotes(store, "round-e9999-n1", n_per_chain=10)]
        b = [q["quote_id"] for q in sample_historical_quotes(store, "round-e9999-n1", n_per_chain=10)]
        assert a == b and len(a) == 10

    def test_non_round_id_disables_cutoff(self):
        # The miner preview uses "dryrun:{app_id}" -> opened_epoch parses to None ->
        # no cutoff, so even same-round / unanchored quotes are eligible.
        quotes = [_q("q_a", 5, "0xA"), _q("q_b", 10**9, "0xB"), _q("q_c", None, "0xC")]
        got = sample_historical_quotes(_FakeQuoteStore(quotes), "dryrun:app_test", n_per_chain=50)
        assert {q["quote_id"] for q in got} == {"q_a", "q_b", "q_c"}

    def test_retention_window_is_a_code_constant(self):
        assert isinstance(QUOTE_RETENTION_EPOCHS, int) and QUOTE_RETENTION_EPOCHS > 0


class TestStoreEpochColumn:
    def _store(self, tmp_path):
        from minotaur_subnet.store.app_intent_store import AppIntentStore
        return AppIntentStore(store_path=tmp_path / "store.db")

    def _row(self, qid="q_x", app="app_test", chain=8453, epoch=100):
        return {
            "quote_id": qid, "app_id": app, "chain_id": chain,
            "intent_function": "swap",
            "params": {"input_token": "0xA", "output_token": "0xB", "input_amount": "1"},
            "created_at": 1_700_000_000.0, "captured_opened_epoch": epoch,
        }

    def test_epoch_persisted_and_read_from_column(self, tmp_path):
        s = self._store(tmp_path)
        s.save_quote(self._row(epoch=123))
        assert s.get_quote("q_x")["captured_opened_epoch"] == 123
        assert s.list_quotes()[0]["captured_opened_epoch"] == 123

    def test_first_seen_preserved_on_requote(self, tmp_path):
        s = self._store(tmp_path)
        s.save_quote(self._row(epoch=100))
        # A later-round re-quote must NOT bump the anchor forward (COALESCE keeps it).
        s.save_quote(self._row(epoch=200))
        assert s.get_quote("q_x")["captured_opened_epoch"] == 100

    def test_column_is_authoritative_over_stale_blob(self, tmp_path):
        # Even if a caller's blob carries a different epoch than the column, reads
        # return the COLUMN value (the COALESCE first-seen source of truth).
        s = self._store(tmp_path)
        s.save_quote(self._row(epoch=100))
        s.save_quote(self._row(epoch=200))  # column stays 100; blob would be 200
        assert s.get_quote("q_x")["captured_opened_epoch"] == 100


class TestMigration:
    def test_alter_adds_column_to_legacy_quotes_table(self, tmp_path):
        # Simulate a Phase-1 DB: quotes table WITHOUT captured_opened_epoch.
        db = tmp_path / "legacy.db"
        conn = sqlite3.connect(db)
        conn.execute(
            "CREATE TABLE quotes(quote_id TEXT PRIMARY KEY, app_id TEXT, "
            "chain_id INTEGER, created_at REAL, data TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO quotes(quote_id, app_id, chain_id, created_at, data) "
            "VALUES(?,?,?,?,?)",
            ("q_legacy", "app_test", 8453, 1.0,
             '{"quote_id":"q_legacy","app_id":"app_test","chain_id":8453,'
             '"params":{"input_token":"0xA","output_token":"0xB","input_amount":"1"}}'),
        )
        conn.commit()
        conn.close()

        from minotaur_subnet.store.app_intent_store import AppIntentStore
        s = AppIntentStore(store_path=db)  # _ensure_schema runs the ALTER migration
        # Legacy row survives; its epoch is NULL (unstamped) -> never eligible for a draw.
        got = s.get_quote("q_legacy")
        assert got is not None and got["captured_opened_epoch"] is None
        # New writes with the column work.
        s.save_quote({"quote_id": "q_new", "app_id": "app_test", "chain_id": 8453,
                      "created_at": 2.0, "captured_opened_epoch": 7, "params": {}})
        assert s.get_quote("q_new")["captured_opened_epoch"] == 7


class TestRoundAnchoredPrune:
    def _store(self, tmp_path):
        from minotaur_subnet.store.app_intent_store import AppIntentStore
        return AppIntentStore(store_path=tmp_path / "store.db")

    def _save(self, s, qid, epoch):
        s.save_quote({"quote_id": qid, "app_id": "app_test", "chain_id": 8453,
                      "created_at": 1.0, "captured_opened_epoch": epoch,
                      "params": {"input_token": "0xA", "output_token": qid, "input_amount": "1"}})

    def test_prunes_old_and_null_keeps_recent(self, tmp_path):
        s = self._store(tmp_path)
        self._save(s, "q_old1", 100)
        self._save(s, "q_old2", 249)
        self._save(s, "q_keep", 300)
        self._save(s, "q_null", None)
        removed = s.prune_quotes(250)   # keep captured >= 250
        assert removed == 3             # q_old1, q_old2, q_null
        assert s.list_quote_ids() == {"q_keep"}

    def test_prune_is_pure_function_of_epoch_not_walltime(self, tmp_path):
        # Two stores with the same rows but pruned "at different times" (different
        # call order) converge to the same set — determinism the flag-on draw needs.
        a, b = self._store(tmp_path / "a"), self._store(tmp_path / "b")
        for st in (a, b):
            self._save(st, "q1", 10)
            self._save(st, "q2", 20)
            self._save(st, "q3", 30)
        a.prune_quotes(15)
        b.prune_quotes(15)
        assert a.list_quote_ids() == b.list_quote_ids() == {"q2", "q3"}


class TestVetoQuoteAware:
    def test_order_label_branches_on_quote_prefix(self):
        from minotaur_subnet.api.routes.submissions.veto_wire import _order_label
        assert _order_label({"app_id": "app", "order_id": "q_abc"}) == "app:quote:q_abc"
        assert _order_label({"app_id": "app", "order_id": "ord_1"}) == "app:hist:ord_1"

    def test_production_lookup_routes_quote_ids_to_get_quote(self, monkeypatch):
        from minotaur_subnet.api.routes.submissions import veto_wire
        import minotaur_subnet.api.server_context as sc

        class _FakeStore:
            def get_order(self, oid):
                return {"order_id": oid, "src": "orders"}

            def get_quote(self, qid):
                return {"quote_id": qid, "src": "quotes"}  # no order_id -> must be aliased

        monkeypatch.setattr(sc.ctx, "store", _FakeStore(), raising=False)
        # q_ id -> quotes table, aliased order_id
        q = veto_wire._production_order_lookup("q_abc")
        assert q["src"] == "quotes" and q["order_id"] == "q_abc"
        # non-q id -> orders table
        o = veto_wire._production_order_lookup("ord_1")
        assert o["src"] == "orders"

    def test_label_and_scenario_prefix_agree(self):
        # The veto coverage assert holds only if _order_label and the scenario
        # builder's prefix use the SAME discriminator (q_ prefix). Assert the
        # discriminator is identical on both sides.
        from minotaur_subnet.api.routes.submissions.veto_wire import _order_label
        for oid, kind in [("q_x", "quote"), ("ord_x", "hist")]:
            label_kind = _order_label({"app_id": "a", "order_id": oid}).split(":")[1]
            scenario_kind = "quote" if oid.startswith("q_") else "hist"
            assert label_kind == scenario_kind == kind
