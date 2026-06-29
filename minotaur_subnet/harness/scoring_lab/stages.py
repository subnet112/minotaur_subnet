"""The scoring stages — each a thin wrapper over a REAL production callable.

Backends are swappable; the always-real stages (ScoreJS, Aggregate, Adopt) are
shared by both the fake and fork simulation paths. Every stage records its
input -> output into the RunTrace, and documents the real code it PORTS_TO so a
redesign change made here maps 1:1 onto production.
"""
from __future__ import annotations

import time

from minotaur_subnet.engine.js_engine import JsExecutionEngine
from minotaur_subnet.harness.benchmark_worker import BenchmarkScorecard, BenchmarkWorker
from minotaur_subnet.harness.orchestrator import BenchmarkResult
from minotaur_subnet.shared.types import (
    ExecutionPlan,
    Interaction,
    IntentState,
    ScoreResult,
    SimulationResult,
    TokenTransfer,
)

from .model import Fill, LabConfig, Scenario, StageRecord


def _ms(t0: float) -> float:
    return round((time.monotonic() - t0) * 1000, 2)


def realized_output(sim: SimulationResult, scenario: Scenario) -> int | None:
    """Output tokens actually delivered to the receiver (mirrors the dex scorer's matching)."""
    recv, app, tok = scenario.receiver.lower(), scenario.contract_address.lower(), scenario.output_token.lower()
    out = 0
    for t in (sim.token_transfers or []):
        if (getattr(t, "token", "") or "").lower() == tok and \
           (getattr(t, "to_addr", "") or "").lower() in (recv, app):
            try:
                out += int(t.amount)
            except (TypeError, ValueError):
                pass
    return out or None


def build_state(scenario: Scenario) -> IntentState:
    """The IntentState a solver + the scorer see (PORTS_TO benchmark_worker._enrich_intents_with_manifests)."""
    params = {
        "input_token": scenario.input_token,
        "output_token": scenario.output_token,
        "input_amount": scenario.input_amount,
        "min_output_amount": scenario.min_output_amount,
        "receiver": scenario.receiver,
        "quoted_output": scenario.quoted_output or scenario.min_output_amount,
    }
    return IntentState(
        contract_address=scenario.contract_address,
        chain_id=scenario.chain_id,
        nonce=0,
        owner=scenario.receiver,
        raw_params=params,
        control={
            "_intent_function": scenario.intent_function,
            "_scenario_name": scenario.name,
            "_stage": scenario.stage,
            "_fund": {scenario.input_token: int(scenario.input_amount)},
        },
    )


# ─────────────────────────────────────────────────────────────────────────────
# Stage 1+2 — FAKE backend: fabricate (plan, simulation) from a scripted Fill.
# PORTS_TO: orchestrator.SolverSession.generate_plan (Solve) + simulator.simulate (Simulate)
# ─────────────────────────────────────────────────────────────────────────────
def fake_solve_simulate(
    scenario: Scenario, fill: Fill,
) -> tuple[ExecutionPlan, SimulationResult, list[StageRecord]]:
    t0 = time.monotonic()
    plan = ExecutionPlan(
        intent_id=f"{scenario.app_id}:{scenario.name}",
        interactions=[Interaction(
            target=scenario.contract_address, value="0", call_data="0x", chain_id=scenario.chain_id,
        )],
        deadline=0, nonce=0,
        metadata={"output_token": scenario.output_token,
                  "min_output_amount": scenario.min_output_amount,
                  "route": "fake", "chain_id": scenario.chain_id},
    )
    solve_rec = StageRecord(
        stage="solve", scenario=scenario.name, ok=True,
        summary=f"fabricated plan ({len(plan.interactions)} interaction)",
        inputs={"params": {k: scenario.to_dict()[k] for k in
                           ("input_token", "output_token", "input_amount", "min_output_amount")}},
        outputs={"interactions": len(plan.interactions), "route": "fake"},
        meta={"backend": "fake"}, duration_ms=_ms(t0),
    )

    t1 = time.monotonic()
    transfers: list[TokenTransfer] = []
    if fill.success and fill.output_amount is not None:
        transfers.append(TokenTransfer(
            token=scenario.output_token, from_addr=scenario.contract_address,
            to_addr=scenario.receiver, amount=str(fill.output_amount),
        ))
    sim = SimulationResult(
        success=fill.success, gas_used=fill.gas_used,
        error=None if fill.success else "scoreIntent reverted (fake)",
        token_transfers=transfers,
        on_chain_score=fill.on_chain_score,
    )
    sim_rec = StageRecord(
        stage="simulate", scenario=scenario.name, ok=sim.success,
        summary=(f"output={fill.output_amount} gas={fill.gas_used} "
                 f"on_chain={fill.on_chain_score}" if sim.success else "revert"),
        inputs={"fill": {"output_amount": fill.output_amount, "gas_used": fill.gas_used,
                         "success": fill.success}},
        outputs={"success": sim.success, "gas_used": sim.gas_used,
                 "transfers": len(transfers), "on_chain_score": sim.on_chain_score},
        meta={"backend": "fake (no fork)"}, duration_ms=_ms(t1),
    )
    return plan, sim, [solve_rec, sim_rec]


# ─────────────────────────────────────────────────────────────────────────────
# Stage 3 — ScoreJS (ALWAYS REAL). PORTS_TO: benchmark_worker._build_score_fn -> engine.score
# ─────────────────────────────────────────────────────────────────────────────
class ScoreJsStage:
    PORTS_TO = "engine/js_engine.py:JsExecutionEngine.score (via benchmark_worker._build_score_fn)"

    def __init__(self, engine: JsExecutionEngine) -> None:
        self.engine = engine

    async def run(
        self, scenario: Scenario, plan: ExecutionPlan, sim: SimulationResult, state: IntentState,
    ) -> tuple[ScoreResult, StageRecord]:
        t0 = time.monotonic()
        res = await self.engine.score(scenario.app_id, plan, sim, state)
        bd = res.breakdown or {}
        rec = StageRecord(
            stage="score_js", scenario=scenario.name, ok=res.valid,
            summary=f"js={res.score:.4f} valid={res.valid} :: {res.reason}",
            inputs={"min_output": scenario.min_output_amount,
                    "transfers": len(sim.token_transfers), "gas_used": sim.gas_used},
            outputs={"score": round(res.score, 6), "valid": res.valid,
                     "breakdown": {k: round(v, 4) if isinstance(v, (int, float)) else v
                                   for k, v in bd.items()}},
            meta={"on_chain_score": sim.on_chain_score, "reason": res.reason},
            duration_ms=_ms(t0),
        )
        return res, rec


# ─────────────────────────────────────────────────────────────────────────────
# Stage 4 — Aggregate (ALWAYS REAL). PORTS_TO: benchmark_worker._build_scorecard / _compute_avg_score
# ─────────────────────────────────────────────────────────────────────────────
_WORKER = BenchmarkWorker.__new__(BenchmarkWorker)  # pure helper; no ctor deps used by these methods


def aggregate(brs: list[BenchmarkResult]) -> tuple[BenchmarkScorecard, StageRecord]:
    t0 = time.monotonic()
    card = _WORKER._build_scorecard(brs)
    rec = StageRecord(
        stage="aggregate", scenario="(all)", ok=True,
        summary=(f"global={card.global_score:.4f} apps="
                 + ",".join(f"{k}={v:.3f}" for k, v in sorted(card.app_scores.items()))),
        inputs={"results": len(brs)},
        outputs={"global_score": round(card.global_score, 6),
                 "app_scores": {k: round(v, 6) for k, v in card.app_scores.items()},
                 "failures": card.failures, "total": card.total},
        meta={"ports_to": "benchmark_worker._build_scorecard / _compute_avg_score"},
        duration_ms=_ms(t0),
    )
    return card, rec


# ─────────────────────────────────────────────────────────────────────────────
# Stage 5 — Adopt.
# ─────────────────────────────────────────────────────────────────────────────
class AdoptRule:
    name = "?"

    def evaluate(self, champ_card, chal_card, champ_oc, chal_oc, cfg, champ_qa=None, chal_qa=None) -> tuple[bool, StageRecord]:
        raise NotImplementedError


class CurrentAdoptRule(AdoptRule):
    """Today's gate, verbatim. PORTS_TO: epoch/manager.py:_should_adopt."""

    name = "current"

    def evaluate(self, champ_card, chal_card, champ_oc, chal_oc, cfg, champ_qa=None, chal_qa=None) -> tuple[bool, StageRecord]:
        t0 = time.monotonic()
        # Drive the floors by passing an explicit config to the SAME pure rule the
        # production gate runs (evaluate_adoption). Production never passes a config
        # (it always uses the fleet-uniform code constants), so the lab is the only
        # caller that sweeps these — and it does so WITHOUT mutating process env, so a
        # sweep value can never leak into a live validator's rule.
        from minotaur_subnet.epoch.adopt_rule import _AdoptRuleConfig, evaluate_adoption
        lab_config = _AdoptRuleConfig(
            per_app_min_score=cfg.per_app_min_score,
            max_app_regression=cfg.max_app_regression,
            onchain_max_regression=cfg.max_app_regression,
        )
        adopt, _reason = evaluate_adoption(
            challenger_score=chal_card.global_score,
            champion_score=champ_card.global_score,
            challenger_scorecard=chal_card.to_dict(),
            champion_scorecard=champ_card.to_dict(),
            dethrone_margin=cfg.dethrone_margin,
            has_champion=True,
            config=lab_config,
        )
        rec = StageRecord(
            stage="adopt", scenario="(all)", ok=True,
            summary=f"rule=current -> {'ADOPT' if adopt else 'REJECT'}",
            inputs={"champion_global": round(champ_card.global_score, 6),
                    "challenger_global": round(chal_card.global_score, 6),
                    "dethrone_margin": cfg.dethrone_margin,
                    "max_app_regression": cfg.max_app_regression},
            outputs={"adopt": adopt},
            meta={"ports_to": "epoch/manager.py:_should_adopt",
                  "note": "on_chain_score is NOT consulted by the current contest"},
            duration_ms=_ms(t0),
        )
        return adopt, rec


ADOPT_RULES = {"current": CurrentAdoptRule}
