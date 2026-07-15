"""Unit tests for the unified RPC exponential-backoff primitive + web3 middleware."""

from __future__ import annotations

import asyncio

import pytest

from minotaur_subnet import rpc_backoff as rb
from minotaur_subnet.blockchain.web3_retry import (
    RpcRetryMiddleware,
    _is_no_retry,
    _response_is_retryable,
)


class _Resp429(Exception):
    def __init__(self):
        self.response = type("R", (), {"status_code": 429})()


class _AiohttpErr(Exception):
    pass


_AiohttpErr.__module__ = "aiohttp.client_exceptions"


# ── classifier ────────────────────────────────────────────────────────────────


class TestClassifier:
    def test_retryable_status(self):
        for s in (429, 500, 502, 503, 504):
            assert rb.is_retryable_status(s)
        for s in (200, 400, 401, 403, 404, None):
            assert not rb.is_retryable_status(s)

    def test_retryable_rpc_code(self):
        assert rb.is_retryable_rpc_code(-32005)
        # -32000 is DELIBERATELY not retryable (generic server-error range that
        # covers deterministic read failures — retrying can't fix them).
        for c in (-32000, 3, -32015, -32602, 0, None, "x"):
            assert not rb.is_retryable_rpc_code(c)

    def test_retryable_exception_network(self):
        assert rb.is_retryable_exception(TimeoutError())
        assert rb.is_retryable_exception(asyncio.TimeoutError())
        assert rb.is_retryable_exception(ConnectionResetError())
        assert rb.is_retryable_exception(ConnectionError())
        assert rb.is_retryable_exception(OSError("ECONNRESET"))

    def test_retryable_exception_by_status(self):
        assert rb.is_retryable_exception(_Resp429())
        # a 404-bearing HTTPError is NOT retryable
        e = _Resp429()
        e.response.status_code = 404
        assert not rb.is_retryable_exception(e)

    def test_retryable_exception_by_client_module(self):
        # a bare aiohttp/requests transport error (no status) → transient
        assert rb.is_retryable_exception(_AiohttpErr())

    def test_non_retryable_exception(self):
        assert not rb.is_retryable_exception(ValueError("bad params"))
        assert not rb.is_retryable_exception(KeyError("x"))

    def test_body_has_retryable_rpc_error(self):
        assert rb.body_has_retryable_rpc_error(
            b'{"jsonrpc":"2.0","id":1,"error":{"code":-32005,"message":"CU limit"}}')
        # -32000 (generic/deterministic) is NOT retried by the body scan
        assert not rb.body_has_retryable_rpc_error(
            b'{"error":{"code":-32000,"message":"missing trie node"}}')
        # a real result (no error) → not retryable
        assert not rb.body_has_retryable_rpc_error(b'{"jsonrpc":"2.0","id":1,"result":"0x1"}')
        # an application error (revert) → not retryable
        assert not rb.body_has_retryable_rpc_error(
            b'{"error":{"code":3,"message":"execution reverted"}}')
        # oversized body is skipped (never scanned)
        assert not rb.body_has_retryable_rpc_error(b'{"error":-32005}' + b"0" * 5000)
        assert not rb.body_has_retryable_rpc_error(b"")
        assert not rb.body_has_retryable_rpc_error(None)


class TestBackoffDelay:
    def test_bounded_by_cap_and_jittered(self):
        for attempt in range(8):
            d = rb.backoff_delay(attempt, base=0.5, cap=4.0)
            # jitter is [0.5, 1.5) of the (capped) ceiling
            assert 0 < d < 4.0 * 1.5
        # deep attempts saturate at the cap band, never explode
        assert rb.backoff_delay(20, base=0.5, cap=4.0) < 4.0 * 1.5

    def test_floor_guards_zero_config(self):
        # BASE/CAP env-set to 0 must NOT produce a zero-delay tight loop.
        for attempt in range(4):
            assert rb.backoff_delay(attempt, base=0.0, cap=0.0) >= rb._MIN_DELAY_SECONDS
            assert rb.backoff_delay(attempt, base=0.25, cap=0.0) >= rb._MIN_DELAY_SECONDS


class TestDeadline:
    def test_deadline_stops_retrying_after_a_slow_attempt(self):
        # A single slow attempt (that trips the deadline) is NOT retried into
        # attempts × timeout — deadline caps cumulative wall-clock.
        n = {"c": 0}
        clock = {"t": 0.0}

        def slow():
            n["c"] += 1
            clock["t"] += 30.0  # each attempt "takes" 30s
            raise _Resp429()

        with pytest.raises(_Resp429):
            rb.retry_sync(
                slow, attempts=4, base=0.001, deadline_seconds=12.0,
                sleep=lambda _s: None, monotonic=lambda: clock["t"],
            )
        assert n["c"] == 1  # tripped the 12s deadline after the first 30s attempt

    def test_deadline_allows_fast_retries(self):
        # Fast attempts (well under the deadline) still retry normally.
        n = {"c": 0}
        clock = {"t": 0.0}

        def fast():
            n["c"] += 1
            clock["t"] += 0.1  # fast
            if n["c"] < 3:
                raise _Resp429()
            return "ok"

        assert rb.retry_sync(
            fast, attempts=5, base=0.001, deadline_seconds=12.0,
            sleep=lambda _s: None, monotonic=lambda: clock["t"],
        ) == "ok"
        assert n["c"] == 3

    def test_deadline_returns_last_retryable_result_fail_loud(self):
        # On a result-based retry that trips the deadline, the last (retryable)
        # result is surfaced — fail-loud, not a hang.
        clock = {"t": 0.0}

        def resp():
            clock["t"] += 20.0
            return {"status": 429}

        out = rb.retry_sync(
            resp, attempts=4, base=0.001, deadline_seconds=5.0,
            retry_on_result=lambda r: r["status"] == 429,
            sleep=lambda _s: None, monotonic=lambda: clock["t"],
        )
        assert out == {"status": 429}


# ── retry drivers ─────────────────────────────────────────────────────────────


class TestRetrySync:
    def test_succeeds_after_transient(self):
        n = {"c": 0}
        def fn():
            n["c"] += 1
            if n["c"] < 3:
                raise _Resp429()
            return "ok"
        assert rb.retry_sync(fn, attempts=5, base=0.001, cap=0.01) == "ok"
        assert n["c"] == 3

    def test_non_retryable_raises_immediately(self):
        n = {"c": 0}
        def fn():
            n["c"] += 1
            raise ValueError("nope")
        with pytest.raises(ValueError):
            rb.retry_sync(fn, attempts=5, base=0.001)
        assert n["c"] == 1

    def test_exhaustion_reraises_last(self):
        n = {"c": 0}
        def fn():
            n["c"] += 1
            raise _Resp429()
        with pytest.raises(_Resp429):
            rb.retry_sync(fn, attempts=3, base=0.001, cap=0.01)
        assert n["c"] == 3

    def test_retry_on_result(self):
        n = {"c": 0}
        def fn():
            n["c"] += 1
            return {"status": 429} if n["c"] < 2 else {"status": 200}
        out = rb.retry_sync(fn, attempts=4, base=0.001,
                            retry_on_result=lambda r: r["status"] == 429)
        assert out == {"status": 200} and n["c"] == 2

    def test_retry_on_result_exhausts_returns_last(self):
        # never satisfied → returns the last (still-retryable) result, fail-loud
        n = {"c": 0}
        def fn():
            n["c"] += 1
            return {"status": 429}
        out = rb.retry_sync(fn, attempts=3, base=0.001,
                            retry_on_result=lambda r: r["status"] == 429)
        assert out == {"status": 429} and n["c"] == 3

    def test_attempts_one_is_no_retry(self):
        n = {"c": 0}
        def fn():
            n["c"] += 1
            raise _Resp429()
        with pytest.raises(_Resp429):
            rb.retry_sync(fn, attempts=1)
        assert n["c"] == 1


class TestRetryAsync:
    @pytest.mark.asyncio
    async def test_succeeds_after_transient(self):
        n = {"c": 0}
        async def fn():
            n["c"] += 1
            if n["c"] < 3:
                raise _Resp429()
            return "ok"
        assert await rb.retry_async(fn, attempts=5, base=0.001, cap=0.01) == "ok"
        assert n["c"] == 3

    @pytest.mark.asyncio
    async def test_cancelled_not_retried(self):
        n = {"c": 0}
        async def fn():
            n["c"] += 1
            raise asyncio.CancelledError()
        with pytest.raises(asyncio.CancelledError):
            await rb.retry_async(fn, attempts=5, base=0.001)
        assert n["c"] == 1  # never retried — shutdown stays prompt

    @pytest.mark.asyncio
    async def test_non_retryable_raises_immediately(self):
        n = {"c": 0}
        async def fn():
            n["c"] += 1
            raise ValueError("nope")
        with pytest.raises(ValueError):
            await rb.retry_async(fn, attempts=5, base=0.001)
        assert n["c"] == 1

    @pytest.mark.asyncio
    async def test_deadline_stops_after_slow_attempt(self):
        # Async deadline (fake clock): a slow attempt tripping the deadline is not
        # retried into attempts × timeout.
        n = {"c": 0}
        clock = {"t": 0.0}
        async def slow():
            n["c"] += 1
            clock["t"] += 30.0
            raise _Resp429()
        with pytest.raises(_Resp429):
            await rb.retry_async(slow, attempts=4, base=0.001,
                                 deadline_seconds=12.0, monotonic=lambda: clock["t"])
        assert n["c"] == 1

    @pytest.mark.asyncio
    async def test_deadline_returns_last_retryable_result(self):
        clock = {"t": 0.0}
        async def resp():
            clock["t"] += 20.0
            return {"status": 429}
        out = await rb.retry_async(
            resp, attempts=4, base=0.001, deadline_seconds=5.0,
            retry_on_result=lambda r: r["status"] == 429, monotonic=lambda: clock["t"])
        assert out == {"status": 429}  # fail-loud, last retryable result


# ── web3 middleware ───────────────────────────────────────────────────────────


class TestWeb3Middleware:
    def test_no_retry_methods(self):
        for m in ("eth_sendRawTransaction", "eth_sendTransaction", "eth_sign",
                  "eth_signTransaction", "personal_sign", "anvil_setBalance",
                  "evm_mine", "evm_snapshot", "hardhat_setCode", "miner_start",
                  "txpool_content"):
            assert _is_no_retry(m), m
        for m in ("eth_call", "eth_getBalance", "eth_blockNumber", "eth_getBlockByNumber",
                  "eth_getTransactionReceipt", "eth_estimateGas", "eth_getLogs"):
            assert not _is_no_retry(m), m

    def test_response_is_retryable(self):
        assert _response_is_retryable({"error": {"code": -32005}})
        assert not _response_is_retryable({"error": {"code": -32000}})  # generic
        assert not _response_is_retryable({"result": "0x1"})
        assert not _response_is_retryable({"error": {"code": 3}})  # revert
        assert not _response_is_retryable("not a dict")

    def test_read_retries_on_rpc_error_body(self, monkeypatch):
        monkeypatch.setattr(rb, "DEFAULT_BASE_SECONDS", 0.001)
        monkeypatch.setattr(rb, "DEFAULT_CAP_SECONDS", 0.01)
        mw = RpcRetryMiddleware(object())
        n = {"c": 0}
        def make_request(method, params):
            n["c"] += 1
            if n["c"] < 2:
                return {"error": {"code": -32005, "message": "CU"}}
            return {"result": "0xabc"}
        wrapped = mw.wrap_make_request(make_request)
        assert wrapped("eth_call", []) == {"result": "0xabc"}
        assert n["c"] == 2

    def test_read_retries_on_raised_429(self, monkeypatch):
        monkeypatch.setattr(rb, "DEFAULT_BASE_SECONDS", 0.001)
        monkeypatch.setattr(rb, "DEFAULT_CAP_SECONDS", 0.01)
        mw = RpcRetryMiddleware(object())
        n = {"c": 0}
        def make_request(method, params):
            n["c"] += 1
            if n["c"] < 3:
                raise _Resp429()
            return {"result": "0x1"}
        wrapped = mw.wrap_make_request(make_request)
        assert wrapped("eth_getBalance", ["0x0"]) == {"result": "0x1"}
        assert n["c"] == 3

    def test_send_is_never_retried(self):
        mw = RpcRetryMiddleware(object())
        n = {"c": 0}
        def make_request(method, params):
            n["c"] += 1
            raise _Resp429()  # a transient error on a SEND
        wrapped = mw.wrap_make_request(make_request)
        with pytest.raises(_Resp429):
            wrapped("eth_sendRawTransaction", ["0x.."])
        assert n["c"] == 1  # sent exactly once, no retry (idempotency safety)

    @pytest.mark.asyncio
    async def test_async_read_retries_and_send_passthrough(self, monkeypatch):
        monkeypatch.setattr(rb, "DEFAULT_BASE_SECONDS", 0.001)
        monkeypatch.setattr(rb, "DEFAULT_CAP_SECONDS", 0.01)
        mw = RpcRetryMiddleware(object())
        n = {"c": 0}
        async def make_request(method, params):
            n["c"] += 1
            if str(method) == "eth_call" and n["c"] < 2:
                raise _Resp429()
            return {"result": "0x1"}
        wrapped = await mw.async_wrap_make_request(make_request)
        assert await wrapped("eth_call", []) == {"result": "0x1"}
        assert n["c"] == 2
        n["c"] = 0
        with pytest.raises(_Resp429):
            # a send that transiently errors is NOT retried
            async def boom(method, params):
                raise _Resp429()
            w2 = await mw.async_wrap_make_request(boom)
            await w2("eth_sendRawTransaction", ["0x"])
