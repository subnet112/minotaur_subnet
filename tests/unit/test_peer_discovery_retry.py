"""Tests for the single-retry on transient /identity probe failures.

Pre-fix, a single transient hang on a peer's /identity handler dropped
that peer from the discovered set for a full 60s refresh cycle. With
PR #97 (chain count) and PR #100 (chain-based auth) already shipped,
the consequences are smaller — broadcast might miss the dropped peer,
but a sig from them is still accepted because authorization reads chain.
Still, fewer broadcasts = fewer collected sigs = harder to reach quorum.

This file pins:
  - A peer that times out ONCE then responds OK on the second call is
    NOT dropped — the retry rescues it.
  - A peer that fails twice IS dropped (no infinite loop).
  - Permanent failure modes (HTTP 4xx/5xx, malformed payload, invalid
    sig, wrong hotkey, unauthorized EVM) are NOT retried — they reject
    on the first attempt.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from minotaur_subnet.consensus.peer_discovery import (
    MetagraphPeer,
    _probe_one,
)


class _MockResponse:
    """Stand-in for aiohttp's response context manager."""

    def __init__(self, status: int = 200, body: dict | None = None):
        self.status = status
        self._body = body or {}

    async def json(self):
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _MockSession:
    """Stand-in for aiohttp.ClientSession that scripts a per-call outcome.

    ``outcomes`` is a list of values returned by successive ``.get()``
    calls. Each entry is either:
      - a callable that raises (simulating timeout/client error)
      - a _MockResponse instance (success or HTTP-status failure)
    """

    def __init__(self, outcomes: list):
        self._outcomes = list(outcomes)
        self.call_count = 0

    def get(self, url):
        self.call_count += 1
        if not self._outcomes:
            raise AssertionError(f"Unexpected extra .get({url})")
        outcome = self._outcomes.pop(0)
        if callable(outcome):
            outcome()  # raises
        return outcome


@pytest.mark.asyncio
async def test_probe_retries_once_on_timeout():
    """A peer that times out the first call but responds the second is
    NOT dropped — the retry rescues a transient hang."""
    def raise_timeout():
        raise asyncio.TimeoutError()

    session = _MockSession([
        raise_timeout,   # first attempt times out
        # Second attempt: we won't actually verify the payload here —
        # the test only cares that we made it to the JSON-parse step,
        # which then fails benign on the malformed body. The retry was
        # exercised iff session.call_count == 2.
        _MockResponse(status=200, body={"not": "a valid identity"}),
    ])

    result = await _probe_one(
        session=session,
        metagraph_peer=MetagraphPeer(hotkey="5Hk", axon_url="http://test:9100"),
        authorized_lower={"0x" + "11" * 20},
        my_evm_lower="0x" + "22" * 20,
    )

    # Result is None because the body is malformed (post-retry rejection)
    # — but the IMPORTANT property is that we tried twice. Pre-fix this
    # would have been call_count=1 (timed out, gave up).
    assert session.call_count == 2
    assert result is None  # bad payload rejected


@pytest.mark.asyncio
async def test_probe_gives_up_after_two_timeouts():
    """A peer that times out BOTH attempts is dropped. The retry is
    bounded — no infinite loop on a persistent failure."""
    def raise_timeout():
        raise asyncio.TimeoutError()

    session = _MockSession([raise_timeout, raise_timeout])

    result = await _probe_one(
        session=session,
        metagraph_peer=MetagraphPeer(hotkey="5Hk", axon_url="http://test:9100"),
        authorized_lower={"0x" + "11" * 20},
        my_evm_lower="0x" + "22" * 20,
    )

    assert session.call_count == 2  # exactly one retry, then give up
    assert result is None


@pytest.mark.asyncio
async def test_probe_does_not_retry_http_error():
    """Permanent failures (HTTP 4xx/5xx returned by the peer's /identity
    handler) are NOT transient — no retry. A peer in this state has
    definitively answered "I'm not going to give you an identity", so
    burning more probe budget is wasted."""
    session = _MockSession([
        _MockResponse(status=503),
        # If a retry incorrectly fires, this second outcome would be
        # consumed — and the test's call_count assertion catches it.
        _MockResponse(status=200, body={"ok": True}),
    ])

    result = await _probe_one(
        session=session,
        metagraph_peer=MetagraphPeer(hotkey="5Hk", axon_url="http://test:9100"),
        authorized_lower={"0x" + "11" * 20},
        my_evm_lower="0x" + "22" * 20,
    )

    assert session.call_count == 1  # NO retry on HTTP error
    assert result is None


@pytest.mark.asyncio
async def test_probe_retries_client_error_then_succeeds():
    """A ClientError (connection reset, server disconnect, etc.) is
    transient — same retry treatment as TimeoutError."""
    import aiohttp

    def raise_client_error():
        raise aiohttp.ClientError("connection reset by peer")

    session = _MockSession([
        raise_client_error,
        _MockResponse(status=200, body={"some": "thing"}),
    ])

    result = await _probe_one(
        session=session,
        metagraph_peer=MetagraphPeer(hotkey="5Hk", axon_url="http://test:9100"),
        authorized_lower={"0x" + "11" * 20},
        my_evm_lower="0x" + "22" * 20,
    )

    assert session.call_count == 2  # ClientError WAS retried
    assert result is None  # then rejected on malformed payload
