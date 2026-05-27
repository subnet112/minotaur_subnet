"""Tests for ``_require_admin_or_signed_miner`` — the gate that lets
either an admin OR a metagraph-registered miner call cost-bearing routes
like ``/orders/{id}/dry-run``.

The path under test is in ``minotaur_subnet.api.routes.apps``. We do NOT
spin up a real FastAPI app — the dependency is a plain callable and we
exercise it directly with stub headers/request, which is much faster and
covers every branch without needing TestClient setup.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from minotaur_subnet.api.routes import apps as apps_route


def _stub_request(method: str = "POST", path: str = "/v1/orders/order_abc/dry-run"):
    """Build a minimal ``request`` stub that satisfies the dependency."""
    req = MagicMock()
    req.method = method
    req.url.path = path
    return req


@pytest.fixture(autouse=True)
def reset_apps_state(monkeypatch):
    """Each test starts with a clean rate-limit bucket + no admin key + no
    metagraph_sync injected. Tests opt in to whichever it needs."""
    monkeypatch.delenv("ADMIN_API_KEY", raising=False)
    monkeypatch.delenv("MINER_RATE_LIMIT_PER_HOUR", raising=False)
    apps_route._MINER_RATE_LIMIT_BUCKETS.clear()
    original_sync = apps_route._metagraph_sync
    apps_route._metagraph_sync = None
    yield
    apps_route._metagraph_sync = original_sync


# ── admin-key path ──────────────────────────────────────────────────────


def test_admin_key_match_allows(monkeypatch):
    """A correct X-Admin-Key skips the miner path entirely."""
    monkeypatch.setenv("ADMIN_API_KEY", "secret123")
    apps_route._require_admin_or_signed_miner(
        request=_stub_request(),
        x_admin_key="secret123",
        x_bittensor_hotkey=None,
        x_bittensor_signature=None,
        x_bittensor_timestamp=None,
    )  # no raise = pass


def test_admin_key_mismatch_falls_through_to_miner_path(monkeypatch):
    """Wrong X-Admin-Key + missing miner headers → 401 (not 'admin bypass'
    semantics — the bad admin key isn't an authentication signal)."""
    monkeypatch.setenv("ADMIN_API_KEY", "secret123")
    with pytest.raises(HTTPException) as exc:
        apps_route._require_admin_or_signed_miner(
            request=_stub_request(),
            x_admin_key="wrong-key",
            x_bittensor_hotkey=None,
            x_bittensor_signature=None,
            x_bittensor_timestamp=None,
        )
    assert exc.value.status_code == 401
    assert "X-Bittensor-Hotkey" in exc.value.detail


# ── missing headers ─────────────────────────────────────────────────────


def test_no_headers_at_all_401():
    """Neither admin key nor miner headers → 401 with helpful detail."""
    with pytest.raises(HTTPException) as exc:
        apps_route._require_admin_or_signed_miner(
            request=_stub_request(),
            x_admin_key=None,
            x_bittensor_hotkey=None,
            x_bittensor_signature=None,
            x_bittensor_timestamp=None,
        )
    assert exc.value.status_code == 401
    assert "admin key" in exc.value.detail.lower()
    assert "signed-miner" in exc.value.detail.lower()


def test_partial_miner_headers_401():
    """Missing any one of the 3 miner headers → 401."""
    with pytest.raises(HTTPException) as exc:
        apps_route._require_admin_or_signed_miner(
            request=_stub_request(),
            x_admin_key=None,
            x_bittensor_hotkey="5FdtBrm...",
            x_bittensor_signature="0xdeadbeef",
            x_bittensor_timestamp=None,  # missing
        )
    assert exc.value.status_code == 401


# ── timestamp freshness ─────────────────────────────────────────────────


def test_timestamp_non_integer_400():
    with pytest.raises(HTTPException) as exc:
        apps_route._require_admin_or_signed_miner(
            request=_stub_request(),
            x_admin_key=None,
            x_bittensor_hotkey="5FdtBrm...",
            x_bittensor_signature="0xdeadbeef",
            x_bittensor_timestamp="not-a-number",
        )
    assert exc.value.status_code == 400


def test_timestamp_too_old_401():
    old_ts = int(time.time()) - 1000
    with pytest.raises(HTTPException) as exc:
        apps_route._require_admin_or_signed_miner(
            request=_stub_request(),
            x_admin_key=None,
            x_bittensor_hotkey="5FdtBrm...",
            x_bittensor_signature="0xdeadbeef",
            x_bittensor_timestamp=str(old_ts),
        )
    assert exc.value.status_code == 401
    assert "off" in exc.value.detail.lower()


def test_timestamp_far_future_401():
    future_ts = int(time.time()) + 1000
    with pytest.raises(HTTPException) as exc:
        apps_route._require_admin_or_signed_miner(
            request=_stub_request(),
            x_admin_key=None,
            x_bittensor_hotkey="5FdtBrm...",
            x_bittensor_signature="0xdeadbeef",
            x_bittensor_timestamp=str(future_ts),
        )
    assert exc.value.status_code == 401


# ── signature verification (mocked Keypair) ─────────────────────────────


def _patch_keypair(verify_returns: bool = True, raises: Exception | None = None):
    """Mock bittensor_wallet.keypair.Keypair so we don't need a real key."""
    mock_kp = MagicMock()
    if raises is not None:
        mock_kp.verify.side_effect = raises
    else:
        mock_kp.verify.return_value = verify_returns
    mock_keypair_class = MagicMock(return_value=mock_kp)
    return patch(
        "bittensor_wallet.keypair.Keypair",
        mock_keypair_class,
    )


def test_bad_signature_401():
    """Keypair.verify returning False → 401 invalid signature."""
    fresh_ts = int(time.time())
    apps_route._metagraph_sync = MagicMock()
    apps_route._metagraph_sync.state.peers = [MagicMock(hotkey="5FdtBrm...")]
    with _patch_keypair(verify_returns=False):
        with pytest.raises(HTTPException) as exc:
            apps_route._require_admin_or_signed_miner(
                request=_stub_request(),
                x_admin_key=None,
                x_bittensor_hotkey="5FdtBrm...",
                x_bittensor_signature="0xdeadbeef",
                x_bittensor_timestamp=str(fresh_ts),
            )
    assert exc.value.status_code == 401
    assert "invalid signature" in exc.value.detail.lower()


# ── metagraph membership ────────────────────────────────────────────────


def test_metagraph_not_synced_503():
    """If _metagraph_sync hasn't populated state yet, fail closed with 503."""
    fresh_ts = int(time.time())
    apps_route._metagraph_sync = MagicMock()
    apps_route._metagraph_sync.state = None  # not synced
    with _patch_keypair(verify_returns=True):
        with pytest.raises(HTTPException) as exc:
            apps_route._require_admin_or_signed_miner(
                request=_stub_request(),
                x_admin_key=None,
                x_bittensor_hotkey="5FdtBrm...",
                x_bittensor_signature="0xdeadbeef",
                x_bittensor_timestamp=str(fresh_ts),
            )
    assert exc.value.status_code == 503


def test_hotkey_not_on_metagraph_403():
    """Valid signature but hotkey isn't in the metagraph peer set → 403."""
    fresh_ts = int(time.time())
    apps_route._metagraph_sync = MagicMock()
    apps_route._metagraph_sync.state.peers = [
        MagicMock(hotkey="5SomeOtherHotkey"),
    ]
    with _patch_keypair(verify_returns=True):
        with pytest.raises(HTTPException) as exc:
            apps_route._require_admin_or_signed_miner(
                request=_stub_request(),
                x_admin_key=None,
                x_bittensor_hotkey="5FdtBrm...",
                x_bittensor_signature="0xdeadbeef",
                x_bittensor_timestamp=str(fresh_ts),
            )
    assert exc.value.status_code == 403
    assert "not on SN112 metagraph" in exc.value.detail


# ── happy path + rate limit ─────────────────────────────────────────────


def test_valid_signed_miner_allowed():
    """Valid sig + hotkey on metagraph + fresh timestamp → no raise."""
    fresh_ts = int(time.time())
    apps_route._metagraph_sync = MagicMock()
    apps_route._metagraph_sync.state.peers = [
        MagicMock(hotkey="5FdtBrm..."),
    ]
    with _patch_keypair(verify_returns=True):
        apps_route._require_admin_or_signed_miner(
            request=_stub_request(),
            x_admin_key=None,
            x_bittensor_hotkey="5FdtBrm...",
            x_bittensor_signature="0xdeadbeef",
            x_bittensor_timestamp=str(fresh_ts),
        )  # no raise


def test_rate_limit_429_after_quota(monkeypatch):
    """61st call within a 60-min window → 429."""
    monkeypatch.setenv("MINER_RATE_LIMIT_PER_HOUR", "3")  # low quota for test
    apps_route._metagraph_sync = MagicMock()
    apps_route._metagraph_sync.state.peers = [
        MagicMock(hotkey="5FdtBrm..."),
    ]
    with _patch_keypair(verify_returns=True):
        for _ in range(3):
            fresh_ts = int(time.time())
            apps_route._require_admin_or_signed_miner(
                request=_stub_request(),
                x_admin_key=None,
                x_bittensor_hotkey="5FdtBrm...",
                x_bittensor_signature="0xdeadbeef",
                x_bittensor_timestamp=str(fresh_ts),
            )
        # 4th should 429
        with pytest.raises(HTTPException) as exc:
            apps_route._require_admin_or_signed_miner(
                request=_stub_request(),
                x_admin_key=None,
                x_bittensor_hotkey="5FdtBrm...",
                x_bittensor_signature="0xdeadbeef",
                x_bittensor_timestamp=str(int(time.time())),
            )
    assert exc.value.status_code == 429
    assert "rate limit" in exc.value.detail.lower()


def test_rate_limit_disabled_when_per_hour_zero(monkeypatch):
    """Operators can disable per-hotkey rate limit with =0."""
    monkeypatch.setenv("MINER_RATE_LIMIT_PER_HOUR", "0")
    apps_route._metagraph_sync = MagicMock()
    apps_route._metagraph_sync.state.peers = [
        MagicMock(hotkey="5FdtBrm..."),
    ]
    with _patch_keypair(verify_returns=True):
        # 5 calls in a row, no 429
        for _ in range(5):
            apps_route._require_admin_or_signed_miner(
                request=_stub_request(),
                x_admin_key=None,
                x_bittensor_hotkey="5FdtBrm...",
                x_bittensor_signature="0xdeadbeef",
                x_bittensor_timestamp=str(int(time.time())),
            )
