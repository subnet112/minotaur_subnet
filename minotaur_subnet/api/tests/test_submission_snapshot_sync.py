"""PR: submission-propagation gap fix (close-time snapshot → follower upsert).

The benchmark pack hash includes a per-submission projection computed from the
LOCAL submission store. Followers never ingest submissions directly, so without
propagation their recompute diverges from the leader's → PACK_HASH_MISMATCH drops
them from quorum. This PR pushes the leader's close-time snapshot in the close
broadcast and has followers upsert it.

Two invariants are pinned here:
  1. The submission payload that feeds the pack hash is STABLE across a status
     change — `status` was removed from the hash precisely because the leader
     hashes at close while a follower recomputes AFTER its evaluate_round has
     advanced statuses. (test_pack_payload_stable_across_status_change)
  2. After a follower upserts the leader's `to_dict` snapshot, the two stores
     produce a byte-identical payload for the round. (parity tests)
"""

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from minotaur_subnet.api.routes.submissions import round_manager as rm
from minotaur_subnet.api.routes.submissions.models import CloseRoundRequest
from minotaur_subnet.harness.round_store import RoundStatus
from minotaur_subnet.harness.submission_store import (
    SubmissionStatus,
    SubmissionStore,
)


def _store(tmp_path, name):
    return SubmissionStore(persist_path=tmp_path / f"{name}.json")


def _pack_submission_payload(store, round_id):
    """Mirror the submissions_payload in _build_solver_round_benchmark_pack_hash
    (api/startup.py): the 7 IDENTITY fields (status is intentionally excluded),
    sorted by submission_id."""
    return [
        {
            "submission_id": sub.submission_id,
            "hotkey": sub.hotkey,
            "repo_url": sub.repo_url,
            "commit_hash": sub.commit_hash,
            "image_id": sub.image_id,
            "solver_name": sub.solver_name,
            "solver_version": sub.solver_version,
        }
        for sub in sorted(store.list_by_round(round_id), key=lambda s: s.submission_id)
    ]


def _seed_leader(store, round_id):
    """A realistic round: several miners, varied statuses + post-screening fields."""
    a = store.create("https://github.com/m/a", "aaaa1111", epoch=100, hotkey="5A",
                     round_id=round_id)
    a.status, a.image_id, a.solver_name, a.solver_version = (
        SubmissionStatus.SCORED, "sha256:aaa", "AlphaSolver", "1.2.0")
    b = store.create("https://github.com/m/b", "bbbb2222", epoch=100, hotkey="5B",
                     round_id=round_id)
    b.status, b.image_id, b.solver_name = SubmissionStatus.BENCHMARKING, "sha256:bbb", "BetaSolver"
    c = store.create("https://github.com/m/c", "cccc3333", epoch=100, hotkey="5C",
                     round_id=round_id)
    c.status = SubmissionStatus.REJECTED


# ── invariant 1: the pack payload ignores mutable status ─────────────────────

def test_pack_payload_stable_across_status_change(tmp_path):
    # THE root-cause fix: leader hashes at close, follower recomputes post-evaluate.
    # If status fed the hash, BENCHMARKING→SCORED would flip it. It must not.
    store = _store(tmp_path, "s")
    sub = store.create("r", "h", epoch=1, hotkey="5X", round_id="rr")
    sub.status, sub.image_id, sub.solver_name = SubmissionStatus.BENCHMARKING, "img1", "S"
    before = _pack_submission_payload(store, "rr")
    sub.status = SubmissionStatus.SCORED  # evaluate_round advances it
    assert _pack_submission_payload(store, "rr") == before


# ── upsert primitive ─────────────────────────────────────────────────────────

def test_upsert_preserves_caller_id_and_is_idempotent(tmp_path):
    store = _store(tmp_path, "f")
    rec = {"submission_id": "sub_LEADER01", "repo_url": "r", "commit_hash": "h",
           "epoch": 7, "hotkey": "5X", "round_id": "round-e7-n0", "status": "scored"}
    store.upsert_submission(rec)
    store.upsert_submission(rec)  # twice → still one record, same id
    got = store.list_by_round("round-e7-n0")
    assert len(got) == 1
    assert got[0].submission_id == "sub_LEADER01"  # NOT a freshly minted uuid
    assert got[0].status is SubmissionStatus.SCORED


def test_upsert_replaces_on_status_change(tmp_path):
    store = _store(tmp_path, "f")
    rec = {"submission_id": "sub_1", "repo_url": "r", "commit_hash": "h", "epoch": 7,
           "hotkey": "5X", "round_id": "rr", "status": "benchmarking"}
    store.upsert_submission(rec)
    store.upsert_submission({**rec, "status": "scored"})
    got = store.list_by_round("rr")
    assert len(got) == 1 and got[0].status is SubmissionStatus.SCORED


def test_upsert_rejects_missing_id(tmp_path):
    store = _store(tmp_path, "f")
    with pytest.raises(ValueError):
        store.upsert_submission({"repo_url": "r", "commit_hash": "h", "epoch": 1,
                                 "hotkey": "5X", "status": "queued"})


def test_upsert_tolerates_unknown_status(tmp_path):
    # An unknown status string must NOT drop the record (status isn't hash-relevant).
    store = _store(tmp_path, "f")
    store.upsert_submission({"submission_id": "sub_u", "repo_url": "r", "commit_hash": "h",
                             "epoch": 1, "hotkey": "5X", "round_id": "rr", "status": "wat"})
    got = store.list_by_round("rr")
    assert len(got) == 1 and got[0].status is SubmissionStatus.QUEUED


def test_upsert_survives_reload(tmp_path):
    p = tmp_path / "f.json"
    SubmissionStore(persist_path=p).upsert_submission(
        {"submission_id": "sub_keep", "repo_url": "r", "commit_hash": "h", "epoch": 3,
         "hotkey": "5X", "round_id": "rr", "status": "scored"})
    reloaded = SubmissionStore(persist_path=p)  # rebuilds record + indexes from disk
    assert reloaded.get("sub_keep") is not None
    assert reloaded.get_by_hotkey_round("5X", "rr").submission_id == "sub_keep"


# ── batch upsert: skip-bad + persist-once ────────────────────────────────────

def test_upsert_submissions_batch_skips_bad_and_persists_once(tmp_path):
    p = tmp_path / "f.json"
    store = SubmissionStore(persist_path=p)
    persists = {"n": 0}
    orig = store._persist_records
    store._persist_records = lambda subs: (persists.__setitem__("n", persists["n"] + 1), orig(subs))[1]
    n = store.upsert_submissions([
        {"submission_id": "sub_a", "repo_url": "r", "commit_hash": "h", "epoch": 1,
         "hotkey": "5A", "round_id": "rr", "status": "scored"},
        {"repo_url": "r", "commit_hash": "h", "epoch": 1, "hotkey": "5B"},  # bad: no id
        {"submission_id": "sub_c", "repo_url": "r", "commit_hash": "h", "epoch": 1,
         "hotkey": "5C", "round_id": "rr", "status": "rejected"},
    ])
    assert n == 2  # the bad record was skipped
    assert persists["n"] == 1  # ONE persist for the whole batch (not O(n))
    assert {s.submission_id for s in store.list_by_round("rr")} == {"sub_a", "sub_c"}


# ── invariant 2: pack-payload parity after snapshot upsert ────────────────────

def test_follower_snapshot_reproduces_leader_pack_payload(tmp_path):
    rid = "round-e100-n0"
    leader = _store(tmp_path, "leader")
    _seed_leader(leader, rid)
    follower = _store(tmp_path, "follower")
    follower.upsert_submissions([s.to_dict() for s in leader.list_by_round(rid)])
    assert _pack_submission_payload(follower, rid) == _pack_submission_payload(leader, rid)


def test_parity_holds_for_empty_round(tmp_path):
    leader, follower = _store(tmp_path, "leader"), _store(tmp_path, "follower")
    assert _pack_submission_payload(follower, "rZ") == _pack_submission_payload(leader, "rZ") == []


def test_parity_unaffected_by_extra_follower_local_records(tmp_path):
    rid = "round-e100-n0"
    leader = _store(tmp_path, "leader")
    _seed_leader(leader, rid)
    follower = _store(tmp_path, "follower")
    follower.upsert_submission({"submission_id": "sub_other", "repo_url": "r",
                                "commit_hash": "h", "epoch": 1, "hotkey": "5Z",
                                "round_id": "some-other-round", "status": "scored"})
    follower.upsert_submissions([s.to_dict() for s in leader.list_by_round(rid)])
    assert _pack_submission_payload(follower, rid) == _pack_submission_payload(leader, rid)


# ── transport model ──────────────────────────────────────────────────────────

def test_close_request_submissions_default_none():
    assert CloseRoundRequest(round_id="rr", close_epoch=100).submissions is None


def test_close_request_rejects_oversize_snapshot():
    too_many = [{"submission_id": f"s{i}"} for i in range(1025)]
    with pytest.raises(ValidationError):
        CloseRoundRequest(round_id="rr", close_epoch=100, submissions=too_many)


# ── follower handler: idempotency-first (no stale re-upsert) ──────────────────

def test_sync_close_skips_upsert_when_already_closed(monkeypatch):
    """A late/duplicate close on an already-closed round must NOT re-upsert."""
    calls = {"upsert": 0}
    fake_store = SimpleNamespace(
        upsert_submissions=lambda recs: calls.__setitem__("upsert", calls["upsert"] + 1) or len(recs))
    closed_round = SimpleNamespace(status=RoundStatus.CLOSED)
    monkeypatch.setattr(rm, "get_store", lambda: fake_store)
    monkeypatch.setattr(rm, "get_round_store", lambda: SimpleNamespace(get_round=lambda rid: closed_round))
    monkeypatch.setattr(rm, "_close_solver_round_state", lambda body: (_ for _ in ()).throw(AssertionError("should not close")))

    body = CloseRoundRequest(round_id="rr", close_epoch=100,
                             submissions=[{"submission_id": "sub_x"}])
    out = rm._sync_close_solver_round_state(body)
    assert out is closed_round
    assert calls["upsert"] == 0  # idempotency check ran BEFORE any upsert


def test_sync_close_force_heals_snapshot_when_already_closed(monkeypatch):
    """A FORCED close (champion re-attest) on an already-closed round re-upserts
    the snapshot — healing a follower that adopted the round shell but missed the
    original snapshot (down/mid-restart at first close). Without this the re-attest
    lever is a no-op for exactly that stuck state: certify-prepare can't find the
    candidate, the round never leaves CLOSED, and every re-attest 409s (observed
    fleet-wide 2026-07-02, round-e29716562-n1). The round FSM must stay untouched."""
    calls = {"upsert": 0}
    fake_store = SimpleNamespace(
        upsert_submissions=lambda recs: calls.__setitem__("upsert", len(recs)) or len(recs))
    closed_round = SimpleNamespace(status=RoundStatus.CLOSED)
    monkeypatch.setattr(rm, "get_store", lambda: fake_store)
    monkeypatch.setattr(rm, "get_round_store", lambda: SimpleNamespace(get_round=lambda rid: closed_round))
    monkeypatch.setattr(rm, "_close_solver_round_state", lambda body: (_ for _ in ()).throw(AssertionError("should not close")))

    body = CloseRoundRequest(round_id="rr", close_epoch=100, force=True,
                             submissions=[{"submission_id": "sub_x"}])
    out = rm._sync_close_solver_round_state(body)
    assert out is closed_round  # FSM untouched — still the early return
    assert calls["upsert"] == 1  # but the snapshot was healed


def test_sync_close_force_unaborts_the_round(monkeypatch):
    """A FORCED close on a locally-ABORTED round reverts it to CLOSED so the
    forced certify can proceed. A follower whose decision deadline elapsed before
    the certificate arrived aborts locally; left ABORTED, certify-prepare (which
    only advances CLOSED/REPLAYING) 409s forever — wedging both the re-attest
    push and the pull reconcile (observed live 2026-07-02, round-e29716673-n1)."""
    aborted = SimpleNamespace(status=RoundStatus.ABORTED)
    closed = SimpleNamespace(status=RoundStatus.CLOSED)
    calls = {"set": None, "upsert": 0}

    def set_round_status(rid, status):
        calls["set"] = (rid, status)
        return closed

    fake_round_store = SimpleNamespace(
        get_round=lambda rid: aborted, set_round_status=set_round_status)
    monkeypatch.setattr(rm, "get_round_store", lambda: fake_round_store)
    monkeypatch.setattr(rm, "get_store", lambda: SimpleNamespace(
        upsert_submissions=lambda recs: calls.__setitem__("upsert", len(recs)) or len(recs)))
    monkeypatch.setattr(rm, "_close_solver_round_state", lambda body: (_ for _ in ()).throw(AssertionError("should not close")))

    body = CloseRoundRequest(round_id="rr", close_epoch=100, force=True,
                             submissions=[{"submission_id": "sub_x"}])
    out = rm._sync_close_solver_round_state(body)
    assert calls["set"] == ("rr", RoundStatus.CLOSED)
    assert calls["upsert"] == 1  # snapshot still healed first
    assert out is closed


def test_sync_close_unforced_leaves_aborted_round_alone(monkeypatch):
    """Without force, an aborted round is untouched (normal idempotency)."""
    aborted = SimpleNamespace(status=RoundStatus.ABORTED)
    fake_round_store = SimpleNamespace(
        get_round=lambda rid: aborted,
        set_round_status=lambda *a: (_ for _ in ()).throw(AssertionError("no status change")))
    monkeypatch.setattr(rm, "get_round_store", lambda: fake_round_store)
    monkeypatch.setattr(rm, "get_store", lambda: SimpleNamespace(
        upsert_submissions=lambda recs: (_ for _ in ()).throw(AssertionError("no upsert"))))
    monkeypatch.setattr(rm, "_close_solver_round_state", lambda body: (_ for _ in ()).throw(AssertionError("should not close")))

    body = CloseRoundRequest(round_id="rr", close_epoch=100,
                             submissions=[{"submission_id": "sub_x"}])
    assert rm._sync_close_solver_round_state(body) is aborted


def test_sync_close_force_without_snapshot_stays_noop(monkeypatch):
    """Force with NO submissions payload changes nothing (no upsert, no close)."""
    fake_store = SimpleNamespace(
        upsert_submissions=lambda recs: (_ for _ in ()).throw(AssertionError("no upsert expected")))
    closed_round = SimpleNamespace(status=RoundStatus.CLOSED)
    monkeypatch.setattr(rm, "get_store", lambda: fake_store)
    monkeypatch.setattr(rm, "get_round_store", lambda: SimpleNamespace(get_round=lambda rid: closed_round))
    monkeypatch.setattr(rm, "_close_solver_round_state", lambda body: (_ for _ in ()).throw(AssertionError("should not close")))

    body = CloseRoundRequest(round_id="rr", close_epoch=100, force=True)
    assert rm._sync_close_solver_round_state(body) is closed_round


def test_sync_close_upserts_on_first_close(monkeypatch):
    """First close (round OPEN / unknown) mirrors the snapshot, then closes."""
    calls = {"upsert": 0, "closed": False}
    fake_store = SimpleNamespace(
        upsert_submissions=lambda recs: calls.__setitem__("upsert", len(recs)) or len(recs))
    sentinel = SimpleNamespace(status=RoundStatus.CLOSED)
    monkeypatch.setattr(rm, "get_store", lambda: fake_store)
    monkeypatch.setattr(rm, "get_round_store", lambda: SimpleNamespace(get_round=lambda rid: None))
    monkeypatch.setattr(rm, "_close_solver_round_state",
                        lambda body: calls.__setitem__("closed", True) or sentinel)

    body = CloseRoundRequest(round_id="rr", close_epoch=100,
                             submissions=[{"submission_id": "sub_x"}, {"submission_id": "sub_y"}])
    out = rm._sync_close_solver_round_state(body)
    assert out is sentinel
    assert calls["upsert"] == 2 and calls["closed"] is True


def test_close_payload_always_carries_snapshot(monkeypatch):
    """The close-sync payload ALWAYS embeds the submission snapshot now — the
    SUBMISSION_SNAPSHOT_SYNC env gate was removed (it's required for cross-host
    pack-hash parity). No env is set here, yet the snapshot must be present."""
    monkeypatch.delenv("SUBMISSION_SNAPSHOT_SYNC", raising=False)
    fake_sub = SimpleNamespace(to_dict=lambda: {"submission_id": "sub_a", "round_id": "round-1"})
    monkeypatch.setattr(rm, "get_store",
                        lambda: SimpleNamespace(list_by_round=lambda rid: [fake_sub]))
    state = SimpleNamespace(
        round_id="round-1", close_epoch=1, benchmark_pack_hash="p", committee_block=1,
        committee_hash="c", quorum_required=1, decision_deadline_epoch=2, effective_epoch=3,
    )
    payload = rm._close_round_sync_payload(state)
    assert payload["submissions"] == [{"submission_id": "sub_a", "round_id": "round-1"}]
