"""Follower votes must see the candidate's ladder metrics (vote-input parity).

Live evidence (2026-07-07 19:16Z, round-e29724169-n1 / sub_c2ea85aa9641): a
factor-tie dethrone the leader ADOPTED (factor_delta=-1647) collected
BENCHMARK_MISMATCH dissents from BOTH followers ("0 better / 0 worse ... did
not meet the net-better rule") — the followers' factor_delta was 0. Root
cause: a candidate's ladder metrics (max_region_nodes / unproductive_*) reach
followers ONLY via the round-close submission snapshot, which is serialized AT
CLOSE — but screening stage 1 (which computes the metrics) can complete AFTER
close (rotation keeps not-yet-screened submissions; a leader restart re-kicks
screening while the coordinator closes the elapsed round at boot). The
leader's decision reads its LIVE record; the follower's vote read the stale
close-time mirror.

Two-sided fix under test here:

  * LEADER: ``_refresh_round_submission_mirror`` re-broadcasts the round's
    CURRENT submission records (force-close snapshot) before the proposal
    fan-out, and ``_certify_solver_round_state`` awaits it BEFORE asking peers
    to vote — so the follower's mirror carries the same metric values the
    leader's decision read. Followers ingest it through the pre-existing
    force-heal branch of ``_sync_close_solver_round_state`` (no follower-side
    deploy needed).
  * FOLLOWER: ``_independent_adopt_vote`` re-reads the candidate's FRESHEST
    store record at vote time (the in-hand reference was fetched at proposal
    receipt, minutes before the vote — an upsert in between replaces the store
    object), falling back None-safely to the passed candidate.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from minotaur_subnet.api.routes.submissions import champion_consensus as cc
from minotaur_subnet.api.routes.submissions import state
from minotaur_subnet.api.routes.submissions.models import CloseRoundRequest
from minotaur_subnet.api.routes.submissions.round_manager import (
    _close_round_sync_payload,
    _sync_close_solver_round_state,
)
from minotaur_subnet.harness.orchestrator import BenchmarkResult
from minotaur_subnet.harness.round_store import RoundStatus, RoundStore
from minotaur_subnet.harness.submission_store import SubmissionStore


CANDIDATE_ID = "sub_c2ea85aa9641"
ROUND_ID = "round-e29724169-n1"

# Leader-persisted screening metrics: candidate better-factored by 1647 nodes
# (>= FACTOR_MARGIN=100) and 300 fewer deadwood nodes at the same metric version.
CANDIDATE_METRICS = {
    "max_region_nodes": 1000,
    "unproductive_nodes": 500,
    "unproductive_metric_version": 1,
}
INCUMBENT_METRICS = {
    "max_region_nodes": 2647,
    "unproductive_nodes": 800,
    "unproductive_metric_version": 1,
}


@pytest.fixture
def stores():
    """Fresh in-memory submission + round stores as the module singletons."""
    prev_sub, prev_round = state._store, state._round_store
    sub_store, round_store = SubmissionStore(), RoundStore()
    state.set_store(sub_store)
    state.set_round_store(round_store)
    try:
        yield sub_store, round_store
    finally:
        state._store, state._round_store = prev_sub, prev_round


def _candidate_record(**overrides):
    rec = {
        "submission_id": CANDIDATE_ID,
        "repo_url": "https://github.com/miner/solver",
        "commit_hash": "abc1234",
        "epoch": 29724169,
        "hotkey": "5Gminer",
        "round_id": ROUND_ID,
        "status": "benchmarking",
        "image_id": "sha256:deadbeef",
        **CANDIDATE_METRICS,
    }
    rec.update(overrides)
    return rec


def _incumbent(**overrides):
    ns = SimpleNamespace(submission_id="sub_champion", **INCUMBENT_METRICS)
    for k, v in overrides.items():
        setattr(ns, k, v)
    return ns


def _results(*pairs):
    return [BenchmarkResult(intent_id=iid, raw_output=sc) for iid, sc in pairs]


class _Worker:
    """BenchmarkWorker stand-in (mirrors tests/unit/test_independent_vote.py)."""

    def __init__(self, *, champ_results, incumbent):
        self._champ_results = champ_results
        self._incumbent = incumbent
        self._epoch_block_number = 123

    def _resolve_incumbent_submission(self):
        return self._incumbent

    def _resolve_champion_image(self):
        return "champ:img"

    async def memo_champion_bench(self, *, run, **_kw):
        return self._champ_results


class _Session:
    async def shutdown(self):
        return None


class _Orch:
    async def start_docker(self, image):
        return _Session()


def _vote(worker, chal_results, candidate):
    intents = [(SimpleNamespace(app_id="dex"), SimpleNamespace(chain_id=8453), None)]
    with patch("minotaur_subnet.harness.orchestrator.SolverOrchestrator", _Orch):
        return asyncio.run(
            cc._independent_adopt_vote(
                worker=worker, intents=intents, score_fn=None, simulator=object(),
                chal_results=chal_results, candidate=candidate, round_id=ROUND_ID,
            )
        )


def _spy_verdict(captured):
    """A stand-in evaluate_relative_adoption that records its delta kwargs."""

    def _spy(champ_results, chal_results, *args, **kwargs):
        captured.update(kwargs)
        return {
            "adopt": False,
            "reason": "spy",
            "n_wins": 0, "n_regressions": 0, "n_blind_spots": 0,
            "n_dropped": 0, "n_matched": 1, "n_blind_spot_repeats": 0,
            "scenarios_compared": 1,
        }

    return _spy


# --------------------------------------------------------------------------- #
# Follower vote: metrics come from the FRESHEST mirrored store record          #
# --------------------------------------------------------------------------- #


def test_vote_reads_metrics_from_freshest_store_record(stores, monkeypatch):
    """A STALE pre-metrics candidate reference (fetched at proposal receipt,
    before the snapshot heal landed) must NOT zero the deltas: the vote re-reads
    the store record — this is the 19:16Z dissent, fixed."""
    sub_store, _ = stores
    sub_store.upsert_submission(_candidate_record())

    captured: dict = {}
    monkeypatch.setattr(
        "minotaur_subnet.epoch.relative_scoring.evaluate_relative_adoption",
        _spy_verdict(captured),
    )
    stale_candidate = SimpleNamespace(submission_id=CANDIDATE_ID)  # no metrics
    w = _Worker(champ_results=_results(("o1", "1000")), incumbent=_incumbent())
    _vote(w, _results(("o1", "1000")), stale_candidate)

    assert captured["factor_delta"] == 1647
    assert captured["deadwood_delta"] == 300


def test_vote_saturated_tie_adopts_via_factor_margin(stores):
    """End-to-end with the REAL rule: an all-matched tie + a better-factored
    candidate (store-mirrored metrics) ADOPTS via the armed FACTOR_MARGIN clause
    — the exact leader verdict the followers dissented from at 19:16Z."""
    sub_store, _ = stores
    sub_store.upsert_submission(_candidate_record())

    stale_candidate = SimpleNamespace(submission_id=CANDIDATE_ID)
    w = _Worker(champ_results=_results(("o1", "1000")), incumbent=_incumbent())
    adopt, counts = _vote(w, _results(("o1", "1000")), stale_candidate)

    assert adopt is True
    assert counts["better"] == 0 and counts["worse"] == 0  # pure factor tie-break


def test_vote_without_mirrored_metrics_reproduces_the_dissent(stores):
    """Pre-fix reproduction (the 19:16Z shape): no metrics anywhere ⇒ deltas 0 ⇒
    the same all-matched tie REJECTS ('0 better / 0 worse'). Also proves the
    re-read is None-safe: missing store record + metric-less candidate never
    crash."""
    stale_candidate = SimpleNamespace(submission_id=CANDIDATE_ID)
    w = _Worker(
        champ_results=_results(("o1", "1000")),
        incumbent=SimpleNamespace(submission_id="sub_champion"),  # no metrics
    )
    adopt, counts = _vote(w, _results(("o1", "1000")), stale_candidate)

    assert adopt is False
    assert counts["better"] == 0 and counts["worse"] == 0


def test_vote_falls_back_to_passed_candidate_when_store_misses(stores, monkeypatch):
    """Store has no record ⇒ the passed candidate's own metrics are used."""
    captured: dict = {}
    monkeypatch.setattr(
        "minotaur_subnet.epoch.relative_scoring.evaluate_relative_adoption",
        _spy_verdict(captured),
    )
    carrying_candidate = SimpleNamespace(
        submission_id=CANDIDATE_ID, **CANDIDATE_METRICS
    )
    w = _Worker(champ_results=_results(("o1", "1000")), incumbent=_incumbent())
    _vote(w, _results(("o1", "1000")), carrying_candidate)

    assert captured["factor_delta"] == 1647
    assert captured["deadwood_delta"] == 300


# --------------------------------------------------------------------------- #
# Snapshot round-trip: close payload -> pydantic body -> follower store        #
# --------------------------------------------------------------------------- #


def test_close_payload_carries_metrics_and_follower_upsert_preserves_them(stores):
    """The full mirror pipeline keeps the metric fields intact: leader
    serializes to_dict() into the close payload, CloseRoundRequest (list[dict])
    does not drop them, and the follower's close-sync upserts them."""
    sub_store, round_store = stores
    sub_store.upsert_submission(_candidate_record())
    round_store.ensure_open_round(opened_epoch=29724169)
    # Leader side: round_id must match the record's for list_by_round.
    leader_state = round_store.get_current_round()
    sub_store.upsert_submission(_candidate_record(round_id=leader_state.round_id))
    payload = _close_round_sync_payload(leader_state)

    subs = payload.get("submissions") or []
    assert subs and subs[0]["max_region_nodes"] == 1000
    assert subs[0]["unproductive_nodes"] == 500
    assert subs[0]["unproductive_metric_version"] == 1

    # Follower side: fresh stores (a different validator).
    follower_sub, follower_round = SubmissionStore(), RoundStore()
    state.set_store(follower_sub)
    state.set_round_store(follower_round)
    body = CloseRoundRequest(
        round_id=leader_state.round_id, close_epoch=29724170,
        submissions=payload["submissions"],
    )
    result = _sync_close_solver_round_state(body)
    assert result.status == RoundStatus.CLOSED

    mirrored = follower_sub.get(CANDIDATE_ID)
    assert mirrored is not None
    assert mirrored.max_region_nodes == 1000
    assert mirrored.unproductive_nodes == 500
    assert mirrored.unproductive_metric_version == 1


def test_force_close_heals_a_stale_pre_metrics_mirror(stores):
    """The leader's pre-proposal refresh lands on a follower whose round is
    already CLOSED: the force branch upserts the fresh metric-carrying records
    over the close-time (metric-less) mirror WITHOUT touching the round FSM.
    This is the follower half of the fix — and it is pre-existing code, so
    already-deployed followers ingest the refresh with no new deploy."""
    sub_store, round_store = stores
    # Follower state: round CLOSED, mirror mirrored pre-screening (no metrics).
    round_store.adopt_round(
        round_id=ROUND_ID, opened_epoch=29724169,
        status=RoundStatus.CLOSED, close_epoch=29724170,
    )
    sub_store.upsert_submission(_candidate_record(
        max_region_nodes=None, unproductive_nodes=None,
        unproductive_metric_version=None,
    ))
    assert sub_store.get(CANDIDATE_ID).max_region_nodes is None

    # The leader's refresh broadcast: same round, force=True, fresh records.
    body = CloseRoundRequest(
        round_id=ROUND_ID, close_epoch=29724170, force=True,
        submissions=[_candidate_record()],
    )
    result = _sync_close_solver_round_state(body)

    assert result.status == RoundStatus.CLOSED  # FSM untouched
    healed = sub_store.get(CANDIDATE_ID)
    assert healed.max_region_nodes == 1000
    assert healed.unproductive_nodes == 500
    assert healed.unproductive_metric_version == 1


# --------------------------------------------------------------------------- #
# Leader: pre-proposal snapshot refresh                                        #
# --------------------------------------------------------------------------- #


def test_refresh_round_submission_mirror_broadcasts_force_close(stores, monkeypatch):
    sub_store, round_store = stores
    round_store.ensure_open_round(opened_epoch=29724169)
    leader_state = round_store.get_current_round()
    sub_store.upsert_submission(_candidate_record(round_id=leader_state.round_id))

    sent: list = []

    async def _capture(path, payload):
        sent.append((path, payload))

    monkeypatch.setattr(cc, "_broadcast_internal_round_sync", _capture)
    asyncio.run(cc._refresh_round_submission_mirror(leader_state))

    assert len(sent) == 1
    path, payload = sent[0]
    assert path == "/v1/solver/round/internal/close"
    assert payload["force"] is True
    assert payload["round_id"] == leader_state.round_id
    subs = payload["submissions"]
    assert subs[0]["submission_id"] == CANDIDATE_ID
    assert subs[0]["max_region_nodes"] == 1000
    assert subs[0]["unproductive_nodes"] == 500


def test_refresh_failure_never_raises(stores, monkeypatch):
    """Best-effort: a broadcast failure degrades to the pre-fix behavior."""
    _, round_store = stores
    round_store.ensure_open_round(opened_epoch=29724169)
    leader_state = round_store.get_current_round()

    async def _boom(path, payload):
        raise RuntimeError("peer network down")

    monkeypatch.setattr(cc, "_broadcast_internal_round_sync", _boom)
    asyncio.run(cc._refresh_round_submission_mirror(leader_state))  # no raise


def test_certify_awaits_refresh_before_proposal_broadcast(stores, monkeypatch):
    """_certify_solver_round_state must deliver the snapshot refresh BEFORE the
    champion-proposal fan-out, so followers vote on the healed mirror."""
    from minotaur_subnet.harness.round_store import ChampionCertificate
    from minotaur_subnet.api.routes.submissions.models import CertifyRoundRequest

    sub_store, round_store = stores
    round_store.adopt_round(
        round_id=ROUND_ID, opened_epoch=29724169,
        status=RoundStatus.CLOSED, close_epoch=29724170,
        benchmark_pack_hash="pack-abc", committee_hash="0xcommittee",
        quorum_required=1, effective_epoch=29724180,
    )
    round_store.set_round_finalist(
        ROUND_ID, submission_id=CANDIDATE_ID, image_id="sha256:deadbeef",
    )
    sub_store.upsert_submission(_candidate_record())

    events: list = []

    async def _capture_close(path, payload):
        events.append(("close-refresh", path, bool(payload.get("force"))))

    monkeypatch.setattr(cc, "_broadcast_internal_round_sync", _capture_close)
    monkeypatch.setattr(cc, "best_effort_champion_quorum_enabled", lambda: False)

    cert = ChampionCertificate(
        round_id=ROUND_ID, candidate_submission_id=CANDIDATE_ID,
        candidate_image_id="sha256:deadbeef", quorum_required=1,
        effective_epoch=29724180,
    )

    class _Mgr:
        committee_hash = "0xcommittee"
        quorum_required = 1
        validators = ["0xleader"]
        protocol_config = None

        async def propose(self, proposal):
            await asyncio.sleep(0)  # let the broadcast task run
            await asyncio.sleep(0)
            return SimpleNamespace(
                reached=True, certificate=cert, collected=1, quorum=1, approvals=[],
            )

    class _Net:
        peers = ["peer-1"]

        async def broadcast_champion_proposal(self, proposal, **_kw):
            events.append(("proposal", proposal.candidate_submission_id))
            return []

    prev_mgr = state._champion_consensus_manager
    prev_net = state._champion_peer_network
    state.set_champion_consensus_manager(_Mgr())
    state.set_champion_peer_network(_Net())
    try:
        result = asyncio.run(cc._certify_solver_round_state(
            CertifyRoundRequest(
                round_id=ROUND_ID, candidate_submission_id=CANDIDATE_ID,
                effective_epoch=29724180,
            )
        ))
    finally:
        state.set_champion_consensus_manager(prev_mgr)
        state.set_champion_peer_network(prev_net)

    assert result.status == RoundStatus.CERTIFIED
    kinds = [e[0] for e in events]
    assert "close-refresh" in kinds and "proposal" in kinds
    # The refresh is AWAITED before the proposal fan-out task is even created.
    assert kinds.index("close-refresh") < kinds.index("proposal")
    refresh = next(e for e in events if e[0] == "close-refresh")
    assert refresh[1] == "/v1/solver/round/internal/close" and refresh[2] is True
