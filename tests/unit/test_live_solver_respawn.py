"""Tests for ``DockerRuntimeSolver`` auto-respawn after session crash.

Pre-fix, ``orchestrator._send`` kills the entire long-lived live solver
on any per-command timeout (necessary to preserve stdio protocol sync —
a half-read response would desynchronize subsequent commands). The api
then permanently 500'd every quote/order until the operator restarted
the api process.

This file pins the post-fix contract:

  - ``is_alive()`` reflects whether the inner session is currently usable
    (and goes False the moment ``_session._closed`` flips).
  - ``respawn_state()`` exposes the diagnostic snapshot /health reads.
  - calling ``quote`` / ``generate_plan`` / ``supported_tokens`` while
    the inner session is dead transparently rebuilds the session and
    retries the request once.
  - the single-retry policy is hard: if the second attempt also fails,
    the caller sees the error (no infinite loop).
  - explicit ``shutdown()`` still works and leaves ``is_alive()`` False.

These are unit-level tests — we stub out the orchestrator's
``start_docker`` so no Docker daemon is needed. The contract is on
DockerRuntimeSolver, not on the Docker subsystem.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from minotaur_subnet.harness.orchestrator import (
    SolverCrashedError,
    SolverTimeoutError,
)
from minotaur_subnet.harness.runtime_solver import DockerRuntimeSolver
from minotaur_subnet.sdk.intent_solver import MarketSnapshot, SolverMetadata


def _make_session(*, closed: bool = False) -> MagicMock:
    """Build a minimal SolverSession stub with the attrs DockerRuntimeSolver reads."""
    session = MagicMock()
    session._closed = closed
    session.quote = AsyncMock()
    session.generate_plan = AsyncMock()
    session.supported_tokens = AsyncMock()
    session.shutdown = AsyncMock()
    return session


def _make_runtime(*, session: MagicMock | None = None) -> DockerRuntimeSolver:
    """Build a DockerRuntimeSolver with a stubbed inner session."""
    session = session or _make_session()
    meta = SolverMetadata(
        name="test-champion",
        version="0.0.1",
        author="test",
        description="",
    )
    return DockerRuntimeSolver(
        session=session,
        image_ref="ghcr.io/test/solver:latest",
        metadata=meta,
        chain_ids=[8453],
        rpc_urls={8453: "http://anvil:8546"},
    )


def _make_intent_state_snapshot():
    """Return the (intent, state, snapshot) triple quote/generate_plan need."""
    intent = MagicMock()
    intent.app_id = "app_test"
    state = MagicMock()
    state.chain_id = 8453
    snapshot = MarketSnapshot.empty(chain_id=8453)
    return intent, state, snapshot


# ── is_alive / respawn_state diagnostic surface ─────────────────────────


def test_is_alive_true_when_session_open():
    """Fresh runtime with an open inner session reports alive."""
    rt = _make_runtime(session=_make_session(closed=False))
    assert rt.is_alive() is True


def test_is_alive_false_when_session_closed():
    """The inner session being closed propagates to is_alive — exactly
    the signal /health needs to display ``live_solver_running=false``."""
    rt = _make_runtime(session=_make_session(closed=True))
    assert rt.is_alive() is False


def test_is_alive_false_after_explicit_shutdown():
    """Explicit shutdown() must leave is_alive False so an intentionally-
    closed runtime doesn't auto-respawn forever."""
    import asyncio
    session = _make_session()
    rt = _make_runtime(session=session)
    asyncio.run(rt.shutdown())
    assert rt.is_alive() is False


def test_respawn_state_initial():
    """A brand-new runtime has zero respawns recorded."""
    rt = _make_runtime()
    snapshot = rt.respawn_state()
    assert snapshot["respawn_count"] == 0
    assert snapshot["last_respawn_at"] is None
    assert snapshot["last_crash_error"] is None


# ── Crash → respawn → retry ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_quote_respawns_on_timeout_and_retries():
    """A timed-out quote (inner session marked _closed by the orchestrator's
    kill-on-timeout path) must trigger respawn + retry on the next call.

    Pre-fix this would surface as a permanent 500 on /v1/apps/*/quote.
    """
    # First quote attempt: session is already closed (simulating the
    # orchestrator having killed it after a previous timeout).
    crashed_session = _make_session(closed=True)
    # We won't reach crashed_session.quote because _ensure_session_alive
    # respawns first; instead, _respawn_session installs a NEW session
    # that we control via the patched factory below.
    healthy_session = _make_session(closed=False)
    healthy_session.quote = AsyncMock(return_value="quote-result")

    rt = _make_runtime(session=crashed_session)

    async def _fake_respawn(self):
        # Mimic the real respawn path: swap in the healthy session and
        # bump the counters.
        self._session = healthy_session
        self._respawn_count += 1

    with patch.object(DockerRuntimeSolver, "_respawn_session", _fake_respawn):
        intent, state, snapshot = _make_intent_state_snapshot()
        result = await rt.quote(intent, state, snapshot)

    assert result == "quote-result"
    assert rt._respawn_count == 1
    healthy_session.quote.assert_awaited_once()


@pytest.mark.asyncio
async def test_quote_respawns_and_retries_when_in_call_timeout():
    """The other crash flavour: the session was alive at request time,
    but the inner ``session.quote`` raised SolverTimeoutError mid-call
    (orchestrator killed the process to preserve protocol sync). The
    runtime must respawn and retry the request once."""
    # First call raises, second call (after respawn) succeeds.
    session = _make_session(closed=False)
    session.quote = AsyncMock(
        side_effect=[
            SolverTimeoutError("Command quote timed out after 5.0s"),
            "quote-result-on-retry",
        ],
    )

    rt = _make_runtime(session=session)

    async def _fake_respawn(self):
        # Don't replace the session — just bump the counter. The same
        # session.quote AsyncMock returns the success value on its 2nd call.
        self._respawn_count += 1

    with patch.object(DockerRuntimeSolver, "_respawn_session", _fake_respawn):
        intent, state, snapshot = _make_intent_state_snapshot()
        result = await rt.quote(intent, state, snapshot)

    assert result == "quote-result-on-retry"
    assert rt._respawn_count == 1
    assert session.quote.await_count == 2


@pytest.mark.asyncio
async def test_generate_plan_respawns_on_crash():
    """generate_plan uses the same _call_with_respawn helper as quote."""
    session = _make_session(closed=False)
    session.generate_plan = AsyncMock(
        side_effect=[
            SolverCrashedError("Solver process exited during generate_plan"),
            "plan-result",
        ],
    )
    rt = _make_runtime(session=session)

    async def _fake_respawn(self):
        self._respawn_count += 1

    with patch.object(DockerRuntimeSolver, "_respawn_session", _fake_respawn):
        intent, state, snapshot = _make_intent_state_snapshot()
        result = await rt.generate_plan(intent, state, snapshot)

    assert result == "plan-result"
    assert rt._respawn_count == 1


@pytest.mark.asyncio
async def test_supported_tokens_respawns_on_timeout():
    """supported_tokens uses _call_with_respawn even though it has an
    extra TTL-cache wrapper above the retry logic."""
    session = _make_session(closed=False)
    session.supported_tokens = AsyncMock(
        side_effect=[
            SolverTimeoutError("Command supported_tokens timed out"),
            [{"address": "0x123", "symbol": "TEST"}],
        ],
    )
    rt = _make_runtime(session=session)

    async def _fake_respawn(self):
        self._respawn_count += 1

    with patch.object(DockerRuntimeSolver, "_respawn_session", _fake_respawn):
        tokens = await rt.supported_tokens(8453)

    assert tokens == [{"address": "0x123", "symbol": "TEST"}]
    assert rt._respawn_count == 1


# ── Retry budget — must not loop on persistent failure ──────────────────


@pytest.mark.asyncio
async def test_only_one_retry_on_persistent_failure():
    """If the second attempt also raises, surface the error to the caller
    rather than entering an infinite respawn loop. A persistent failure
    means something is genuinely wrong (RPC dead, solver bug, etc.) and
    the operator needs to see the error, not have it masked by retries."""
    session = _make_session(closed=False)
    persistent_err = SolverTimeoutError("Command quote timed out after 5.0s")
    session.quote = AsyncMock(side_effect=persistent_err)
    rt = _make_runtime(session=session)

    async def _fake_respawn(self):
        self._respawn_count += 1

    with patch.object(DockerRuntimeSolver, "_respawn_session", _fake_respawn):
        intent, state, snapshot = _make_intent_state_snapshot()
        with pytest.raises(SolverTimeoutError):
            await rt.quote(intent, state, snapshot)

    # Exactly two calls: original + one retry. No third attempt.
    assert session.quote.await_count == 2
    assert rt._respawn_count == 1


@pytest.mark.asyncio
async def test_respawn_failure_propagates():
    """If the respawn itself fails (Docker daemon down, image gone, etc.),
    the caller MUST see the underlying error rather than the workflow
    silently masking it. Record the crash error so /health surfaces it."""
    session = _make_session(closed=True)
    rt = _make_runtime(session=session)

    async def _fake_respawn_fails(self):
        raise RuntimeError("docker daemon unreachable")

    with patch.object(DockerRuntimeSolver, "_respawn_session", _fake_respawn_fails):
        intent, state, snapshot = _make_intent_state_snapshot()
        with pytest.raises(RuntimeError, match="docker daemon unreachable"):
            await rt.quote(intent, state, snapshot)

    # The crash error gets recorded for /health diagnostics — operators
    # see "live_solver_running=false, last_crash_error=respawn failed:..."
    # rather than a silent failure.
    state_dict = rt.respawn_state()
    assert state_dict["last_crash_error"] is not None
    assert "respawn failed" in state_dict["last_crash_error"]


# ── Closed-runtime guard — explicit shutdown wins over respawn ──────────


@pytest.mark.asyncio
async def test_quote_after_shutdown_raises_no_respawn():
    """After explicit shutdown(), the runtime must NOT respawn — it's
    been intentionally torn down by upstream lifecycle (champion swap,
    api shutdown, etc.)."""
    rt = _make_runtime()
    await rt.shutdown()

    async def _fake_respawn_should_not_fire(self):
        pytest.fail("respawn must not run after explicit shutdown")

    with patch.object(DockerRuntimeSolver, "_respawn_session", _fake_respawn_should_not_fire):
        intent, state, snapshot = _make_intent_state_snapshot()
        with pytest.raises(RuntimeError, match="Champion runtime solver is closed"):
            await rt.quote(intent, state, snapshot)
