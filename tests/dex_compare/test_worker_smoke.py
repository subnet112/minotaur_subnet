"""Smoke tests for the worker loop (offline, no keys, no network)."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

from minotaur_subnet.dex_compare.config import DexCompareConfig
from minotaur_subnet.dex_compare.models import QuoteOutcome
from minotaur_subnet.dex_compare.store import DexCompareStore
from minotaur_subnet.dex_compare.worker import DexCompareWorker
from tests.dex_compare._helpers import make_trade

_VALID_ORDER = {
    "order_id": "o1", "app_id": "app_1", "intent_function": "swap",
    "status": "filled", "chain_id": 8453,
    "params": {"input_token": "0xIN", "output_token": "0xOUT", "input_amount": "1000"},
}
_CROSS_ORDER = {
    "order_id": "o2", "app_id": "app_1", "intent_function": "swap",
    "status": "filled", "chain_id": 8453,
    "params": {
        "input_token": "0xIN", "output_token": "0xOUT",
        "input_amount": "1000", "dest_chain_id": 1,
    },
}
_WRONG_CHAIN_ORDER = {**_VALID_ORDER, "order_id": "o3", "chain_id": 999}
_ETH_ORDER = {
    "order_id": "e1", "app_id": "app_1", "intent_function": "swap",
    "status": "filled", "chain_id": 1,
    "params": {"input_token": "0xIN", "output_token": "0xOUT", "input_amount": "1000"},
}


def _cfg(store_path, chains=(8453,)) -> DexCompareConfig:
    return DexCompareConfig(
        enabled=True, interval_seconds=0.01, jitter_seconds=0.0, startup_delay_seconds=0.0,
        api_base_url="http://127.0.0.1:8080", slippage_bps=50, http_timeout=5.0,
        max_retries=1, retain_days=90, max_rows=1000, supported_chain_ids=chains,
        store_path=str(store_path),
        normalize_size=False, target_usd=5000.0, price_cache_ttl=600.0, max_price_impact_bps=300,
        cow_base_url="https://api.cow.fi", velora_base_url="https://api.velora.xyz",
        oneinch_api_key=None, oneinch_base_url="https://api.1inch.dev", oneinch_version="v6.0",
        zerox_api_key=None, zerox_base_url="https://api.0x.org",
    )


class FakeAppStore:
    def __init__(self, orders):
        self._orders = orders

    def list_orders(self, app_id=None, status=None):
        return list(self._orders)


class StubAgg:
    name = "cow"

    def supports(self, chain_id):
        return True

    def is_configured(self):
        return True

    async def quote(self, session, trade):
        return QuoteOutcome("cow", "ok", output_raw="90", is_net_of_gas=True)


def _make_worker(orders, tmp_path, chains=(8453,)):
    store = DexCompareStore(tmp_path / "dc.db")
    worker = DexCompareWorker(FakeAppStore(orders), store, _cfg(tmp_path / "dc.db", chains))
    worker._aggregators = [StubAgg()]
    worker._session = object()

    async def _gas(_chain):
        return "1000000000"

    worker._snapshot_gas_price = _gas
    return worker, store


def test_run_once_writes_one_row(tmp_path):
    worker, store = _make_worker([_VALID_ORDER, _CROSS_ORDER, _WRONG_CHAIN_ORDER], tmp_path)
    with patch(
        "minotaur_subnet.dex_compare.worker.resolve_trade_tokens",
        new=AsyncMock(return_value=make_trade()),
    ), patch(
        "minotaur_subnet.dex_compare.worker.fetch_minotaur_quote",
        new=AsyncMock(return_value=QuoteOutcome("minotaur", "ok", output_raw="100", gas_units=1)),
    ):
        wrote = asyncio.run(worker.run_once())
    assert wrote == 1
    assert store.count() == 1
    row = store.fetch_since(None, 0.0)[0]
    assert row["results"]["cow"]["output_raw"] == "90"
    assert row["results"]["minotaur"]["status"] == "ok"


def test_no_candidates_writes_nothing(tmp_path):
    # Only a cross-chain + a wrong-chain order -> filtered out -> no candidates.
    worker, store = _make_worker([_CROSS_ORDER, _WRONG_CHAIN_ORDER], tmp_path)
    wrote = asyncio.run(worker.run_once())
    assert wrote == 0 and store.count() == 0


def test_warming_up_skips_write(tmp_path):
    worker, store = _make_worker([_VALID_ORDER], tmp_path)
    with patch(
        "minotaur_subnet.dex_compare.worker.resolve_trade_tokens",
        new=AsyncMock(return_value=make_trade()),
    ), patch(
        "minotaur_subnet.dex_compare.worker.fetch_minotaur_quote",
        new=AsyncMock(return_value=QuoteOutcome("minotaur", "warming_up")),
    ):
        wrote = asyncio.run(worker.run_once())
    assert wrote == 0 and store.count() == 0


def test_draws_one_per_chain(tmp_path):
    # Corpus has a Base and an Ethereum candidate (+ a cross-chain to be filtered).
    # One run_once must draw once PER enabled chain -> exactly one row per chain.
    worker, store = _make_worker(
        [_VALID_ORDER, _ETH_ORDER, _CROSS_ORDER], tmp_path, chains=(1, 8453),
    )

    async def _resolve(order, _cache):
        return make_trade(chain_id=int(order["chain_id"]))

    with patch(
        "minotaur_subnet.dex_compare.worker.resolve_trade_tokens", new=_resolve,
    ), patch(
        "minotaur_subnet.dex_compare.worker.fetch_minotaur_quote",
        new=AsyncMock(return_value=QuoteOutcome("minotaur", "ok", output_raw="100", gas_units=1)),
    ):
        wrote = asyncio.run(worker.run_once())
    assert wrote == 2
    chains = {r["chain_id"] for r in store.fetch_since(None, 0.0)}
    assert chains == {1, 8453}


def test_run_loop_runs_then_stops(tmp_path):
    worker, store = _make_worker([_VALID_ORDER], tmp_path)

    async def drive():
        with patch(
            "minotaur_subnet.dex_compare.worker.resolve_trade_tokens",
            new=AsyncMock(return_value=make_trade()),
        ), patch(
            "minotaur_subnet.dex_compare.worker.fetch_minotaur_quote",
            new=AsyncMock(return_value=QuoteOutcome("minotaur", "ok", output_raw="100", gas_units=1)),
        ):
            task = asyncio.create_task(worker.run_loop(interval=0.01))
            await asyncio.sleep(0.1)
            worker.stop()
            await asyncio.wait_for(task, timeout=5)

    asyncio.run(drive())
    assert store.count() >= 1
