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
    orig = store._persist
    store._persist = lambda: (persists.__setitem__("n", persists["n"] + 1), orig())[1]
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
