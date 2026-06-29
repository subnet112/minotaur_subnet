"""Unit tests for the resolver-aware ValidatorAppCatalogSync.

The API process now runs the app-catalog sync with a metagraph LEADER RESOLVER
(callable) instead of a fixed URL, plus an ``is_follower`` gate — so followers
auto-sync the leader's apps into ``ctx.store`` (which feeds the benchmark pack hash)
WITHOUT a per-follower ``LEADER_API_URL``, and the leader never self-syncs. These
tests cover the new resolve/gate logic (the early returns) without any HTTP.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from minotaur_subnet.validator.app_sync import ValidatorAppCatalogSync


class _Store:  # minimal stand-in; the gated paths never touch it
    pass


def _sync(leader_url, is_follower=None):
    return ValidatorAppCatalogSync(store=_Store(), leader_url=leader_url, is_follower=is_follower)


class TestResolve:
    def test_fixed_string(self):
        assert _sync("http://leader:8080/")._resolve_leader_url() == "http://leader:8080"

    def test_callable(self):
        assert _sync(lambda: "http://x:8080")._resolve_leader_url() == "http://x:8080"

    def test_callable_returns_none(self):
        assert _sync(lambda: None)._resolve_leader_url() is None

    def test_callable_returns_empty(self):
        assert _sync(lambda: "")._resolve_leader_url() is None


class TestSyncOnceGate:
    def test_skips_when_not_follower(self):
        # is_follower=False -> the leader owns the catalog; must NOT self-sync (no HTTP).
        s = _sync(lambda: "http://leader:8080", is_follower=lambda: False)
        assert asyncio.run(s.sync_once()) == (0, 0)

    def test_skips_when_leader_unresolved(self):
        # follower, but leader not resolvable yet (metagraph not synced) -> retry, no HTTP.
        s = _sync(lambda: None, is_follower=lambda: True)
        assert asyncio.run(s.sync_once()) == (0, 0)

    def test_validator_usage_is_ungated(self):
        # Back-compat: the validator passes a fixed string + no gate -> _is_follower None,
        # so the gate is OFF (it would proceed to the HTTP fetch, not short-circuit).
        s = _sync("http://leader:8080")
        assert s._is_follower is None
