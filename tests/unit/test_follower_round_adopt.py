"""Unit tests for follower 'adopt-when-behind' round adoption.

A follower validator that is stuck at a stale genesis round cannot reconstruct
the leader's exact round_id locally. When it receives an authenticated lifecycle
broadcast for the leader's current round, it ADOPTS that round verbatim by the
broadcast round_id so the handler can process it instead of 404/409-ing against
its own stale round.

Adoption is wired into the genuinely useful catch-up paths ONLY — CLOSE and
CERTIFY, which create/advance the round so a *later* activate works on the
now-existing round. It is deliberately NOT wired into ABORT (terminal, no
catch-up value) or ACTIVATE (a fresh CERTIFIED round has no champion data so it
would 409) — adopting there would only strand the follower's current OPEN round
in a terminal state.

These tests exercise:
  * ``RoundStore.adopt_round`` (the store mutator) directly, and
  * ``_adopt_leader_round_if_behind`` + the ``_sync_*`` helpers (the guard +
    wiring) against a freshly injected store — no HTTP layer required.
"""

from __future__ import annotations

import pytest

from minotaur_subnet.harness.round_store import (
    ChampionSnapshot,
    RoundStatus,
    RoundStore,
)
from minotaur_subnet.api.routes.submissions import state
from minotaur_subnet.api.routes.submissions.models import (
    AbortRoundRequest,
    CloseRoundRequest,
)
from minotaur_subnet.api.routes.submissions.round_manager import (
    _adopt_leader_round_if_behind,
    _parse_round_opened_epoch,
    _sync_abort_solver_round_state,
    _sync_close_solver_round_state,
)


@pytest.fixture
def fresh_store():
    """Inject a fresh in-memory RoundStore as the module-level singleton."""
    previous = state._round_store
    store = RoundStore()
    state.set_round_store(store)
    try:
        yield store
    finally:
        state._round_store = previous


# --------------------------------------------------------------------------- #
# RoundStore.adopt_round                                                       #
# --------------------------------------------------------------------------- #


def test_adopt_round_creates_verbatim_round():
    store = RoundStore()

    adopted = store.adopt_round(
        round_id="round-e500-n1",
        opened_epoch=500,
        status=RoundStatus.CLOSED,
        close_epoch=501,
        benchmark_pack_hash="pack-abc",
    )

    assert adopted.round_id == "round-e500-n1"
    assert adopted.status == RoundStatus.CLOSED
    assert adopted.opened_epoch == 500
    assert adopted.close_epoch == 501
    assert adopted.benchmark_pack_hash == "pack-abc"
    # It becomes the current round.
    current = store.get_current_round()
    assert current is not None
    assert current.round_id == "round-e500-n1"


def test_adopt_round_supersedes_stale_open_round():
    store = RoundStore()
    stale = store.ensure_open_round(opened_epoch=1)
    assert stale.round_id == "round-e1-n1"

    store.adopt_round(
        round_id="round-e500-n1",
        opened_epoch=500,
        status=RoundStatus.CLOSED,
        close_epoch=500,
    )

    # The stale OPEN round is superseded (aborted) so it can't masquerade as live.
    superseded = store.get_round("round-e1-n1")
    assert superseded is not None
    assert superseded.status == RoundStatus.ABORTED
    assert "superseded by leader round round-e500-n1" in (superseded.abort_reason or "")
    # The leader's round is now current.
    assert store.get_current_round().round_id == "round-e500-n1"


def test_adopt_round_skips_none_field_updates():
    store = RoundStore()
    adopted = store.adopt_round(
        round_id="round-e10-n1",
        opened_epoch=10,
        status=RoundStatus.ABORTED,
        abort_reason="superseded",
        committee_hash=None,  # None values must NOT be applied
    )
    assert adopted.abort_reason == "superseded"
    assert adopted.committee_hash is None


def test_adopt_round_applies_incumbent():
    store = RoundStore()
    champ = ChampionSnapshot(submission_id="sub_x", image_id="sha256:" + "b" * 64,
                             hotkey="5Gminer")
    adopted = store.adopt_round(
        round_id="round-e10-n1",
        opened_epoch=10,
        status=RoundStatus.CLOSED,
        incumbent=champ,
    )
    assert adopted.incumbent_submission_id == "sub_x"
    assert adopted.incumbent_hotkey == "5Gminer"


def test_adopt_round_ignores_unknown_field():
    store = RoundStore()
    adopted = store.adopt_round(
        round_id="round-e10-n1",
        opened_epoch=10,
        status=RoundStatus.CLOSED,
        close_epoch=11,  # known field — applied
        bogus_attr="should-not-stick",  # unknown — skipped, no setattr
    )
    assert adopted.close_epoch == 11
    assert not hasattr(adopted, "bogus_attr")


# --------------------------------------------------------------------------- #
# _parse_round_opened_epoch                                                    #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "round_id,expected",
    [
        ("round-e1-n1", 1),
        ("round-e29708265-n1", 29708265),
        ("round-e500-n3", 500),
        ("", None),
        ("garbage", None),
        ("round-e1", None),
        ("round-eXX-n1", None),
        ("round-e1-nX", None),
    ],
)
def test_parse_round_opened_epoch(round_id, expected):
    assert _parse_round_opened_epoch(round_id) == expected


# --------------------------------------------------------------------------- #
# _adopt_leader_round_if_behind guard                                          #
# --------------------------------------------------------------------------- #


def test_adopt_if_behind_adopts_newer_round(fresh_store):
    fresh_store.ensure_open_round(opened_epoch=1)  # stale genesis round

    adopted = _adopt_leader_round_if_behind(
        "round-e500-n1",
        status=RoundStatus.CLOSED,
        close_epoch=500,
    )

    assert adopted is True
    got = fresh_store.get_round("round-e500-n1")
    assert got is not None and got.status == RoundStatus.CLOSED
    assert fresh_store.get_current_round().round_id == "round-e500-n1"


def test_adopt_if_behind_rejects_older_round(fresh_store):
    fresh_store.adopt_round(
        round_id="round-e500-n1", opened_epoch=500, status=RoundStatus.OPEN,
    )

    adopted = _adopt_leader_round_if_behind(
        "round-e1-n1",  # OLDER than current (e500)
        status=RoundStatus.CLOSED,
        close_epoch=1,
    )

    assert adopted is False
    assert fresh_store.get_round("round-e1-n1") is None
    assert fresh_store.get_current_round().round_id == "round-e500-n1"


def test_adopt_if_behind_rejects_equal_epoch_round(fresh_store):
    # Same opened_epoch but a different count = NOT strictly ahead -> do not adopt.
    fresh_store.adopt_round(
        round_id="round-e500-n1", opened_epoch=500, status=RoundStatus.OPEN,
    )
    adopted = _adopt_leader_round_if_behind(
        "round-e500-n2", status=RoundStatus.CLOSED, close_epoch=500,
    )
    assert adopted is False
    assert fresh_store.get_round("round-e500-n2") is None


def test_adopt_if_behind_cold_start_accepts_when_empty(fresh_store):
    # True cold start: no current round AND no rounds at all -> accept.
    adopted = _adopt_leader_round_if_behind(
        "round-e500-n1", status=RoundStatus.CLOSED, close_epoch=500,
    )
    assert adopted is True
    assert fresh_store.get_round("round-e500-n1") is not None


def test_adopt_if_behind_cold_start_rejects_ancient_replay(fresh_store):
    # No CURRENT round (e.g. all rounds terminal after restart) but the store
    # remembers a newer round -> a replayed ancient signed broadcast must NOT
    # pin us behind. Mark the known round non-current so get_current_round()
    # is None while list_rounds() still reports it.
    fresh_store.adopt_round(
        round_id="round-e500-n1", opened_epoch=500, status=RoundStatus.ABORTED,
        abort_reason="done",
    )
    fresh_store._current_round_id = None  # simulate no live round after restart
    assert fresh_store.get_current_round() is None

    adopted = _adopt_leader_round_if_behind(
        "round-e1-n1", status=RoundStatus.CLOSED, close_epoch=1,  # ANCIENT
    )
    assert adopted is False
    assert fresh_store.get_round("round-e1-n1") is None


def test_adopt_if_behind_cold_start_accepts_newer_than_known(fresh_store):
    # No current round, but the leader is AHEAD of our newest known round -> ok.
    fresh_store.adopt_round(
        round_id="round-e500-n1", opened_epoch=500, status=RoundStatus.ABORTED,
        abort_reason="done",
    )
    fresh_store._current_round_id = None
    assert fresh_store.get_current_round() is None

    adopted = _adopt_leader_round_if_behind(
        "round-e600-n1", status=RoundStatus.CLOSED, close_epoch=600,
    )
    assert adopted is True
    assert fresh_store.get_round("round-e600-n1") is not None


def test_adopt_if_behind_noop_when_round_present(fresh_store):
    # We already have the round -> let the handler resolve it normally.
    fresh_store.adopt_round(
        round_id="round-e500-n1", opened_epoch=500, status=RoundStatus.CLOSED,
        close_epoch=500,
    )
    adopted = _adopt_leader_round_if_behind(
        "round-e500-n1", status=RoundStatus.ABORTED, abort_reason="x",
    )
    assert adopted is False
    # Status unchanged (no re-adoption).
    assert fresh_store.get_round("round-e500-n1").status == RoundStatus.CLOSED


def test_adopt_if_behind_rejects_unparseable_round(fresh_store):
    fresh_store.ensure_open_round(opened_epoch=1)
    adopted = _adopt_leader_round_if_behind(
        "not-a-round-id", status=RoundStatus.CLOSED, close_epoch=1,
    )
    assert adopted is False


# --------------------------------------------------------------------------- #
# _sync_* helpers (close / certify adopt; abort does NOT)                      #
# --------------------------------------------------------------------------- #


def test_sync_close_adopts_leader_round_when_behind(fresh_store):
    fresh_store.ensure_open_round(opened_epoch=1)  # stuck genesis round

    body = CloseRoundRequest(
        round_id="round-e500-n1",
        close_epoch=501,
        benchmark_pack_hash="pack-xyz",
        committee_hash="0xcommittee",
        quorum_required=2,
        decision_deadline_epoch=510,
        effective_epoch=520,
    )
    result = _sync_close_solver_round_state(body)

    assert result.round_id == "round-e500-n1"
    assert result.status == RoundStatus.CLOSED
    assert result.close_epoch == 501
    assert result.benchmark_pack_hash == "pack-xyz"
    assert result.committee_hash == "0xcommittee"
    assert result.quorum_required == 2
    assert result.decision_deadline_epoch == 510
    assert result.effective_epoch == 520
    # The leader's round is now this follower's current round, and the stale
    # genesis round was superseded.
    assert fresh_store.get_current_round().round_id == "round-e500-n1"
    assert fresh_store.get_round("round-e1-n1").status == RoundStatus.ABORTED


def test_sync_close_is_idempotent_when_behind(fresh_store):
    fresh_store.ensure_open_round(opened_epoch=1)
    body = CloseRoundRequest(round_id="round-e500-n1", close_epoch=501)

    first = _sync_close_solver_round_state(body)
    assert first.status == RoundStatus.CLOSED

    # Re-delivery: idempotency short-circuit returns the existing CLOSED round
    # without re-adopting (no exception, no duplicate state mutation).
    second = _sync_close_solver_round_state(body)
    assert second.status == RoundStatus.CLOSED
    assert second.round_id == "round-e500-n1"
    # Only one round id for e500 -> no double-adopt.
    e500 = [r for r in fresh_store.list_rounds() if r.opened_epoch == 500]
    assert len(e500) == 1


def test_certify_prep_adopts_leader_round_closed_when_behind(fresh_store):
    """certify-sync prep on a not-present newer round adopts it CLOSED.

    ``_maybe_prepare_round_for_certification`` is the certify-sync entry point;
    with no local round and no epoch manager configured it adopts the leader's
    round verbatim as CLOSED and returns it (the normal prep flow would then
    advance CLOSED -> CERTIFYING when a candidate/manager is present).
    """
    import asyncio

    from minotaur_subnet.api.routes.submissions.champion_consensus import (
        _maybe_prepare_round_for_certification,
    )

    fresh_store.ensure_open_round(opened_epoch=1)  # stuck genesis round
    state.set_epoch_manager(None)

    result = asyncio.run(
        _maybe_prepare_round_for_certification(
            "round-e500-n1",
            benchmark_pack_hash="pack-cert",
            committee_hash="0xcommittee",
            effective_epoch=520,
            quorum_required=2,
        )
    )

    assert result.round_id == "round-e500-n1"
    assert result.status == RoundStatus.CLOSED
    assert result.benchmark_pack_hash == "pack-cert"
    assert result.committee_hash == "0xcommittee"
    assert fresh_store.get_current_round().round_id == "round-e500-n1"
    assert fresh_store.get_round("round-e1-n1").status == RoundStatus.ABORTED


def test_sync_abort_never_adopts_unknown_round(fresh_store):
    # Abort is a terminal, no-catch-up-value path: it must NOT adopt a never-seen
    # leader round (which would strand the follower's current OPEN round). Even
    # when BEHIND a stale genesis round, an abort for an unknown newer round 404s
    # and leaves the follower's open round untouched.
    fresh_store.ensure_open_round(opened_epoch=1)
    body = AbortRoundRequest(round_id="round-e500-n1", reason="leader_aborted")

    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        _sync_abort_solver_round_state(body)
    assert exc.value.status_code == 404
    # The leader's round was NOT adopted, and our open round is still open.
    assert fresh_store.get_round("round-e500-n1") is None
    current = fresh_store.get_current_round()
    assert current is not None
    assert current.round_id == "round-e1-n1"
    assert current.status == RoundStatus.OPEN


def test_certify_prep_with_candidate_transitions_closed_to_certifying(fresh_store):
    """REGRESSION: a follower accepting the leader's proposal. With the leader's candidate
    (which the proposal endpoint now passes), prepare SKIPS evaluate_round and goes
    CLOSED -> CERTIFYING. Without the candidate it ran the full evaluate flow — a no-op on
    a follower (no benchmark worker, #385) — leaving the round CLOSED so the proposal gate
    rejected it ('is closed; expected certifying') and the quorum could never form."""
    import asyncio
    import time as _time

    from minotaur_subnet.harness.submission_store import (
        SubmissionStore, Submission, SubmissionStatus,
    )
    from minotaur_subnet.api.routes.submissions.champion_consensus import (
        _maybe_prepare_round_for_certification,
    )

    prev_store = getattr(state, "_store", None)
    prev_mgr = getattr(state, "_epoch_manager", None)
    try:
        cand = Submission(
            submission_id="sub_cand", repo_url="https://github.com/test/solver",
            commit_hash="abc123", epoch=500, hotkey="5Gtest", round_id="round-e500-n1",
            status=SubmissionStatus.SCORED, created_at=_time.time(), updated_at=_time.time(),
            image_tag="solver:v1", image_id="sha256:" + "c" * 64, solver_name="top-miner",
            solver_version="1.0.0", benchmark_rank=1,
            # Genuinely scored, value-delivering candidate: >=1 per-order row with
            # raw_output>0 is what marks a submission as SCORED-with-value now that
            # the scalar benchmark_score is gone.
            benchmark_details={"per_intent": [{"intent_id": "app:scn", "raw_output": "1000"}]},
        )
        sub_store = SubmissionStore()
        sub_store._submissions["sub_cand"] = cand
        state.set_store(sub_store)
        state.set_epoch_manager(None)  # follower: evaluate can't/ won't run

        fresh_store.ensure_open_round(opened_epoch=500)
        fresh_store.close_current_round(
            close_epoch=500, benchmark_pack_hash="pack",
            committee_hash="0xc", quorum_required=2,
        )

        result = asyncio.run(
            _maybe_prepare_round_for_certification(
                "round-e500-n1",
                candidate_submission_id="sub_cand",
                close_epoch=500, benchmark_pack_hash="pack",
                committee_hash="0xc", quorum_required=2,
            )
        )
        # Transitioned to CERTIFYING with the leader's candidate — gate would now pass.
        assert result.status == RoundStatus.CERTIFYING
        assert result.finalist_submission_id == "sub_cand"
    finally:
        if prev_store is not None:
            state.set_store(prev_store)
        state.set_epoch_manager(prev_mgr)
