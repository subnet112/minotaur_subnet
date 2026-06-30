"""Unit tests for the validator ChampionResolver (state-consolidation keystone, Phase 2).

The validator reads the champion from its co-located API (GET /v1/solver/champion) with a
bounded last-known-good memo, so a transient API restart never flips a standing champion
to 100% burn. These tests drive the memo/TTL logic deterministically by overriding the
single network seam (_fetch) and passing a monotonic `now` — no real HTTP, no wall-clock.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from minotaur_subnet.validator.champion_client import ChampionResolver


class _FakeResolver(ChampionResolver):
    """ChampionResolver with the network seam replaced by a scripted next-result."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self._next = ("ok", None)  # ("ok", hotkey|None) or ("raise",)

    async def _fetch(self):
        if self._next[0] == "raise":
            raise RuntimeError("api down")
        return self._next[1]


def _run(coro):
    return asyncio.run(coro)


def test_unconfigured_url_is_none():
    r = _FakeResolver("")
    assert r.configured is False
    assert _run(r.resolve(now=1000.0)) == (None, "none")


def test_api_success_refreshes_memo():
    r = _FakeResolver("http://api:8080", memo_ttl_seconds=100)
    r._next = ("ok", "5CM7real")
    assert _run(r.resolve(now=1000.0)) == ("5CM7real", "api")


def test_api_definitive_no_champion_is_api_none():
    r = _FakeResolver("http://api:8080", memo_ttl_seconds=100)
    r._next = ("ok", None)  # API reachable, reports no champion
    assert _run(r.resolve(now=1000.0)) == (None, "api")


def test_failure_within_ttl_returns_memo():
    r = _FakeResolver("http://api:8080", memo_ttl_seconds=100)
    r._next = ("ok", "5CM7real")
    _run(r.resolve(now=1000.0))           # seed the memo
    r._next = ("raise",)
    assert _run(r.resolve(now=1050.0)) == ("5CM7real", "memo")  # within TTL


def test_failure_past_ttl_returns_none():
    r = _FakeResolver("http://api:8080", memo_ttl_seconds=100)
    r._next = ("ok", "5CM7real")
    _run(r.resolve(now=1000.0))
    r._next = ("raise",)
    assert _run(r.resolve(now=2000.0)) == (None, "none")  # memo expired


def test_failure_with_no_memo_returns_none():
    r = _FakeResolver("http://api:8080", memo_ttl_seconds=100)
    r._next = ("raise",)
    assert _run(r.resolve(now=1000.0)) == (None, "none")


def test_none_read_does_not_poison_memo():
    """A definitive 'no champion' (None) must NOT overwrite a good last-known-good memo —
    else a transient no-champion read (e.g. the API's store-load window) becomes a sticky
    burn the next time the API blips."""
    r = _FakeResolver("http://api:8080", memo_ttl_seconds=100)
    r._next = ("ok", "5CM7real")
    assert _run(r.resolve(now=1000.0)) == ("5CM7real", "api")   # seed a good memo
    r._next = ("ok", None)                                       # definitive no-champion
    assert _run(r.resolve(now=1010.0)) == (None, "api")         # returned in real time...
    r._next = ("raise",)
    assert _run(r.resolve(now=1020.0)) == ("5CM7real", "memo")  # ...but memo NOT poisoned
