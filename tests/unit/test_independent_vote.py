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


class _Worker:
    """Stand-in for BenchmarkWorker: serves champion image + scores + scorecards."""

    def __init__(self, *, champ_image, chal_card, champ_card, champ_score):
        self._champ_image = champ_image
        self._chal_card = chal_card
        self._champ_card = champ_card
        self._champ_score = champ_score
        self._epoch_block_number = 123

    def _resolve_champion_image(self):
        return self._champ_image

    def _compute_avg_score(self, results):  # only called for the champion run
        return self._champ_score

    def _build_scorecard(self, results):
        return _Card(self._champ_card if results == "CHAMP_RESULTS" else self._chal_card)


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


def test_rejects_when_champion_image_unresolvable(monkeypatch):
    # Conservative guard: no champion image -> never silently adopt.
    w = _Worker(
        champ_image=None,
        chal_card={"app_scores": {"dex": 0.9}, "app_onchain": {}},
        champ_card={},
        champ_score=0.0,
    )
    adopt, score = _vote(w, 0.9, monkeypatch)
    assert adopt is False and score == 0.9
