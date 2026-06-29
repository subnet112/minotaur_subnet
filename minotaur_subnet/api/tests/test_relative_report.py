"""Tests for the AUTHORITATIVE relative-count API surface.

The relative rule is the sole adoption path, so the relative block + ``scoring_mode``
are ALWAYS emitted (no flag). These tests pin: the report / round response / app
status always carry ``scoring_mode == "relative"`` and the relative count block when
both sides have shadow_score rows; the count block is gracefully omitted (no error)
when either side lacks shadow_score rows.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# Disable background workers before importing the app (mirrors test_routes).
os.environ["DISABLE_BENCHMARK_WORKER"] = "1"
os.environ["DISABLE_BLOCK_LOOP"] = "1"

from fastapi.testclient import TestClient  # noqa: E402

from minotaur_subnet.api.routes.submissions import round_manager  # noqa: E402
from minotaur_subnet.api.routes.submissions.models import SolverRoundResponse  # noqa: E402
from minotaur_subnet.api.routes.submissions.report import build_submission_report  # noqa: E402
from minotaur_subnet.harness.round_store import RoundState, RoundStatus  # noqa: E402


# ── fixtures / helpers ───────────────────────────────────────────────────────

# shadow_score is an EXACT INTEGER DECIMAL STRING, not a float.
_CHAMP_INTENT = [
    {"intent_id": "o1", "score": 0.90, "shadow_score": "100"},
    {"intent_id": "o2", "score": 0.80, "shadow_score": "200"},
]
_CHAL_INTENT = [
    {"intent_id": "o1", "score": 0.95, "shadow_score": "120"},
    {"intent_id": "o2", "score": 0.85, "shadow_score": "250"},
]


def _sub(per_intent, *, sid="sub-1", score=0.9, status="scored"):
    return SimpleNamespace(
        submission_id=sid,
        status=status,
        benchmark_score=score,
        benchmark_details={"per_intent": per_intent, "scorecard": {}},
        screening={},
    )


def _build_report(sub, champ_details):
    return build_submission_report(
        sub,
        champion_score=0.9,
        threshold=0.5,
        dethrone_margin=0.01,
        reason=None,
        champion_details=champ_details,
    )


# ── report.py: per-submission relative block (always on) ─────────────────────


def test_report_always_emits_relative_counts():
    rpt = _build_report(_sub(_CHAL_INTENT), {"per_intent": _CHAMP_INTENT})
    assert rpt["scoring_mode"] == "relative"
    rel = rpt["relative"]
    assert rel["better"] == 2
    assert rel["worse"] == 0
    assert rel["verdict"] == "dethrone"
    assert rpt["reason_relative"].startswith("adopted")
    # Legacy fields still present (additive, cleanup deferred).
    assert rpt["aggregate"]["your_score"] == 0.9
    # The observe-only shadow block predates this PR and is still present.
    assert "shadow_relative" in rpt


def test_report_no_shadow_rows_omits_block():
    # Champion benched before the cutover: rows but no shadow_score.
    champ = {"per_intent": [{"intent_id": "o1", "score": 0.9}]}
    rpt = _build_report(_sub(_CHAL_INTENT), champ)
    assert rpt["scoring_mode"] == "relative"  # mode always set
    assert "relative" not in rpt             # but no counts (graceful omit)


# ── round response: finalist relative block (always on) ──────────────────────


def _wire_round_stores(monkeypatch, finalist_intent, champ_intent):
    finalist = _sub(finalist_intent, sid="fin-1")
    champ = _sub(champ_intent, sid="champ-1")
    fake_store = SimpleNamespace(
        get=lambda sid: {"fin-1": finalist, "champ-1": champ}.get(sid)
    )
    fake_round_store = SimpleNamespace(
        get_active_champion=lambda: SimpleNamespace(submission_id="champ-1")
    )
    monkeypatch.setattr(round_manager, "get_store", lambda: fake_store)
    monkeypatch.setattr(round_manager, "get_round_store", lambda: fake_round_store)


def _round_state():
    return RoundState(
        round_id="round-e1-n1",
        status=RoundStatus.CERTIFYING,
        opened_epoch=1,
        finalist_submission_id="fin-1",
        finalist_score=0.95,
    )


def test_round_response_attaches_finalist_relative(monkeypatch):
    _wire_round_stores(monkeypatch, _CHAL_INTENT, _CHAMP_INTENT)
    resp = round_manager._round_state_to_response(_round_state())
    dumped = resp.model_dump()
    assert dumped["scoring_mode"] == "relative"
    assert dumped["finalist_relative"]["better"] == 2
    assert dumped["finalist_relative"]["verdict"] == "dethrone"
    assert dumped["reason_relative"].startswith("adopted fin-1")
    # Legacy finalist_score still present (not replaced).
    assert dumped["finalist_score"] == 0.95
    # The ONLY keys beyond the declared model fields are the relative extras.
    extras = set(dumped) - set(SolverRoundResponse.model_fields)
    assert extras == {"scoring_mode", "finalist_relative", "reason_relative"}


def test_round_response_no_shadow_rows_omits_block(monkeypatch):
    _wire_round_stores(
        monkeypatch,
        [{"intent_id": "o1", "score": 0.9}],   # finalist: no shadow rows
        _CHAMP_INTENT,
    )
    resp = round_manager._round_state_to_response(_round_state())
    dumped = resp.model_dump()
    assert dumped["scoring_mode"] == "relative"
    assert "finalist_relative" not in dumped


def test_round_response_no_finalist_marks_mode_only():
    state = RoundState(round_id="round-e1-n1", status=RoundStatus.OPEN, opened_epoch=1)
    resp = round_manager._round_state_to_response(state)
    dumped = resp.model_dump()
    assert dumped["scoring_mode"] == "relative"
    assert "finalist_relative" not in dumped


# ── get_app_status: scoring_mode marker (always on) ──────────────────────────


def _seed_app() -> str:
    """Insert an app straight into the route's store, bypassing the
    validation-heavy create path (which needs the JS sandbox)."""
    import uuid

    from minotaur_subnet.api.server import store as server_store
    from minotaur_subnet.shared.types import AppIntentDefinition

    app_id = f"rel-status-{uuid.uuid4().hex[:10]}"
    server_store.save_app(AppIntentDefinition(
        app_id=app_id, name="Relative Status Test", version="1.0.0", intent_type="swap",
        js_code="module.exports = { score: () => ({score: 0.5, valid: true}) }",
        solidity_code="// SPDX-License-Identifier: MIT\ncontract T {}",
    ))
    return app_id


def test_app_status_marks_scoring_mode():
    from minotaur_subnet.api.server import app
    client = TestClient(app, raise_server_exceptions=False)
    app_id = _seed_app()
    data = client.get(f"/v1/apps/{app_id}/status").json()
    assert data["scoring_mode"] == "relative"
    assert "champion_score" in data  # legacy field still present
