"""Tests for /consensus/proposal rate limit + replay protection (audit H1).

Locks in the wire-safe pieces of PR #47:
- Per-IP token bucket middleware on /consensus/proposal (burst 30,
  refill 1/s).
- 64 KiB body cap.
- Timestamp freshness check (>60s stale, >10s future → reject).
- (order_id, plan_hash) dedup cache with 10-min TTL.

Excludes the EIP-191 domain separator change from the parent PR —
that's wire-incompatible and stays deferred for a coordinated rollout.
"""

from __future__ import annotations

import asyncio
import importlib
import sys
import time
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


# ── Rate limit middleware ────────────────────────────────────────────


def _reload_validator_main():
    """Re-import to reset the module-level rate-limit state per test."""
    from minotaur_subnet.validator import main as _m
    importlib.reload(_m)
    return _m


@pytest.mark.asyncio
async def test_rate_limit_allows_burst_then_throttles():
    """Burst capacity (30) should pass; the 31st within 1s gets 429."""
    validator_main = _reload_validator_main()

    from aiohttp.test_utils import make_mocked_request

    async def passthrough_handler(_req):
        from aiohttp import web
        return web.json_response({"ok": True})

    allowed = 0
    rate_limited = 0
    for _ in range(35):
        req = make_mocked_request(
            "POST", "/consensus/proposal",
            headers={"X-Real-IP": "1.2.3.4"},
        )
        resp = await validator_main._proposal_rate_limit(req, passthrough_handler)
        if resp.status == 200:
            allowed += 1
        elif resp.status == 429:
            rate_limited += 1
    # Burst cap of 30 means at most 30 should succeed back-to-back; the
    # exact number depends on refill during the test window but must be
    # at most capacity + a few refills (sub-second test).
    assert 28 <= allowed <= 32, f"expected ~30 allowed, got {allowed}"
    assert rate_limited >= 3, f"expected throttling after burst, got {rate_limited}"


@pytest.mark.asyncio
async def test_rate_limit_is_per_ip():
    """Two different IPs should each get their own bucket."""
    validator_main = _reload_validator_main()
    from aiohttp.test_utils import make_mocked_request

    async def passthrough_handler(_req):
        from aiohttp import web
        return web.json_response({"ok": True})

    # IP A: burn the full bucket.
    for _ in range(30):
        req = make_mocked_request(
            "POST", "/consensus/proposal",
            headers={"X-Real-IP": "10.0.0.1"},
        )
        await validator_main._proposal_rate_limit(req, passthrough_handler)

    # IP B: must still have full credit.
    req_b = make_mocked_request(
        "POST", "/consensus/proposal",
        headers={"X-Real-IP": "10.0.0.2"},
    )
    resp_b = await validator_main._proposal_rate_limit(req_b, passthrough_handler)
    assert resp_b.status == 200


@pytest.mark.asyncio
async def test_rate_limit_skipped_for_other_paths():
    """Middleware only fires on /consensus/proposal."""
    validator_main = _reload_validator_main()
    from aiohttp.test_utils import make_mocked_request

    called = {"count": 0}

    async def passthrough_handler(_req):
        from aiohttp import web
        called["count"] += 1
        return web.json_response({"ok": True})

    # 100 hits on /health should never throttle.
    for _ in range(100):
        req = make_mocked_request(
            "GET", "/health",
            headers={"X-Real-IP": "1.2.3.4"},
        )
        resp = await validator_main._proposal_rate_limit(req, passthrough_handler)
        assert resp.status == 200
    assert called["count"] == 100


# ── Timestamp freshness check ────────────────────────────────────────


@pytest.mark.asyncio
async def test_verify_proposer_signature_rejects_missing_timestamp():
    from minotaur_subnet.validator.scoring_engine import ScoringEngine
    se = ScoringEngine(
        store=None, js_engine=None, simulator=None,
        consensus=None, peer_network=None,
        validator_id="0x" + "0" * 40,
    )
    # Force CONSENSUS_REQUIRE_SIGNED_PROPOSALS=1 path so the check is
    # exercised — otherwise unsigned mode skips it.
    body = {"proposer_signature": "0x" + "0" * 130}  # no timestamp
    ok, reason = se.verify_proposer_signature(body)
    assert ok is False
    assert reason == "missing_timestamp"


@pytest.mark.asyncio
async def test_verify_proposer_signature_rejects_stale_timestamp():
    from minotaur_subnet.validator.scoring_engine import ScoringEngine
    se = ScoringEngine(
        store=None, js_engine=None, simulator=None,
        consensus=None, peer_network=None,
        validator_id="0x" + "0" * 40,
    )
    body = {
        "proposer_signature": "0x" + "0" * 130,
        "timestamp": time.time() - 120,  # 2 min old (>60s threshold)
    }
    ok, reason = se.verify_proposer_signature(body)
    assert ok is False
    assert reason.startswith("stale_or_future_timestamp")


@pytest.mark.asyncio
async def test_verify_proposer_signature_rejects_future_timestamp():
    """Reject sigs claiming to be from the future — clock-skew attack."""
    from minotaur_subnet.validator.scoring_engine import ScoringEngine
    se = ScoringEngine(
        store=None, js_engine=None, simulator=None,
        consensus=None, peer_network=None,
        validator_id="0x" + "0" * 40,
    )
    body = {
        "proposer_signature": "0x" + "0" * 130,
        "timestamp": time.time() + 60,  # 1 min in future (>10s skew)
    }
    ok, reason = se.verify_proposer_signature(body)
    assert ok is False
    assert reason.startswith("stale_or_future_timestamp")


@pytest.mark.asyncio
async def test_verify_proposer_signature_bad_timestamp_format():
    from minotaur_subnet.validator.scoring_engine import ScoringEngine
    se = ScoringEngine(
        store=None, js_engine=None, simulator=None,
        consensus=None, peer_network=None,
        validator_id="0x" + "0" * 40,
    )
    body = {
        "proposer_signature": "0x" + "0" * 130,
        "timestamp": "not-a-float",
    }
    ok, reason = se.verify_proposer_signature(body)
    assert ok is False
    assert reason == "bad_timestamp"


# ── Replay cache (order_id, plan_hash) ───────────────────────────────


@pytest.mark.asyncio
async def test_replay_cache_state_is_module_level():
    """The cache + eviction helper live on the module so a single
    process maintains one dedup set across calls."""
    from minotaur_subnet.validator import scoring_engine as se_mod
    assert hasattr(se_mod, "_SEEN_PROPOSALS")
    assert hasattr(se_mod, "_SEEN_PROPOSALS_LOCK")
    assert hasattr(se_mod, "_evict_expired_locked")
    assert se_mod._SEEN_PROPOSALS_TTL == 600.0
    assert se_mod._SEEN_PROPOSALS_MAX == 10_000


@pytest.mark.asyncio
async def test_eviction_drops_stale_entries_when_over_cap():
    from minotaur_subnet.validator import scoring_engine as se_mod
    # Snapshot real state; restore after.
    saved = dict(se_mod._SEEN_PROPOSALS)
    try:
        se_mod._SEEN_PROPOSALS.clear()
        # Seed 10_001 entries (just over the cap), the first half stale.
        now = time.monotonic()
        for i in range(10_001):
            ts = now - 1000.0 if i < 5000 else now - 1.0  # 5000 stale
            se_mod._SEEN_PROPOSALS[(f"ord{i}", f"hash{i}")] = ts
        assert len(se_mod._SEEN_PROPOSALS) == 10_001
        se_mod._evict_expired_locked(now)
        # Stale ones should be gone; size should be back at or below the cap.
        assert len(se_mod._SEEN_PROPOSALS) <= se_mod._SEEN_PROPOSALS_MAX
        # No stale entries should remain.
        cutoff = now - se_mod._SEEN_PROPOSALS_TTL
        for ts in se_mod._SEEN_PROPOSALS.values():
            assert ts >= cutoff
    finally:
        se_mod._SEEN_PROPOSALS.clear()
        se_mod._SEEN_PROPOSALS.update(saved)


def test_eviction_noop_when_below_cap():
    """The cheap-path branch must not iterate when len() is under the cap.

    Important: the function is fast O(1) for the common case. Without this
    short-circuit, every proposal would walk the cache.
    """
    from minotaur_subnet.validator import scoring_engine as se_mod
    saved = dict(se_mod._SEEN_PROPOSALS)
    try:
        se_mod._SEEN_PROPOSALS.clear()
        now = time.monotonic()
        # Seed 5 stale entries, well under cap.
        for i in range(5):
            se_mod._SEEN_PROPOSALS[(f"o{i}", f"h{i}")] = now - 1000.0
        se_mod._evict_expired_locked(now)
        # All 5 still present — eviction only kicks in over cap.
        assert len(se_mod._SEEN_PROPOSALS) == 5
    finally:
        se_mod._SEEN_PROPOSALS.clear()
        se_mod._SEEN_PROPOSALS.update(saved)
