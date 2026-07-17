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

    async def _fake_prepare(round_id, *, candidate_submission_id=None, benchmark_anchor_epoch=None):
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

    async def _fake_prepare(round_id, *, candidate_submission_id=None, benchmark_anchor_epoch=None):
        return certifying

    with patch.object(cc, "_maybe_prepare_round_for_certification", _fake_prepare), \
         patch.object(cc, "_round_certification_deadline_elapsed", lambda rs: False), \
         patch.object(cc, "_build_champion_proposal_for_round", _fake_builder), \
         patch.object(cc, "get_round_store", lambda: SimpleNamespace()):
        with pytest.raises(RuntimeError, match="stop"):
            await cc._certify_solver_round_state(body)

    assert captured["nonce_override"] is None      # 0 -> None (leader computes its own)
    assert captured["deadline_override"] is None


# ── incumbent_image_id propagation (same class of bug as #414) ────────────────
# incumbent_image_id is part of the SIGNED champion-approval digest but was NOT
# declared on CertifyRoundRequest nor carried in the /certify payload, so a
# follower rebuilt it from its OWN (differently-represented) round record → the
# digest diverged → verify_approval rejected the leader's own approval as
# "Invalid champion approvals" → the round stranded leader-only (100% burn until
# a signature-bypassing reattest). These pin the round-trip + forwarding.


def test_certify_request_round_trips_incumbent_image_id():
    body = CertifyRoundRequest(
        round_id="r", effective_epoch=6, quorum_required=1,
        incumbent_image_id="sha256:" + "ab" * 32,
    )
    assert body.incumbent_image_id == "sha256:" + "ab" * 32
    # Absent on an old-leader cert -> safe default (never AttributeError).
    assert CertifyRoundRequest(round_id="r", effective_epoch=6).incumbent_image_id is None


@pytest.mark.asyncio
async def test_certify_handler_forwards_incumbent_override():
    """The handler MUST forward the leader's signed incumbent as
    ``incumbent_image_id_override`` so the follower rebuilds the identical digest;
    absent -> None so the leader uses its own round record (signing path unchanged)."""
    from minotaur_subnet.api.routes.submissions import champion_consensus as cc
    from minotaur_subnet.harness.round_store import RoundStatus

    certifying = SimpleNamespace(status=RoundStatus.CERTIFYING, decision_deadline_epoch=None)

    async def _fake_prepare(round_id, *, candidate_submission_id=None, benchmark_anchor_epoch=None):
        return certifying

    async def _run(body):
        captured: dict = {}

        def _fake_builder(round_state, **kwargs):
            captured.update(kwargs)
            raise RuntimeError("stop")

        with patch.object(cc, "_maybe_prepare_round_for_certification", _fake_prepare), \
             patch.object(cc, "_round_certification_deadline_elapsed", lambda rs: False), \
             patch.object(cc, "_build_champion_proposal_for_round", _fake_builder), \
             patch.object(cc, "get_round_store", lambda: SimpleNamespace()):
            with pytest.raises(RuntimeError, match="stop"):
                await cc._certify_solver_round_state(body)
        return captured

    with_inc = await _run(CertifyRoundRequest(
        round_id="r", effective_epoch=6, quorum_required=1,
        incumbent_image_id="sha256:LEADER_SIGNED",
    ))
    assert with_inc["incumbent_image_id_override"] == "sha256:LEADER_SIGNED"

    without = await _run(CertifyRoundRequest(round_id="r", effective_epoch=6, quorum_required=1))
    assert without["incumbent_image_id_override"] is None  # "" -> None (leader's own record)


def test_builder_incumbent_override_wins_else_local_record():
    """The proposal builder uses the SIGNED override when given (follower path) and
    the local round record otherwise (leader path). This is the actual fix: with the
    override both sides sign the SAME incumbent -> the digest matches."""
    from minotaur_subnet.api.routes.submissions import champion_consensus as cc

    rs = SimpleNamespace(
        round_id="r", finalist_submission_id="s", finalist_image_id="builtin:x",
        committee_hash="ch", benchmark_pack_hash="bp", shadow_case_log_hash="sc",
        effective_epoch=6, incumbent_image_id="sha256:LOCAL_FOLLOWER_ID", quorum_required=1,
    )
    cand = SimpleNamespace(
        submission_id="s", hotkey="__genesis__", repo_url="builtin://x",
        image_digest=None, image_id=None, commit_hash="c",
    )
    with patch.object(cc, "get_store", lambda: SimpleNamespace(get=lambda sid: cand)), \
         patch.object(cc, "get_champion_consensus_manager", lambda: None):
        # Follower path: override wins -> matches the leader's signed value.
        prop, _, _ = cc._build_champion_proposal_for_round(
            rs, incumbent_image_id_override="sha256:LEADER_SIGNED")
        assert prop.incumbent_image_id == "sha256:LEADER_SIGNED"
        # Leader path: no override -> its own round record (signing unchanged).
        prop2, _, _ = cc._build_champion_proposal_for_round(rs)
        assert prop2.incumbent_image_id == "sha256:LOCAL_FOLLOWER_ID"
