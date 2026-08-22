"""The proxies between anvil and the upstream must retry a gateway timeout.

`-32070 Gateway request timeout` arrives as an HTTP 200 body carrying a
JSON-RPC error, so the proxies' retry decision runs through
`body_has_retryable_rpc_error`. That helper hardcoded `-32005` while
`RETRYABLE_RPC_CODES` was maintained separately — the drift is why a gateway
timeout was never retried anywhere, surfaced as a raw exception inside the
scoreIntent simulation, and reached the miner as "your plan reverted".

Absorbing it HERE is the cheapest fix available: anvil never sees an error and
the benchmark never has to re-run the scenario.
"""
from minotaur_subnet.rpc_backoff import (
    RETRYABLE_RPC_CODES,
    body_has_retryable_rpc_error,
    is_retryable_rpc_code,
)

GATEWAY = b'{"jsonrpc":"2.0","id":1,"error":{"code":-32070,"message":"Gateway request timeout"}}'
RATELIMIT = b'{"jsonrpc":"2.0","id":1,"error":{"code":-32005,"message":"rate limited"}}'
DETERMINISTIC = b'{"jsonrpc":"2.0","id":1,"error":{"code":-32000,"message":"missing trie node"}}'
OK = b'{"jsonrpc":"2.0","id":1,"result":"0x1"}'


def test_gateway_timeout_body_is_retryable():
    assert body_has_retryable_rpc_error(GATEWAY)


def test_rate_limit_body_still_retryable():
    assert body_has_retryable_rpc_error(RATELIMIT)


def test_deterministic_error_is_not_retried():
    """-32000 is the generic server-error range nodes return for DETERMINISTIC
    read failures; retrying it burns attempts on something a retry can't fix."""
    assert not body_has_retryable_rpc_error(DETERMINISTIC)


def test_success_and_empty_bodies_are_not_retried():
    assert not body_has_retryable_rpc_error(OK)
    assert not body_has_retryable_rpc_error(None)
    assert not body_has_retryable_rpc_error(b"")


def test_large_bodies_are_skipped():
    """A big `result` body is never scanned — error responses are tiny."""
    assert not body_has_retryable_rpc_error(b'{"error"' + b"x" * 5000)


def test_body_patterns_track_the_code_set():
    """The regression guard: these two must not drift apart again."""
    for code in RETRYABLE_RPC_CODES:
        body = b'{"error":{"code":' + str(code).encode() + b',"message":"x"}}'
        assert body_has_retryable_rpc_error(body), code
        assert is_retryable_rpc_code(code)
