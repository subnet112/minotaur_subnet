"""Unit tests for the persistent + background token-list cache.

Covers the store round-trip and the TokenListCache refresh semantics that keep
token discovery off the request path.
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from minotaur_subnet.store.app_intent_store import AppIntentStore
from minotaur_subnet.api.token_cache import TokenListCache


_TOKENS = [
    {"address": "0xWETH", "symbol": "WETH", "decimals": 18, "pool_count": 9},
    {"address": "0xUSDC", "symbol": "USDC", "decimals": 6, "pool_count": 7},
]


def _store() -> AppIntentStore:
    tmp = tempfile.mkdtemp()
    return AppIntentStore(store_path=Path(tmp) / "store.json")


class TestTokenListStore(unittest.TestCase):
    def test_save_get_roundtrip(self):
        s = _store()
        self.assertIsNone(s.get_token_list(8453))
        s.save_token_list(8453, _TOKENS, updated_at=1234.0)
        got = s.get_token_list(8453)
        self.assertIsNotNone(got)
        ts, tokens = got
        self.assertEqual(ts, 1234.0)
        self.assertEqual(tokens, _TOKENS)

    def test_upsert_overwrites(self):
        s = _store()
        s.save_token_list(8453, _TOKENS, updated_at=1.0)
        s.save_token_list(8453, [_TOKENS[0]], updated_at=2.0)
        ts, tokens = s.get_token_list(8453)
        self.assertEqual(ts, 2.0)
        self.assertEqual(len(tokens), 1)

    def test_per_chain_isolation(self):
        s = _store()
        s.save_token_list(8453, _TOKENS)
        self.assertIsNone(s.get_token_list(1))


class _SyncSolver:
    def __init__(self, tokens):
        self.tokens = tokens
        self.calls = 0

    def supported_tokens(self, chain_id):
        self.calls += 1
        return list(self.tokens)


class _AsyncSolver:
    def __init__(self, tokens):
        self.tokens = tokens
        self.calls = 0

    async def supported_tokens(self, chain_id):
        self.calls += 1
        return list(self.tokens)


class TestTokenListCache(unittest.TestCase):
    def test_refresh_persists_sync_solver(self):
        s = _store()
        bl = SimpleNamespace(solver=_SyncSolver(_TOKENS))
        cache = TokenListCache(s, bl, [8453])
        n = asyncio.run(cache.refresh_chain(8453))
        self.assertEqual(n, 2)
        _, tokens = s.get_token_list(8453)
        self.assertEqual(tokens, _TOKENS)

    def test_refresh_persists_async_solver(self):
        s = _store()
        bl = SimpleNamespace(solver=_AsyncSolver(_TOKENS))
        cache = TokenListCache(s, bl, [8453])
        n = asyncio.run(cache.refresh_chain(8453))
        self.assertEqual(n, 2)
        self.assertEqual(s.get_token_list(8453)[1], _TOKENS)

    def test_empty_result_keeps_previous(self):
        # A transient empty discovery must NOT wipe a good cached list.
        s = _store()
        s.save_token_list(8453, _TOKENS, updated_at=5.0)
        bl = SimpleNamespace(solver=_SyncSolver([]))
        cache = TokenListCache(s, bl, [8453])
        n = asyncio.run(cache.refresh_chain(8453))
        self.assertEqual(n, 0)
        ts, tokens = s.get_token_list(8453)
        self.assertEqual(ts, 5.0)          # untouched
        self.assertEqual(tokens, _TOKENS)  # previous list preserved

    def test_no_solver_returns_minus_one_and_persists_nothing(self):
        s = _store()
        bl = SimpleNamespace(solver=None)
        cache = TokenListCache(s, bl, [8453])
        n = asyncio.run(cache.refresh_chain(8453))
        self.assertEqual(n, -1)
        self.assertIsNone(s.get_token_list(8453))

    def test_reads_live_solver_from_block_loop(self):
        # The cache must pick up a hot-swapped champion solver.
        s = _store()
        bl = SimpleNamespace(solver=_SyncSolver([_TOKENS[0]]))
        cache = TokenListCache(s, bl, [8453])
        asyncio.run(cache.refresh_chain(8453))
        self.assertEqual(len(s.get_token_list(8453)[1]), 1)
        bl.solver = _SyncSolver(_TOKENS)  # champion swap
        asyncio.run(cache.refresh_chain(8453))
        self.assertEqual(len(s.get_token_list(8453)[1]), 2)

    def test_refresh_all_survives_one_chain_failing(self):
        s = _store()

        class _Boom:
            def supported_tokens(self, chain_id):
                if chain_id == 1:
                    raise RuntimeError("rpc down")
                return _TOKENS

        cache = TokenListCache(s, SimpleNamespace(solver=_Boom()), [1, 8453])
        asyncio.run(cache.refresh_all())  # must not raise
        self.assertIsNone(s.get_token_list(1))
        self.assertEqual(s.get_token_list(8453)[1], _TOKENS)


if __name__ == "__main__":
    unittest.main()
