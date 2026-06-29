"""Shadow adopt-vote — observe-only per-validator vote against the REAL reference
champion (the adopted champion, or the official genesis when none is adopted —
resolved from the store, never an injectable env), so the fleet can demonstrate
the challenger-quorum decision without an organic on-chain champion.

Exercises the orchestration glue in ``BenchmarkWorker.run_shadow_vote``: benchmark
the reference champion + the challenger, apply the AUTHORITATIVE per-order relative
rule (``evaluate_relative_adoption``), return this validator's vote. The rule itself
is covered by epoch/test_relative_scoring; here we verify the wiring routes a
challenger that delivers MORE per order to ADOPT, one that regresses to REJECT, a
tie to REJECT, and fails safe with no champion.
"""
from __future__ import annotations

import asyncio

import pytest

from minotaur_subnet.harness import benchmark_worker as bw_mod
from minotaur_subnet.harness import orchestrator as orch_mod
from minotaur_subnet.harness.orchestrator import BenchmarkResult


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


def _make_worker(monkeypatch, scores, champion="champ:ref"):
    w = bw_mod.BenchmarkWorker(
        submission_store=_FakeSubStore(), use_docker=False, validator_identity="val-1",
    )
    # The reference is the store-resolved champion (or genesis), NOT an env.
    monkeypatch.setattr(w, "_resolve_champion_image", lambda: champion)
    monkeypatch.setattr(w, "_load_benchmark_intents", lambda **k: [(object(), _FakeState(), object())])

    async def _sf(intents):
        return lambda *a, **k: 1.0

    monkeypatch.setattr(w, "_build_score_fn", _sf)
    monkeypatch.setattr(w, "_enrich_intents_with_manifests", lambda intents: intents)
    # Aggregate mean is LOGGING/DISPLAY only now; read it off the per-order result.
    monkeypatch.setattr(w, "_compute_avg_score", lambda results: results[0].score)

    async def _run_benchmark(session, intents, **kwargs):
        # One per-order result carrying the RAW delivered output (shadow_score, an
        # exact decimal-wei STRING) the relative rule actually compares. Same
        # intent_id on both sides so they JOIN. Output scales with the score map.
        sc = scores[session.image]
        return [BenchmarkResult(
            intent_id="dex:s1", score=sc, shadow_score=str(int(round(sc * 100))),
        )]

    monkeypatch.setattr(orch_mod, "run_benchmark", _run_benchmark)
    monkeypatch.setattr(orch_mod, "SolverOrchestrator", _FakeOrch)
    return w


def test_better_challenger_votes_adopt(monkeypatch):
    # Challenger delivers MORE on the order (90 > 70) -> per-order win -> ADOPT.
    w = _make_worker(monkeypatch, {"champ:ref": 0.70, "chal:good": 0.90})
    vote = asyncio.run(w.run_shadow_vote("chal:good"))
    assert vote["vote"] == "ADOPT", vote
    assert vote["champ_score"] == 0.70 and vote["chal_score"] == 0.90
    assert vote["champion_image"] == "champ:ref"
    assert vote["validator_id"] == "val-1"
    assert vote["n_wins"] == 1 and vote["n_regressions"] == 0


def test_worse_challenger_votes_reject(monkeypatch):
    # Challenger delivers LESS on the order (50 < 70) -> per-order regression -> REJECT.
    w = _make_worker(monkeypatch, {"champ:ref": 0.70, "chal:bad": 0.50})
    vote = asyncio.run(w.run_shadow_vote("chal:bad"))
    assert vote["vote"] == "REJECT", vote
    assert vote["n_regressions"] == 1


def test_tie_challenger_votes_reject(monkeypatch):
    # Equal per-order output -> matched, no win -> REJECT (don't churn the champion).
    w = _make_worker(monkeypatch, {"champ:ref": 0.80, "chal:tie": 0.80})
    vote = asyncio.run(w.run_shadow_vote("chal:tie"))
    assert vote["vote"] == "REJECT", vote


def test_no_champion_fails_safe(monkeypatch):
    w = _make_worker(monkeypatch, {}, champion=None)
    vote = asyncio.run(w.run_shadow_vote("chal:good"))
    assert "error" in vote and "champion" in vote["error"].lower()
