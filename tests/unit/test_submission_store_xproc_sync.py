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


# ── review-fix regressions ──────────────────────────────────────────────────
#
# Each of these FAILS against the first cut of this feature. They are the reason
# the adversarial review round happened, so they are kept as the guard rail.


def test_two_concurrent_writers_never_duplicate_a_seq(tmp_path: Path):
    """updated_seq must be unique — it is the ONLY record of what a peer has seen.

    The first cut stamped MAX(updated_seq)+1 inside `with self._conn:`. Python's
    legacy isolation mode emits an implicit BEGIN only before the first DML, so
    that SELECT ran in AUTOCOMMIT holding no write lock: two processes read the
    same MAX and stamped the SAME seq. A peer's watermark then advances past both
    having applied only one → the other row is never pulled again (a benchmark
    result the coordinator never sees). The fix is BEGIN IMMEDIATE.
    """
    import threading

    a = _mk(tmp_path)
    b = _mk(tmp_path)
    errors: list[BaseException] = []

    def hammer(store: SubmissionStore, tag: str) -> None:
        try:
            for i in range(25):
                _create(store, hotkey=f"5G{tag}{i:03d}")
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [
        threading.Thread(target=hammer, args=(a, "a")),
        threading.Thread(target=hammer, args=(b, "b")),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, f"writer raised: {errors!r}"

    conn = sqlite3.connect(str(tmp_path / "submissions.db"))
    rows, distinct = conn.execute(
        "SELECT COUNT(updated_seq), COUNT(DISTINCT updated_seq) FROM submissions"
    ).fetchone()
    conn.close()
    assert rows == 50, f"expected 50 rows, got {rows}"
    assert rows == distinct, (
        f"DUPLICATE updated_seq under two writers ({rows} rows, {distinct} distinct) "
        "— a peer's watermark will skip a row permanently"
    )


def test_failed_pull_does_not_advance_the_watermark(tmp_path: Path):
    """A FAILED pull and 'no peer rows' are opposite instructions.

    The first cut returned [] on exception, which is indistinguishable from "no
    peer rows" → the caller advanced the watermark past rows it never applied →
    permanent silent loss. load_since now returns None on failure.
    """
    api = _mk(tmp_path)
    worker = _mk(tmp_path)

    sub = _create(api, hotkey="5Gpull")
    before = worker._last_seen_seq

    # Simulate a transient read blip on the pull.
    def boom(_seq):
        return None

    worker._db.load_since = boom  # type: ignore[method-assign]
    worker._maybe_reload()
    assert worker._last_seen_seq == before, (
        "the watermark advanced past a FAILED pull — those rows are lost for good"
    )

    # Recovery: once the read works again the same range is retried, not skipped.
    del worker._db.load_since
    assert worker.get(sub.submission_id) is not None, (
        "the row was not recovered after the blip cleared"
    )


def test_bad_peer_row_stalls_loudly_without_skipping(tmp_path: Path, caplog):
    """A row that won't apply must stall the watermark AT it, not be skipped.

    The first cut logged a warning and advanced past the bad row, dropping it
    permanently and hiding the corruption. We now advance only over a contiguous
    applied prefix.
    """
    api = _mk(tmp_path)
    worker = _mk(tmp_path)

    good = _create(api, hotkey="5Ggood")
    worker.get(good.submission_id)  # apply the good row, advance the watermark
    at_good = worker._last_seen_seq

    bad = _create(api, hotkey="5Gbad0")
    later = _create(api, hotkey="5Glate")

    real_upsert = worker._upsert_one

    def selective(record):
        if record.get("submission_id") == bad.submission_id:
            raise ValueError("synthetic undecodable row")
        return real_upsert(record)

    worker._upsert_one = selective  # type: ignore[method-assign]
    with caplog.at_level("ERROR"):
        worker._maybe_reload()

    assert worker._last_seen_seq == at_good, (
        "the watermark advanced past a row that never applied — it is now "
        f"unreachable (stalled at {worker._last_seen_seq}, expected {at_good})"
    )
    assert any("STALLED" in r.message for r in caplog.records), (
        "an unapplyable peer row must be LOUD (error), not a silent skip"
    )
    # The row AFTER the bad one is not applied either — the prefix is contiguous,
    # so nothing beyond the stall point leaks in out of order.
    assert worker._submissions.get(later.submission_id) is None

    # Recovery: once the row applies, the stall clears and everything catches up.
    worker._upsert_one = real_upsert  # type: ignore[method-assign]
    worker._maybe_reload()
    assert worker._last_seen_seq > at_good
    assert worker.get(bad.submission_id) is not None
    assert worker.get(later.submission_id) is not None


def test_concurrent_same_record_rmw_does_not_lose_a_field(tmp_path: Path):
    """Two processes mutating DIFFERENT fields of the SAME record must not lose
    either field.

    #794 removed the cross-process flock, reasoning that "any future
    second-process writer is serialized by SQLite's own write lock". That is not
    enough: SQLite serializes the write TRANSACTION, not the whole
    pull → mutate → persist, and _persist_records UPSERTs the WHOLE record blob.
    Interleaved, one side's field is lost entirely (the live shape: rotation sets
    REJECTED while the worker writes SCORED). The flock in _write_guard is what
    makes the read-modify-write atomic across processes.
    """
    import threading

    api = _mk(tmp_path)
    worker = _mk(tmp_path)

    sub = _create(api, hotkey="5Grmw")
    sid = sub.submission_id
    worker.get(sid)

    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def api_side() -> None:
        try:
            barrier.wait()
            api.set_max_region_nodes(sid, 4242)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    def worker_side() -> None:
        try:
            barrier.wait()
            worker.set_benchmark_result(
                sid, valid=True, rank=7, details={"total_intents": 1}
            )
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=api_side), threading.Thread(target=worker_side)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, f"a writer raised: {errors!r}"

    # A THIRD reader sees the durable truth: both fields survived.
    fresh = _mk(tmp_path).get(sid)
    assert fresh is not None
    assert fresh.max_region_nodes == 4242, "the api's field was clobbered by the worker"
    assert fresh.benchmark_rank == 7, "the worker's field was clobbered by the api"


# ── second-review regressions (the fixes to the fixes) ──────────────────────


def test_stale_offlock_snapshot_does_not_revert_a_newer_write(tmp_path: Path):
    """THE consensus bug: _maybe_reload SELECTs off-lock, then applies in-lock.

    A writer can supersede one of those rows in between (its own _write_guard
    reload advances the watermark past the peer row, then writes a NEWER blob).
    Applying the stale snapshot reverts the newer state in memory — PERMANENTLY,
    because the healing row carries our OWN writer id and load_since filters it
    out forever. The next mutation then UPSERTs the reverted blob back over the
    good DB row, so the DB is not a safety net. That is precisely the
    wrong-champion adoption this whole feature exists to prevent.
    Fixed by re-reading the watermark inside the lock and skipping seq <= it.
    """
    api = _mk(tmp_path)
    worker = _mk(tmp_path)

    sub = _create(api, hotkey="5Grace")
    sid = sub.submission_id
    worker.get(sid)

    # The peer's (older) result — this is what the stale snapshot will carry.
    api.set_benchmark_result(sid, valid=True, rank=1, details={"total_intents": 1})

    real_load_since = worker._db.load_since
    raced = []

    def racing_load_since(seq):
        rows = real_load_since(seq)  # the stale snapshot: rank=1
        if not raced:
            raced.append(True)
            # The race: our own guarded write lands AFTER the SELECT, stamping a
            # NEWER seq and advancing the watermark past the peer's row.
            worker.set_benchmark_result(
                sid, valid=True, rank=7, details={"total_intents": 9}
            )
        return rows

    worker._db.load_since = racing_load_since  # type: ignore[method-assign]
    worker._maybe_reload()
    del worker._db.load_since

    assert worker._submissions[sid].benchmark_rank == 7, (
        "a stale off-lock snapshot reverted a newer write in memory — and it can "
        "NEVER heal (own-writer rows are filtered out of the pull for good)"
    )
    # And the durable row still agrees, so a later mutation can't re-persist stale.
    assert _mk(tmp_path).get(sid).benchmark_rank == 7


def test_peer_holding_the_flock_cannot_freeze_a_reader(tmp_path: Path):
    """Lock ORDER. fcntl.flock(LOCK_EX) blocks indefinitely; taking it while
    holding _rmw_lock let a peer process freeze the api's EVENT LOOP for as long
    as it held the lock file (every read path calls _maybe_reload -> _rmw_lock).
    That is the Phase-1 stall class, re-introduced and made cross-process."""
    import fcntl as _fcntl
    import os as _os
    import threading
    import time

    HOLD = 1.0
    api = _mk(tmp_path)
    worker = _mk(tmp_path)
    sub = _create(api, hotkey="5Gflock")
    sid = sub.submission_id
    lock_path = tmp_path / "submissions.json.lock"

    # A PENDING PEER ROW is the precondition: without one, _maybe_reload returns at
    # the max_seq gate before ever taking _rmw_lock, and the read never contends.
    worker.get(sid)
    worker.set_max_region_nodes(sid, 1234)
    assert api._db.max_seq() > api._last_seen_seq, "precondition: a peer row is pending"

    holding = threading.Event()

    def peer() -> None:
        fd = _os.open(str(lock_path), _os.O_RDWR | _os.O_CREAT, 0o644)
        _fcntl.flock(fd, _fcntl.LOCK_EX)
        holding.set()
        time.sleep(HOLD)
        _fcntl.flock(fd, _fcntl.LOCK_UN)
        _os.close(fd)

    pt = threading.Thread(target=peer, daemon=True)
    pt.start()
    assert holding.wait(3), "peer never took the lock"

    writer_started = threading.Event()

    def writer() -> None:
        writer_started.set()
        api.set_max_region_nodes(sid, 99)  # blocks on the peer's flock

    wt = threading.Thread(target=writer, daemon=True)
    wt.start()
    assert writer_started.wait(3)
    time.sleep(0.25)  # let the writer reach the blocking flock wait

    t0 = time.perf_counter()
    api.get(sid)  # a plain read on the "event loop" — must NOT block
    elapsed = time.perf_counter() - t0

    pt.join(timeout=5)
    wt.join(timeout=5)
    assert elapsed < 0.3, (
        f"a read blocked {elapsed:.2f}s because a writer held _rmw_lock while "
        "waiting on the flock — that is a frozen event loop"
    )


def test_failed_flock_acquire_does_not_silently_disarm_the_lock(tmp_path: Path):
    """A single OSError (EMFILE — this leader has an fd-leak history) used to
    leave _lock_depth incremented forever, so every later guard computed
    outermost=False and ran with NO cross-process lock at all. Silently."""
    store = _mk(tmp_path)
    real_acquire = store._acquire_file_lock

    def boom() -> int:
        raise OSError(24, "Too many open files")

    store._acquire_file_lock = boom  # type: ignore[method-assign]
    with pytest.raises(OSError):
        _create(store, hotkey="5Gboom")

    acquired: list[int] = []

    def counting():
        acquired.append(1)
        return real_acquire()

    store._acquire_file_lock = counting  # type: ignore[method-assign]
    _create(store, hotkey="5Gafter")
    assert acquired, (
        "the flock was silently disarmed for the process lifetime after one "
        "failed acquire — writes now race across processes with no lock"
    )


def test_write_records_stamps_unique_seqs_without_the_store_flock(tmp_path: Path):
    """Drives SubmissionDB DIRECTLY, bypassing the store's _write_guard.

    The flock now serializes the store's writers, which MASKS a BEGIN IMMEDIATE
    regression: test_two_concurrent_writers_never_duplicate_a_seq would stay green
    even if _immediate() were reverted to the legacy `with self._conn:`. This is
    the only test that can actually see that regression, so it is the guard on the
    atomicity of MAX(updated_seq)+1 itself.
    """
    import threading

    from minotaur_subnet.harness.submission_db import SubmissionDB

    db_path = tmp_path / "direct.db"
    dbs = [SubmissionDB(db_path), SubmissionDB(db_path)]

    def hammer(db: SubmissionDB, tag: str) -> None:
        for i in range(25):
            sid = f"sub_{tag}{i:03d}"
            db.write_records([(sid, {"submission_id": sid, "hotkey": f"5G{tag}"})])

    threads = [
        threading.Thread(target=hammer, args=(dbs[0], "a")),
        threading.Thread(target=hammer, args=(dbs[1], "b")),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    for db in dbs:
        db.close()

    conn = sqlite3.connect(str(db_path))
    rows, distinct = conn.execute(
        "SELECT COUNT(updated_seq), COUNT(DISTINCT updated_seq) FROM submissions"
    ).fetchone()
    conn.close()
    assert rows == 50, f"expected 50 rows, got {rows}"
    assert rows == distinct, (
        f"DUPLICATE updated_seq ({rows} rows, {distinct} distinct) — the MAX+1 "
        "read-then-write is not atomic; a peer's watermark will skip a row"
    )


def test_load_takes_the_watermark_before_hydrating(tmp_path: Path):
    """A peer row committed DURING load_all() must still be pulled afterwards.

    Seeding the watermark AFTER the hydrate counts that row as 'seen' even though
    the SELECT never returned it → neither hydrated nor pulled → silently lost
    until it happens to be written again. The api restarts hourly while the worker
    keeps committing, so this window is real.
    """
    api = _mk(tmp_path)
    _create(api, hotkey="5Gaaa")
    worker = _mk(tmp_path)

    real_load_all = worker._db.load_all
    injected: dict[str, str] = {}

    def racing_load_all():
        rows = list(real_load_all())
        # A peer commits mid-hydrate — after the SELECT, before we seed the mark.
        s = _create(api, hotkey="5Gmid")
        injected["sid"] = s.submission_id
        return iter(rows)

    worker._db.load_all = racing_load_all  # type: ignore[method-assign]
    worker._load()
    del worker._db.load_all

    assert injected["sid"] not in worker._submissions, "precondition: not hydrated"
    assert worker.get(injected["sid"]) is not None, (
        "a row committed during the hydrate was never hydrated AND never pulled "
        "— the watermark was seeded after load_all()"
    )
