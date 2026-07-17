"""Tests for the pluggable trade sources (offline, no network).

Covers the CoW GPv2Settlement Trade-event decoder, the CoW on-chain sampler
(with a fake web3), and HistoricalOrderSource parity with the legacy picker.
"""

from __future__ import annotations

import asyncio

from hexbytes import HexBytes
from web3 import Web3

from minotaur_subnet.dex_compare.config import DexCompareConfig
from minotaur_subnet.dex_compare import sources
from minotaur_subnet.dex_compare.sources import (
    CowOnchainSource,
    HistoricalOrderSource,
    TRADE_TOPIC0,
    build_source,
    decode_trade,
    is_candidate,
    _is_range_cap,
)

# ── fixtures ─────────────────────────────────────────────────────────────────
SELL = "0x1111111111111111111111111111111111111111"
BUY = "0x2222222222222222222222222222222222222222"
BUY2 = "0x3333333333333333333333333333333333333333"
NATIVE = "0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE"  # CoW "buy ETH" sentinel


def _cfg(**over) -> DexCompareConfig:
    base = dict(
        enabled=True, interval_seconds=1.0, jitter_seconds=0.0, startup_delay_seconds=0.0,
        api_base_url="http://x", slippage_bps=50, http_timeout=5.0, max_retries=1,
        retain_days=90, max_rows=1000, supported_chain_ids=(1, 8453), store_path=":memory:",
        normalize_size=False, target_usd=5000.0, price_cache_ttl=600.0, max_price_impact_bps=300,
        cow_base_url="", velora_base_url="", oneinch_api_key=None, oneinch_base_url="",
        oneinch_version="v6.0", zerox_api_key=None, zerox_base_url="",
        source="cow_onchain", cow_lookback_blocks={1: 100, 8453: 100},
        cow_lookback_default=100, cow_max_block_span=1000, cow_min_block_span=10,
    )
    base.update(over)
    return DexCompareConfig(**base)


def _word_addr(addr: str) -> bytes:
    return bytes(12) + bytes.fromhex(addr[2:])


def _word_uint(n: int) -> bytes:
    return int(n).to_bytes(32, "big")


def _raw_log(sell=SELL, buy=BUY, sell_amt=1000, *, buy_amt=999, fee=7, topic0=TRADE_TOPIC0, hexbytes=False):
    """Build a Trade log dict: words sellToken,buyToken,sellAmount,buyAmount,fee + tail."""
    data = (
        _word_addr(sell) + _word_addr(buy) + _word_uint(sell_amt)
        + _word_uint(buy_amt) + _word_uint(fee)
        + _word_uint(160) + _word_uint(56) + bytes(64)  # dynamic orderUid (ignored)
    )
    if hexbytes:
        return {"topics": [HexBytes(topic0)], "data": HexBytes(data)}
    return {"topics": [topic0], "data": "0x" + data.hex()}


# ── decode_trade ─────────────────────────────────────────────────────────────
def test_decode_trade_extracts_sell_buy_amount():
    sell, buy, amt = decode_trade(_raw_log(SELL, BUY, 123456))
    assert sell == Web3.to_checksum_address(SELL)
    assert buy == Web3.to_checksum_address(BUY)
    assert amt == 123456


def test_decode_trade_ignores_buy_fee_uid():
    a = decode_trade(_raw_log(SELL, BUY, 1000, buy_amt=0, fee=0))
    b = decode_trade(_raw_log(SELL, BUY, 1000, buy_amt=10 ** 30, fee=10 ** 20))
    assert a == b  # buyAmount / feeAmount / orderUid never affect the result


def test_decode_trade_wrong_topic0_returns_none():
    bad = _raw_log(topic0="0x" + "de" * 32)
    assert decode_trade(bad) is None


def test_decode_trade_short_data_returns_none():
    assert decode_trade({"topics": [TRADE_TOPIC0], "data": "0x" + "00" * 64}) is None  # < 96 bytes


def test_decode_trade_buy_native_sentinel_passthrough():
    _, buy, _ = decode_trade(_raw_log(SELL, NATIVE, 1000))
    assert buy == Web3.to_checksum_address(NATIVE)  # not filtered here (resolved downstream)


def test_decode_handles_hexbytes_and_plain_dict():
    plain = decode_trade(_raw_log(SELL, BUY, 42, hexbytes=False))
    hb = decode_trade(_raw_log(SELL, BUY, 42, hexbytes=True))
    assert plain == hb and plain[2] == 42


def test_is_range_cap_matches_provider_messages():
    for msg in ("query returned more than 10000 results", "block range is too large",
                "error -32005: limit exceeded", "Response size exceeded"):
        assert _is_range_cap(Exception(msg))
    assert not _is_range_cap(Exception("connection reset by peer"))


# ── is_candidate (shared filter) ─────────────────────────────────────────────
_VALID = {"status": "filled", "chain_id": 8453,
          "params": {"input_token": "0xIN", "output_token": "0xOUT", "input_amount": "1"}}
_CROSS = {**_VALID, "params": {**_VALID["params"], "dest_chain_id": 1}}
_WRONG_CHAIN = {**_VALID, "chain_id": 999}
_OPEN = {**_VALID, "status": "open"}


def test_is_candidate_filters():
    assert is_candidate(_VALID, (1, 8453))
    assert not is_candidate(_CROSS, (1, 8453))       # cross-chain
    assert not is_candidate(_WRONG_CHAIN, (1, 8453))  # unsupported chain
    assert not is_candidate(_OPEN, (1, 8453))         # not terminal


# ── fake web3 for CowOnchainSource ───────────────────────────────────────────
class _FakeEth:
    def __init__(self, head, logs, fail_head=False, range_cap_once=False):
        self._head = head
        self._logs = logs
        self._fail_head = fail_head
        self._range_cap_once = range_cap_once
        self.get_logs_calls = []

    @property
    def block_number(self):
        if self._fail_head:
            raise RuntimeError("head fetch failed")
        return self._head

    def get_logs(self, flt):
        self.get_logs_calls.append(flt)
        if self._range_cap_once:
            self._range_cap_once = False
            raise RuntimeError("query returned more than 10000 results")
        return list(self._logs)


class _FakeW3:
    def __init__(self, eth):
        self.eth = eth


class _FakeAppStore:
    def __init__(self, orders):
        self._orders = orders

    def list_orders(self, app_id=None, status=None):
        return list(self._orders)


def _borrowable(chain_id, app_id="app_x"):
    return {"order_id": "o", "app_id": app_id, "chain_id": chain_id, "intent_function": "swap",
            "status": "filled", "params": {"input_token": "0xIN", "output_token": "0xOUT",
                                            "input_amount": "1"}}


def _patch_web3(monkeypatch, w3):
    monkeypatch.setattr(sources, "get_web3", lambda chain_id: w3)


import random


def _src(cfg, orders, w3, monkeypatch):
    _patch_web3(monkeypatch, w3)
    return CowOnchainSource(_FakeAppStore(orders), cfg, random.Random(0))


# ── CowOnchainSource.sample ──────────────────────────────────────────────────
def test_sample_one_order_per_chain(monkeypatch):
    w3 = _FakeW3(_FakeEth(1000, [_raw_log(SELL, BUY, 555)]))
    src = _src(_cfg(supported_chain_ids=(8453,)), [_borrowable(8453)], w3, monkeypatch)
    out = asyncio.run(src.sample((8453,)))
    assert len(out) == 1
    o = out[0]
    assert o["chain_id"] == 8453 and o["status"] == "filled" and o["intent_function"] == "swap"
    assert o["app_id"] == "app_x"
    assert o["params"]["input_token"] == Web3.to_checksum_address(SELL)
    assert o["params"]["output_token"] == Web3.to_checksum_address(BUY)
    assert o["params"]["input_amount"] == "555"
    assert o["order_id"].startswith("cow:8453:")


def test_sample_dedup_collapses_batch(monkeypatch):
    # Three identical Trade logs (one settlement) -> one candidate.
    logs = [_raw_log(SELL, BUY, 100)] * 3
    w3 = _FakeW3(_FakeEth(1000, logs))
    src = _src(_cfg(supported_chain_ids=(8453,)), [_borrowable(8453)], w3, monkeypatch)
    cand = src._collect(logs)
    assert len(cand) == 1 and cand[0][2] == 100


def test_sample_skips_zero_amount_and_same_token(monkeypatch):
    logs = [_raw_log(SELL, BUY, 0), _raw_log(SELL, SELL, 100), _raw_log(SELL, BUY, 50)]
    src = _src(_cfg(supported_chain_ids=(8453,)), [_borrowable(8453)], _FakeW3(_FakeEth(1000, logs)), monkeypatch)
    cand = src._collect(logs)
    assert len(cand) == 1 and cand[0] == (Web3.to_checksum_address(SELL), Web3.to_checksum_address(BUY), 50)


def test_sample_dedup_by_pair_keeps_fattest(monkeypatch):
    logs = [_raw_log(SELL, BUY, 100), _raw_log(SELL, BUY, 900), _raw_log(SELL, BUY, 500)]
    src = _src(_cfg(supported_chain_ids=(8453,), cow_dedup_by_pair=True), [_borrowable(8453)],
               _FakeW3(_FakeEth(1000, logs)), monkeypatch)
    cand = src._collect(logs)
    assert len(cand) == 1 and cand[0][2] == 900


def test_sample_uses_cfg_cow_app_id_when_set(monkeypatch):
    cfg = _cfg(supported_chain_ids=(8453,), cow_app_ids={8453: "override_app"})
    # No orders in the store -> would fail to borrow, but the override wins.
    src = _src(cfg, [], _FakeW3(_FakeEth(1000, [_raw_log(SELL, BUY, 10)])), monkeypatch)
    out = asyncio.run(src.sample((8453,)))
    assert out and out[0]["app_id"] == "override_app"


def test_sample_falls_back_to_recent_order_app_id(monkeypatch):
    src = _src(_cfg(supported_chain_ids=(8453,)), [_borrowable(8453, app_id="borrowed")],
               _FakeW3(_FakeEth(1000, [_raw_log(SELL, BUY, 10)])), monkeypatch)
    out = asyncio.run(src.sample((8453,)))
    assert out and out[0]["app_id"] == "borrowed"


def test_sample_no_app_surface_skips_chain(monkeypatch):
    # No override, no borrowable order -> chain skipped, no crash.
    src = _src(_cfg(supported_chain_ids=(8453,)), [], _FakeW3(_FakeEth(1000, [_raw_log(SELL, BUY, 10)])), monkeypatch)
    assert asyncio.run(src.sample((8453,))) == []


def test_sample_empty_window_returns_nothing(monkeypatch):
    src = _src(_cfg(supported_chain_ids=(8453,)), [_borrowable(8453)], _FakeW3(_FakeEth(1000, [])), monkeypatch)
    assert asyncio.run(src.sample((8453,))) == []


def test_fetch_rpc_head_failure_skips_chain(monkeypatch):
    src = _src(_cfg(supported_chain_ids=(8453,)), [_borrowable(8453)],
               _FakeW3(_FakeEth(1000, [_raw_log(SELL, BUY, 10)], fail_head=True)), monkeypatch)
    assert asyncio.run(src.sample((8453,))) == []  # error swallowed, loop survives


def test_fetch_adaptive_halving_on_range_cap(monkeypatch):
    eth = _FakeEth(1000, [_raw_log(SELL, BUY, 10)], range_cap_once=True)
    cfg = _cfg(supported_chain_ids=(8453,), cow_lookback_blocks={8453: 100},
               cow_max_block_span=100, cow_min_block_span=10)
    src = _src(cfg, [_borrowable(8453)], _FakeW3(eth), monkeypatch)
    out = asyncio.run(src.sample((8453,)))
    assert out  # recovered after halving the span
    # first call spanned 100, retried with a smaller window
    spans = [(f["toBlock"] - f["fromBlock"] + 1) for f in eth.get_logs_calls]
    assert spans[0] == 100 and spans[1] < 100


def test_sample_per_chain_independent(monkeypatch):
    w3 = _FakeW3(_FakeEth(1000, [_raw_log(SELL, BUY, 10)]))
    src = _src(_cfg(supported_chain_ids=(1, 8453)), [_borrowable(1), _borrowable(8453)], w3, monkeypatch)
    out = asyncio.run(src.sample((1, 8453)))
    assert {o["chain_id"] for o in out} == {1, 8453}


# ── HistoricalOrderSource ────────────────────────────────────────────────────
def test_historical_source_one_per_chain_and_filters():
    orders = [
        {**_VALID, "order_id": "b", "chain_id": 8453, "app_id": "a"},
        {**_VALID, "order_id": "e", "chain_id": 1, "app_id": "a"},
        {**_CROSS, "order_id": "x", "chain_id": 8453, "app_id": "a"},  # filtered
    ]
    src = HistoricalOrderSource(_FakeAppStore(orders), _cfg(source="historical", supported_chain_ids=(1, 8453)),
                                random.Random(0))
    out = asyncio.run(src.sample((1, 8453)))
    assert {o["chain_id"] for o in out} == {1, 8453}
    assert all(o["order_id"] in {"b", "e"} for o in out)


def test_build_source_selects_by_config():
    hs = build_source(_cfg(source="historical"), _FakeAppStore([]), random.Random(0))
    cs = build_source(_cfg(source="cow_onchain"), _FakeAppStore([]), random.Random(0))
    assert isinstance(hs, HistoricalOrderSource) and hs.name == "historical"
    assert isinstance(cs, CowOnchainSource) and cs.name == "cow_onchain"
