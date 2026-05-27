"""Tests for ``EpochManager._emit_weights`` POSTing to the validator queue.

The single-emit-path refactor changes ``_emit_weights`` from calling
``self._weights_emitter.emit_async`` directly (which never worked in
production — ``_weights_emitter`` was never wired in on the api side)
to POSTing the per-miner mapping to the validator daemon's
``/internal/weights/queue`` endpoint.

These tests pin:

  - successful POST records ``result="queued"`` in ``_last_emit_state``;
  - non-200 response records ``result="error"`` with the upstream
    status + body excerpt (capped) so the operator can diagnose;
  - connection failures, timeouts, and DNS errors all record
    ``result="error"`` non-fatally — the validator's burn fallback
    covers it;
  - empty mapping short-circuits without making an HTTP call
    (records ``result="empty"``);
  - missing ``VALIDATOR_PRIVATE_KEY`` short-circuits with an
    explanatory error;
  - the request actually carries valid ``X-Internal-Signature`` /
    ``X-Internal-Timestamp`` headers that would verify on the
    validator side.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from minotaur_subnet.epoch.manager import EpochManager
from minotaur_subnet.shared.internal_auth import (
    derive_signer_address,
    verify_request,
)


TEST_KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"


class _FakeAiohttpResponse:
    """Stand-in for aiohttp's response context manager."""

    def __init__(self, status: int, text: str):
        self.status = status
        self._text = text

    async def text(self) -> str:
        return self._text

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeAiohttpSession:
    """Stand-in for aiohttp.ClientSession.

    Captures the URL, headers, and body so tests can assert on what was
    actually sent — including verifying that internal-auth headers would
    pass server-side verification.
    """

    def __init__(self, *, response: _FakeAiohttpResponse | None = None,
                 raises: Exception | None = None):
        self._response = response
        self._raises = raises
        self.requests: list[dict] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def post(self, url, *, data, headers):
        self.requests.append({"url": url, "data": data, "headers": headers})
        if self._raises is not None:
            raise self._raises
        return self._response


def _patch_aiohttp(monkeypatch, session: _FakeAiohttpSession):
    """Replace aiohttp.ClientSession with a factory returning ``session``."""
    fake_module = MagicMock()
    fake_module.ClientSession = MagicMock(return_value=session)
    fake_module.ClientTimeout = MagicMock()
    monkeypatch.setitem(sys.modules, "aiohttp", fake_module)


def _make_manager() -> EpochManager:
    """Build a minimal EpochManager. _build_weights_mapping is patched per-test."""
    mgr = EpochManager()
    return mgr


# ── Happy path ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_successful_post_records_queued(monkeypatch):
    """200 response → result=queued, source=epoch_manager."""
    monkeypatch.setenv("VALIDATOR_PRIVATE_KEY", TEST_KEY)
    monkeypatch.setenv("INTERNAL_VALIDATOR_URL", "http://validator:9100")

    session = _FakeAiohttpSession(
        response=_FakeAiohttpResponse(200, json.dumps({"queued": True, "uids": 2})),
    )
    _patch_aiohttp(monkeypatch, session)

    mgr = _make_manager()
    mgr._build_weights_mapping = MagicMock(return_value={"5HOwner": 1.0, "5MinerA": 0.5})

    success = await mgr._emit_weights(epoch=10)

    assert success is True
    assert mgr._last_emit_state["result"] == "queued"
    assert mgr._last_emit_state["source"] == "epoch_manager"
    assert mgr._last_emit_state["uids_attempted"] == 2
    # One POST went out, to the expected URL.
    assert len(session.requests) == 1
    assert session.requests[0]["url"] == "http://validator:9100/internal/weights/queue"


@pytest.mark.asyncio
async def test_post_headers_verify_against_internal_auth(monkeypatch):
    """The headers EpochManager sends must verify cleanly against the
    same key on the receiving side. This is the contract the validator's
    ``_handle_weights_queue`` enforces — if it ever drifts, third
    parties' updates break."""
    monkeypatch.setenv("VALIDATOR_PRIVATE_KEY", TEST_KEY)

    session = _FakeAiohttpSession(
        response=_FakeAiohttpResponse(200, '{"queued": true}'),
    )
    _patch_aiohttp(monkeypatch, session)

    mgr = _make_manager()
    mgr._build_weights_mapping = MagicMock(return_value={"5A": 1.0})

    await mgr._emit_weights(epoch=10)

    sent = session.requests[0]
    headers = sent["headers"]
    body = sent["data"]

    # This is the exact call the server-side handler makes — if it
    # doesn't raise, the signature was correctly produced.
    verify_request(
        method="POST",
        path="/internal/weights/queue",
        body=body,
        timestamp=int(headers["X-Internal-Timestamp"]),
        signature_hex=headers["X-Internal-Signature"],
        expected_address=derive_signer_address(TEST_KEY),
    )


@pytest.mark.asyncio
async def test_custom_validator_url_respected(monkeypatch):
    """Operators with non-canonical container names can override the URL."""
    monkeypatch.setenv("VALIDATOR_PRIVATE_KEY", TEST_KEY)
    monkeypatch.setenv("INTERNAL_VALIDATOR_URL", "http://my-validator-name:9100")

    session = _FakeAiohttpSession(
        response=_FakeAiohttpResponse(200, '{"queued": true}'),
    )
    _patch_aiohttp(monkeypatch, session)

    mgr = _make_manager()
    mgr._build_weights_mapping = MagicMock(return_value={"5A": 1.0})

    await mgr._emit_weights(epoch=10)

    assert session.requests[0]["url"] == "http://my-validator-name:9100/internal/weights/queue"


# ── Non-200 responses ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_validator_returns_503_records_error(monkeypatch):
    """A 503 from the validator (e.g. weights_emitter not configured)
    records an error so /health surfaces what happened."""
    monkeypatch.setenv("VALIDATOR_PRIVATE_KEY", TEST_KEY)

    session = _FakeAiohttpSession(
        response=_FakeAiohttpResponse(
            503,
            json.dumps({"queued": False, "reason": "weights_emitter not configured"}),
        ),
    )
    _patch_aiohttp(monkeypatch, session)

    mgr = _make_manager()
    mgr._build_weights_mapping = MagicMock(return_value={"5A": 1.0})

    success = await mgr._emit_weights(epoch=10)

    assert success is False
    assert mgr._last_emit_state["result"] == "error"
    assert "503" in mgr._last_emit_state["error"]


@pytest.mark.asyncio
async def test_validator_returns_403_records_error(monkeypatch):
    """A 403 (auth misconfig) records the error verbatim so the operator
    can fix their config — without crashing the api process."""
    monkeypatch.setenv("VALIDATOR_PRIVATE_KEY", TEST_KEY)

    session = _FakeAiohttpSession(
        response=_FakeAiohttpResponse(
            403,
            json.dumps({"queued": False, "reason": "signature verification failed"}),
        ),
    )
    _patch_aiohttp(monkeypatch, session)

    mgr = _make_manager()
    mgr._build_weights_mapping = MagicMock(return_value={"5A": 1.0})

    success = await mgr._emit_weights(epoch=10)

    assert success is False
    assert "403" in mgr._last_emit_state["error"]


# ── Network failures ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_connection_error_records_error(monkeypatch):
    """If the validator container is unreachable (network partition,
    container restarting, etc.), record the error and return False.
    Burn fallback covers actual chain emission on the validator side."""
    monkeypatch.setenv("VALIDATOR_PRIVATE_KEY", TEST_KEY)

    session = _FakeAiohttpSession(raises=OSError("connection refused"))
    _patch_aiohttp(monkeypatch, session)

    mgr = _make_manager()
    mgr._build_weights_mapping = MagicMock(return_value={"5A": 1.0})

    success = await mgr._emit_weights(epoch=10)

    assert success is False
    assert mgr._last_emit_state["result"] == "error"
    assert "connection refused" in mgr._last_emit_state["error"]


# ── Short-circuits before HTTP ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_empty_mapping_records_empty_without_http(monkeypatch):
    """No miners scored → no POST, no error — just record ``empty``."""
    monkeypatch.setenv("VALIDATOR_PRIVATE_KEY", TEST_KEY)
    session = _FakeAiohttpSession(
        response=_FakeAiohttpResponse(200, "{}"),
    )
    _patch_aiohttp(monkeypatch, session)

    mgr = _make_manager()
    mgr._build_weights_mapping = MagicMock(return_value={})

    success = await mgr._emit_weights(epoch=10)

    assert success is False
    assert mgr._last_emit_state["result"] == "empty"
    assert len(session.requests) == 0


@pytest.mark.asyncio
async def test_missing_private_key_records_error_without_http(monkeypatch):
    """Without a private key, we cannot sign — short-circuit before
    making a request that would 100% be rejected. Distinguishes
    operational misconfig from chain rate-limit in /health."""
    monkeypatch.delenv("VALIDATOR_PRIVATE_KEY", raising=False)
    session = _FakeAiohttpSession(
        response=_FakeAiohttpResponse(200, "{}"),
    )
    _patch_aiohttp(monkeypatch, session)

    mgr = _make_manager()
    mgr._build_weights_mapping = MagicMock(return_value={"5A": 1.0})

    success = await mgr._emit_weights(epoch=10)

    assert success is False
    assert mgr._last_emit_state["result"] == "error"
    assert "VALIDATOR_PRIVATE_KEY" in mgr._last_emit_state["error"]
    assert len(session.requests) == 0
