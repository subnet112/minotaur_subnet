"""Cross-process read-sync for the SQLite SubmissionStore (Phase 2 split).

#794 made the store per-record SQLite but kept the in-memory dict as the SOLE
read source, so a SECOND process's writes were invisible — the store was
single-writer by assumption. The Phase-2 split breaks that assumption in BOTH
directions:

  * the benchmark **worker** must see submissions the api just intook
    (QUEUED→BENCHMARKING), or it benches a stale slate;
  * the **api** must see the benchmark results the worker just scored, or the
    coordinator ranks a stale slate and adopts the wrong champion.

Each test drives TWO SubmissionStore instances over ONE db file — the same shape
as the api and worker containers sharing /data.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pytest

from minotaur_subnet.harness.submission_store import (
    SubmissionStore,
    SubmissionStatus,
)


def _mk(tmp_path: Path, name: str = "submissions.json") -> SubmissionStore:
    """A store rooted at the SHARED persist path (→ the same submissions.db)."""
    return SubmissionStore(persist_path=tmp_path / name)


def _create(store: SubmissionStore, hotkey: str = "5Gtest", epoch: int = 1):
    return store.create(
        repo_url="https://github.com/test/solver",
        commit_hash="abc123",
        epoch=epoch,
        hotkey=hotkey,
        round_id="round-e1-n1",
    )


# ── the two directions the split depends on ─────────────────────────────────


def test_worker_sees_submission_the_api_just_created(tmp_path: Path):
    """worker ← api. Without the pull the worker's in-memory dict never learns
    about the new submission and it benches a stale slate."""
    api = _mk(tmp_path)
    worker = _mk(tmp_path)

    sub = _create(api)

    # The worker never wrote this row; it must appear via the cross-process pull.
    assert worker.get(sub.submission_id) is not None, (
        "worker cannot see the api's newly-intaken submission — it would bench a "
        "stale slate"
    )
    assert worker.get(sub.submission_id).hotkey == "5Gtest"


def test_api_sees_benchmark_result_the_worker_just_scored(tmp_path: Path):
    """api ← worker. This is the consensus-relevant direction: the coordinator
    ranks on benchmark_details, so a stale api adopts the wrong champion."""
    api = _mk(tmp_path)
    worker = _mk(tmp_path)

    sub = _create(api)
    sid = sub.submission_id
    worker.get(sid)  # worker pulls it in

    details = {"total_intents": 3, "per_intent": [{"intent_id": "o1", "score": 0.9}]}
    worker.set_benchmark_result(sid, valid=True, rank=1, details=details)

    fresh = api.get(sid)
    assert fresh is not None
    assert fresh.benchmark_details == details, (
        "api cannot see the worker's benchmark result — the coordinator would rank "
        "a stale slate"
    )
    assert fresh.benchmark_rank == 1
    assert fresh.status == SubmissionStatus.SCORED


def test_list_by_status_reflects_peer_writes(tmp_path: Path):
    """The slate query the worker actually uses (list_by_status) must be
    peer-fresh, not just point get()."""
    api = _mk(tmp_path)
    worker = _mk(tmp_path)

    a = _create(api, hotkey="5GaaA")
    b = _create(api, hotkey="5GbbB")
    for sub in (a, b):
        record = sub.to_dict()
        record["status"] = SubmissionStatus.BENCHMARKING.value
        api.upsert_submission(record)

    got = {s.submission_id for s in worker.list_by_status(SubmissionStatus.BENCHMARKING)}
    assert got == {a.submission_id, b.submission_id}


def test_sync_is_incremental_not_a_full_reload(tmp_path: Path):
    """Only rows past the watermark are pulled, and only a PEER's — the watermark
    advances so the same row isn't re-pulled forever."""
    api = _mk(tmp_path)
    worker = _mk(tmp_path)

    s1 = _create(api, hotkey="5G111")
    worker.get(s1.submission_id)          # pulls row 1, advances the watermark
    seq_after_first = worker._last_seen_seq
    assert seq_after_first > 0

    # A read with no peer write in between pulls nothing (the max_seq gate).
    assert worker._db.load_since(worker._last_seen_seq) == []

    s2 = _create(api, hotkey="5G222")
    pending = worker._db.load_since(worker._last_seen_seq)
    assert [sid for sid, _r, _q in pending] == [s2.submission_id], (
        "the pull should carry ONLY the new peer row"
    )
    worker.get(s2.submission_id)
    assert worker._last_seen_seq > seq_after_first


def test_writer_never_repulls_its_own_rows(tmp_path: Path):
    """A single-writer node (monolith leader / every follower) must stay inert:
    its own rows are filtered IN SQL, so no row is ever re-parsed on a read."""
    solo = _mk(tmp_path)
    _create(solo, hotkey="5Gsolo")

    # Its own write moved the seq, but load_since excludes writer==self.
    assert solo._db.load_since(0) == [], (
        "a lone writer re-parsed its own rows on read — needless work on the "
        "monolith's hot read path"
    )
    # And a read still works + advances the watermark past its own write.
    assert len(solo.list_by_status(SubmissionStatus.QUEUED)) == 1
    assert solo._last_seen_seq == solo._db.max_seq()


def test_peer_row_not_skipped_when_we_write_first(tmp_path: Path):
    """Watermark-skip guard: our own write takes a HIGHER seq than an unpulled
    peer row. The watermark must not jump past that peer row (it would be lost
    from memory until a restart)."""
    api = _mk(tmp_path)
    worker = _mk(tmp_path)

    peer = _create(api, hotkey="5Gpeer")            # seq N (worker hasn't pulled)
    mine = _create(worker, hotkey="5Gmine")         # seq N+1, written by worker

    # The worker must still see the api's earlier row.
    assert worker.get(peer.submission_id) is not None, (
        "peer row was skipped because our own later write advanced past it"
    )
    assert worker.get(mine.submission_id) is not None


# ── inertness / safety ──────────────────────────────────────────────────────


def test_kill_switch_restores_the_no_op(tmp_path: Path, monkeypatch):
    """SUBMISSION_STORE_XPROC_SYNC=0 → strict pre-Phase-2 behavior (no pull)."""
    monkeypatch.setenv("SUBMISSION_STORE_XPROC_SYNC", "0")
    api = _mk(tmp_path)
    worker = _mk(tmp_path)
    sub = _create(api)
    assert worker.get(sub.submission_id) is None, "kill switch did not disable the pull"


def test_concurrent_peer_writes_and_reads_no_deadlock_or_loss(tmp_path: Path):
    """The pull takes _rmw_lock for its in-memory merge while a mutator (holding
    _rmw_lock) itself calls _maybe_reload → _read_lock. Prove that ordering can't
    deadlock, that a lock-free reader never trips 'dict changed size during
    iteration' against a concurrent pull, and that nothing is lost."""
    import threading

    api = _mk(tmp_path)
    worker = _mk(tmp_path)
    created: list[str] = []
    errors: list[Exception] = []

    def write_peer():
        try:
            for i in range(40):
                created.append(_create(api, hotkey=f"5Gw{i:03d}").submission_id)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    def read_worker():
        try:
            for _ in range(300):
                worker.list_by_status(SubmissionStatus.QUEUED)
                worker.list_by_round("round-e1-n1")
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    t_w = threading.Thread(target=write_peer)
    t_r = threading.Thread(target=read_worker)
    t_w.start(); t_r.start()
    t_w.join(60); t_r.join(60)

    assert not t_w.is_alive() and not t_r.is_alive(), "deadlock between _rmw_lock/_read_lock"
    assert not errors, f"concurrent read/pull raised: {errors[:3]}"
    seen = {s.submission_id for s in worker.list_by_status(SubmissionStatus.QUEUED)}
    assert set(created) <= seen, "peer rows lost under concurrency"


def test_in_memory_store_has_no_sync(tmp_path: Path):
    """A DB-less store (tests / persist_path=None) must not touch the sync path."""
    s = SubmissionStore()
    assert s._db is None
    s._maybe_reload()  # must not raise
    assert _create(s) is not None


# ── schema migration (the leader's DB ALREADY exists) ───────────────────────


def test_alters_a_pre_phase2_db_in_place(tmp_path: Path):
    """#794 shipped submissions.db WITHOUT updated_seq/writer and it is already
    live on the leader — opening it must ALTER the columns in, not fail, and the
    pre-existing rows (seq 0) must stay readable and never be re-pulled."""
    db_path = tmp_path / "submissions.db"
    # Build a #794-era DB by hand (old schema, no seq/writer columns).
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE submissions (submission_id TEXT PRIMARY KEY, data BLOB NOT NULL);
        CREATE TABLE submission_details (
            submission_id TEXT PRIMARY KEY REFERENCES submissions(submission_id) ON DELETE CASCADE,
            details BLOB NOT NULL);
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        """
    )
    conn.execute(
        "INSERT INTO submissions(submission_id, data) VALUES(?, ?)",
        ("sub_old", b'{"submission_id":"sub_old","repo_url":"r","commit_hash":"c",'
                    b'"epoch":1,"hotkey":"5Gold","round_id":"round-e1-n1",'
                    b'"status":"queued","created_at":1.0,"updated_at":1.0}'),
    )
    conn.execute("INSERT INTO meta(key,value) VALUES('migrated_from_json','1')")
    conn.commit()
    conn.close()

    store = _mk(tmp_path)  # opens the SAME db → must migrate the schema in place

    cols = {r[1] for r in sqlite3.connect(str(db_path)).execute(
        "PRAGMA table_info(submissions)")}
    assert {"updated_seq", "writer"} <= cols, "Phase-2 columns were not ALTERed in"

    # The legacy row survived the migration and is readable.
    old = store.get("sub_old")
    assert old is not None and old.hotkey == "5Gold"
    # Legacy rows carry seq 0 and are already in memory → never re-pulled.
    assert store._db.load_since(0) == []
    # And the upgraded DB still accepts writes (which now carry a real seq).
    new = _create(store, hotkey="5Gnew")
    assert store._db.max_seq() > 0
    assert store.get(new.submission_id) is not None
