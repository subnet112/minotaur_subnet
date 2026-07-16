"""Tests for the shared HTTP backoff helper."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, patch

import aiohttp

from minotaur_subnet.dex_compare.backoff import request_with_backoff
from tests.dex_compare._helpers import FakeResp, FakeSession


def _run(coro):
    return asyncio.run(coro)


def test_success_first_try():
    session = FakeSession([FakeResp(200, json.dumps({"ok": 1}))])
    with patch("minotaur_subnet.dex_compare.backoff.asyncio.sleep", new=AsyncMock()) as slp:
        res = _run(request_with_backoff(session, "GET", "http://x", max_retries=4))
    assert res.ok and res.status == 200 and res.data == {"ok": 1}
    assert res.attempts == 1
    slp.assert_not_awaited()


def test_retry_after_header_honored():
    session = FakeSession([
        FakeResp(429, "", {"Retry-After": "2"}),
        FakeResp(200, json.dumps({"done": True})),
    ])
    with patch("minotaur_subnet.dex_compare.backoff.asyncio.sleep", new=AsyncMock()) as slp:
        res = _run(request_with_backoff(session, "GET", "http://x", max_retries=4))
    assert res.ok and res.data == {"done": True} and res.attempts == 2
    # The 429 slept exactly the Retry-After value (no jitter on Retry-After).
    slp.assert_awaited_once()
    assert slp.await_args.args[0] == 2.0


def test_gives_up_after_max_retries():
    session = FakeSession([FakeResp(429) for _ in range(5)])
    with patch("minotaur_subnet.dex_compare.backoff.asyncio.sleep", new=AsyncMock()) as slp:
        res = _run(request_with_backoff(session, "GET", "http://x", max_retries=2))
    assert not res.ok and res.status == 429
    assert res.attempts == 3          # 1 initial + 2 retries
    assert slp.await_count == 2       # slept before each retry, not the last


def test_network_error_retried_then_succeeds():
    session = FakeSession([
        aiohttp.ClientError("boom"),
        FakeResp(200, json.dumps({"ok": 1})),
    ])
    with patch("minotaur_subnet.dex_compare.backoff.asyncio.sleep", new=AsyncMock()) as slp:
        res = _run(request_with_backoff(session, "GET", "http://x", max_retries=3))
    assert res.ok and res.attempts == 2
    slp.assert_awaited_once()


def test_network_error_exhausted():
    session = FakeSession([aiohttp.ClientError("boom")] * 3)
    with patch("minotaur_subnet.dex_compare.backoff.asyncio.sleep", new=AsyncMock()):
        res = _run(request_with_backoff(session, "GET", "http://x", max_retries=2))
    assert not res.ok and res.data is None and "ClientError" in (res.error or "")


def test_non_retryable_4xx_returns_immediately():
    session = FakeSession([FakeResp(400, json.dumps({"error": "bad token"}))])
    with patch("minotaur_subnet.dex_compare.backoff.asyncio.sleep", new=AsyncMock()) as slp:
        res = _run(request_with_backoff(session, "GET", "http://x", max_retries=4))
    assert not res.ok and res.status == 400 and res.error == "bad token"
    slp.assert_not_awaited()
