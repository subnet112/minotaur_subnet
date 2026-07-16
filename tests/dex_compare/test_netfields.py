"""Tests for the net-comparison fields and size normalization."""

from __future__ import annotations

import asyncio
import json

from minotaur_subnet.dex_compare.aggregators.cow import CowClient
from minotaur_subnet.dex_compare.aggregators.oneinch import OneInchClient
from minotaur_subnet.dex_compare.aggregators.velora import VeloraClient
from minotaur_subnet.dex_compare.aggregators.zerox import ZeroxClient
from minotaur_subnet.dex_compare.config import DexCompareConfig
from minotaur_subnet.dex_compare.models import QuoteOutcome
from minotaur_subnet.dex_compare.store import DexCompareStore
from minotaur_subnet.dex_compare.worker import DexCompareWorker
from tests.dex_compare._helpers import FakeResp, FakeSession, make_trade


def _run(coro):
    return asyncio.run(coro)


# ── net-field capture ────────────────────────────────────────────────────
def test_velora_captures_after_fee_and_usd():
    body = {"priceRoute": {
        "destAmount": "1000", "destAmountAfterFee": "990", "gasCost": "180000",
        "gasCostUSD": "0.01", "srcUSD": "1000.0", "destUSD": "1002.0",
        "maxImpactReached": False}}
    o = _run(VeloraClient("https://x", 2).quote(FakeSession([FakeResp(200, json.dumps(body))]), make_trade()))
    assert o.status == "ok"
    assert o.output_raw == "1000" and o.output_after_fee_raw == "990"
    assert o.input_usd == 1000.0 and o.output_usd == 1002.0 and o.gas_usd == 0.01
    assert o.price_impact_reached is False


def test_velora_impact_flag():
    body = {"priceRoute": {"destAmount": "1", "maxImpactReached": True}}
    o = _run(VeloraClient("https://x", 2).quote(FakeSession([FakeResp(200, json.dumps(body))]), make_trade()))
    assert o.price_impact_reached is True


def test_zerox_captures_network_fee_and_zerox_fee():
    body = {"buyAmount": "777", "gas": "150000", "gasPrice": "9000000",
            "totalNetworkFee": "1350000000000",
            "fees": {"zeroExFee": {"amount": "1500", "token": "0xabc", "type": "volume"}}}
    o = _run(ZeroxClient("K", "https://api.0x.org", 2).quote(FakeSession([FakeResp(200, json.dumps(body))]), make_trade()))
    assert o.output_raw == "777" and o.output_after_fee_raw == "777"
    assert o.gas_native_wei == "1350000000000" and o.protocol_fee_raw == "1500"


def test_cow_after_fee_is_buyamount_and_gasless():
    body = {"quote": {"buyAmount": "1234", "feeAmount": "5"}}
    o = _run(CowClient("https://api.cow.fi", 2).quote(FakeSession([FakeResp(200, json.dumps(body))]), make_trade()))
    assert o.output_after_fee_raw == "1234" and o.is_net_of_gas is True
    assert o.gas_native_wei == "0" and o.protocol_fee_raw == "5"


def test_oneinch_requests_and_captures_gas():
    s = FakeSession([FakeResp(200, json.dumps({"dstAmount": "888", "gas": "210000"}))])
    o = _run(OneInchClient("K", "https://api.1inch.dev", "v6.0", 2).quote(s, make_trade()))
    assert o.status == "ok" and o.gas_units == 210000
    assert o.output_after_fee_raw == "888"   # no 1inch fee -> after-fee == gross
    _m, _url, _h, params, _j = s.calls[0]
    assert params.get("includeGas") == "true"


# ── size normalization ───────────────────────────────────────────────────
def _cfg(store_path, normalize=True, target=5000.0):
    return DexCompareConfig(
        enabled=True, interval_seconds=0.01, jitter_seconds=0.0, startup_delay_seconds=0.0,
        api_base_url="http://x", slippage_bps=50, http_timeout=5.0, max_retries=1,
        retain_days=90, max_rows=1000, supported_chain_ids=(8453,), store_path=str(store_path),
        normalize_size=normalize, target_usd=target, price_cache_ttl=600.0, max_price_impact_bps=300,
        cow_base_url="", velora_base_url="", oneinch_api_key=None, oneinch_base_url="",
        oneinch_version="v6.0", zerox_api_key=None, zerox_base_url="",
    )


class _VeloraPrice:
    """Stub Velora that prices the input at $1e-6 per base unit (via srcUSD)."""
    name = "velora"

    def supports(self, chain_id):
        return True

    async def quote(self, session, trade):
        return QuoteOutcome("velora", "ok", output_raw="1", input_usd=int(trade.input_amount) * 1e-6)


def test_normalize_scales_to_target_usd(tmp_path):
    worker = DexCompareWorker(object(), DexCompareStore(tmp_path / "dc.db"), _cfg(tmp_path / "dc.db", target=5000.0))
    worker._velora = _VeloraPrice()
    worker._session = object()
    trade = make_trade(input_amount="1000000")   # srcUSD = $1 (dust)
    scaled = asyncio.run(worker._normalize(trade))
    # upbu = 1e-6 ; scaled = 5000 / 1e-6 = 5_000_000_000
    assert scaled.input_amount == "5000000000"
    assert scaled.notional_usd == 5000.0 and scaled.original_input_amount == "1000000"


def test_normalize_noop_when_unpriceable(tmp_path):
    worker = DexCompareWorker(object(), DexCompareStore(tmp_path / "dc.db"), _cfg(tmp_path / "dc.db"))

    class _Dead:
        name = "velora"

        def supports(self, chain_id):
            return True

        async def quote(self, session, trade):
            return QuoteOutcome("velora", "error")

    worker._velora = _Dead()
    worker._session = object()
    trade = make_trade(input_amount="1000000")
    out = asyncio.run(worker._normalize(trade))
    assert out.input_amount == "1000000" and out.notional_usd is None


def test_price_cache_avoids_second_quote(tmp_path):
    worker = DexCompareWorker(object(), DexCompareStore(tmp_path / "dc.db"), _cfg(tmp_path / "dc.db"))
    calls = {"n": 0}

    class _Counting:
        name = "velora"

        def supports(self, chain_id):
            return True

        async def quote(self, session, trade):
            calls["n"] += 1
            return QuoteOutcome("velora", "ok", output_raw="1", input_usd=int(trade.input_amount) * 1e-6)

    worker._velora = _Counting()
    worker._session = object()
    trade = make_trade(input_amount="1000000")
    asyncio.run(worker._input_price(trade))
    asyncio.run(worker._input_price(trade))
    assert calls["n"] == 1   # second call served from cache
