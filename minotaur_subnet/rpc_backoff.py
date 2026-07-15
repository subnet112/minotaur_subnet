"""Unified exponential-backoff-with-jitter for RPC egress.

ONE primitive so every path that talks to a chain provider — Alchemy, the
lite BT-EVM endpoint, or the deterministic pin/fork proxies — retries transient
failures identically (HTTP 429, JSON-RPC ``-32005`` compute-unit throttling,
5xx, timeouts, connection resets), instead of the ad-hoc mix that let a single
provider hiccup silently zero a miner's benchmark order.

DETERMINISM (why retrying is consensus-safe):
  - A retried READ is idempotent: chain reads are block-pinned in the benchmark
    proxies, so a retry returns the SAME bytes — validators converge on the true
    value rather than one scoring 0 on a 429 and another the real number.
  - The benchmark proxy charges its deterministic per-session BUDGET *before* the
    upstream call, so retries never change ``spent`` or the benchmark pack hash.
  - Only the wall-clock (how long a read took / how many attempts) varies; the
    scored value and the budget do not.

WRITES ARE NOT RETRIED HERE. ``eth_sendRawTransaction`` / ``eth_sendTransaction``
and ``anvil_*`` / ``evm_*`` state mutations are non-idempotent — a blind retry can
double-apply. The relayer owns nonce/gas-aware send retries; the web3 middleware
below hard-excludes write methods.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import time
from typing import Any, Awaitable, Callable, Iterable, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

# ── configuration (env-overridable; conservative defaults) ────────────────────


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip() or default)
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "").strip() or default)
    except (TypeError, ValueError):
        return default


# Total attempts (1 = no retry). 4 attempts with base 0.25s / cap 4s adds only
# ~0.25 + 0.5 + 1.0s of BACKOFF SLEEP before the last try — a fast 429/-32005 is
# cheap to ride out. NOTE: this backoff figure does NOT include per-attempt
# transport time: a caller whose per-request timeout is long (e.g. the proxies'
# 30s) must pass ``deadline_seconds`` so a stalled request isn't re-tried into a
# multi-timeout wall-clock blow-up (see retry_async).
DEFAULT_ATTEMPTS = _env_int("RPC_BACKOFF_ATTEMPTS", 4)
DEFAULT_BASE_SECONDS = _env_float("RPC_BACKOFF_BASE_SECONDS", 0.25)
DEFAULT_CAP_SECONDS = _env_float("RPC_BACKOFF_CAP_SECONDS", 4.0)

# Per-context cumulative wall-clock deadlines (see retry_sync deadline_seconds).
# Proxy forward: the aiohttp per-request timeout is 30s, so a stalled upstream
# must not be re-tried into 4×30s — 12s stops after the first slow attempt while
# still allowing several fast 429/-32005 retries.
DEFAULT_FORWARD_DEADLINE_SECONDS = _env_float("RPC_BACKOFF_FORWARD_DEADLINE_SECONDS", 12.0)
# web3 read middleware: a LIGHTER retry (fewer attempts, tighter deadline) so it
# doesn't nest badly with a caller that already retries (contracts._retry_rpc) or
# block a latency-sensitive loop for long.
DEFAULT_WEB3_ATTEMPTS = _env_int("RPC_BACKOFF_WEB3_ATTEMPTS", 3)
DEFAULT_WEB3_DEADLINE_SECONDS = _env_float("RPC_BACKOFF_WEB3_DEADLINE_SECONDS", 6.0)

# ── retryable classification ──────────────────────────────────────────────────

# Transient HTTP statuses. 429 = rate limit; 5xx = provider/proxy fault.
RETRYABLE_HTTP_STATUS = frozenset({429, 500, 502, 503, 504})

# Transient JSON-RPC error codes. -32005 = Alchemy "compute units per second" /
# rate limit (can arrive as HTTP 200 with this in the error body) — the true
# throttle signal. -32000 is DELIBERATELY EXCLUDED: it is the generic server-error
# range nodes return for many DETERMINISTIC read failures ("missing trie node",
# estimateGas "gas required exceeds allowance"), so retrying it wastes attempts on
# an error a retry can't fix. A genuinely-transient upstream failure surfaces as a
# 429/5xx HTTP status or a timeout/reset (caught by is_retryable_status/exception).
RETRYABLE_RPC_CODES = frozenset({-32005})


def is_retryable_status(status: int | None) -> bool:
    return status in RETRYABLE_HTTP_STATUS


def is_retryable_rpc_code(code: Any) -> bool:
    try:
        return int(code) in RETRYABLE_RPC_CODES
    except (TypeError, ValueError):
        return False


def body_has_retryable_rpc_error(body: bytes | None) -> bool:
    """Cheap check: does a raw JSON-RPC response body carry the TRANSIENT
    ``-32005`` rate-limit error served as HTTP 200? Alchemy returns compute-unit
    throttling this way as well as via HTTP 429. (Only ``-32005`` — not the
    generic ``-32000`` — see RETRYABLE_RPC_CODES.)

    Byte-substring only (no JSON parse) and skips large bodies — error responses
    are tiny, so a big ``result`` body is never scanned. A false positive would
    only cost one idempotent re-fetch of the same value, so erring toward a
    match is safe.
    """
    if not body or len(body) > 4096:
        return False
    if b'"error"' not in body:
        return False
    return b"-32005" in body


def _status_of(exc: BaseException) -> int | None:
    """Best-effort HTTP status from a requests/aiohttp/web3 exception."""
    resp = getattr(exc, "response", None)
    for attr in ("status_code", "status"):
        val = getattr(resp, attr, None) if resp is not None else getattr(exc, attr, None)
        if isinstance(val, int):
            return val
    return None


def _rpc_code_of(exc: BaseException) -> Any | None:
    """Best-effort JSON-RPC error code from a web3 exception."""
    for attr in ("code", "rpc_response"):
        val = getattr(exc, attr, None)
        if isinstance(val, dict):
            err = val.get("error")
            if isinstance(err, dict) and "code" in err:
                return err.get("code")
        elif val is not None and attr == "code":
            return val
    # web3 often puts the response dict as the first arg
    for arg in getattr(exc, "args", ()) or ():
        if isinstance(arg, dict):
            err = arg.get("error") if isinstance(arg.get("error"), dict) else arg
            if isinstance(err, dict) and "code" in err:
                return err.get("code")
    return None


def is_retryable_exception(exc: BaseException) -> bool:
    """True for TRANSIENT network/provider errors worth retrying (reads only).

    Deliberately broad for network faults (this is only ever wrapped around RPC
    egress, where OSError/ConnectionError are network): timeouts, connection
    resets, and 429/5xx or ``-32005`` surfaced by requests/aiohttp/web3. A
    non-network error (bad params, contract revert, generic ``-32000``) is NOT
    retryable.
    """
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError, ConnectionError, ConnectionResetError)):
        return True
    status = _status_of(exc)
    if status is not None:
        return is_retryable_status(status)
    code = _rpc_code_of(exc)
    if code is not None:
        return is_retryable_rpc_code(code)
    mod = (type(exc).__module__ or "")
    # Transport-layer errors from the HTTP client libs (no status/code = a
    # connect/read failure, not an application error) → transient.
    if mod.split(".", 1)[0] in {"aiohttp", "requests", "urllib3", "httpx"}:
        return True
    if isinstance(exc, OSError):
        return True
    return False


# ── backoff delay ─────────────────────────────────────────────────────────────


# Never sleep less than this between retries, even if base/cap are (mis)configured
# to 0 — a zero-delay loop just hammers a throttled provider with no de-throttle.
_MIN_DELAY_SECONDS = 0.02


def backoff_delay(attempt: int, base: float, cap: float) -> float:
    """Exponential backoff with full-ish jitter: ``min(base·2^attempt, cap)``
    scaled by a uniform ``[0.5, 1.5)`` factor, floored at ``_MIN_DELAY_SECONDS``.
    ``attempt`` is 0-indexed (the delay taken AFTER attempt N, before attempt
    N+1). Jitter de-synchronises a fleet all throttled at once so they don't
    retry in lockstep. The floor guards against a BASE/CAP=0 misconfig producing
    a zero-delay tight loop.
    """
    ceiling = min(base * (2 ** max(0, attempt)), cap)
    return max(_MIN_DELAY_SECONDS, ceiling * random.uniform(0.5, 1.5))


# ── core retry drivers (sync + async) ─────────────────────────────────────────


def _should_retry_result(result: Any, retry_on_result: Callable[[Any], bool] | None) -> bool:
    return bool(retry_on_result and retry_on_result(result))


_UNSET = object()


def retry_sync(
    fn: Callable[[], T],
    *,
    attempts: int = DEFAULT_ATTEMPTS,
    base: float = DEFAULT_BASE_SECONDS,
    cap: float = DEFAULT_CAP_SECONDS,
    deadline_seconds: float | None = None,
    retry_on_exc: Callable[[BaseException], bool] = is_retryable_exception,
    retry_on_result: Callable[[Any], bool] | None = None,
    on_retry: Callable[[int, BaseException | None], None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    label: str = "rpc",
) -> T:
    """Call ``fn`` with exponential backoff on transient failures (SYNC).

    Retries when ``fn`` raises a ``retry_on_exc`` error OR returns a value the
    ``retry_on_result`` predicate flags (e.g. an HTTP response with a 429
    status). Re-raises the last exception / returns the last result once
    ``attempts`` OR ``deadline_seconds`` is exhausted — fail-loud, never a silent
    success.

    ``deadline_seconds`` bounds the CUMULATIVE wall-clock: once elapsed since the
    first attempt reaches it, no further attempt is made (checked before each
    backoff). Essential when a single attempt can itself be slow (a long transport
    timeout) — without it, ``attempts`` × ``timeout`` can balloon far past the
    intended budget. A fast 429/-32005 rarely reaches the deadline; a stalled
    request trips it after one attempt.
    """
    attempts = max(1, attempts)
    start = monotonic()
    last_exc: BaseException | None = None
    last_result: Any = _UNSET
    for attempt in range(attempts):
        is_last = attempt == attempts - 1
        try:
            result = fn()
        except BaseException as exc:  # noqa: BLE001 — classified below
            if is_last or not retry_on_exc(exc):
                raise
            last_exc, last_result = exc, _UNSET
        else:
            if is_last or not _should_retry_result(result, retry_on_result):
                return result
            last_exc, last_result = None, result
        if deadline_seconds is not None and (monotonic() - start) >= deadline_seconds:
            if last_result is not _UNSET:
                return last_result  # fail-loud: surface the last retryable result
            raise last_exc  # type: ignore[misc]  # fail-loud: the last retryable exc
        if on_retry is not None:
            on_retry(attempt, last_exc)
        _log_retry(label, attempt, attempts, last_exc)
        sleep(backoff_delay(attempt, base, cap))
    # Unreachable (loop always returns/raises on the last attempt), but keep the
    # type-checker + a defensive fallback honest.
    raise last_exc if last_exc else RuntimeError(f"{label}: retry exhausted")


async def retry_async(
    fn: Callable[[], Awaitable[T]],
    *,
    attempts: int = DEFAULT_ATTEMPTS,
    base: float = DEFAULT_BASE_SECONDS,
    cap: float = DEFAULT_CAP_SECONDS,
    deadline_seconds: float | None = None,
    retry_on_exc: Callable[[BaseException], bool] = is_retryable_exception,
    retry_on_result: Callable[[Any], bool] | None = None,
    on_retry: Callable[[int, BaseException | None], None] | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    label: str = "rpc",
) -> T:
    """Call awaitable ``fn`` with exponential backoff on transient failures.

    Async twin of :func:`retry_sync` (see it for ``deadline_seconds``, which is
    what keeps retrying a 30s-timeout upstream from ballooning to ~4×30s).
    ``asyncio.CancelledError`` is never retried (propagates immediately) so
    shutdown stays prompt. The backoff ``await asyncio.sleep`` yields the loop —
    a throttled session never blocks the others.
    """
    attempts = max(1, attempts)
    start = monotonic()
    last_exc: BaseException | None = None
    last_result: Any = _UNSET
    for attempt in range(attempts):
        is_last = attempt == attempts - 1
        try:
            result = await fn()
        except asyncio.CancelledError:
            raise
        except BaseException as exc:  # noqa: BLE001 — classified below
            if is_last or not retry_on_exc(exc):
                raise
            last_exc, last_result = exc, _UNSET
        else:
            if is_last or not _should_retry_result(result, retry_on_result):
                return result
            last_exc, last_result = None, result
        if deadline_seconds is not None and (monotonic() - start) >= deadline_seconds:
            if last_result is not _UNSET:
                return last_result  # fail-loud: surface the last retryable result
            raise last_exc  # type: ignore[misc]  # fail-loud: the last retryable exc
        if on_retry is not None:
            on_retry(attempt, last_exc)
        _log_retry(label, attempt, attempts, last_exc)
        await asyncio.sleep(backoff_delay(attempt, base, cap))
    raise last_exc if last_exc else RuntimeError(f"{label}: retry exhausted")


def _log_retry(label: str, attempt: int, attempts: int, exc: BaseException | None) -> None:
    logger.info(
        "[rpc-backoff] %s: transient failure on attempt %d/%d (%s) — backing off",
        label, attempt + 1, attempts,
        f"{type(exc).__name__}: {exc}" if exc is not None else "retryable response",
    )


__all__ = [
    "DEFAULT_ATTEMPTS",
    "DEFAULT_BASE_SECONDS",
    "DEFAULT_CAP_SECONDS",
    "RETRYABLE_HTTP_STATUS",
    "RETRYABLE_RPC_CODES",
    "is_retryable_status",
    "is_retryable_rpc_code",
    "is_retryable_exception",
    "body_has_retryable_rpc_error",
    "backoff_delay",
    "retry_sync",
    "retry_async",
]
