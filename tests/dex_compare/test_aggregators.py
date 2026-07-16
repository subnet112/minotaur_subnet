"""Tests for the aggregator clients (parsing, support, configuration)."""

from __future__ import annotations

import asyncio
import json

from minotaur_subnet.dex_compare.aggregators.cow import CowClient
from minotaur_subnet.dex_compare.aggregators.oneinch import OneInchClient
from minotaur_subnet.dex_compare.aggregators.velora import VeloraClient
from minotaur_subnet.dex_compare.aggregators.zerox import ZeroxClient
from tests.dex_compare._helpers import FakeResp, FakeSession, make_trade


def _run(coro):
    return asyncio.run(coro)


# ── CoW ──────────────────────────────────────────────────────────────────
def test_cow_success_is_net_of_gas():
    session = FakeSession([FakeResp(200, json.dumps(
        {"quote": {"buyAmount": "1234567", "feeAmount": "5000"}}))])
    out = _run(CowClient("https://api.cow.fi", 2).quote(session, make_trade()))
    assert out.status == "ok"
    assert out.output_raw == "1234567"
    assert out.fee_raw == "5000"
    assert out.is_net_of_gas is True


def test_cow_no_route_is_failed():
    session = FakeSession([FakeResp(200, json.dumps({"quote": {"buyAmount": "0"}}))])
    out = _run(CowClient("https://api.cow.fi", 2).quote(session, make_trade()))
    assert out.status == "failed"


def test_cow_http_error_is_error():
    session = FakeSession([FakeResp(500, json.dumps({"description": "upstream"}))])
    out = _run(CowClient("https://api.cow.fi", 0).quote(session, make_trade()))
    assert out.status == "error" and out.error == "upstream"


def test_cow_unsupported_chain_optimism():
    # CoW does not support Optimism (10) — no HTTP call is made.
    session = FakeSession([])
    out = _run(CowClient("https://api.cow.fi", 2).quote(session, make_trade(chain_id=10)))
    assert out.status == "unsupported"
    assert session.calls == []


# ── Velora ───────────────────────────────────────────────────────────────
def test_velora_success_gross_with_gas():
    session = FakeSession([FakeResp(200, json.dumps(
        {"priceRoute": {"destAmount": "999", "gasCost": "180000"}}))])
    out = _run(VeloraClient("https://api.velora.xyz", 2).quote(session, make_trade()))
    assert out.status == "ok"
    assert out.output_raw == "999"
    assert out.gas_units == 180000
    assert out.is_net_of_gas is False


def test_velora_sends_decimals_and_network():
    session = FakeSession([FakeResp(200, json.dumps(
        {"priceRoute": {"destAmount": "1"}}))])
    _run(VeloraClient("https://api.velora.xyz", 2).quote(session, make_trade()))
    _method, _url, _headers, params, _json = session.calls[0]
    assert params["srcDecimals"] == "6" and params["destDecimals"] == "18"
    assert params["network"] == "8453" and params["side"] == "SELL"


# ── 1inch ────────────────────────────────────────────────────────────────
def test_oneinch_no_key_is_unsupported():
    session = FakeSession([])
    out = _run(OneInchClient(None, "https://api.1inch.dev", "v6.0", 2).quote(session, make_trade()))
    assert out.status == "unsupported"
    assert session.calls == []


def test_oneinch_success_with_key():
    session = FakeSession([FakeResp(200, json.dumps({"dstAmount": "888", "gas": "210000"}))])
    client = OneInchClient("KEY", "https://api.1inch.dev", "v6.0", 2)
    out = _run(client.quote(session, make_trade()))
    assert out.status == "ok" and out.output_raw == "888" and out.gas_units == 210000
    _m, url, headers, _p, _j = session.calls[0]
    assert "/swap/v6.0/8453/quote" in url
    assert headers["Authorization"] == "Bearer KEY"


# ── 0x ───────────────────────────────────────────────────────────────────
def test_zerox_no_key_is_unsupported():
    session = FakeSession([])
    out = _run(ZeroxClient(None, "https://api.0x.org", 2).quote(session, make_trade()))
    assert out.status == "unsupported"


def test_zerox_success_headers_and_parse():
    session = FakeSession([FakeResp(200, json.dumps({"buyAmount": "777", "gas": "150000"}))])
    out = _run(ZeroxClient("K", "https://api.0x.org", 2).quote(session, make_trade()))
    assert out.status == "ok" and out.output_raw == "777" and out.gas_units == 150000
    _m, _url, headers, params, _j = session.calls[0]
    assert headers["0x-api-key"] == "K" and headers["0x-version"] == "v2"
    assert params["chainId"] == "8453" and params["sellAmount"] == "1000000000"
