"""Scoring lab — drive the REAL champion-scoring code with controlled inputs.

A staged, fully-traced harness for confirming how Minotaur scores solvers and
elects a champion, without standing up the full Docker testnet. Two simulation
backends share the same always-real scoring stages:

    fake : fabricate (plan, simulation) from a scripted Fill — fast, no fork.
    fork : real solver (genesis / candidate) + real Anvil fork scoreIntent.

The 5 stages each wrap a production callable (see PORT_MAP), so a change tried in
the lab ports 1:1 onto the real code. Every stage records its input -> output into
a RunTrace, so you can change one stage without breaking the others and inspect
exactly what each produced.

CLI:
    python -m minotaur_subnet.harness.scoring_lab demo
    python -m minotaur_subnet.harness.scoring_lab run  --scenarios my.json [--rule p2 --on-chain-floor 5000]
    python -m minotaur_subnet.harness.scoring_lab bench --base-rpc <url> [--fork-block N --candidate path.py]
"""
from __future__ import annotations

from pathlib import Path

from minotaur_subnet.engine.js_engine import JsExecutionEngine

from .model import (
    DEFAULT_DEX_CONTRACT,
    DEFAULT_DEX_SCORER,
    DEFAULT_GENESIS_IMAGE,
    Fill,
    LabConfig,
    RunTrace,
    Scenario,
    StageRecord,
)
from .pipeline import FakeBackend, SolverResult, compare, run_solver
from .report import build_report, compute_report_hash, diff_reports, scorer_digest
from .stages import ADOPT_RULES, ScoreJsStage, aggregate, build_state

# Each pipeline stage -> the production code it wraps. Porting a redesign change =
# apply the stage's diff at the named callable.
PORT_MAP: dict[str, str] = {
    "solve": "orchestrator.SolverSession.generate_plan (fork) | fabricated Fill (fake)",
    "simulate": "simulator/anvil_simulator.py:_simulate_via_score_intent (fork) | fabricated (fake)",
    "score_js": "engine/js_engine.py:JsExecutionEngine.score (benchmark_worker._build_score_fn)",
    "aggregate": "harness/benchmark_worker.py:_build_scorecard / _compute_avg_score",
    "adopt": "epoch/manager.py:_should_adopt",
}

__all__ = [
    "Scenario", "Fill", "LabConfig", "RunTrace", "StageRecord", "SolverResult",
    "FakeBackend", "run_solver", "compare", "ScoreJsStage", "aggregate", "build_state",
    "ADOPT_RULES", "PORT_MAP", "make_engine",
    "build_report", "diff_reports", "compute_report_hash", "scorer_digest",
    "DEFAULT_DEX_SCORER", "DEFAULT_DEX_CONTRACT", "DEFAULT_GENESIS_IMAGE",
]


async def make_engine(scorer_path: str, app_ids, timeout_ms: int = 10_000) -> JsExecutionEngine:
    """Load the real JS scorer under each app_id (one scorer can back several demo apps)."""
    engine = JsExecutionEngine(timeout_ms=timeout_ms)
    code = Path(scorer_path).read_text()
    for app_id in dict.fromkeys(app_ids):  # de-dup, preserve order
        await engine.load_intent(app_id, code)
    return engine
