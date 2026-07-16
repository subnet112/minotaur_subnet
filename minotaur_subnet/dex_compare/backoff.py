"""Shared HTTP request helper with 429/5xx + Retry-After exponential backoff.

There is no generic HTTP-backoff util in the repo (``_retry_rpc`` in
``blockchain/contracts.py`` is web3-specific), so this mirrors the inline retry
pattern used in ``consensus/peer_discovery.py`` but generalised for the DEX
aggregators, which throttle aggressively on their free tiers.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import Any

import aiohttp

logger = logging.getLogger(__name__)

# Statuses worth retrying: rate limit + transient upstream errors.
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


@dataclass
class BackoffResult:
    ok: bool
    status: int | None
    data: Any | None
    error: str | None
    attempts: int


def _expo_delay(attempt: int, base: float = 1.0, cap: float = 30.0) -> float:
    """Exponential backoff with jitter: base * 2^(attempt-1), capped, +0-25% jitter."""
    raw = min(cap, base * (2 ** (attempt - 1)))
    return raw + random.uniform(0, raw * 0.25)


def _retry_after_seconds(resp: aiohttp.ClientResponse) -> float | None:
    """Parse a Retry-After header (delta-seconds or HTTP-date) into seconds."""
    header = resp.headers.get("Retry-After")
    if not header:
        return None
    header = header.strip()
    if header.isdigit():
        return float(header)
    try:
        dt = parsedate_to_datetime(header)
        if dt is None:
            return None
        import time as _time

        # HTTP-date is absolute; convert to a delay from now (never negative).
        return max(0.0, dt.timestamp() - _time.time())
    except (TypeError, ValueError, OverflowError):
        return None


def _extract_error(data: Any, status: int | None) -> str:
    if isinstance(data, dict):
        for key in ("description", "error", "message", "reason", "detail"):
            val = data.get(key)
            if isinstance(val, str) and val:
                return val
    return f"HTTP {status}" if status is not None else "request failed"


async def request_with_backoff(
    session: aiohttp.ClientSession,
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    params: dict[str, str] | None = None,
    json_body: Any | None = None,
    max_retries: int = 4,
) -> BackoffResult:
    """Issue an HTTP request, retrying rate-limits/transient errors with backoff.

    Never raises: transport errors and exhausted retries are returned as a
    non-ok :class:`BackoffResult`. On success ``data`` holds the parsed JSON.
    """
    total_attempts = max(1, max_retries + 1)
    last_status: int | None = None
    last_error: str | None = None

    for attempt in range(1, total_attempts + 1):
        is_last = attempt == total_attempts
        try:
            async with session.request(
                method, url, headers=headers, params=params, json=json_body,
            ) as resp:
                last_status = resp.status
                if resp.status in RETRYABLE_STATUS and not is_last:
                    delay = _retry_after_seconds(resp)
                    if delay is None:
                        delay = _expo_delay(attempt)
                    logger.debug(
                        "backoff %s %s -> HTTP %d, sleeping %.2fs (attempt %d/%d)",
                        method, url, resp.status, delay, attempt, total_attempts,
                    )
                    await asyncio.sleep(delay)
                    continue

                text = await resp.text()
                data: Any = None
                if text:
                    try:
                        data = json.loads(text)
                    except (ValueError, json.JSONDecodeError):
                        data = None

                if 200 <= resp.status < 300:
                    return BackoffResult(True, resp.status, data, None, attempt)
                return BackoffResult(
                    False, resp.status, data, _extract_error(data, resp.status), attempt,
                )
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            if is_last:
                return BackoffResult(False, last_status, None, last_error, attempt)
            await asyncio.sleep(_expo_delay(attempt))

    # Unreachable in practice (the loop always returns on the last attempt).
    return BackoffResult(False, last_status, None, last_error or "request failed", total_attempts)
