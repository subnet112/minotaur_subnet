"""Follower independent adopt-vote (CHALLENGER_QUORUM_MODE).

`_independent_adopt_vote` benchmarks the CURRENT champion on this follower's own
shared corpus and applies the AUTHORITATIVE relative per-order rule
(`evaluate_relative_adoption`) — the IDENTICAL rule the leader runs — returning an
independent ADOPT/REJECT vote plus a relative COUNTS dict
(better/worse/matched/compared). These tests drive it with the REAL rule and
controlled per-order RAW outputs (raw_output), plus the conservative
champion-unresolvable guard and the bootstrap carve-out.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import patch

from minotaur_subnet.api.routes.submissions import champion_consensus as cc
from minotaur_subnet.harness.orchestrator import BenchmarkResult


_PRESENT = object()  # sentinel: a champion submission exists


def _results(*pairs):
    """Per-order BenchmarkResults carrying intent_id + RAW output (decimal str)."""
    return [BenchmarkResult(intent_id=iid, raw_output=sc) for iid, sc in pairs]


class _Worker:
    """Stand-in for BenchmarkWorker: serves champion submission/image + champion
    per-order results (the relative rule joins them against the challenger's)."""

    def __init__(self, *, champ_image, champ_results, champ_sub=_PRESENT):
        self._champ_image = champ_image
        self._champ_results = champ_results
        self._epoch_block_number = 123
        # The champion SUBMISSION (or None for true bootstrap). Defaults to present
        # so the existing has-champion tests are unaffected.
        self._champ_sub = champ_sub

    def _resolve_incumbent_submission(self):
        return self._champ_sub

    def _resolve_champion_image(self):
        return self._champ_image

    async def memo_champion_bench(
        self,
        *,
        round_id,
        image,
        fork_block,
        intents,
        require_real_sim,
        run,
    ):
        # Return the champion per-order results directly (a cache-hit-shaped stub);
        # the memo itself is covered by test_champion_bench_memo.py.
        return self._champ_results


class _Session:
    async def shutdown(self):
        return None


class _Orch:
    async def start_docker(self, image):
        return _Session()


def _vote(worker, chal_results, monkeypatch):
    cand = SimpleNamespace(submission_id="sub_test")
    intents = [(SimpleNamespace(app_id="dex"), SimpleNamespace(chain_id=8453), None)]
    with patch(
        "minotaur_subnet.harness.orchestrator.SolverOrchestrator", _Orch
    ):
        return asyncio.run(
            cc._independent_adopt_vote(
                worker=worker, intents=intents, score_fn=None, simulator=object(),
                chal_results=chal_results,
                candidate=cand, round_id="r1",
            )
        )


def test_adopts_clear_improvement(monkeypatch):
    # Challenger delivers strictly more on every order -> 2 wins, 0 regressions.
    w = _Worker(champ_image="champ:img", champ_results=_results(("o1", "100"), ("o2", "200")))
    adopt, counts = _vote(w, _results(("o1", "120"), ("o2", "250")), monkeypatch)
    assert adopt is True
    assert counts["better"] == 2 and counts["worse"] == 0


def test_rejects_regression(monkeypatch):
    # Challenger delivers less on an order the champion served -> regression veto.
    w = _Worker(champ_image="champ:img", champ_results=_results(("o1", "200")))
    adopt, counts = _vote(w, _results(("o1", "100")), monkeypatch)
    assert adopt is False
    assert counts["worse"] >= 1


def test_rejects_when_only_matched(monkeypatch):
    # Challenger ties the champion everywhere (within the noise band) -> no win -> REJECT.
    w = _Worker(champ_image="champ:img", champ_results=_results(("o1", "1000")))
    adopt, counts = _vote(w, _results(("o1", "1000")), monkeypatch)
    assert adopt is False
    assert counts["matched"] >= 1 and counts["better"] == 0


def test_rejects_when_champion_exists_but_image_unresolvable(monkeypatch):
    # has_champion=True (a champion submission exists) but its image can't resolve
    # -> can't benchmark the incumbent to prove improvement -> conservative REJECT.
    w = _Worker(champ_sub=_PRESENT, champ_image=None, champ_results=[])
    adopt, counts = _vote(w, _results(("o1", "120")), monkeypatch)
    assert adopt is False and counts == {}


def test_bootstrap_adopts_first_champion_when_no_incumbent(monkeypatch):
    # True bootstrap: no champion submission AT ALL -> has_champion=False, matching
    # the leader. A challenger that delivers value on any order is ADOPTED (no
    # incumbent to dethrone) — NOT auto-rejected (which would deadlock first adoption).
    w = _Worker(champ_sub=None, champ_image=None, champ_results=[])
    adopt, counts = _vote(w, _results(("o1", "120")), monkeypatch)
    assert adopt is True
    assert counts["better"] >= 1


def test_bootstrap_rejects_when_challenger_delivers_nothing(monkeypatch):
    # Bootstrap but the challenger delivers no value on any order -> nothing to
    # adopt -> REJECT even with no incumbent.
    w = _Worker(champ_sub=None, champ_image=None, champ_results=[])
    adopt, _ = _vote(w, _results(("o1", "0")), monkeypatch)
    assert adopt is False


# ── has_champion PARITY with the leader (genesis-as-bar, #242 user decision) ──
# The first champion must BEAT the genesis reference. The leader seeds self._champion
# from a genesis that DELIVERED VALUE (>=1 order with raw_output>0) at decision time
# (_maybe_seed_genesis_incumbent); the follower MUST resolve has_champion identically
# via _resolve_incumbent_submission, or the first adoption diverges -> 0 quorum. These
# lock the SHARED predicate (adopted | snapshot | SCORED-genesis-that-delivered-value).

def _bench_worker(*, adopted=None, snapshot_sid=None, snapshot_sub=None, scored_genesis=None):
    from unittest.mock import MagicMock
    from minotaur_subnet.harness.benchmark_worker import BenchmarkWorker
    sub = MagicMock()
    sub.get_champion.return_value = adopted
    sub.get_by_hotkey_epoch.return_value = scored_genesis
    sub.get.return_value = snapshot_sub
    rs = MagicMock()
    rs.get_active_champion.return_value = SimpleNamespace(submission_id=snapshot_sid)
    return BenchmarkWorker(submission_store=sub, round_store=rs)


def _genesis(delivered):
    # A SCORED genesis. "delivered" -> a per_intent row whose raw_output parses to >0
    # (the delivered-value gate that replaced benchmark_score>0); otherwise a "0" row
    # (scored but delivered nothing, so it is NOT a usable bar).
    from minotaur_subnet.harness.submission_store import SubmissionStatus
    return SimpleNamespace(
        submission_id="sub_genesis",
        status=SubmissionStatus.SCORED,
        benchmark_details={
            "per_intent": [{"intent_id": "app:scn", "raw_output": "1000" if delivered else "0"}]
        },
    )


def test_incumbent_includes_scored_genesis_as_bar():
    # genesis-as-bar: a SCORED genesis that DELIVERED VALUE IS the incumbent (the
    # first champion must beat it) — matching the leader's _maybe_seed_genesis_incumbent.
    g = _genesis(delivered=True)
    w = _bench_worker(adopted=None, snapshot_sid=None, scored_genesis=g)
    assert w._resolve_incumbent_submission() is g


def test_incumbent_excludes_unscored_genesis():
    # Genesis present but no usable bar yet (delivered nothing) -> None -> true bootstrap.
    w = _bench_worker(adopted=None, snapshot_sid=None, scored_genesis=_genesis(delivered=False))
    assert w._resolve_incumbent_submission() is None


def test_incumbent_returns_adopted_champion():
    champ = SimpleNamespace(submission_id="sub_champ")
    w = _bench_worker(adopted=champ, scored_genesis=_genesis(delivered=True))  # adopted wins over genesis
    assert w._resolve_incumbent_submission() is champ


def test_incumbent_returns_snapshot_when_no_adopted():
    snap = SimpleNamespace(submission_id="sub_snap")
    w = _bench_worker(adopted=None, snapshot_sid="sub_snap", snapshot_sub=snap)
    assert w._resolve_incumbent_submission() is snap
