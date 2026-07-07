"""benchmark_details retention keeps the persisted submission store bounded so
_persist can't re-serialize a 142MB blob on the event loop (~25s api freezes)."""
import json

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


def test_persist_is_compact_and_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(ss, "_BENCHMARK_DETAILS_RETENTION", 2)
    p = tmp_path / "submissions.json"
    store = SubmissionStore(persist_path=p)
    for e in range(1, 8):
        _mk(store, e, SubmissionStatus.SCORED)
    # create() reloads from disk on each write, dropping post-create in-memory
    # edits — set status/details on the live stored objects before persisting.
    for sub in store._submissions.values():
        sub.status = SubmissionStatus.SCORED
        sub.benchmark_details = {"per_order": ["x"] * 50}
    store._persist()
    text = p.read_text()
    assert "\n" not in text, "must be compact json (no indent)"
    data = json.loads(text)
    assert len(data) == 7, f"all records retained, only details pruned: {len(data)}"
    with_details = [v for v in data.values() if v.get("benchmark_details")]
    assert len(with_details) == 2, f"retention cap not applied on persist: {len(with_details)}"
