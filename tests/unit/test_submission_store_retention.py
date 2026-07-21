"""benchmark_details retention keeps the persisted submission store bounded so
_persist can't re-serialize a 142MB blob on the event loop (~25s api freezes)."""

from minotaur_subnet.harness import submission_store as ss
from minotaur_subnet.harness.submission_store import SubmissionStore, SubmissionStatus


_ctr = [0]


def _mk(store, epoch, status):
    _ctr[0] += 1
    n = _ctr[0]
    sub = store.create(
        repo_url=f"https://example.com/r{n}.git",
        commit_hash=f"{n:040d}",          # unique per submission → no dedup
        epoch=epoch,
        hotkey=f"hk{n}",
        round_id=f"round-{n}",
        max_per_round=0,
        max_rounds_per_commit=0,
    )
    sub.status = status
    sub.benchmark_details = {"per_order": ["x"] * 50, "epoch": epoch}
    return sub


def test_retention_strips_old_terminal_keeps_recent_and_active(monkeypatch):
    monkeypatch.setattr(ss, "_BENCHMARK_DETAILS_RETENTION", 3)
    store = SubmissionStore(persist_path=None)
    scored = [_mk(store, e, SubmissionStatus.SCORED) for e in range(1, 6)]  # epochs 1..5
    active = [_mk(store, 2, SubmissionStatus.BENCHMARKING),
              _mk(store, 1, SubmissionStatus.SCREENING_STAGE_2)]

    store._enforce_benchmark_details_retention()

    # 3 most-recent scored (epochs 5,4,3) keep details; older (2,1) stripped.
    kept = {s.epoch for s in scored if s.benchmark_details is not None}
    assert kept == {5, 4, 3}, kept
    assert all(s.benchmark_details is None for s in scored if s.epoch in (1, 2))
    # Non-terminal (in-flight) submissions are never stripped.
    assert all(s.benchmark_details is not None for s in active)


def test_retention_noop_under_cap(monkeypatch):
    monkeypatch.setattr(ss, "_BENCHMARK_DETAILS_RETENTION", 100)
    store = SubmissionStore(persist_path=None)
    subs = [_mk(store, e, SubmissionStatus.REJECTED) for e in range(1, 6)]
    store._enforce_benchmark_details_retention()
    assert all(s.benchmark_details is not None for s in subs)


def test_retention_disabled_when_zero(monkeypatch):
    monkeypatch.setattr(ss, "_BENCHMARK_DETAILS_RETENTION", 0)
    store = SubmissionStore(persist_path=None)
    subs = [_mk(store, e, SubmissionStatus.SCORED) for e in range(1, 11)]
    store._enforce_benchmark_details_retention()
    assert all(s.benchmark_details is not None for s in subs)


def test_persist_is_bounded_on_disk_via_reload(tmp_path, monkeypatch):
    """Per-record persist + retention on disk: ALL records are kept but only the
    details of the cap-most-recent terminal records survive — verified by
    reloading a fresh store from the SQLite DB."""
    monkeypatch.setattr(ss, "_BENCHMARK_DETAILS_RETENTION", 2)
    p = tmp_path / "submissions.json"
    store = SubmissionStore(persist_path=p)
    for e in range(1, 8):
        sub = store.create(
            repo_url=f"https://example.com/r{e}.git",
            commit_hash=f"{e:040d}",
            epoch=e,
            hotkey=f"hk{e}",
            round_id=f"round-{e}",
            max_per_round=0,
            max_rounds_per_commit=0,
        )
        store.update_status(sub.submission_id, SubmissionStatus.BENCHMARKING)
        # SCORED + details, persisted per-record through the real write path.
        store.set_benchmark_result(
            sub.submission_id, valid=True,
            details={"per_intent": [{"raw_output": "1"}], "epoch": e},
        )

    # Reload a fresh store from the same DB — retention must hold on disk.
    store2 = SubmissionStore(persist_path=p)
    assert len(store2._submissions) == 7, "all records retained"
    with_details = [s for s in store2._submissions.values() if s.benchmark_details]
    assert len(with_details) == 2, f"retention cap not applied on disk: {len(with_details)}"


def test_record_retention_prunes_old_terminal_keeps_champion_and_inflight(tmp_path, monkeypatch):
    """SUBMISSIONS_MAX_RECORDS caps the record count at load: the OLDEST terminal
    rows beyond the cap are hard-deleted from the DB, while every in-flight
    submission and every ADOPTED champion survives regardless of age."""
    monkeypatch.setattr(ss, "_MAX_RECORDS", 3)
    p = tmp_path / "submissions.json"
    store = SubmissionStore(persist_path=p)

    def add(created_at, status):
        _ctr[0] += 1
        n = _ctr[0]
        sub = store.create(
            repo_url=f"https://example.com/r{n}.git", commit_hash=f"{n:040d}",
            epoch=n, hotkey=f"hk{n}", round_id=f"round-{n}",
            max_per_round=0, max_rounds_per_commit=0,
        )
        sub.created_at = float(created_at)
        store.update_status(sub.submission_id, status)
        return sub

    champ = add(100, SubmissionStatus.ADOPTED)                               # OLD champion
    scored = [add(200 + i, SubmissionStatus.SCORED) for i in range(5)]       # ascending age
    active = add(999, SubmissionStatus.BENCHMARKING)                         # newest, in-flight

    # Reload → load-time retention prunes to cap=3 (drops the 4 OLDEST scored).
    store2 = SubmissionStore(persist_path=p)
    ids = set(store2._submissions)
    assert len(ids) == 3, sorted(ids)
    assert champ.submission_id in ids, "ADOPTED champion pruned despite being oldest"
    assert active.submission_id in ids, "in-flight submission pruned"
    assert scored[-1].submission_id in ids, "newest scored pruned"
    assert all(s.submission_id not in ids for s in scored[:4]), "old scored survived"

    # The DB itself is bounded — a second fresh reload sees only the retained rows.
    store3 = SubmissionStore(persist_path=p)
    assert len(store3._submissions) == 3


def test_record_retention_off_by_default(tmp_path, monkeypatch):
    """With the cap unset/0, every record is retained (no prune)."""
    monkeypatch.setattr(ss, "_MAX_RECORDS", 0)
    p = tmp_path / "submissions.json"
    store = SubmissionStore(persist_path=p)
    for e in range(1, 11):
        sub = store.create(
            repo_url=f"https://example.com/z{e}.git", commit_hash=f"{e + 500:040d}",
            epoch=e, hotkey=f"zk{e}", round_id=f"zround-{e}",
            max_per_round=0, max_rounds_per_commit=0,
        )
        store.update_status(sub.submission_id, SubmissionStatus.REJECTED)
    store2 = SubmissionStore(persist_path=p)
    assert len(store2._submissions) == 10
