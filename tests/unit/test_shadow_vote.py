"""Shadow-champion adopt-vote — observe-only per-validator vote against a
DESIGNATED reference champion (SHADOW_CHAMPION_IMAGE), so the fleet can
demonstrate the challenger-quorum decision without an organic on-chain champion.

Exercises the orchestration glue in ``BenchmarkWorker.run_shadow_vote``: benchmark
the reference champion + the challenger, apply the shared ``evaluate_adoption`` rule,
return this validator's vote. The rule + diverse-subset sampling are covered by
test_adopt_rule / test_quorum_validation; here we verify the wiring routes a
better challenger to ADOPT and a worse one to REJECT, and fails safe with no champion.
"""
from __future__ import annotations

import asyncio

import pytest

from minotaur_subnet.harness import benchmark_worker as bw_mod
from minotaur_subnet.harness import orchestrator as orch_mod


class _FakeSubStore:
    def get_champion(self):
        return None

    def get_by_hotkey_epoch(self, *a):
        return None


class _FakeState:
    chain_id = 8453


class _FakeSession:
    def __init__(self, image):
        self.image = image

    async def shutdown(self):
        pass


class _FakeOrch:
    async def start_docker(self, image):
        return _FakeSession(image)


class _FakeCard:
    def __init__(self, score):
        self._s = score

    def to_dict(self):
        # empty app_onchain -> on-chain gate neutral, JS score decides (as in
        # test_quorum_validation), so the wiring's score routing is what's tested.
        return {"app_scores": {"dex": self._s}, "app_onchain": {}}


def _make_worker(monkeypatch, scores):
    w = bw_mod.BenchmarkWorker(
        submission_store=_FakeSubStore(), use_docker=False, validator_identity="val-1",
    )
    monkeypatch.setattr(w, "_load_benchmark_intents", lambda **k: [(object(), _FakeState(), object())])

    async def _sf(intents):
        return lambda *a, **k: 1.0

    monkeypatch.setattr(w, "_build_score_fn", _sf)
    monkeypatch.setattr(w, "_enrich_intents_with_manifests", lambda intents: intents)
    monkeypatch.setattr(w, "_compute_avg_score", lambda results: scores[results[0]])
    monkeypatch.setattr(w, "_build_scorecard", lambda results: _FakeCard(scores[results[0]]))

    async def _run_benchmark(session, intents, **kwargs):
        return [session.image]  # marker = the image that was benchmarked

    monkeypatch.setattr(orch_mod, "run_benchmark", _run_benchmark)
    monkeypatch.setattr(orch_mod, "SolverOrchestrator", _FakeOrch)
    return w


def test_better_challenger_votes_adopt(monkeypatch):
    monkeypatch.setenv("SHADOW_CHAMPION_IMAGE", "champ:ref")
    w = _make_worker(monkeypatch, {"champ:ref": 0.70, "chal:good": 0.90})
    vote = asyncio.run(w.run_shadow_vote("chal:good"))
    assert vote["vote"] == "ADOPT", vote
    assert vote["champ_score"] == 0.70 and vote["chal_score"] == 0.90
    assert vote["champion_image"] == "champ:ref"
    assert vote["validator_seed"] == "val-1"


def test_worse_challenger_votes_reject(monkeypatch):
    monkeypatch.setenv("SHADOW_CHAMPION_IMAGE", "champ:ref")
    w = _make_worker(monkeypatch, {"champ:ref": 0.70, "chal:bad": 0.50})
    vote = asyncio.run(w.run_shadow_vote("chal:bad"))
    assert vote["vote"] == "REJECT", vote


def test_tie_challenger_votes_reject(monkeypatch):
    # No improvement above the dethrone margin -> REJECT (don't churn the champion).
    monkeypatch.setenv("SHADOW_CHAMPION_IMAGE", "champ:ref")
    w = _make_worker(monkeypatch, {"champ:ref": 0.80, "chal:tie": 0.80})
    vote = asyncio.run(w.run_shadow_vote("chal:tie"))
    assert vote["vote"] == "REJECT", vote


def test_no_champion_fails_safe(monkeypatch):
    monkeypatch.delenv("SHADOW_CHAMPION_IMAGE", raising=False)
    w = _make_worker(monkeypatch, {})
    vote = asyncio.run(w.run_shadow_vote("chal:good"))
    assert "error" in vote and "champion" in vote["error"].lower()
