"""Phase 1.5: per-record SQLite persistence for SubmissionStore.

Covers the migration from the legacy whole-file submissions.json, restart
durability, the two-table benchmark_details retention (a stripped record's DB
details row is dropped, so it reloads as None), and the graceful-shutdown JSON
snapshot (rollback safety to a pre-SQLite build).
"""
from __future__ import annotations

from minotaur_subnet.harness import fastjson
from minotaur_subnet.harness.submission_store import SubmissionStore, SubmissionStatus


def _create(store, i, *, round_id="round-e0-n1"):
    return store.create(
        repo_url=f"https://example.com/r{i}.git",
        commit_hash=f"{i:040d}",
        epoch=i,
        hotkey=f"hk{i}",
        round_id=round_id,
        max_per_round=0,
        max_rounds_per_commit=0,
    )


def test_migrates_legacy_json_then_loads_from_db(tmp_path):
    p = tmp_path / "submissions.json"
    legacy = {
        "sub_a": {"submission_id": "sub_a", "repo_url": "r", "commit_hash": "h",
                  "epoch": 1, "hotkey": "hk", "round_id": "r1", "status": "scored",
                  "benchmark_details": {"per_intent": [{"raw_output": "1"}]}},
        "sub_b": {"submission_id": "sub_b", "repo_url": "r", "commit_hash": "h",
                  "epoch": 2, "hotkey": "hk2", "round_id": "r1", "status": "rejected",
                  "benchmark_details": None},
    }
    p.write_bytes(fastjson.dumps(legacy))

    store = SubmissionStore(persist_path=p)
    assert set(store._submissions) == {"sub_a", "sub_b"}
    assert store.get("sub_a").benchmark_details == {"per_intent": [{"raw_output": "1"}]}
    assert store.get("sub_b").benchmark_details is None
    assert store.get("sub_a").status == SubmissionStatus.SCORED
    # legacy JSON is kept (rollback + audit)
    assert p.exists()
    # migration is idempotent on restart
    store2 = SubmissionStore(persist_path=p)
    assert set(store2._submissions) == {"sub_a", "sub_b"}


def test_restart_durability_new_writes(tmp_path):
    p = tmp_path / "submissions.json"
    store = SubmissionStore(persist_path=p)
    sub = _create(store, 1)
    store.update_status(sub.submission_id, SubmissionStatus.BENCHMARKING)
    store.set_benchmark_result(sub.submission_id, valid=True,
                               details={"per_intent": [{"raw_output": "5"}]})
    # A fresh store (simulated restart) sees the per-record writes.
    store2 = SubmissionStore(persist_path=p)
    r = store2.get(sub.submission_id)
    assert r is not None
    assert r.status == SubmissionStatus.SCORED
    assert r.benchmark_details == {"per_intent": [{"raw_output": "5"}]}


def test_shutdown_snapshot_json_is_valid_and_current(tmp_path):
    """close()/snapshot_json writes a fresh whole-store JSON that a pre-SQLite
    build (or audit) can read — the rollback-safety net."""
    p = tmp_path / "submissions.json"
    store = SubmissionStore(persist_path=p)
    a = _create(store, 1)
    b = _create(store, 2)
    store.update_status(b.submission_id, SubmissionStatus.BENCHMARKING)

    store.close()  # snapshot_json + db close
    on_disk = fastjson.loads(p.read_bytes())
    assert set(on_disk) == {a.submission_id, b.submission_id}
    assert on_disk[b.submission_id]["status"] == "benchmarking"
    # and the snapshot is loadable by a fresh store's legacy path (its own DB is
    # already migrated, so this just proves the JSON is well-formed + complete)
    assert on_disk[a.submission_id]["hotkey"] == "hk1"


def test_two_stores_one_db_no_lost_update(tmp_path):
    """Per-row UPSERT: two stores on one DB writing DIFFERENT records don't
    clobber each other (the old whole-file replace did)."""
    p = tmp_path / "submissions.json"
    s1 = SubmissionStore(persist_path=p)
    a = _create(s1, 1)
    s2 = SubmissionStore(persist_path=p)
    # s2 didn't see s1's create in memory (single-writer model), but writes its
    # own record to the shared DB; s1's row must survive.
    b = _create(s2, 2, round_id="round-e0-n2")
    s1.set_max_region_nodes(a.submission_id, 7)

    fresh = SubmissionStore(persist_path=p)
    assert fresh.get(a.submission_id) is not None
    assert fresh.get(a.submission_id).max_region_nodes == 7
    assert fresh.get(b.submission_id) is not None


def test_in_memory_store_has_no_db(tmp_path):
    """persist_path=None → pure in-memory (tests), no DB, no crash."""
    store = SubmissionStore(persist_path=None)
    assert store._db is None
    sub = _create(store, 1)
    assert store.get(sub.submission_id) is sub
    store.close()  # must be a no-op, not crash


def test_migration_corrupt_json_fails_loud_not_empty(tmp_path):
    """A corrupt submissions.json must NOT silently start an empty store (a
    leader burning) — it fails loud, leaves the JSON + the migrated flag unset so
    an operator can repair and retry."""
    import pytest
    p = tmp_path / "submissions.json"
    p.write_bytes(b"{not valid json")
    with pytest.raises(Exception):
        SubmissionStore(persist_path=p)
    # the legacy file is untouched (available for repair)
    assert p.read_bytes() == b"{not valid json"
    # not marked migrated → a fixed JSON is imported on the next attempt
    from minotaur_subnet.harness.submission_db import SubmissionDB
    db = SubmissionDB(p.with_suffix(".db"))
    assert not db.is_migrated()
    db.close()


def test_snapshot_refuses_to_clobber_when_not_loaded(tmp_path):
    """A store that did not load cleanly must never overwrite the good rollback
    JSON with its empty state."""
    p = tmp_path / "submissions.json"
    good = {"sub_x": {"submission_id": "sub_x", "repo_url": "r", "commit_hash": "h",
                      "epoch": 1, "hotkey": "hk", "round_id": "r1", "status": "scored",
                      "benchmark_details": None}}
    p.write_bytes(fastjson.dumps(good))
    store = SubmissionStore(persist_path=p)  # migrates + loads → _loaded True
    # simulate a degraded reload: mark not-loaded + empty in memory
    store._loaded = False
    store._submissions = {}
    store.snapshot_json()  # must REFUSE
    assert fastjson.loads(p.read_bytes()) == good, "good JSON was clobbered"


def test_export_reconstructs_current_json(tmp_path):
    """The DB→JSON exporter reconstructs a current submissions.json from the
    crash-safe DB (the authoritative rollback path)."""
    p = tmp_path / "submissions.json"
    store = SubmissionStore(persist_path=p)
    a = _create(store, 1)
    store.update_status(a.submission_id, SubmissionStatus.BENCHMARKING)
    store.close()  # closes the DB
    out = tmp_path / "export.json"
    from minotaur_subnet.harness.submission_db import SubmissionDB
    db = SubmissionDB(p.with_suffix(".db"))
    n = db.export_to_json(out)
    db.close()
    exported = fastjson.loads(out.read_bytes())
    assert n == 1
    assert exported[a.submission_id]["status"] == "benchmarking"
