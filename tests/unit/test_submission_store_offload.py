"""SubmissionStore async persist offload + copy-on-write + missing decorators.

The 44MB submissions.json was re-read (json.loads) and re-serialized (json.dumps)
on the event loop on every mutation. The offload moves the whole locked read-
modify-write onto a dedicated writer thread (``aoffload``); that is only safe
with (a) copy-on-write on ``_submissions`` size changes so lock-free loop reads
never hit "dictionary changed size during iteration", and (b) the previously
undecorated mutators (``set_max_region_nodes`` / ``upsert_*``) taking the same
cross-process write lock as every other mutator.
"""
from __future__ import annotations

import asyncio
import json
import threading

from minotaur_subnet.harness.submission_store import (
    SubmissionStore,
    SubmissionStatus,
    offload_write,
)


_ctr = [0]


def _mk(store, *, round_id="round-e0-n1", status=None):
    _ctr[0] += 1
    n = _ctr[0]
    sub = store.create(
        repo_url=f"https://example.com/r{n}.git",
        commit_hash=f"{n:040d}",
        epoch=0,
        hotkey=f"hk{n}",
        round_id=round_id,
        max_per_round=0,
        max_rounds_per_commit=0,
    )
    if status is not None:
        store.update_status(sub.submission_id, status)
    return sub


# ── the previously-undecorated mutators now take the write lock ──────────────

def test_undecorated_mutators_now_write_locked():
    # _write_locked wraps via functools.wraps → the wrapper exposes __wrapped__.
    for name in ("set_max_region_nodes", "upsert_submission", "upsert_submissions"):
        method = getattr(SubmissionStore, name)
        assert hasattr(method, "__wrapped__"), f"{name} is not @_write_locked"


def test_two_writer_no_lost_update(tmp_path):
    """Two processes sharing one file: a benchmark write on one and a
    max_region_nodes write on the other must not clobber each other. Before the
    decorator fix, set_max_region_nodes skipped the flock and its whole-file
    rewrite could drop a concurrent scored result."""
    path = tmp_path / "submissions.json"
    w1 = SubmissionStore(persist_path=path)
    a = _mk(w1)
    b = _mk(w1)

    w2 = SubmissionStore(persist_path=path)
    w2._maybe_reload()

    def hammer_benchmark():
        for _ in range(200):
            w1.set_benchmark_result(a.submission_id, valid=True, details={"ok": 1})

    def hammer_region():
        for i in range(200):
            w2.set_max_region_nodes(b.submission_id, i)

    t1 = threading.Thread(target=hammer_benchmark)
    t2 = threading.Thread(target=hammer_region)
    t1.start(); t2.start()
    t1.join(); t2.join()

    fresh = SubmissionStore(persist_path=path)
    fresh._maybe_reload()
    ra = fresh.get(a.submission_id)
    rb = fresh.get(b.submission_id)
    # Neither write was lost: A carries its benchmark verdict, B its metric.
    assert ra is not None and ra.status == SubmissionStatus.SCORED
    assert rb is not None and rb.max_region_nodes == 199


# ── copy-on-write keeps lock-free loop reads safe against off-loop writes ─────

def test_cow_read_safe_under_concurrent_offloaded_create(tmp_path):
    async def _run():
        store = SubmissionStore(persist_path=tmp_path / "submissions.json")
        for _ in range(40):
            _mk(store)
        errors: list[BaseException] = []
        stop = False

        async def reader():
            while not stop:
                try:
                    # iterates _submissions.values() lock-free on the loop thread
                    store.list_by_status(SubmissionStatus.QUEUED)
                    store.list_by_round("round-e0-n1")
                except RuntimeError as exc:  # "dictionary changed size ..."
                    errors.append(exc)
                # Real (tiny) yield: releases the GIL so the writer's executor
                # thread isn't starved by a busy sleep(0) loop, while still
                # interleaving reads with the concurrent offloaded writes.
                await asyncio.sleep(0.0005)

        async def writer():
            nonlocal stop
            for i in range(150):
                # offloaded create → in-place-forbidden insert on the writer thread
                await offload_write(
                    store.create,
                    repo_url=f"https://example.com/c{i}.git",
                    commit_hash=f"c{i:039d}",
                    epoch=0,
                    hotkey=f"c{i}",
                    round_id="round-e0-n1",
                    max_per_round=0,
                    max_rounds_per_commit=0,
                )
            stop = True

        r = asyncio.create_task(reader())
        await writer()
        await r
        assert errors == [], errors[:3]

    asyncio.run(_run())


# ── aoffload: durable + awaited + genuinely off the event loop ───────────────

def test_aoffload_write_is_durable_after_await(tmp_path):
    async def _run():
        path = tmp_path / "submissions.json"
        store = SubmissionStore(persist_path=path)
        sub = await offload_write(
            store.create,
            repo_url="https://example.com/x.git",
            commit_hash="x" * 40,
            epoch=0,
            hotkey="hk",
            round_id="round-e0-n1",
            max_per_round=0,
            max_rounds_per_commit=0,
        )
        await offload_write(store.set_max_region_nodes, sub.submission_id, 4242)
        # The per-record write has landed in the DB by the time the await returns:
        # a fresh store loading from the same DB sees it.
        store2 = SubmissionStore(persist_path=path)
        assert store2.get(sub.submission_id).max_region_nodes == 4242

    asyncio.run(_run())


def test_aoffload_runs_on_writer_thread(tmp_path):
    async def _run():
        store = SubmissionStore(persist_path=tmp_path / "submissions.json")
        main = threading.current_thread().name
        worker = await store.aoffload(lambda: threading.current_thread().name)
        assert worker != main
        assert worker.startswith("substore-writer")

    asyncio.run(_run())


def test_set_benchmark_ranks_batch_persists_once(tmp_path):
    """The ranking pass must set N ranks in ONE persist, not N whole-store writes."""
    path = tmp_path / "submissions.json"
    store = SubmissionStore(persist_path=path)
    subs = [_mk(store, status=SubmissionStatus.SCORED) for _ in range(5)]
    ids = [s.submission_id for s in subs]

    persists = {"n": 0}
    real = store._persist_records

    def counting(subs):
        persists["n"] += 1
        real(subs)

    store._persist_records = counting
    store.set_benchmark_ranks({sid: i + 1 for i, sid in enumerate(ids)})

    assert persists["n"] == 1  # ONE per-record batch write for the whole ranking
    for i, sid in enumerate(ids):
        assert store.get(sid).benchmark_rank == i + 1
    # Unknown ids are skipped, not raised.
    store._persist_records = real
    store.set_benchmark_ranks({"sub_does_not_exist": 9})
    assert store.get(ids[0]).benchmark_rank == 1


def test_offload_write_falls_back_inline_for_store_without_aoffload():
    """A test double / stub store (no aoffload) runs its sync mutator inline —
    call sites stay decoupled from the store's async surface."""
    calls = []

    class StubStore:
        def reject(self, submission_id, reason, **kw):
            calls.append((submission_id, reason))
            return "ok"

    async def _run():
        stub = StubStore()
        result = await offload_write(stub.reject, "sub_1", "nope")
        assert result == "ok"
        assert calls == [("sub_1", "nope")]

    asyncio.run(_run())
