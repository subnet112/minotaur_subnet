"""Tests for M1 (fail-closed metagraph gate) + M2 (X-Real-IP rate-limit key).

Both findings from 2026-05-25 audit.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from minotaur_subnet.api.routes.submissions.routes import (
    _require_registered_miner,
    _resolve_client_ip,
    _enforce_rate_limit,
    _rate_limit_buckets,
)


# ──────────────────────────────────────────────────────────────────────
# M1 — _require_registered_miner now fails CLOSED on missing metagraph
# ──────────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_env():
    """Each test starts from a clean env for the flags we care about."""
    keys = [
        "SUBMISSIONS_ALLOW_UNREGISTERED",
        "LOCAL_TESTNET",
        "TRUST_PROXY_HEADERS",
        "SUBMISSIONS_RATE_LIMIT_PER_MINUTE",
    ]
    saved = {k: os.environ.pop(k, None) for k in keys}
    _rate_limit_buckets.clear()
    yield
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def test_m1_rejects_when_metagraph_sync_is_none():
    """Audit found this fail-open; now MUST fail closed → 503."""
    with patch(
        "minotaur_subnet.api.server_context.ctx",
        SimpleNamespace(solver_round_metagraph_sync=None),
    ):
        with pytest.raises(HTTPException) as exc:
            _require_registered_miner("5GAnyHotkey")
    assert exc.value.status_code == 503
    assert "metagraph sync" in exc.value.detail.lower()


def test_m1_rejects_when_state_has_never_synced():
    """``state is None`` was the live-exploit case in the audit."""
    sync = MagicMock()
    sync.state = None
    with patch(
        "minotaur_subnet.api.server_context.ctx",
        SimpleNamespace(solver_round_metagraph_sync=sync),
    ):
        with pytest.raises(HTTPException) as exc:
            _require_registered_miner("5GFakeHotkey")
    assert exc.value.status_code == 503
    assert "metagraph state" in exc.value.detail.lower()


def test_m1_local_testnet_still_fails_open():
    """LOCAL_TESTNET=1 preserves the previous fail-open behavior for dev."""
    os.environ["LOCAL_TESTNET"] = "1"
    # ctx fields don't matter — the gate returns before reading them.
    _require_registered_miner("5GDevHotkey")  # no raise


def test_m1_emergency_override_still_works():
    """SUBMISSIONS_ALLOW_UNREGISTERED=1 is the operator override."""
    os.environ["SUBMISSIONS_ALLOW_UNREGISTERED"] = "1"
    _require_registered_miner("5GAnything")  # no raise


def test_m1_registered_hotkey_passes():
    """Happy path: hotkey in metagraph → no rejection."""
    sync = MagicMock()
    sync.state = SimpleNamespace(
        peers=[SimpleNamespace(hotkey="5GRegistered")],
    )
    with patch(
        "minotaur_subnet.api.server_context.ctx",
        SimpleNamespace(solver_round_metagraph_sync=sync),
    ):
        _require_registered_miner("5GRegistered")  # no raise


def test_m1_unregistered_hotkey_returns_403():
    """Existing behavior preserved: real metagraph + unknown hotkey → 403."""
    sync = MagicMock()
    sync.state = SimpleNamespace(
        peers=[SimpleNamespace(hotkey="5GRegistered")],
    )
    with patch(
        "minotaur_subnet.api.server_context.ctx",
        SimpleNamespace(solver_round_metagraph_sync=sync),
    ):
        with pytest.raises(HTTPException) as exc:
            _require_registered_miner("5GNotInRegistry")
    assert exc.value.status_code == 403


# ──────────────────────────────────────────────────────────────────────
# M2 — Rate limiter reads X-Real-IP / X-Forwarded-For behind a proxy
# ──────────────────────────────────────────────────────────────────────


def _request(client_host: str, headers: dict[str, str]) -> MagicMock:
    """Build a minimal Request-like mock the resolver/limiter can read."""
    req = MagicMock()
    req.client = SimpleNamespace(host=client_host)
    req.headers = headers
    req.url = SimpleNamespace(path="/v1/submissions")
    return req


def test_m2_falls_back_to_client_host_when_proxy_not_trusted():
    """Default: don't trust headers (could be spoofed on direct exposure)."""
    req = _request("203.0.113.7", {"x-real-ip": "10.0.0.1"})
    assert _resolve_client_ip(req) == "203.0.113.7"


def test_m2_reads_x_real_ip_when_proxy_trusted():
    """With TRUST_PROXY_HEADERS=1, nginx's X-Real-IP wins over client.host."""
    os.environ["TRUST_PROXY_HEADERS"] = "1"
    req = _request("127.0.0.1", {"x-real-ip": "203.0.113.7"})
    assert _resolve_client_ip(req) == "203.0.113.7"


def test_m2_reads_x_forwarded_for_first_hop():
    """When only X-Forwarded-For is present, take the leftmost entry."""
    os.environ["TRUST_PROXY_HEADERS"] = "1"
    req = _request("127.0.0.1", {
        "x-forwarded-for": "203.0.113.7, 10.0.0.1, 172.16.0.1",
    })
    assert _resolve_client_ip(req) == "203.0.113.7"


def test_m2_prefers_x_real_ip_over_x_forwarded_for():
    """X-Real-IP is set by nginx for the immediate remote_addr; prefer it."""
    os.environ["TRUST_PROXY_HEADERS"] = "1"
    req = _request("127.0.0.1", {
        "x-real-ip": "198.51.100.5",
        "x-forwarded-for": "203.0.113.7, 10.0.0.1",
    })
    assert _resolve_client_ip(req) == "198.51.100.5"


def test_m2_falls_back_to_client_host_when_headers_empty():
    """Trust-proxy mode but no proxy headers present (direct dev curl)."""
    os.environ["TRUST_PROXY_HEADERS"] = "1"
    req = _request("203.0.113.7", {})
    assert _resolve_client_ip(req) == "203.0.113.7"


def test_m2_two_distinct_ips_get_separate_rate_buckets():
    """The audit pointed out: behind nginx, every external caller appeared
    as 127.0.0.1 and shared one global bucket. With X-Real-IP, they should
    have independent buckets."""
    os.environ["TRUST_PROXY_HEADERS"] = "1"
    os.environ["SUBMISSIONS_RATE_LIMIT_PER_MINUTE"] = "3"

    req_a = _request("127.0.0.1", {"x-real-ip": "203.0.113.7"})
    req_b = _request("127.0.0.1", {"x-real-ip": "198.51.100.5"})

    # A burns its 3-request budget
    for _ in range(3):
        _enforce_rate_limit(req_a, principal="")
    with pytest.raises(HTTPException) as exc:
        _enforce_rate_limit(req_a, principal="")
    assert exc.value.status_code == 429

    # B has a fresh budget despite same client.host
    for _ in range(3):
        _enforce_rate_limit(req_b, principal="")  # no raise
