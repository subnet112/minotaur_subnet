"""Tests for the active blindspot reprober (drip + on-adoption sweep)."""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, patch

from minotaur_subnet.dex_compare.blindspots import build_blindspots_response
from minotaur_subnet.dex_compare.models import ComparisonRow, QuoteOutcome
from minotaur_subnet.dex_compare.reprobe import BlindspotReprober
from minotaur_subnet.dex_compare.stats import build_stats_response
from minotaur_subnet.dex_compare.store import DexCompareStore
from minotaur_subnet.dex_compare.tokens_resolve import DecimalsCache, SymbolCache
from tests.dex_compare._helpers import make_trade

from .test_worker_smoke import _cfg  # shared offline config factory

_IN = "0x1111111111111111111111111111111111111111"
_OUT = "0x2222222222222222222222222222222222222222"


def _run(coro):
    return asyncio.run(coro)


def _seed_open_blindspot(
    store, *, chain_id=8453, input_token=_IN, output_token=_OUT, n=3, age_days=0.0,
):
    """Insert ``n`` recent no-route cow rows so the pair classifies as OPEN."""
    now = time.time() - age_days * 86400
    for i in range(n):
        trade = make_trade(chain_id=chain_id, input_token=input_token, output_token=output_token)
        trade.trade_source = "cow_onchain"
        store.insert(ComparisonRow(
            created_at=now - 3600 * (i + 1),
            trade=trade,
            gas_price_wei=None,
            outcomes={"minotaur": QuoteOutcome(
                "minotaur", "failed", output_raw="0", error="no route / zero output",
            )},
        ))


def _reprober(store, cfg):
    return BlindspotReprober(store, cfg, DecimalsCache(), SymbolCache())


def _patch_resolve():
    """resolve_trade_tokens without RPC: echo the order back as a descriptor."""
    async def _resolve(order, _dec, _sym=None):
        p = order["params"]
        t = make_trade(
            chain_id=order["chain_id"],
            input_token=p["input_token"], output_token=p["output_token"],
            input_amount=str(p["input_amount"]),
        )
        t.app_id = order["app_id"]
        return t
    return patch("minotaur_subnet.dex_compare.reprobe.resolve_trade_tokens", new=_resolve)


def test_drip_probes_open_pair_and_writes_reprobe_row(tmp_path):
    store = DexCompareStore(tmp_path / "dc.db")
    _seed_open_blindspot(store)
    rp = _reprober(store, _cfg(tmp_path / "dc.db"))
    ok = QuoteOutcome("minotaur", "ok", output_raw="12345")
    with (
        _patch_resolve(),
        patch("minotaur_subnet.dex_compare.reprobe.fetch_minotaur_quote",
              new=AsyncMock(return_value=ok)),
        patch.object(rp, "_champion_id", new=AsyncMock(return_value="sub_a")),
    ):
        written = _run(rp.tick(session=None))
    assert written == 1
    rows = store.fetch_since(None, 0.0, None)
    probe = rows[0]  # newest-first
    assert probe["trade_source"] == "reprobe"
    assert probe["input_token"] == _IN and probe["output_token"] == _OUT
    assert probe["results"]["minotaur"]["status"] == "ok"
    assert set(probe["results"]) == {"minotaur"}  # no aggregators on probes


def test_reprobe_rows_flip_pair_to_covered_but_stay_out_of_stats(tmp_path):
    store = DexCompareStore(tmp_path / "dc.db")
    # Fails 6 days old: still inside the RECENT sub-window at probe time (so the
    # pair is open and gets probed), but in the EARLIER window when classified
    # 5 days later — the champion-fixed timeline.
    _seed_open_blindspot(store, n=3, age_days=6)
    rp = _reprober(store, _cfg(tmp_path / "dc.db"))
    ok = QuoteOutcome("minotaur", "ok", output_raw="12345")
    with (
        _patch_resolve(),
        patch("minotaur_subnet.dex_compare.reprobe.fetch_minotaur_quote",
              new=AsyncMock(return_value=ok)),
        patch.object(rp, "_champion_id", new=AsyncMock(return_value="sub_a")),
    ):
        for _ in range(3):  # three ok probes -> recent ok rate 100%
            _run(rp.tick(session=None))

    rows = store.fetch_since(None, 0.0, None)
    # Blindspots: earlier no-routes + recent ok reprobes == a COVERED gap...
    bs = build_blindspots_response(rows, 14, 7, 20, now=time.time() + 5 * 86400)
    chain = bs["chains"][0]
    assert chain["covered_count"] == 1 and chain["open_count"] == 0
    # ...while stats never count the synthetic probes as demand.
    stats = build_stats_response(rows, 30)
    assert stats["total_comparisons"] == 3  # the seeded cow rows only
    cov = stats["chains"][0]["coverage"]
    assert cov["minotaur_ok"] == 0 and cov["minotaur_no_route"] == 3


def test_champion_change_sweeps_all_open_pairs(tmp_path):
    store = DexCompareStore(tmp_path / "dc.db")
    for i in range(4):  # four distinct open pairs
        _seed_open_blindspot(store, input_token=f"0x{str(i) * 40}", n=2)
    rp = _reprober(store, _cfg(tmp_path / "dc.db"))
    ok = QuoteOutcome("minotaur", "ok", output_raw="1")
    quote = AsyncMock(return_value=ok)
    with (
        _patch_resolve(),
        patch("minotaur_subnet.dex_compare.reprobe.fetch_minotaur_quote", new=quote),
        patch("minotaur_subnet.dex_compare.reprobe._INTER_PROBE_SLEEP", 0.0),
        patch.object(rp, "_champion_id", new=AsyncMock(side_effect=["sub_a", "sub_b"])),
    ):
        first = _run(rp.tick(session=None))   # drip: capped at reprobe_per_cycle
        swept = _run(rp.tick(session=None))   # champion flip: sweep everything
    assert first == 2                          # default reprobe_per_cycle
    assert swept == 4                          # all open pairs in one sweep batch
    assert quote.await_count == 6


def test_sweep_drains_in_batches(tmp_path):
    store = DexCompareStore(tmp_path / "dc.db")
    for i in range(4):
        _seed_open_blindspot(store, input_token=f"0x{str(i) * 40}", n=2)
    import dataclasses
    cfg = dataclasses.replace(_cfg(tmp_path / "dc.db"), reprobe_sweep_batch=3)
    rp = _reprober(store, cfg)
    ok = QuoteOutcome("minotaur", "ok", output_raw="1")
    with (
        _patch_resolve(),
        patch("minotaur_subnet.dex_compare.reprobe.fetch_minotaur_quote",
              new=AsyncMock(return_value=ok)),
        patch("minotaur_subnet.dex_compare.reprobe._INTER_PROBE_SLEEP", 0.0),
        patch.object(rp, "_champion_id",
                     new=AsyncMock(side_effect=["sub_a", "sub_b", "sub_b"])),
    ):
        _run(rp.tick(session=None))                 # baseline champion
        assert _run(rp.tick(session=None)) == 3     # sweep batch 1
        assert len(rp._sweep_queue) == 1
        assert _run(rp.tick(session=None)) >= 1     # sweep drains (+ drip resumes)
        assert rp._sweep_queue == []


def test_warming_up_aborts_batch_without_rows(tmp_path):
    store = DexCompareStore(tmp_path / "dc.db")
    _seed_open_blindspot(store)
    before = store.count()
    rp = _reprober(store, _cfg(tmp_path / "dc.db"))
    warming = QuoteOutcome("minotaur", "warming_up")
    with (
        _patch_resolve(),
        patch("minotaur_subnet.dex_compare.reprobe.fetch_minotaur_quote",
              new=AsyncMock(return_value=warming)),
        patch.object(rp, "_champion_id", new=AsyncMock(return_value=None)),
    ):
        written = _run(rp.tick(session=None))
    assert written == 0 and store.count() == before


def test_disabled_when_per_cycle_zero(tmp_path):
    store = DexCompareStore(tmp_path / "dc.db")
    _seed_open_blindspot(store)
    import dataclasses
    cfg = dataclasses.replace(_cfg(tmp_path / "dc.db"), reprobe_per_cycle=0)
    rp = _reprober(store, cfg)
    champion = AsyncMock(return_value="sub_a")
    with patch.object(rp, "_champion_id", new=champion):
        assert _run(rp.tick(session=None)) == 0
    assert champion.await_count == 0  # fully inert, no polling either
