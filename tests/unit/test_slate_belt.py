"""Slate belt reads the RECORDED rotation slate (PR-A race fix).

The double-bench bug (2026-07-08, round-e29724975-n1): the benchmark worker's
belt recomputed select_rotation_slate against a ledger the close had already
advanced (mark_selected), so it picked a DISJOINT slate and benched a second
trio → 4 scored on 3 slots. The fix records the slate on the round at close;
the belt reads it instead of recomputing.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from minotaur_subnet.harness.benchmark_worker import BenchmarkWorker
from minotaur_subnet.harness.round_store import RoundState, RoundStore, RoundStatus


def _sub(hotkey, sid, status="benchmarking"):
    return SimpleNamespace(
        submission_id=sid, hotkey=hotkey,
        status=SimpleNamespace(value=status), benchmark_details=None,
    )


class _SubStore:
    def __init__(self, subs):
        self._subs = subs

    def list_by_round(self, round_id):
        return list(self._subs)


def _worker(sub_store, round_store):
    w = BenchmarkWorker.__new__(BenchmarkWorker)
    w._sub_store = sub_store
    w._round_store = round_store
    return w


def _round_store_with(tmp_path, round_id, slate):
    rs = RoundStore(persist_path=tmp_path / "rounds.json")
    rs._rounds[round_id] = RoundState(round_id=round_id, status=RoundStatus.REPLAYING)
    rs._current_round_id = round_id
    if slate is not None:
        rs.set_benched_slate(round_id, slate)
    return rs


def test_belt_reads_recorded_slate_not_recompute(tmp_path, monkeypatch):
    # 9 live submissions, slate width 3, RECORDED slate = {A,B,C}. The belt must
    # keep exactly {A,B,C} regardless of what a recomputation would pick.
    monkeypatch.setenv("SOLVER_ROUND_MAX_SUBMISSIONS", "3")
    subs = [_sub(f"hk{i}", f"sub_{c}") for i, c in enumerate("ABCDEFGHI")]
    recorded = ["sub_A", "sub_B", "sub_C"]
    w = _worker(_SubStore(subs), _round_store_with(tmp_path, "r1", recorded))
    kept = w._cap_to_rotation_slate(subs, "r1")
    assert {s.submission_id for s in kept} == set(recorded)


def test_belt_recorded_slate_survives_ledger_advance(tmp_path, monkeypatch):
    # The regression: even if a recomputation WOULD pick a different trio (ledger
    # advanced so the recorded winners now look recently-benched), the recorded
    # slate wins. We prove it by recording a slate that pure LRU would NOT pick.
    monkeypatch.setenv("SOLVER_ROUND_MAX_SUBMISSIONS", "3")
    subs = [_sub(f"hk{i}", f"sub_{c}") for i, c in enumerate("ABCDEFGHI")]
    # Record a deliberately "wrong-by-LRU" slate — the tail three.
    recorded = ["sub_G", "sub_H", "sub_I"]
    w = _worker(_SubStore(subs), _round_store_with(tmp_path, "r1", recorded))
    kept = w._cap_to_rotation_slate(subs, "r1")
    assert {s.submission_id for s in kept} == set(recorded)
    assert len(kept) == 3


def test_belt_noop_when_within_slots(tmp_path, monkeypatch):
    monkeypatch.setenv("SOLVER_ROUND_MAX_SUBMISSIONS", "3")
    subs = [_sub("hk0", "sub_A"), _sub("hk1", "sub_B")]
    w = _worker(_SubStore(subs), _round_store_with(tmp_path, "r1", ["sub_A"]))
    assert w._cap_to_rotation_slate(subs, "r1") is subs  # untouched


def test_belt_falls_back_to_recompute_without_recorded_slate(tmp_path, monkeypatch):
    # No recorded slate (pre-field / rotation never ran) → recompute path caps
    # to slots deterministically. Never-benched all tie, salted-hash picks 3.
    monkeypatch.setenv("SOLVER_ROUND_MAX_SUBMISSIONS", "3")
    subs = [_sub(f"hk{i}", f"sub_{c}") for i, c in enumerate("ABCDEFGHI")]
    w = _worker(_SubStore(subs), _round_store_with(tmp_path, "r1", None))
    kept = w._cap_to_rotation_slate(subs, "r1")
    assert len(kept) == 3  # capped even without a recorded slate


def test_recorded_slate_roundtrips_on_round_store(tmp_path):
    rs = RoundStore(persist_path=tmp_path / "rounds.json")
    rs._rounds["r1"] = RoundState(round_id="r1")
    rs.set_benched_slate("r1", ["sub_A", "sub_B"])
    reloaded = RoundStore(persist_path=tmp_path / "rounds.json")
    assert reloaded.get_round("r1").benched_slate == ["sub_A", "sub_B"]
