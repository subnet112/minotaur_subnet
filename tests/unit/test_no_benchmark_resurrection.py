"""A submission that is terminal-for-round with its token purged (REJECTED or
WAITLISTED) must never be resurrected by a late benchmark result.

Rotation parks the slate overflow at round close "regardless of benchmark
progress" and irreversibly purges the private-repo token (memory + encrypted
sidecar). An in-flight bench finishing after that — or a restart re-benching an
orphaned round — used to flip the terminal status -> SCORED in
``set_benchmark_result``, letting the resurrected record rank (and, under the
tie-break ladder, WIN) as finalist, certify, and die at relayer-finalize
"no token — FAIL-CLOSED" (observed live 2026-07-07: 5 consecutive merge_failed
round aborts). Rotation moved overflow from ``reject()`` to ``waitlist()`` in
#620, but the guard kept checking only REJECTED, so the WAITLISTED door
reopened the class — 2026-07-22 sub_a91b87fdd63e certified then failed to
merge with no token. The guard now covers both (``_NO_RESURRECTION_STATUSES``).

Three guards close the class (the screening re-queue leg is already covered by
``_terminal_during_screening`` / test_screening_respects_rotation_reject):
  1. store: ``set_benchmark_result`` records details but never resurrects.
  2. store: a legitimate SCORED transition clears any stale rejection_reason.
  3. worker: the bench loop re-fetches status and skips submissions that are
     no longer BENCHMARKING (saves the serialized sim time entirely).
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from minotaur_subnet.harness.submission_store import SubmissionStatus, SubmissionStore


def _store_with_sub(**kwargs):
    store = SubmissionStore(persist_path=None)
    sub = store.create(
        repo_url="https://example.com/r.git",
        commit_hash="c" * 40,
        epoch=1,
        hotkey="hk1",
        round_id="round-e1-n1",
        max_per_round=0,
        max_rounds_per_commit=0,
        **kwargs,
    )
    return store, sub


ROTATION_REASON = "not selected for round-e1-n1 (rotation: 15 candidates, 3 slots)"
_VALID_DETAILS = {"per_intent": [{"intent_id": "o1", "raw_output": "12345"}]}


# ── 1. no resurrection ────────────────────────────────────────────────────────


def test_rejected_is_not_resurrected_by_valid_bench():
    store, sub = _store_with_sub()
    store.reject(sub.submission_id, ROTATION_REASON)

    # The racing bench completes AFTER the rotation reject.
    store.set_benchmark_result(sub.submission_id, valid=True, details=_VALID_DETAILS)

    fresh = store.get(sub.submission_id)
    assert fresh.status == SubmissionStatus.REJECTED          # not resurrected
    assert fresh.rejection_reason == ROTATION_REASON          # reason immutable
    # ... but the bench outcome is still recorded for the miner's report.
    assert fresh.benchmark_details == _VALID_DETAILS


def test_rejected_stays_rejected_on_invalid_bench():
    store, sub = _store_with_sub()
    store.reject(sub.submission_id, ROTATION_REASON)
    store.set_benchmark_result(sub.submission_id, valid=False, details={"error": "x"})
    fresh = store.get(sub.submission_id)
    assert fresh.status == SubmissionStatus.REJECTED
    # The ORIGINAL terminal reason is not overwritten by the validity-gate text.
    assert fresh.rejection_reason == ROTATION_REASON


def test_rejected_private_token_stays_purged():
    store, sub = _store_with_sub(repo_token="ghp_secret", is_private=True,
                                 private_repo_full="m/r")
    assert store.get_repo_token(sub.submission_id) == "ghp_secret"
    store.reject(sub.submission_id, ROTATION_REASON)          # terminal: purges
    assert store.get_repo_token(sub.submission_id) is None
    store.set_benchmark_result(sub.submission_id, valid=True, details=_VALID_DETAILS)
    # No resurrection ⇒ the record can never again be selected as a finalist
    # whose merge would need the (gone) token.
    assert store.get(sub.submission_id).status == SubmissionStatus.REJECTED
    assert store.get_repo_token(sub.submission_id) is None


# ── 1b. WAITLISTED (rotation overflow, #620) is likewise not resurrected ──────


def test_waitlisted_is_not_resurrected_by_valid_bench():
    # The exact 2026-07-22 chimera: rotation waitlists the overflow (purging the
    # token), then an in-flight/replayed bench completes valid. Must NOT flip to
    # SCORED, or it becomes a token-less finalist that certifies then FAIL-CLOSEs.
    store, sub = _store_with_sub()
    store.waitlist(
        sub.submission_id, ROTATION_REASON,
        outcome_code="rotation_not_selected", position=14, contenders=18, slots=3,
    )
    store.set_benchmark_result(sub.submission_id, valid=True, details=_VALID_DETAILS)

    fresh = store.get(sub.submission_id)
    assert fresh.status == SubmissionStatus.WAITLISTED       # not resurrected
    assert fresh.outcome_code == "rotation_not_selected"     # loser fossil intact
    assert fresh.waitlist["position"] == 14                  # context preserved
    assert fresh.benchmark_details == _VALID_DETAILS         # score still recorded


def test_waitlisted_private_token_stays_purged_no_tokenless_finalist():
    store, sub = _store_with_sub(repo_token="ghp_secret", is_private=True,
                                 private_repo_full="m/r")
    assert store.get_repo_token(sub.submission_id) == "ghp_secret"
    store.waitlist(
        sub.submission_id, ROTATION_REASON,
        outcome_code="rotation_not_selected", position=14, contenders=18, slots=3,
    )
    assert store.get_repo_token(sub.submission_id) is None   # waitlist purged it
    store.set_benchmark_result(sub.submission_id, valid=True, details=_VALID_DETAILS)
    # No resurrection ⇒ never re-enters the finalist pool with a token it lost.
    assert store.get(sub.submission_id).status == SubmissionStatus.WAITLISTED
    assert store.get_repo_token(sub.submission_id) is None


# ── 2. legitimate SCORED clears stale reason ─────────────────────────────────


def test_scored_clears_stale_rejection_reason():
    store, sub = _store_with_sub()
    store.update_status(sub.submission_id, SubmissionStatus.BENCHMARKING)
    # Simulate a stale reason left by an earlier non-terminal path.
    store.get(sub.submission_id).rejection_reason = "stale text"
    store.set_benchmark_result(sub.submission_id, valid=True, details=_VALID_DETAILS)
    fresh = store.get(sub.submission_id)
    assert fresh.status == SubmissionStatus.SCORED
    assert fresh.rejection_reason is None


def test_normal_valid_and_invalid_paths_unchanged():
    store, sub = _store_with_sub()
    store.update_status(sub.submission_id, SubmissionStatus.BENCHMARKING)
    store.set_benchmark_result(sub.submission_id, valid=True, details=_VALID_DETAILS)
    assert store.get(sub.submission_id).status == SubmissionStatus.SCORED

    store2, sub2 = _store_with_sub()
    store2.update_status(sub2.submission_id, SubmissionStatus.BENCHMARKING)
    store2.set_benchmark_result(sub2.submission_id, valid=False, details={})
    fresh2 = store2.get(sub2.submission_id)
    assert fresh2.status == SubmissionStatus.REJECTED
    assert "no order delivered value" in fresh2.rejection_reason


# ── 3. worker skips mid-pass status flips ────────────────────────────────────


def test_worker_skips_submission_rejected_after_queue_snapshot(monkeypatch):
    """Simulate the race: the sub is in the BENCHMARKING snapshot, but rotation
    rejects it before its turn in the bench loop — the worker must skip it
    without spending sim time."""
    from minotaur_subnet.harness.benchmark_worker import BenchmarkWorker

    store, sub = _store_with_sub()
    store.update_status(sub.submission_id, SubmissionStatus.BENCHMARKING)
    stale = store.get(sub.submission_id)
    # Give it a solver_path so, were it NOT skipped, the loop would try to bench.
    stale.solver_path = "/tmp/nonexistent-solver.py"

    worker = BenchmarkWorker(store)

    # Freeze the queue snapshot to the stale object, then flip the real status.
    monkeypatch.setattr(
        store, "list_by_status",
        lambda status: [stale] if status == SubmissionStatus.BENCHMARKING else [],
    )
    store.reject(sub.submission_id, ROTATION_REASON)

    called = []
    async def _must_not_bench(*a, **k):
        called.append(True)
        raise AssertionError("bench must not run for a rejected submission")
    monkeypatch.setattr(worker, "_benchmark_solver_path", _must_not_bench)

    asyncio.run(worker.run_once())

    assert called == []                                       # never benched
    fresh = store.get(sub.submission_id)
    assert fresh.status == SubmissionStatus.REJECTED          # still rejected
    assert fresh.rejection_reason == ROTATION_REASON
