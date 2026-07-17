"""Tests for the /dex-compare route handlers (called directly, no TestClient)."""

from __future__ import annotations

import asyncio
import time

from minotaur_subnet.api.routes import dex_compare as route
from minotaur_subnet.dex_compare.store import DexCompareStore
from tests.dex_compare._helpers import make_row, make_trade, outcome


def _run(coro):
    return asyncio.run(coro)


def test_stats_reports_disabled_without_store():
    route.set_store(None)
    resp = _run(route.dex_compare_stats(window_days=30, chain_id=None))
    assert resp["enabled"] is False and resp["chains"] == []


def test_stats_and_samples_with_store(tmp_path):
    store = DexCompareStore(tmp_path / "dc.db")
    store.insert(make_row(
        {
            "minotaur": outcome("minotaur", output_raw="100"),
            "cow": outcome("cow", output_raw="90", is_net_of_gas=True),
        },
        created_at=time.time(),
    ))
    route.set_store(store)
    try:
        stats = _run(route.dex_compare_stats(window_days=30, chain_id=None))
        assert stats["enabled"] is True
        assert stats["total_comparisons"] == 1
        assert stats["chains"][0]["chain_id"] == 8453
        assert stats["chains"][0]["raw"]["vs_source"]["cow"]["minotaur_wins"] == 1

        samples = _run(route.dex_compare_samples(chain_id=None, limit=10))
        assert samples["enabled"] is True and samples["count"] == 1
    finally:
        route.set_store(None)


def test_stats_source_filter(tmp_path):
    store = DexCompareStore(tmp_path / "dc.db")
    cow_trade = make_trade()
    cow_trade.trade_source = "cow_onchain"
    hist_trade = make_trade()
    hist_trade.trade_source = "historical"
    for tr in (cow_trade, hist_trade):
        store.insert(make_row(
            {"minotaur": outcome("minotaur", output_raw="100")}, trade=tr, created_at=time.time(),
        ))
    route.set_store(store)
    try:
        both = _run(route.dex_compare_stats(window_days=30, chain_id=None))
        assert both["total_comparisons"] == 2
        cow = _run(route.dex_compare_stats(window_days=30, chain_id=None, source="cow_onchain"))
        assert cow["total_comparisons"] == 1
        hist = _run(route.dex_compare_stats(window_days=30, chain_id=None, source="historical"))
        assert hist["total_comparisons"] == 1
    finally:
        route.set_store(None)
