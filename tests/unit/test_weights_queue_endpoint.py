"""Tests for the validator daemon's /internal/weights/queue endpoint.

The endpoint is the single integration point between the api process's
EpochManager and the chain set_weights call. It must:

  - reject any request without valid internal-auth headers (so a
    container that can reach the validator on the internal network
    can't drive emissions without holding VALIDATOR_PRIVATE_KEY);
  - store the mapping in a single in-process slot (newest-wins);
  - return 503 cleanly when the daemon can't emit (no wallet, no
    auth configured) so the caller doesn't busy-retry;
  - validate the body shape before accepting.

These are unit-level tests against ``AppIntentsValidator._handle_weights_queue``
with a mocked aiohttp request. The integration with ``_epoch_loop``'s
consumption is exercised by ``test_burn_fallback_defer.py``.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from minotaur_subnet.shared.internal_auth import (
    derive_signer_address,
    sign_request,
)
from minotaur_subnet.validator.main import AppIntentsValidator


# Anvil's well-known account #0 — public test fixture, not a real key.
TEST_KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
OTHER_KEY = "0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d"


def _make_request(*, method="POST", path="/internal/weights/queue", body=b"", headers=None):
    """Build a minimal aiohttp.web.Request stand-in.

    We deliberately don't import aiohttp.test_utils — the real Request
    factories require an Application context. The handler only touches
    method/path/headers/body, so a Mock with those is sufficient.
    """
    req = MagicMock()
    req.method = method
    req.path = path
    req.headers = headers or {}
    req.read = AsyncMock(return_value=body)
    return req


def _make_validator_stub(
    *,
    has_emitter: bool = True,
    has_validator_id: bool = True,
) -> MagicMock:
    """Stub the validator instance with only the fields the handler reads.

    We avoid invoking ``AppIntentsValidator.__init__`` (which needs an
    OrderBook, AnvilSimulator, etc.) by using a MagicMock with the
    handful of attributes the handler touches.
    """
    self_stub = MagicMock(spec=AppIntentsValidator)
    self_stub._weights_emitter = MagicMock() if has_emitter else None
    self_stub._validator_id = derive_signer_address(TEST_KEY) if has_validator_id else ""
    self_stub._queued_weights_mapping = None
    self_stub._queued_weights_source = None
    return self_stub


def _make_signed_request(
    *,
    key: str = TEST_KEY,
    body_dict: dict | None = None,
    method: str = "POST",
    path: str = "/internal/weights/queue",
    timestamp: int | None = None,
):
    """Build a signed request whose headers verify against ``TEST_KEY``."""
    if body_dict is None:
        body_dict = {
            "mapping": {"5HOwnerHotkey": 1.0},
            "source": "epoch_manager",
            "epoch": 10,
        }
    body = json.dumps(body_dict).encode()
    ts, sig = sign_request(
        key,
        method=method,
        path=path,
        body=body,
        timestamp=timestamp,
    )
    headers = {
        "X-Internal-Timestamp": str(ts),
        "X-Internal-Signature": sig,
    }
    return _make_request(method=method, path=path, body=body, headers=headers)


# ── Happy path ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_signed_request_queues_mapping():
    """A correctly-signed POST stores the mapping in the slot."""
    self_stub = _make_validator_stub()
    req = _make_signed_request(
        body_dict={
            "mapping": {"5HOwnerHotkey": 1.0, "5MinerA": 0.5},
            "source": "epoch_manager",
            "epoch": 12,
        }
    )

    resp = await AppIntentsValidator._handle_weights_queue(self_stub, req)

    assert resp.status == 200
    body = json.loads(resp.body)
    assert body["queued"] is True
    assert body["uids"] == 2
    assert self_stub._queued_weights_mapping == {"5HOwnerHotkey": 1.0, "5MinerA": 0.5}
    assert self_stub._queued_weights_source == "epoch_manager"


@pytest.mark.asyncio
async def test_second_post_overwrites_first():
    """Single-slot newest-wins. Documented behavior — not a race."""
    self_stub = _make_validator_stub()

    req1 = _make_signed_request(body_dict={"mapping": {"5A": 1.0}, "source": "first"})
    await AppIntentsValidator._handle_weights_queue(self_stub, req1)
    assert self_stub._queued_weights_mapping == {"5A": 1.0}

    req2 = _make_signed_request(body_dict={"mapping": {"5B": 1.0}, "source": "second"})
    await AppIntentsValidator._handle_weights_queue(self_stub, req2)
    assert self_stub._queued_weights_mapping == {"5B": 1.0}
    assert self_stub._queued_weights_source == "second"


# ── Auth failures ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_missing_headers_returns_403():
    """No X-Internal-* headers → 403, slot untouched."""
    self_stub = _make_validator_stub()
    req = _make_request(body=b'{"mapping": {"5A": 1.0}}')

    resp = await AppIntentsValidator._handle_weights_queue(self_stub, req)

    assert resp.status == 403
    assert self_stub._queued_weights_mapping is None


@pytest.mark.asyncio
async def test_wrong_key_returns_403():
    """A sig from a different key (one that doesn't match
    self._validator_id) must be rejected."""
    self_stub = _make_validator_stub()
    # Sign with OTHER_KEY but self_stub's _validator_id was derived
    # from TEST_KEY → mismatch.
    req = _make_signed_request(key=OTHER_KEY)

    resp = await AppIntentsValidator._handle_weights_queue(self_stub, req)

    assert resp.status == 403
    assert self_stub._queued_weights_mapping is None


@pytest.mark.asyncio
async def test_tampered_body_returns_403():
    """If the body bytes change after signing, sig recovery yields a
    different address → 403."""
    self_stub = _make_validator_stub()
    # Sign over body_a, then swap in body_b.
    body_a = json.dumps({"mapping": {"5A": 1.0}}).encode()
    ts, sig = sign_request(
        TEST_KEY, method="POST", path="/internal/weights/queue", body=body_a,
    )
    body_b = json.dumps({"mapping": {"5Attacker": 1.0}}).encode()
    req = _make_request(
        body=body_b,
        headers={
            "X-Internal-Timestamp": str(ts),
            "X-Internal-Signature": sig,
        },
    )

    resp = await AppIntentsValidator._handle_weights_queue(self_stub, req)

    assert resp.status == 403
    assert self_stub._queued_weights_mapping is None


@pytest.mark.asyncio
async def test_stale_timestamp_returns_403():
    """Timestamp older than MAX_REQUEST_AGE_SECONDS → 403."""
    self_stub = _make_validator_stub()
    old_ts = int(time.time()) - 600  # 10 min ago, well past 30s window
    req = _make_signed_request(timestamp=old_ts)

    resp = await AppIntentsValidator._handle_weights_queue(self_stub, req)

    assert resp.status == 403
    assert self_stub._queued_weights_mapping is None


@pytest.mark.asyncio
async def test_malformed_timestamp_returns_403():
    """Non-integer X-Internal-Timestamp → 403 (not 500)."""
    self_stub = _make_validator_stub()
    req = _make_request(
        body=b'{"mapping": {"5A": 1.0}}',
        headers={
            "X-Internal-Timestamp": "not-a-number",
            "X-Internal-Signature": "0xdeadbeef",
        },
    )

    resp = await AppIntentsValidator._handle_weights_queue(self_stub, req)

    assert resp.status == 403


# ── Operational state ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_no_emitter_returns_503():
    """If the daemon has no weights_emitter (eg wallet failed to load),
    return 503 so callers can stop retrying. 503 NOT 403 so operators
    can tell auth from operational failure."""
    self_stub = _make_validator_stub(has_emitter=False)
    req = _make_signed_request()

    resp = await AppIntentsValidator._handle_weights_queue(self_stub, req)

    assert resp.status == 503
    body = json.loads(resp.body)
    assert "weights_emitter not configured" in body["reason"]


@pytest.mark.asyncio
async def test_no_validator_id_returns_503():
    """If self._validator_id was never set (validator_private_key empty),
    return 503 — auth is impossible without a known signer."""
    self_stub = _make_validator_stub(has_validator_id=False)
    req = _make_signed_request()

    resp = await AppIntentsValidator._handle_weights_queue(self_stub, req)

    assert resp.status == 503
    body = json.loads(resp.body)
    assert "internal auth not configured" in body["reason"]


# ── Body validation ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_empty_mapping_returns_400():
    """An empty mapping is non-sensical — reject before queueing.
    Otherwise the slot gets stuck with {} and the burn path can't run."""
    self_stub = _make_validator_stub()
    req = _make_signed_request(body_dict={"mapping": {}})

    resp = await AppIntentsValidator._handle_weights_queue(self_stub, req)

    assert resp.status == 400
    assert self_stub._queued_weights_mapping is None


@pytest.mark.asyncio
async def test_mapping_missing_returns_400():
    """No ``mapping`` key in the body → 400."""
    self_stub = _make_validator_stub()
    req = _make_signed_request(body_dict={"source": "nothing-to-emit"})

    resp = await AppIntentsValidator._handle_weights_queue(self_stub, req)

    assert resp.status == 400


@pytest.mark.asyncio
async def test_non_numeric_values_returns_400():
    """Mapping values must be coercible to float — reject typos."""
    self_stub = _make_validator_stub()
    # Build a request whose body has a string value (won't sign-then-tamper —
    # body must match sig). Sign over the bad-shape body.
    body = json.dumps({"mapping": {"5A": "not-a-float"}}).encode()
    ts, sig = sign_request(
        TEST_KEY, method="POST", path="/internal/weights/queue", body=body,
    )
    req = _make_request(
        body=body,
        headers={
            "X-Internal-Timestamp": str(ts),
            "X-Internal-Signature": sig,
        },
    )

    resp = await AppIntentsValidator._handle_weights_queue(self_stub, req)

    assert resp.status == 400


@pytest.mark.asyncio
async def test_malformed_json_returns_400():
    """Body that isn't valid JSON → 400."""
    self_stub = _make_validator_stub()
    body = b"not json at all"
    ts, sig = sign_request(
        TEST_KEY, method="POST", path="/internal/weights/queue", body=body,
    )
    req = _make_request(
        body=body,
        headers={
            "X-Internal-Timestamp": str(ts),
            "X-Internal-Signature": sig,
        },
    )

    resp = await AppIntentsValidator._handle_weights_queue(self_stub, req)

    assert resp.status == 400
