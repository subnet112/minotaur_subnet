"""The two reapers for submissions stranded in BENCHMARKING by ended rounds.

BENCHMARKING is set at screening pass (pre-slate). `EpochManager._complete_round`
settles every row at round end (#227), but the DEADLINE abort goes through
`_abort_solver_round_state` (round_manager) which never touched submissions —
stranding screening-passers in BENCHMARKING forever. Observed live 2026-07-26:
62 stranded rows across ~20 rounds; 3 fingerprints locked out at the
2-benched-rounds quota without one completed bench; the UI rendered them as
live concurrent benchmarks.

Covered here:
  * `_reap_benchmarking_for_terminal_round` — inline reap on the abort path.
  * `reap_orphaned_benchmarking` — boot sweep that settles the backlog and
    leaves live-round rows alone.
"""

from __future__ import annotations

import pytest

from minotaur_subnet.harness.submission_store import SubmissionStatus, SubmissionStore


def _mk_store(tmp_path):
    store = SubmissionStore(persist_path=tmp_path / "subs.json")
    subs = {}
    for name, round_id, status in (
        ("dead_bench", "r_dead", SubmissionStatus.BENCHMARKING),
        ("dead_scored", "r_dead", SubmissionStatus.SCORED),
        ("live_bench", "r_live", SubmissionStatus.BENCHMARKING),
    ):
        s = store.create(
            repo_url="https://example.com/r.git", commit_hash=f"c_{name}",
            epoch=1, hotkey=f"hk_{name}", round_id=round_id,
        )
        store.update_status(s.submission_id, status)
        subs[name] = s.submission_id
    return store, subs


def test_abort_path_reaps_benchmarking_to_no_fault_waitlist(tmp_path, monkeypatch):
    from minotaur_subnet.api.routes.submissions import round_manager

    store, subs = _mk_store(tmp_path)
    monkeypatch.setattr(round_manager, "get_store", lambda: store)

    reaped = round_manager._reap_benchmarking_for_terminal_round("r_dead")

    assert reaped == 1
    dead = store.get(subs["dead_bench"])
    assert dead.status == SubmissionStatus.WAITLISTED  # no-fault, keeps seniority
    assert dead.outcome_code == "round_aborted_unbenched"
    assert "no quota burned" in (dead.rejection_reason or "")
    # A completed bench in the same round is NOT rewritten.
    assert store.get(subs["dead_scored"]).status == SubmissionStatus.SCORED
    # Other rounds untouched.
    assert store.get(subs["live_bench"]).status == SubmissionStatus.BENCHMARKING


def test_abort_path_reap_never_breaks_the_abort(monkeypatch):
    from minotaur_subnet.api.routes.submissions import round_manager

    def _boom():
        raise RuntimeError("store unavailable")

    monkeypatch.setattr(round_manager, "get_store", _boom)
    # Must swallow and report 0, not raise into the abort path.
    assert round_manager._reap_benchmarking_for_terminal_round("r_x") == 0


@pytest.mark.asyncio
async def test_boot_sweep_settles_orphans_and_spares_live_round(tmp_path, monkeypatch):
    from minotaur_subnet.api.routes.submissions import screening_pipeline, state

    store, subs = _mk_store(tmp_path)

    class _Round:
        def __init__(self, status):
            self.status = type("S", (), {"value": status})()

    class _RoundStore:
        def get_round(self, round_id):
            # r_dead ended (aborted); r_live is mid-flight; anything else unknown.
            if round_id == "r_dead":
                return _Round("aborted")
            if round_id == "r_live":
                return _Round("replaying")
            return None

    monkeypatch.setattr(screening_pipeline, "get_store", lambda: store)
    monkeypatch.setattr(state, "get_round_store", lambda: _RoundStore())

    healed = await screening_pipeline.reap_orphaned_benchmarking()

    assert healed == 1
    dead = store.get(subs["dead_bench"])
    assert dead.status == SubmissionStatus.WAITLISTED
    assert dead.outcome_code == "round_ended_unbenched"
    # The live round's in-flight row is the benchmark worker's business.
    assert store.get(subs["live_bench"]).status == SubmissionStatus.BENCHMARKING
    # Scored rows are never rewritten.
    assert store.get(subs["dead_scored"]).status == SubmissionStatus.SCORED


@pytest.mark.asyncio
async def test_boot_sweep_waitlists_rows_of_missing_rounds(tmp_path, monkeypatch):
    """A round the store no longer knows (pruned / pre-rebuild history) is as
    terminal as an aborted one — its BENCHMARKING rows must settle too."""
    from minotaur_subnet.api.routes.submissions import screening_pipeline, state

    store, subs = _mk_store(tmp_path)

    class _EmptyRoundStore:
        def get_round(self, round_id):
            return None

    monkeypatch.setattr(screening_pipeline, "get_store", lambda: store)
    monkeypatch.setattr(state, "get_round_store", lambda: _EmptyRoundStore())

    healed = await screening_pipeline.reap_orphaned_benchmarking()

    # BOTH benchmarking rows settle (their rounds are unknown), scored stays.
    assert healed == 2
    assert store.get(subs["dead_bench"]).status == SubmissionStatus.WAITLISTED
    assert store.get(subs["live_bench"]).status == SubmissionStatus.WAITLISTED
    assert store.get(subs["dead_scored"]).status == SubmissionStatus.SCORED
