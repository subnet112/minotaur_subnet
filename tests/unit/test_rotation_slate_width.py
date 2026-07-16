"""Slate-width enforcement beyond the close-time reject sweep.

The rotation slate is ``SOLVER_ROUND_MAX_SUBMISSIONS`` wide; at close the
overflow is REJECTED. Observed live (2026-07-07, round-e29724243-n1): the api
process was killed mid-sweep — 12 of 19 overflow rejects landed, the 7
un-rejected survivors stayed BENCHMARKING and got benched, inflating the round
to 10 scored on 3 slots. Two independent layers close that:

  1. BELT — benchmark_worker caps a CLOSED/REPLAYING round's bench pass at the
     slate width, re-deriving the SAME slate rotation chose (rotation_sort_key
     over the shared ledger, which a truncated close leaves un-advanced).
  2. CLEANUP — scripts/cleanup_resurrected_submissions.py repairs the pre-#596
     poison (scored records still carrying a "(rotation:" reject reason).
"""
from __future__ import annotations

import asyncio
import importlib.util
import logging
import sys
from pathlib import Path
from types import SimpleNamespace

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from minotaur_subnet.harness.round_store import RoundStatus
from minotaur_subnet.harness.rotation import select_rotation_slate
from minotaur_subnet.harness.submission_store import SubmissionStatus, SubmissionStore

ROUND_ID = "round-e1-n1"


def _store_with_benchmarking(n: int, round_id: str = ROUND_ID):
    store = SubmissionStore(persist_path=None)
    subs = []
    for i in range(n):
        sub = store.create(
            repo_url="https://example.com/r.git",
            commit_hash=f"{i:02d}" * 20,
            epoch=1,
            hotkey=f"hk{i}",
            round_id=round_id,
            max_per_round=0,
            max_rounds_per_commit=0,
        )
        store.update_status(sub.submission_id, SubmissionStatus.BENCHMARKING)
        # Docker-shaped submission so the per-sub loop takes the (stubbed)
        # docker bench path — see _worker_for's fake bench layer.
        store._submissions[sub.submission_id].image_tag = f"solver-{i:02d}:screening"
        subs.append(sub)
    return store, subs


def _worker_for(store, monkeypatch, tmp_path, slots: int = 3):
    from minotaur_subnet.harness.benchmark_worker import BenchmarkWorker

    monkeypatch.setenv("SOLVER_ROUND_MAX_SUBMISSIONS", str(slots))
    monkeypatch.setenv("SOLVER_ROTATION_LEDGER_PATH", str(tmp_path / "rot.json"))
    # No pin resolver is wired in this unit rig; the (default-on) round-anchored
    # pin gate would otherwise defer every pass before reaching the bench loop.
    monkeypatch.setenv("ROUND_ANCHORED_PIN", "0")
    worker = BenchmarkWorker(store, use_docker=False)
    closed = SimpleNamespace(round_id=ROUND_ID, status=RoundStatus.CLOSED)
    monkeypatch.setattr(worker, "_current_replay_round", lambda: closed)
    # Fast fake bench layer: non-empty intents + a stubbed docker bench that
    # returns zero results — every capped-in submission gets a
    # set_benchmark_result validity-reject (the recognisable "bench attempt"
    # touch) with zero sim machinery. Replaces the pre-2026-07-16 trick of
    # forcing the no-intents branch: that branch is now a LOUD config-failure
    # raise and never touches submissions (see
    # test_no_intents_config_failure.py).
    monkeypatch.setattr(worker, "_load_benchmark_intents", lambda *a, **k: [object()])

    async def _score_fn(*_a, **_k):
        return None

    monkeypatch.setattr(worker, "_build_score_fn", lambda intents: _score_fn())
    monkeypatch.setattr(worker, "_enrich_intents_with_manifests", lambda intents: intents)

    async def _no_results(*_a, **_k):
        return []

    monkeypatch.setattr(worker, "_benchmark_submission", _no_results)
    return worker


# ── the belt ──────────────────────────────────────────────────────────────────


def test_closed_round_benches_at_most_the_slate(monkeypatch, caplog, tmp_path):
    """10 BENCHMARKING on 3 slots (a truncated close sweep) → exactly the
    3-wide rotation slate is benched, loudly; the overflow is left alone."""
    store, subs = _store_with_benchmarking(10)
    worker = _worker_for(store, monkeypatch, tmp_path, slots=3)

    with caplog.at_level(logging.WARNING):
        processed = asyncio.run(worker.run_once())

    assert processed == 3
    # The benched trio is EXACTLY the slate rotation would have selected at
    # close (empty ledger — a truncated sweep never reaches mark_selected).
    expected = {s.submission_id for s in select_rotation_slate(subs, 3, {}, ROUND_ID)[0]}
    touched = {
        s.submission_id for s in store.list_by_round(ROUND_ID)
        if s.status != SubmissionStatus.BENCHMARKING
    }
    assert touched == expected
    # The 7 overflow survivors are untouched — NOT benched, NOT auto-rejected.
    survivors = [
        s for s in store.list_by_round(ROUND_ID)
        if s.status == SubmissionStatus.BENCHMARKING
    ]
    assert len(survivors) == 7
    assert all(s.rejection_reason is None for s in survivors)
    assert any(
        "reject sweep must have been truncated" in rec.message
        for rec in caplog.records
    )


def test_belt_does_not_leak_slots_across_passes(monkeypatch, caplog, tmp_path):
    """A slate member that got benched (even validity-rejected) keeps holding
    its slot: repeated passes must not trickle the overflow in 3-sub bites."""
    store, subs = _store_with_benchmarking(10)
    worker = _worker_for(store, monkeypatch, tmp_path, slots=3)

    with caplog.at_level(logging.WARNING):
        assert asyncio.run(worker.run_once()) == 3
        assert asyncio.run(worker.run_once()) == 0  # nothing more to bench
        assert asyncio.run(worker.run_once()) == 0

    still_benchmarking = [
        s for s in store.list_by_round(ROUND_ID)
        if s.status == SubmissionStatus.BENCHMARKING
    ]
    assert len(still_benchmarking) == 7  # overflow never benched


def test_belt_noop_when_slate_respected(monkeypatch, caplog, tmp_path):
    """The normal case (close sweep landed: overflow already rejected) is
    untouched — no cap, no warning."""
    store, subs = _store_with_benchmarking(10)
    # Simulate a HEALTHY close: rotation rejected everything off-slate.
    slate = select_rotation_slate(subs, 3, {}, ROUND_ID)[0]
    slate_ids = {s.submission_id for s in slate}
    for s in subs:
        if s.submission_id not in slate_ids:
            store.reject(s.submission_id, f"not selected for {ROUND_ID} (rotation: 10 candidates, 3 slots)")
    worker = _worker_for(store, monkeypatch, tmp_path, slots=3)

    with caplog.at_level(logging.WARNING):
        processed = asyncio.run(worker.run_once())

    assert processed == 3
    assert not any(
        "reject sweep must have been truncated" in rec.message
        for rec in caplog.records
    )


def test_belt_inert_when_rotation_disabled(monkeypatch, tmp_path):
    store, subs = _store_with_benchmarking(5)
    worker = _worker_for(store, monkeypatch, tmp_path, slots=0)
    assert asyncio.run(worker.run_once()) == 5  # 0 = unlimited, unchanged


# ── the cleanup script ────────────────────────────────────────────────────────


def _load_cleanup_module():
    path = _REPO_ROOT / "scripts" / "cleanup_resurrected_submissions.py"
    spec = importlib.util.spec_from_file_location("cleanup_resurrected_submissions", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _FakeRoundStore:
    def __init__(self, rounds: dict[str, str], champion_id: str | None = None):
        self._rounds = rounds  # round_id -> status str
        self._champion_id = champion_id

    def get_round(self, round_id):
        status = self._rounds.get(round_id)
        if status is None:
            return None
        return SimpleNamespace(round_id=round_id, status=SimpleNamespace(value=status))

    def get_active_champion(self):
        return SimpleNamespace(submission_id=self._champion_id)


_POISON_REASON = "not selected for round-e1-n1 (rotation: 22 candidates, 3 slots)"
_DETAILS = {"per_intent": [{"intent_id": "o1", "raw_output": "123"}]}


def _poisoned_sub(store, hotkey, round_id=ROUND_ID):
    """A pre-#596 resurrected record: scored, but carrying the rotation reason."""
    sub = store.create(
        repo_url="https://example.com/r.git",
        commit_hash=hotkey.encode().hex().ljust(40, "0")[:40],
        epoch=1,
        hotkey=hotkey,
        round_id=round_id,
        max_per_round=0,
        max_rounds_per_commit=0,
    )
    store.update_status(sub.submission_id, SubmissionStatus.SCORED)
    live = store.get(sub.submission_id)
    live.rejection_reason = _POISON_REASON.replace("round-e1-n1", round_id)
    live.benchmark_details = dict(_DETAILS)
    return live


def test_cleanup_flips_poison_and_preserves_reason_and_details():
    mod = _load_cleanup_module()
    store = SubmissionStore(persist_path=None)
    poison = _poisoned_sub(store, "hkP")
    rounds = _FakeRoundStore({ROUND_ID: "activated"})

    flipped = mod.run(store, rounds, apply=True)

    assert flipped == 1
    fresh = store.get(poison.submission_id)
    assert fresh.status == SubmissionStatus.REJECTED
    assert fresh.rejection_reason == _POISON_REASON      # reason preserved
    assert fresh.benchmark_details == _DETAILS           # details preserved


def test_cleanup_excludes_champion_and_inflight_rounds():
    mod = _load_cleanup_module()
    store = SubmissionStore(persist_path=None)
    champ = _poisoned_sub(store, "hkC", round_id="round-old-n1")
    inflight = _poisoned_sub(store, "hkI", round_id="round-live-n1")
    eligible = _poisoned_sub(store, "hkE", round_id="round-old-n1")
    rounds = _FakeRoundStore(
        {"round-old-n1": "activated", "round-live-n1": "replaying"},
        champion_id=champ.submission_id,
    )

    flipped = mod.run(store, rounds, apply=True)

    assert flipped == 1
    assert store.get(champ.submission_id).status == SubmissionStatus.SCORED
    assert store.get(inflight.submission_id).status == SubmissionStatus.SCORED
    assert store.get(eligible.submission_id).status == SubmissionStatus.REJECTED


def test_cleanup_ignores_healthy_scored_and_adopted():
    mod = _load_cleanup_module()
    store = SubmissionStore(persist_path=None)
    healthy = _poisoned_sub(store, "hkH")
    store.get(healthy.submission_id).rejection_reason = None  # legit scored
    adopted = _poisoned_sub(store, "hkA")
    store.adopt(adopted.submission_id)  # adopted is not status==scored
    rounds = _FakeRoundStore({ROUND_ID: "activated"})

    flipped = mod.run(store, rounds, apply=True)

    assert flipped == 0
    assert store.get(healthy.submission_id).status == SubmissionStatus.SCORED
    assert store.get(adopted.submission_id).status == SubmissionStatus.ADOPTED


def test_cleanup_dry_run_mutates_nothing():
    mod = _load_cleanup_module()
    store = SubmissionStore(persist_path=None)
    poison = _poisoned_sub(store, "hkD")
    rounds = _FakeRoundStore({ROUND_ID: "activated"})

    flipped = mod.run(store, rounds, apply=False)  # the default

    assert flipped == 0
    fresh = store.get(poison.submission_id)
    assert fresh.status == SubmissionStatus.SCORED  # untouched
    assert fresh.rejection_reason == _POISON_REASON


def test_cleanup_treats_unknown_round_as_finished():
    mod = _load_cleanup_module()
    store = SubmissionStore(persist_path=None)
    poison = _poisoned_sub(store, "hkU", round_id="round-purged-n1")
    rounds = _FakeRoundStore({})  # round store lost/purged the round

    assert mod.run(store, rounds, apply=True) == 1
    assert store.get(poison.submission_id).status == SubmissionStatus.REJECTED
