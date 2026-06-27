"""Follower independent adopt-vote (CHALLENGER_QUORUM_MODE).

`_independent_adopt_vote` benchmarks the CURRENT champion on this follower's own
(diverse) intents and applies the shared `evaluate_adoption` rule, returning an
independent ADOPT/REJECT vote. These tests drive it with the REAL rule and
controlled scorecards/scores, plus the conservative champion-unresolvable guard.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import patch

from minotaur_subnet.api.routes.submissions import champion_consensus as cc


class _Card:
    def __init__(self, d):
        self._d = d

    def to_dict(self):
        return self._d


_PRESENT = object()  # sentinel: a champion submission exists


class _Worker:
    """Stand-in for BenchmarkWorker: serves champion submission/image + scores."""

    def __init__(self, *, champ_image, chal_card, champ_card, champ_score, champ_sub=_PRESENT):
        self._champ_image = champ_image
        self._chal_card = chal_card
        self._champ_card = champ_card
        self._champ_score = champ_score
        self._epoch_block_number = 123
        # The champion SUBMISSION (or None for true bootstrap). Defaults to present
        # so the existing has-champion tests are unaffected.
        self._champ_sub = champ_sub

    def _resolve_incumbent_submission(self):
        return self._champ_sub

    def _resolve_champion_image(self):
        return self._champ_image

    def _compute_avg_score(self, results):  # only called for the champion run
        return self._champ_score

    def _build_scorecard(self, results):
        return _Card(self._champ_card if results == "CHAMP_RESULTS" else self._chal_card)

    async def memo_champion_bench(
        self,
        *,
        round_id,
        image,
        fork_block,
        intents,
        require_real_sim,
        reference_quotes=None,
        run,
    ):
        # Pass-through stub matching BenchmarkWorker.memo_champion_bench's keyword-only
        # surface; the memo itself is covered by test_champion_bench_memo.py, so here we
        # just run the caller's thunk (== flag-off behavior).
        return await run()


class _Session:
    async def shutdown(self):
        return None


class _Orch:
    async def start_docker(self, image):
        return _Session()


async def _fake_run_benchmark(*a, **k):
    return "CHAMP_RESULTS"


def _vote(worker, chal_score, monkeypatch):
    for k in (
        "ADOPT_RULE", "MIN_CHAMPION_SCORE", "PER_APP_MIN_SCORE",
        "MAX_APP_REGRESSION", "ONCHAIN_FLOOR_BPS",
    ):
        monkeypatch.delenv(k, raising=False)
    cand = SimpleNamespace(submission_id="sub_test")
    intents = [(SimpleNamespace(app_id="dex"), SimpleNamespace(chain_id=8453), None)]
    with patch(
        "minotaur_subnet.harness.orchestrator.SolverOrchestrator", _Orch
    ), patch(
        "minotaur_subnet.harness.orchestrator.run_benchmark", _fake_run_benchmark
    ):
        return asyncio.run(
            cc._independent_adopt_vote(
                worker=worker, intents=intents, score_fn=None, simulator=object(),
                chal_results="CHAL_RESULTS", chal_score=chal_score,
                candidate=cand, round_id="r1",
            )
        )


def test_adopts_clear_improvement(monkeypatch):
    w = _Worker(
        champ_image="champ:img",
        chal_card={"app_scores": {"dex": 0.9}, "app_onchain": {}},
        champ_card={"app_scores": {"dex": 0.5}, "app_onchain": {}},
        champ_score=0.5,
    )
    adopt, score = _vote(w, 0.9, monkeypatch)
    assert adopt is True and score == 0.9


def test_rejects_regression(monkeypatch):
    # Challenger drops the app's score below the champion (well past the margin).
    w = _Worker(
        champ_image="champ:img",
        chal_card={"app_scores": {"dex": 0.5}, "app_onchain": {}},
        champ_card={"app_scores": {"dex": 0.9}, "app_onchain": {}},
        champ_score=0.9,
    )
    adopt, _ = _vote(w, 0.5, monkeypatch)
    assert adopt is False


def test_rejects_within_margin(monkeypatch):
    # Challenger barely above champion but not by the dethrone margin -> REJECT.
    w = _Worker(
        champ_image="champ:img",
        chal_card={"app_scores": {"dex": 0.701}, "app_onchain": {}},
        champ_card={"app_scores": {"dex": 0.70}, "app_onchain": {}},
        champ_score=0.70,
    )
    adopt, _ = _vote(w, 0.701, monkeypatch)  # 0.701 < 0.70 * 1.005 = 0.7035
    assert adopt is False


def test_rejects_when_champion_exists_but_image_unresolvable(monkeypatch):
    # has_champion=True (a champion submission exists) but its image can't resolve
    # -> can't benchmark the incumbent to prove the margin -> conservative REJECT.
    w = _Worker(
        champ_sub=_PRESENT,
        champ_image=None,
        chal_card={"app_scores": {"dex": 0.9}, "app_onchain": {}},
        champ_card={},
        champ_score=0.0,
    )
    adopt, score = _vote(w, 0.9, monkeypatch)
    assert adopt is False and score == 0.9


def test_bootstrap_adopts_first_champion_when_no_incumbent(monkeypatch):
    # True bootstrap: no champion submission AT ALL -> has_champion=False, matching
    # the leader. A challenger that clears the absolute floor is ADOPTED (no margin,
    # no incumbent benchmark) — NOT auto-rejected (which would deadlock first adoption).
    w = _Worker(
        champ_sub=None,
        champ_image=None,
        chal_card={"app_scores": {"dex": 0.9}, "app_onchain": {}},
        champ_card={},
        champ_score=0.0,
    )
    adopt, score = _vote(w, 0.9, monkeypatch)
    assert adopt is True and score == 0.9


def test_bootstrap_rejects_first_champion_below_floor(monkeypatch):
    # Bootstrap but the challenger fails the absolute per-app floor -> REJECT even
    # with no incumbent (the floor still applies).
    w = _Worker(
        champ_sub=None,
        champ_image=None,
        chal_card={"app_scores": {"dex": 0.1}, "app_onchain": {}},
        champ_card={},
        champ_score=0.0,
    )
    adopt, _ = _vote(w, 0.1, monkeypatch)
    assert adopt is False


# ── has_champion PARITY with the leader (genesis-as-bar, #242 user decision) ──
# The first champion must BEAT the genesis reference. The leader seeds self._champion
# from a SCORED genesis (score>0) at decision time (_maybe_seed_genesis_incumbent);
# the follower MUST resolve has_champion identically via _resolve_incumbent_submission,
# or the first adoption diverges -> 0 quorum. These lock the SHARED predicate
# (adopted | snapshot | SCORED-genesis-with-score>0).

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


def _genesis(score):
    from minotaur_subnet.harness.submission_store import SubmissionStatus
    return SimpleNamespace(
        submission_id="sub_genesis", status=SubmissionStatus.SCORED, benchmark_score=score
    )


def test_incumbent_includes_scored_genesis_as_bar():
    # genesis-as-bar: a SCORED genesis with score>0 IS the incumbent (the first
    # champion must beat it) — matching the leader's _maybe_seed_genesis_incumbent.
    g = _genesis(0.5)
    w = _bench_worker(adopted=None, snapshot_sid=None, scored_genesis=g)
    assert w._resolve_incumbent_submission() is g


def test_incumbent_excludes_unscored_genesis():
    # Genesis present but no usable bar yet (score 0) -> None -> true bootstrap.
    w = _bench_worker(adopted=None, snapshot_sid=None, scored_genesis=_genesis(0.0))
    assert w._resolve_incumbent_submission() is None


def test_incumbent_returns_adopted_champion():
    champ = SimpleNamespace(submission_id="sub_champ")
    w = _bench_worker(adopted=champ, scored_genesis=_genesis(0.9))  # adopted wins over genesis
    assert w._resolve_incumbent_submission() is champ


def test_incumbent_returns_snapshot_when_no_adopted():
    snap = SimpleNamespace(submission_id="sub_snap")
    w = _bench_worker(adopted=None, snapshot_sid="sub_snap", snapshot_sub=snap)
    assert w._resolve_incumbent_submission() is snap
