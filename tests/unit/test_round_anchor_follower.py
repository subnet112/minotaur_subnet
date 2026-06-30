"""P3: follower parity — resolve-or-derive the round pin + fork the reactive
benchmark at it.

Covers `_resolve_round_fork_pins` (cached / derive+cache / defer) and that the
follower reactive benchmark forks at the round-derived pin when the gate is on,
falling back to the env path when off.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from minotaur_subnet.api import startup
from minotaur_subnet.api.startup import _resolve_round_fork_pins
from minotaur_subnet.harness.round_store import RoundState, RoundStatus


def _store_with(round_state):
    store = MagicMock()
    store.get_round.return_value = round_state
    return store


def _patch_store(store):
    return patch(
        "minotaur_subnet.api.routes.submissions.get_round_store",
        return_value=store,
    )


# ── _resolve_round_fork_pins ──────────────────────────────────────────────────


def test_resolve_none_when_gate_off(monkeypatch):
    monkeypatch.setenv("ROUND_ANCHORED_PIN", "0")  # emergency override -> off
    rs = RoundState(round_id="r1", status=RoundStatus.CLOSED, close_epoch=100,
                    fork_pins={8453: 3000})
    with _patch_store(_store_with(rs)):
        assert _resolve_round_fork_pins("r1") is None


def test_resolve_returns_cached_without_deriving(monkeypatch):
    monkeypatch.setenv("ROUND_ANCHORED_PIN", "1")
    rs = RoundState(round_id="r1", status=RoundStatus.CLOSED, close_epoch=100,
                    fork_pins={8453: 3000})
    with _patch_store(_store_with(rs)), \
         patch.object(startup, "_derive_round_fork_pins") as derive:
        assert _resolve_round_fork_pins("r1") == {8453: 3000}
        derive.assert_not_called()  # cached -> no derivation


def test_resolve_derives_and_caches_when_uncached(monkeypatch):
    monkeypatch.setenv("ROUND_ANCHORED_PIN", "1")
    rs = RoundState(round_id="r1", status=RoundStatus.CLOSED, opened_epoch=95,
                    close_epoch=100, fork_pins=None)
    store = _store_with(rs)
    with _patch_store(store), \
         patch.object(startup, "_derive_round_fork_pins", return_value={8453: 3000}) as derive:
        assert _resolve_round_fork_pins("r1") == {8453: 3000}
        derive.assert_called_once_with(95)             # anchored on OPENED_epoch, not close
        store.set_round_fork_pins.assert_called_once_with("r1", {8453: 3000})  # cached


def test_resolve_derives_during_open_window(monkeypatch):
    """The fork-pin fix: the pin anchors at opened_epoch, so it RESOLVES during the OPEN
    window (close_epoch still None) instead of deferring until close — the defect that made
    every round abort benchmarked=0."""
    monkeypatch.setenv("ROUND_ANCHORED_PIN", "1")
    rs = RoundState(round_id="r1", status=RoundStatus.OPEN, opened_epoch=95,
                    close_epoch=None, fork_pins=None)
    store = _store_with(rs)
    with _patch_store(store), \
         patch.object(startup, "_derive_round_fork_pins", return_value={8453: 2970}) as derive:
        assert _resolve_round_fork_pins("r1") == {8453: 2970}
        derive.assert_called_once_with(95)             # opened_epoch, available at OPEN


def test_resolve_none_when_round_missing(monkeypatch):
    monkeypatch.setenv("ROUND_ANCHORED_PIN", "1")
    with _patch_store(_store_with(None)):
        assert _resolve_round_fork_pins("r1") is None


def test_set_round_fork_pins_idempotent_guard():
    """A non-None pin is FIXED once set: a DIFFERING overwrite is refused (consensus
    safety — the pin is already folded into the signed pack hash), while the same value
    or a clear-to-None is allowed."""
    from minotaur_subnet.harness.round_store import RoundStore
    store = RoundStore()
    r = store.ensure_open_round(opened_epoch=10)
    store.set_round_fork_pins(r.round_id, {8453: 100})
    store.set_round_fork_pins(r.round_id, {8453: 100})          # same -> ok
    assert store.get_round(r.round_id).fork_pins == {8453: 100}
    store.set_round_fork_pins(r.round_id, {8453: 999})          # DIFFERING -> refused
    assert store.get_round(r.round_id).fork_pins == {8453: 100}
    store.set_round_fork_pins(r.round_id, None)                 # clear -> allowed
    assert store.get_round(r.round_id).fork_pins is None


# ── follower reactive benchmark forks at the round pin ────────────────────────

from minotaur_subnet.api.routes.submissions.champion_consensus import (  # noqa: E402
    _reactive_benchmark_candidate,
)
from minotaur_subnet.harness.benchmark_worker import BenchmarkWorker  # noqa: E402


def _intents():
    from minotaur_subnet.harness.test_harness import make_intent, make_snapshot, make_state
    return [(make_intent(), make_state(), make_snapshot())]


async def _run_reactive(captured: dict, round_id="round-pin"):
    async def fake_run_benchmark(session, intents, **kwargs):
        captured.update(kwargs)
        return []

    fake_session = MagicMock()
    fake_session.shutdown = AsyncMock()
    fake_orch = MagicMock()
    fake_orch.start_docker = AsyncMock(return_value=fake_session)
    candidate = MagicMock(submission_id="sub_pin", image_tag="solver-x:screening", image_id="")

    with (
        patch("minotaur_subnet.api.server_context.ctx", MagicMock(store=MagicMock())),
        patch("minotaur_subnet.api.routes.submissions.champion_consensus.get_store",
              return_value=MagicMock()),
        patch("minotaur_subnet.harness.orchestrator.run_benchmark", new=fake_run_benchmark),
        patch("minotaur_subnet.harness.orchestrator.SolverOrchestrator", return_value=fake_orch),
        patch.object(BenchmarkWorker, "_load_benchmark_intents", return_value=_intents()),
        patch.object(BenchmarkWorker, "_build_score_fn", new=AsyncMock(return_value=AsyncMock())),
        patch.object(BenchmarkWorker, "_enrich_intents_with_manifests",
                     side_effect=lambda self, i: i, autospec=True),
        patch.object(BenchmarkWorker, "_load_historical_scenarios", return_value=[]),
    ):
        return await _reactive_benchmark_candidate(
            candidate=candidate, leader_score=0.5, round_id=round_id,
        )


@pytest.mark.asyncio
async def test_follower_forks_at_round_pin_when_gate_on(monkeypatch):
    monkeypatch.setenv("ROUND_ANCHORED_PIN", "1")
    monkeypatch.delenv("BENCHMARK_EPOCH_BLOCK", raising=False)
    captured: dict = {}
    with patch.object(startup, "_resolve_round_fork_pins", return_value={8453: 3000}):
        await _run_reactive(captured)
    assert captured.get("fork_block") == 3000, (
        "follower must re-verify at the round-anchored pin, not live head"
    )


@pytest.mark.asyncio
async def test_follower_falls_back_to_env_when_no_round_pin(monkeypatch):
    # Gate effectively off (resolver returns None) + no env pin -> live head.
    monkeypatch.delenv("BENCHMARK_EPOCH_BLOCK", raising=False)
    captured: dict = {}
    with patch.object(startup, "_resolve_round_fork_pins", return_value=None):
        await _run_reactive(captured)
    assert captured.get("fork_block") is None
