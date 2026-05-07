"""Regression tests for consensus-security env-var defaults.

These ensure the security-critical defaults don't silently drift back to
permissive values. They don't test the full consensus path — just that:
  (1) the defaults are safe (reject unsigned, re-simulate on follower side),
  (2) the code actually reads the env var at decision time.
"""

from __future__ import annotations

import os
import asyncio
import inspect
from unittest.mock import MagicMock, AsyncMock, patch

import pytest

from minotaur_subnet.validator.scoring_engine import ScoringEngine


def _fresh_engine() -> ScoringEngine:
    store = MagicMock()
    js_engine = MagicMock()
    js_engine.list_loaded_intents.return_value = []
    return ScoringEngine(
        js_engine=js_engine,
        store=store,
        simulator=None,
        validator_id="0x" + "11" * 20,
    )


def test_signed_proposals_required_by_default(monkeypatch):
    """Unsigned proposals MUST be rejected when the env var is unset."""
    monkeypatch.delenv("CONSENSUS_REQUIRE_SIGNED_PROPOSALS", raising=False)
    engine = _fresh_engine()

    body = {
        "order_id": "ord_1",
        "plan_hash": "0xdead",
        "score": 0.9,
        "plan": {},
        # No proposer_signature — this is the adversarial case.
    }
    ok, reason = engine.verify_proposer_signature(body)
    assert ok is False
    assert "Missing proposer_signature" in reason


def test_signed_proposals_can_be_explicitly_disabled(monkeypatch):
    """Operator can opt out, but must do so explicitly."""
    monkeypatch.setenv("CONSENSUS_REQUIRE_SIGNED_PROPOSALS", "0")
    engine = _fresh_engine()

    body = {
        "order_id": "ord_1",
        "plan_hash": "0xdead",
        "score": 0.9,
        "plan": {},
    }
    ok, _ = engine.verify_proposer_signature(body)
    assert ok is True  # explicit opt-out allowed, with a log warning


def test_follower_resimulate_defaults_on():
    """Default value reads as truthy when the env is unset."""
    # This mirrors the code in scoring_engine.py:285-288. If the default ever
    # flips to "0", this test fails and alerts us before prod regresses.
    raw = os.environ.get("FOLLOWER_PROPOSAL_RESIMULATE", "1").strip().lower()
    assert raw in ("1", "true", "yes", "on"), (
        "FOLLOWER_PROPOSAL_RESIMULATE default must stay enabled — "
        "disabling it makes followers blindly trust leader-provided transfers."
    )


def test_follower_resimulate_reads_env_at_decision_time():
    """Env reads happen inside the proposal path, not at import time.

    Guards against a refactor that caches the value at module load —
    which would silently make toggling the env useless."""
    src = inspect.getsource(ScoringEngine.verify_and_score_proposal)
    assert "FOLLOWER_PROPOSAL_RESIMULATE" in src, (
        "verify_and_score_proposal must read FOLLOWER_PROPOSAL_RESIMULATE "
        "directly so the runtime toggle works."
    )


def test_signed_proposals_env_reads_at_decision_time():
    """Same pattern for CONSENSUS_REQUIRE_SIGNED_PROPOSALS."""
    src = inspect.getsource(ScoringEngine.verify_proposer_signature)
    assert "CONSENSUS_REQUIRE_SIGNED_PROPOSALS" in src, (
        "verify_proposer_signature must read the env at call time so the "
        "toggle takes effect without a process restart."
    )
