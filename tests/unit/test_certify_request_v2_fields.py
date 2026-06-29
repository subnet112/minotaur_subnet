"""Regression for the merged-but-broken champion cert v2-field propagation (PR #414).

PR #414 made ``_certify_solver_round_state`` read ``body.commit_hash/nonce/deadline``
and the ``/certify`` broadcast carry them — but FORGOT to declare those fields on
``CertifyRoundRequest``. Pydantic (default ``extra="ignore"``) drops them, so
``body.commit_hash`` raises ``AttributeError`` and 500s EVERY certify (the leader's own
included) → the champion pipeline dies. #414's own test mocked the follower rebuild and
never parsed a real ``CertifyRoundRequest``, so it missed this. These tests parse the
real model and drive the real handler to the proposal-builder call.
"""

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from minotaur_subnet.api.routes.submissions.models import CertifyRoundRequest  # noqa: E402


def test_certify_request_round_trips_v2_fields():
    """The model MUST carry the leader's signed commit_hash/nonce/deadline (the fields
    the broadcast adds). Without the declarations Pydantic silently drops them and
    attribute access raises AttributeError — this asserts the round-trip."""
    body = CertifyRoundRequest(
        round_id="r", effective_epoch=6, quorum_required=1,
        commit_hash="0x" + "7a" * 32, nonce=1_726_000_000_123, deadline=1_726_003_600,
    )
    assert body.commit_hash == "0x" + "7a" * 32
    assert body.nonce == 1_726_000_000_123
    assert body.deadline == 1_726_003_600


def test_certify_request_v2_fields_default_when_absent():
    """An OLD leader's broadcast (no v2 fields) parses with safe defaults — never
    AttributeError — so ``body.nonce or None`` => None => the leader computes its own
    nonce (legacy path preserved during the staggered rollout)."""
    body = CertifyRoundRequest(round_id="r", effective_epoch=6)
    assert body.commit_hash is None
    assert body.nonce == 0
    assert body.deadline == 0


@pytest.mark.asyncio
async def test_certify_handler_forwards_v2_overrides_without_attributeerror():
    """Drive the REAL ``_certify_solver_round_state`` to the proposal builder and assert
    it (a) does NOT raise AttributeError on body.commit_hash/nonce/deadline — the actual
    crash on develop — and (b) forwards the leader's signed values as ``*_override``.
    This is the broadcast→parse→rebuild round-trip #414's mocked test skipped."""
    from minotaur_subnet.api.routes.submissions import champion_consensus as cc
    from minotaur_subnet.harness.round_store import RoundStatus

    body = CertifyRoundRequest(
        round_id="r", effective_epoch=6, quorum_required=1,
        commit_hash="0xfeed", nonce=1_726_000_000_123, deadline=1_726_003_600,
    )
    certifying = SimpleNamespace(
        status=RoundStatus.CERTIFYING, decision_deadline_epoch=None,
    )
    captured: dict = {}

    def _fake_builder(round_state, **kwargs):
        captured.update(kwargs)
        raise RuntimeError("stop-after-builder")  # we only need the forwarded kwargs

    async def _fake_prepare(round_id, *, candidate_submission_id=None):
        return certifying

    with patch.object(cc, "_maybe_prepare_round_for_certification", _fake_prepare), \
         patch.object(cc, "_round_certification_deadline_elapsed", lambda rs: False), \
         patch.object(cc, "_build_champion_proposal_for_round", _fake_builder), \
         patch.object(cc, "get_round_store", lambda: SimpleNamespace()):
        # On develop this raises AttributeError at body.commit_hash BEFORE the builder.
        with pytest.raises(RuntimeError, match="stop-after-builder"):
            await cc._certify_solver_round_state(body)

    # Reaching here => no AttributeError => the model carries the fields. And forwarded:
    assert captured["commit_hash_override"] == "0xfeed"
    assert captured["nonce_override"] == 1_726_000_000_123
    assert captured["deadline_override"] == 1_726_003_600


@pytest.mark.asyncio
async def test_certify_handler_absent_v2_fields_passes_none_override():
    """An old-leader cert (no v2 fields) must forward None (not 0) so the builder
    computes its own nonce — guarding the ``body.nonce or None`` semantics."""
    from minotaur_subnet.api.routes.submissions import champion_consensus as cc
    from minotaur_subnet.harness.round_store import RoundStatus

    body = CertifyRoundRequest(round_id="r", effective_epoch=6, quorum_required=1)
    certifying = SimpleNamespace(status=RoundStatus.CERTIFYING, decision_deadline_epoch=None)
    captured: dict = {}

    def _fake_builder(round_state, **kwargs):
        captured.update(kwargs)
        raise RuntimeError("stop")

    async def _fake_prepare(round_id, *, candidate_submission_id=None):
        return certifying

    with patch.object(cc, "_maybe_prepare_round_for_certification", _fake_prepare), \
         patch.object(cc, "_round_certification_deadline_elapsed", lambda rs: False), \
         patch.object(cc, "_build_champion_proposal_for_round", _fake_builder), \
         patch.object(cc, "get_round_store", lambda: SimpleNamespace()):
        with pytest.raises(RuntimeError, match="stop"):
            await cc._certify_solver_round_state(body)

    assert captured["nonce_override"] is None      # 0 -> None (leader computes its own)
    assert captured["deadline_override"] is None
