"""Tests for the SQLite comparison store."""

from __future__ import annotations

from minotaur_subnet.dex_compare.store import DexCompareStore
from tests.dex_compare._helpers import make_row, make_trade, outcome


def test_roundtrip_preserves_bigint_amounts(tmp_path):
    store = DexCompareStore(tmp_path / "dc.db")
    big = str(10 ** 30)  # far beyond SQLite's 64-bit INTEGER
    row = make_row({
        "minotaur": outcome("minotaur", output_raw=big, gas_units=100000, dex="uniswap"),
        "cow": outcome("cow", output_raw=str(10 ** 29), is_net_of_gas=True, fee_raw="500"),
    })
    rid = store.insert(row)
    assert rid >= 1

    rows = store.fetch_since(None, 0.0)
    assert len(rows) == 1
    r = rows[0]
    assert r["results"]["minotaur"]["output_raw"] == big
    assert r["results"]["cow"]["is_net_of_gas"] is True
    assert r["input_amount"] == make_trade().input_amount
    assert r["output_is_native"] is True
    assert r["chain_id"] == 8453


def test_fetch_since_window_and_limit(tmp_path):
    store = DexCompareStore(tmp_path / "dc.db")
    for i in range(3):
        store.insert(make_row(
            {"minotaur": outcome("minotaur", output_raw="1")}, created_at=100.0 + i,
        ))
    # window filters out the oldest
    assert len(store.fetch_since(None, 101.0)) == 2
    # limit returns newest-first
    newest = store.fetch_since(None, 0.0, limit=1)
    assert len(newest) == 1 and newest[0]["created_at"] == 102.0


def test_prune_by_age_and_max_rows(tmp_path):
    store = DexCompareStore(tmp_path / "dc.db")
    for i in range(5):
        store.insert(make_row(
            {"minotaur": outcome("minotaur", output_raw="1")}, created_at=100.0 + i,
        ))
    deleted = store.prune(102.0)          # removes created_at 100, 101
    assert deleted == 2 and store.count() == 3
    store.prune(0.0, max_rows=1)          # keep only the newest
    assert store.count() == 1


def test_distinct_chains(tmp_path):
    store = DexCompareStore(tmp_path / "dc.db")
    store.insert(make_row({"minotaur": outcome("minotaur", output_raw="1")},
                          trade=make_trade(chain_id=8453)))
    store.insert(make_row({"minotaur": outcome("minotaur", output_raw="1")},
                          trade=make_trade(chain_id=1)))
    assert store.distinct_chains() == [1, 8453]


def test_wal_mode_and_indexes(tmp_path):
    store = DexCompareStore(tmp_path / "dc.db")
    with store._connect() as conn:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == "wal"
        names = {r["name"] for r in conn.execute("PRAGMA index_list('comparisons')")}
        assert "idx_dc_chain_time" in names
        assert "idx_dc_time" in names
