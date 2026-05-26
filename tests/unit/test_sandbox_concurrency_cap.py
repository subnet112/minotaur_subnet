"""Tests for the JS sandbox process-wide concurrency cap (audit H9).

The cap prevents a flood of proposals from spawning unbounded Node.js
subprocesses (~128 MB heap each) and OOMing the host — exactly the
failure mode the 2026-05-26 prod incident hit when Watchtower mass-
recreated 7 containers simultaneously.

What we lock in:
- ``execute_async`` acquires the process-wide semaphore before spawning.
- Acquire blocks past timeout → ``SandboxOverloadedError`` raised.
- The semaphore is *lazy* — built on first await, not at import time —
  so tests with their own event loop don't hit "attached to wrong loop"
  errors.
- Default cap is 4 (overridable via ``JS_SANDBOX_MAX_CONCURRENT``) and
  acquire timeout is 5.0s (``JS_SANDBOX_ACQUIRE_TIMEOUT_SEC``).
- ``proposal_handler.handle_proposal`` returns HTTP 503 with
  ``error=sandbox_overloaded`` when the inner scorer raises
  ``SandboxOverloadedError`` (the leader needs an explicit retryable
  signal, not a 500 stack trace).
"""

from __future__ import annotations

import asyncio
import importlib
import os
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


# Reload the sandbox module under whichever env we set in each test so
# the module-level _SANDBOX_CONCURRENCY constant picks up the new value.
def _reload_sandbox():
    from minotaur_subnet.engine import sandbox as _sandbox_mod
    importlib.reload(_sandbox_mod)
    return _sandbox_mod


@pytest.fixture(autouse=True)
def _reset_sandbox_env(monkeypatch):
    """Strip env between tests so each starts from a known baseline."""
    for name in ("JS_SANDBOX_MAX_CONCURRENT", "JS_SANDBOX_ACQUIRE_TIMEOUT_SEC"):
        monkeypatch.delenv(name, raising=False)
    yield


# ── Sandbox module-level behavior ──────────────────────────────────


def test_default_cap_is_4():
    sandbox = _reload_sandbox()
    assert sandbox._SANDBOX_CONCURRENCY == 4


def test_default_acquire_timeout_is_5s():
    sandbox = _reload_sandbox()
    assert sandbox._SANDBOX_ACQUIRE_TIMEOUT_SEC == 5.0


def test_env_override_cap(monkeypatch):
    monkeypatch.setenv("JS_SANDBOX_MAX_CONCURRENT", "2")
    sandbox = _reload_sandbox()
    assert sandbox._SANDBOX_CONCURRENCY == 2


def test_env_override_acquire_timeout(monkeypatch):
    monkeypatch.setenv("JS_SANDBOX_ACQUIRE_TIMEOUT_SEC", "0.25")
    sandbox = _reload_sandbox()
    assert sandbox._SANDBOX_ACQUIRE_TIMEOUT_SEC == 0.25


def test_sandbox_overloaded_error_is_jssandbox_subclass():
    """503 mapping in proposal_handler relies on the inheritance chain."""
    from minotaur_subnet.engine.sandbox import (
        JsSandboxError,
        SandboxOverloadedError,
    )
    assert issubclass(SandboxOverloadedError, JsSandboxError)


def test_sandbox_overloaded_error_reexported_from_engine():
    """Outside callers import via ``minotaur_subnet.engine`` package.

    Both ``from minotaur_subnet.engine import SandboxOverloadedError``
    and the direct submodule import must resolve to the *same* class,
    so that ``isinstance(exc, SandboxOverloadedError)`` in
    proposal_handler matches what scoring_engine raised.
    """
    # NB: don't reload the module here — reload creates a fresh class
    # that fails identity check against re-exports cached at __init__
    # import time. The other tests that use _reload_sandbox() to flip
    # env vars don't intersect with this check.
    import minotaur_subnet.engine
    import minotaur_subnet.engine.sandbox as _sandbox_mod
    importlib.reload(_sandbox_mod)
    importlib.reload(minotaur_subnet.engine)
    assert (
        minotaur_subnet.engine.SandboxOverloadedError
        is minotaur_subnet.engine.sandbox.SandboxOverloadedError
    )


# ── Semaphore behavior (lazy + process-wide) ────────────────────────


def test_semaphore_is_lazy(monkeypatch):
    sandbox = _reload_sandbox()
    assert sandbox._SANDBOX_SEMAPHORE is None


@pytest.mark.asyncio
async def test_semaphore_constructed_on_first_get():
    sandbox = _reload_sandbox()
    assert sandbox._SANDBOX_SEMAPHORE is None
    sem = await sandbox._get_sandbox_semaphore()
    assert sem is not None
    assert sandbox._SANDBOX_SEMAPHORE is sem


@pytest.mark.asyncio
async def test_semaphore_is_singleton_across_calls():
    sandbox = _reload_sandbox()
    sem1 = await sandbox._get_sandbox_semaphore()
    sem2 = await sandbox._get_sandbox_semaphore()
    assert sem1 is sem2


# ── Saturation → SandboxOverloadedError ─────────────────────────────


@pytest.mark.asyncio
async def test_acquire_raises_overloaded_after_timeout(monkeypatch):
    """When the cap is saturated and acquire times out, surface
    SandboxOverloadedError — NOT a generic TimeoutError or
    UnboundLocalError. This is what proposal_handler keys off of."""
    monkeypatch.setenv("JS_SANDBOX_MAX_CONCURRENT", "1")
    monkeypatch.setenv("JS_SANDBOX_ACQUIRE_TIMEOUT_SEC", "0.05")
    sandbox = _reload_sandbox()

    # Saturate the cap by acquiring the lone slot.
    sem = await sandbox._get_sandbox_semaphore()
    await sem.acquire()
    try:
        sb = sandbox.JsSandbox()
        with pytest.raises(sandbox.SandboxOverloadedError) as excinfo:
            await sb.execute_async("module.exports.f = () => 1;", "f", [])
        # Error message should mention the cap value so operators can
        # diagnose without reading source.
        msg = str(excinfo.value)
        assert "cap=1" in msg
        assert "saturated" in msg.lower()
    finally:
        sem.release()


@pytest.mark.asyncio
async def test_slot_released_on_inner_exception(monkeypatch):
    """If the inner ``_do_execute_async`` raises, the semaphore slot
    must still release — otherwise one bad Node spawn permanently
    poisons the cap. The semaphore.release happens in a finally
    block; this test verifies that."""
    monkeypatch.setenv("JS_SANDBOX_MAX_CONCURRENT", "1")
    monkeypatch.setenv("JS_SANDBOX_ACQUIRE_TIMEOUT_SEC", "0.05")
    sandbox = _reload_sandbox()

    sb = sandbox.JsSandbox()

    # Patch the inner method to always raise.
    async def boom(*args, **kwargs):
        raise sandbox.JsRuntimeError("forced")

    sb._do_execute_async = boom

    # First call raises but releases the slot.
    with pytest.raises(sandbox.JsRuntimeError):
        await sb.execute_async("", "f", [])

    # Second call must be able to acquire — if the slot leaked, this
    # would hit SandboxOverloadedError after the timeout.
    with pytest.raises(sandbox.JsRuntimeError):
        await sb.execute_async("", "f", [])


# ── proposal_handler 503 mapping ─────────────────────────────────────


@pytest.mark.asyncio
async def test_proposal_handler_maps_overload_to_503():
    """The leader needs an explicit retryable signal. Make sure the
    503 + ``error=sandbox_overloaded`` envelope is what we surface."""
    from unittest.mock import AsyncMock, MagicMock
    from aiohttp.test_utils import make_mocked_request

    from minotaur_subnet.engine import SandboxOverloadedError
    from minotaur_subnet.validator.proposal_handler import ProposalHandler

    scoring_engine = MagicMock()
    # verify_proposer_signature must return a (ok, reason) tuple — the
    # handler unpacks it before the scoring call.
    scoring_engine.verify_proposer_signature = MagicMock(return_value=(True, ""))
    scoring_engine.verify_and_score_proposal = AsyncMock(
        side_effect=SandboxOverloadedError("saturated cap=4")
    )
    consensus = MagicMock()
    handler = ProposalHandler(
        scoring_engine=scoring_engine,
        consensus=consensus,
        score_threshold=0.5,
    )

    proposal_body = {
        "order_id": "test",
        "plan": {"executor": "0x" + "0" * 40, "interactions": []},
        "leader_id": "0x" + "0" * 40,
        "leader_signature": "0x" + "0" * 130,
        "simulation": {},
    }

    req = make_mocked_request("POST", "/consensus/proposal")
    req.json = AsyncMock(return_value=proposal_body)

    resp = await handler.handle_proposal(req)

    assert resp.status == 503
    import json as _json
    body = _json.loads(resp.body)
    assert body["error"] == "sandbox_overloaded"
    assert "saturated" in body["reason"].lower()


@pytest.mark.asyncio
async def test_proposal_handler_propagates_other_exceptions():
    """Non-overload exceptions must NOT be silently 503'd."""
    from unittest.mock import AsyncMock, MagicMock
    from aiohttp.test_utils import make_mocked_request

    from minotaur_subnet.validator.proposal_handler import ProposalHandler

    scoring_engine = MagicMock()
    scoring_engine.verify_proposer_signature = MagicMock(return_value=(True, ""))
    scoring_engine.verify_and_score_proposal = AsyncMock(
        side_effect=RuntimeError("not an overload")
    )
    handler = ProposalHandler(
        scoring_engine=scoring_engine,
        consensus=MagicMock(),
        score_threshold=0.5,
    )

    proposal_body = {
        "order_id": "test",
        "plan": {"executor": "0x" + "0" * 40, "interactions": []},
        "leader_id": "0x" + "0" * 40,
        "leader_signature": "0x" + "0" * 130,
        "simulation": {},
    }

    req = make_mocked_request("POST", "/consensus/proposal")
    req.json = AsyncMock(return_value=proposal_body)

    with pytest.raises(RuntimeError, match="not an overload"):
        await handler.handle_proposal(req)
