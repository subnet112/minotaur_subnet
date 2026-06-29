"""Gating tests for the additive RELATIVE-COUNT API surface.

The relative report block self-activates on the SAME flag as the live scoring
(``RELATIVE_SCORING_ENABLED`` -> ``relative_scoring_active()``). These tests pin
the safety invariant: with the flag OFF (default) every response is byte-for-byte
unchanged (no ``scoring_mode`` / ``relative`` / ``finalist_relative`` keys); with
it ON the relative block + ``scoring_mode`` appear. A submission with no
shadow_score rows gets the mode marker but no relative block (graceful omit).
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

# shadow_score is an EXACT INTEGER DECIMAL STRING (#395), not a float.
_CHAMP_INTENT = [
    {"intent_id": "o1", "score": 0.90, "shadow_score": "100"},
    {"intent_id": "o2", "score": 0.80, "shadow_score": "200"},
]
_CHAL_INTENT = [
    {"intent_id": "o1", "score": 0.95, "shadow_score": "120"},
    {"intent_id": "o2", "score": 0.85, "shadow_score": "250"},
]


# A STORED same-pin relative block, as persisted onto a competitor's
# benchmark_details["relative"] by EpochManager._persist_round_relative_counts at
# round evaluation (champion@A vs competitor@A — same fork-pin).
_STORED_DETHRONE = {
    "better": 2, "worse": 0, "matched": 0, "new": 0, "compared": 2,
    "verdict": "dethrone", "per_order": [], "round_id": "round-e1-n1",
}
# A stored block that disagrees with what a cross-fork recompute of the fixtures
# below WOULD produce (those fixtures recompute to better=2/dethrone). Used to
# prove the read side surfaces the STORED block verbatim, never a recompute.
_STORED_MATCHED = {
    "better": 0, "worse": 0, "matched": 2, "new": 0, "compared": 2,
    "verdict": "matched", "per_order": [], "round_id": "round-e1-n1",
}


def _sub(per_intent, *, sid="sub-1", score=0.9, status="scored", relative=None):
    details = {"per_intent": per_intent, "scorecard": {}}
    if relative is not None:
        details["relative"] = relative
    return SimpleNamespace(
        submission_id=sid,
        status=status,
        benchmark_score=score,
        benchmark_details=details,
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


# ── report.py: per-submission relative block ─────────────────────────────────


def test_report_off_is_unchanged(monkeypatch):
    monkeypatch.delenv("RELATIVE_SCORING_ENABLED", raising=False)
    rpt = _build_report(_sub(_CHAL_INTENT), {"per_intent": _CHAMP_INTENT})
    assert rpt is not None
    assert "scoring_mode" not in rpt
    assert "relative" not in rpt
    assert "reason_relative" not in rpt
    # Legacy fields intact.
    assert rpt["aggregate"]["your_score"] == 0.9
    assert "per_case" in rpt
    # The pre-existing observe-only shadow block is still present (it predates
    # this PR and is flag-independent).
    assert "shadow_relative" in rpt


def test_report_on_reads_stored_same_pin_counts(monkeypatch):
    monkeypatch.setenv("RELATIVE_SCORING_ENABLED", "1")
    rpt = _build_report(
        _sub(_CHAL_INTENT, relative=_STORED_DETHRONE), {"per_intent": _CHAMP_INTENT},
    )
    assert rpt["scoring_mode"] == "relative"
    # Surfaced verbatim from the submission's OWN stored same-pin block.
    assert rpt["relative"] is _STORED_DETHRONE
    assert rpt["relative"]["better"] == 2
    assert rpt["relative"]["verdict"] == "dethrone"
    assert rpt["reason_relative"].startswith("adopted")
    # Legacy fields still present (additive, cleanup deferred).
    assert rpt["aggregate"]["your_score"] == 0.9


def test_report_relative_is_stored_not_cross_fork_recompute(monkeypatch):
    """The relative block is READ from the stored same-pin counts, NEVER recomputed
    cross-fork against the champion's latest details. Proof: the stored block says
    'matched' while a cross-fork recompute of these fixtures (chal 120/250 vs champ
    100/200) would say better=2/dethrone — the report MUST show the stored block."""
    monkeypatch.setenv("RELATIVE_SCORING_ENABLED", "1")
    rpt = _build_report(
        _sub(_CHAL_INTENT, relative=_STORED_MATCHED), {"per_intent": _CHAMP_INTENT},
    )
    assert rpt["relative"] == _STORED_MATCHED
    assert rpt["relative"]["verdict"] == "matched"
    assert rpt["relative"]["better"] == 0  # NOT the 2 a cross-fork recompute gives


def test_report_on_no_stored_relative_omits_block(monkeypatch):
    """No STORED relative block → the block is omitted (pending). Even though the
    submission AND the passed champion_details both carry shadow_score rows (a
    cross-fork recompute WOULD produce a block), there is no fallback."""
    monkeypatch.setenv("RELATIVE_SCORING_ENABLED", "1")
    rpt = _build_report(_sub(_CHAL_INTENT), {"per_intent": _CHAMP_INTENT})
    assert rpt["scoring_mode"] == "relative"  # mode flips on the flag
    assert "relative" not in rpt             # no stored counts → graceful omit
    assert "reason_relative" not in rpt


# ── round response: finalist relative block ──────────────────────────────────


def _wire_round_stores(monkeypatch, *, finalist_relative=None):
    """Wire a fake store whose finalist carries (or lacks) a STORED relative block.
    The round response now READS the finalist's own ``benchmark_details['relative']``
    (same-pin, persisted at evaluation) — it never recomputes against the champion."""
    finalist = _sub(_CHAL_INTENT, sid="fin-1", relative=finalist_relative)
    fake_store = SimpleNamespace(get=lambda sid: {"fin-1": finalist}.get(sid))
    monkeypatch.setattr(round_manager, "get_store", lambda: fake_store)


def _round_state():
    return RoundState(
        round_id="round-e1-n1",
        status=RoundStatus.CERTIFYING,
        opened_epoch=1,
        finalist_submission_id="fin-1",
        finalist_score=0.95,
    )


def test_round_response_off_byte_for_byte(monkeypatch):
    monkeypatch.delenv("RELATIVE_SCORING_ENABLED", raising=False)
    resp = round_manager._round_state_to_response(_round_state())
    dumped = resp.model_dump()
    # Exactly the declared fields — no extras leaked in.
    assert set(dumped) == set(SolverRoundResponse.model_fields)
    assert "scoring_mode" not in dumped
    assert "finalist_relative" not in dumped
    # Legacy finalist_score untouched.
    assert dumped["finalist_score"] == 0.95


def test_round_response_on_attaches_finalist_relative(monkeypatch):
    monkeypatch.setenv("RELATIVE_SCORING_ENABLED", "1")
    _wire_round_stores(monkeypatch, finalist_relative=_STORED_DETHRONE)
    resp = round_manager._round_state_to_response(_round_state())
    dumped = resp.model_dump()
    assert dumped["scoring_mode"] == "relative"
    assert dumped["finalist_relative"]["better"] == 2
    assert dumped["finalist_relative"]["verdict"] == "dethrone"
    assert dumped["reason_relative"].startswith("adopted fin-1")
    # Legacy finalist_score still present (not replaced).
    assert dumped["finalist_score"] == 0.95


def test_round_response_on_no_stored_relative_omits_block(monkeypatch):
    monkeypatch.setenv("RELATIVE_SCORING_ENABLED", "1")
    _wire_round_stores(monkeypatch, finalist_relative=None)  # finalist has no stored block
    resp = round_manager._round_state_to_response(_round_state())
    dumped = resp.model_dump()
    assert dumped["scoring_mode"] == "relative"
    assert "finalist_relative" not in dumped


def test_round_response_on_no_finalist_marks_mode_only(monkeypatch):
    monkeypatch.setenv("RELATIVE_SCORING_ENABLED", "1")
    state = RoundState(round_id="round-e1-n1", status=RoundStatus.OPEN, opened_epoch=1)
    resp = round_manager._round_state_to_response(state)
    dumped = resp.model_dump()
    assert dumped["scoring_mode"] == "relative"
    assert "finalist_relative" not in dumped


# ── get_app_status: scoring_mode marker ──────────────────────────────────────


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


def test_app_status_off_has_no_scoring_mode(monkeypatch):
    monkeypatch.delenv("RELATIVE_SCORING_ENABLED", raising=False)
    from minotaur_subnet.api.server import app
    client = TestClient(app, raise_server_exceptions=False)
    app_id = _seed_app()
    data = client.get(f"/v1/apps/{app_id}/status").json()
    assert "scoring_mode" not in data
    assert "champion_score" in data  # legacy field intact


def test_app_status_on_flips_scoring_mode(monkeypatch):
    monkeypatch.setenv("RELATIVE_SCORING_ENABLED", "1")
    from minotaur_subnet.api.server import app
    client = TestClient(app, raise_server_exceptions=False)
    app_id = _seed_app()
    data = client.get(f"/v1/apps/{app_id}/status").json()
    assert data["scoring_mode"] == "relative"
    assert "champion_score" in data  # legacy field still present
