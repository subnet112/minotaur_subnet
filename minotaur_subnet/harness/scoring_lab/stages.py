"""The scoring stages — each a thin wrapper over a REAL production callable.

Backends are swappable; the always-real stages (ScoreJS, Aggregate, Adopt) are
shared by both the fake and fork simulation paths. Every stage records its
input -> output into the RunTrace, and documents the real code it PORTS_TO so a
redesign change made here maps 1:1 onto production.
"""
from __future__ import annotations

import os
import time
from typing import Any

from minotaur_subnet.engine.js_engine import JsExecutionEngine
from minotaur_subnet.epoch.manager import EpochManager
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
from types import SimpleNamespace

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
        token_transfers=transfers, price_impact=fill.price_impact,
        on_chain_score=fill.on_chain_score,
    )
    sim_rec = StageRecord(
        stage="simulate", scenario=scenario.name, ok=sim.success,
        summary=(f"output={fill.output_amount} gas={fill.gas_used} "
                 f"on_chain={fill.on_chain_score}" if sim.success else "revert"),
        inputs={"fill": {"output_amount": fill.output_amount, "gas_used": fill.gas_used,
                         "price_impact": fill.price_impact, "success": fill.success}},
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
# Stage 5 — Adopt. Two swappable rules.
# ─────────────────────────────────────────────────────────────────────────────
def _onchain_pass(scores: list[int | None], floor: int) -> tuple[bool, int | None, int]:
    """all_pass, min_bps, n_missing — a champion-covered app must clear the floor on every scenario."""
    present = [s for s in scores if s is not None]
    n_missing = sum(1 for s in scores if s is None)
    all_pass = n_missing == 0 and all(s >= floor for s in present)
    return all_pass, (min(present) if present else None), n_missing


def _app_onchain_mean(scores: list[int | None]) -> float | None:
    """Mean on-chain (scoreIntent BPS) over the present scenarios for an app — the unfakeable
    output-quality signal used for the no-regression check (independent of the gas-weighted JS score)."""
    present = [s for s in scores if s is not None]
    return (sum(present) / len(present)) if present else None


class AdoptRule:
    name = "?"

    def evaluate(self, champ_card, chal_card, champ_oc, chal_oc, cfg, champ_qa=None, chal_qa=None) -> tuple[bool, StageRecord]:
        raise NotImplementedError


class CurrentAdoptRule(AdoptRule):
    """Today's gate, verbatim. PORTS_TO: epoch/manager.py:_should_adopt."""

    name = "current"

    def evaluate(self, champ_card, chal_card, champ_oc, chal_oc, cfg, champ_qa=None, chal_qa=None) -> tuple[bool, StageRecord]:
        t0 = time.monotonic()
        # Drive the floors via env (as the real gate reads them) + margin via the ctor field.
        prev = {k: os.environ.get(k) for k in
                ("MIN_CHAMPION_SCORE", "PER_APP_MIN_SCORE", "MAX_APP_REGRESSION")}
        os.environ["MIN_CHAMPION_SCORE"] = str(cfg.min_champion_score)
        os.environ["PER_APP_MIN_SCORE"] = str(cfg.per_app_min_score)
        os.environ["MAX_APP_REGRESSION"] = str(cfg.max_app_regression)
        try:
            mgr = EpochManager.__new__(EpochManager)
            mgr._champion = SimpleNamespace(submission_id="champion",
                                            benchmark_score=champ_card.global_score)
            mgr._dethrone_margin = cfg.dethrone_margin
            mgr._get_incumbent_scorecard = lambda: champ_card.to_dict()
            mgr._get_scorecard = lambda sub: chal_card.to_dict()
            adopt = mgr._should_adopt(
                SimpleNamespace(submission_id="challenger", benchmark_score=chal_card.global_score))
        finally:
            for k, v in prev.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
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


class P2AdoptRule(AdoptRule):
    """Prototype of the design's rule: per-app no-regression on the JS metric ABOVE a
    per-app on-chain floor, plus a deliberate net-gain margin. NOT yet in production —
    this is the stage you iterate on, then port to epoch/manager.py:_should_adopt.
    """

    name = "p2"

    def evaluate(self, champ_card, chal_card, champ_oc, chal_oc, cfg, champ_qa=None, chal_qa=None) -> tuple[bool, StageRecord]:
        t0 = time.monotonic()
        floor = cfg.on_chain_floor if cfg.on_chain_floor is not None else 0
        reasons: list[str] = []
        adopt = True

        champ_apps = champ_card.app_scores
        chal_apps = chal_card.app_scores

        # (a) On-chain admission floor — every champion-covered app must clear it.
        floor_detail: dict[str, Any] = {}
        for app in champ_apps:
            all_pass, min_bps, n_missing = _onchain_pass(chal_oc.get(app, []), floor)
            floor_detail[app] = {"all_pass": all_pass, "min_bps": min_bps, "n_missing": n_missing}
            if cfg.on_chain_floor is not None and not all_pass:
                adopt = False
                reasons.append(f"{app}: on-chain floor fail (min={min_bps} missing={n_missing})")

        # (b) Per-app no-regression on the JS metric (strict: cover every champion app).
        for app, inc in champ_apps.items():
            ch = chal_apps.get(app)
            if ch is None:
                adopt = False
                reasons.append(f"{app}: dropped (champion covers it)")
                continue
            if inc > 0 and ch < inc * (1 - cfg.max_app_regression):
                adopt = False
                reasons.append(f"{app}: regress {inc:.3f}->{ch:.3f}")

        # (c) Net gain at a deliberate margin (global, on the JS metric).
        required = champ_card.global_score * (1 + cfg.dethrone_margin)
        if chal_card.global_score < required:
            adopt = False
            reasons.append(f"net gain {chal_card.global_score:.4f} < required {required:.4f}")

        rec = StageRecord(
            stage="adopt", scenario="(all)", ok=True,
            summary=f"rule=p2 -> {'ADOPT' if adopt else 'REJECT'}" + (
                "" if adopt else f" :: {'; '.join(reasons)}"),
            inputs={"on_chain_floor": cfg.on_chain_floor, "dethrone_margin": cfg.dethrone_margin,
                    "max_app_regression": cfg.max_app_regression},
            outputs={"adopt": adopt, "reasons": reasons, "onchain_floor_by_app": floor_detail},
            meta={"ports_to": "epoch/manager.py:_should_adopt (redesign target)",
                  "note": "graded on-chain ranking is a TODO; this enforces the floor as admission"},
            duration_ms=_ms(t0),
        )
        return adopt, rec


class P2RefAdoptRule(AdoptRule):
    """Reference-anchored adoption (the design's head-to-head). The decision is the per-app
    SURPLUS — challenger app-score minus champion app-score on the IDENTICAL sealed cases —
    above the on-chain floor, with a usage-weighted net surplus over a deliberate margin.
    (Phase 1's quote-derived min makes the JS score a champion-relative metric, so the surplus
    is meaningful even though absolute scores saturate ~0.5.) Usage weights = equal for now
    (sybil-hardened weighting is Phase 8). Ports to: epoch/manager.py:_should_adopt.
    """

    name = "p2ref"

    def evaluate(self, champ_card, chal_card, champ_oc, chal_oc, cfg, champ_qa=None, chal_qa=None) -> tuple[bool, StageRecord]:
        t0 = time.monotonic()
        floor = cfg.on_chain_floor
        champ_apps = champ_card.app_scores
        chal_apps = chal_card.app_scores
        reasons: list[str] = []
        flags: list[str] = []
        diffs: dict[str, Any] = {}
        surpluses: list[float] = []
        adopt = True

        for app, inc in champ_apps.items():
            ch = chal_apps.get(app)
            # (1) on-chain admission floor on every champion-covered app
            if floor is not None:
                all_pass, min_bps, n_missing = _onchain_pass(chal_oc.get(app, []), floor)
                if not all_pass:
                    adopt = False
                    reasons.append(f"{app}: on-chain floor fail (min={min_bps} missing={n_missing})")
            # (2) coverage — dropping a champion-covered app is a hard regression
            if ch is None:
                adopt = False
                reasons.append(f"{app}: dropped")
                diffs[app] = {"champion": round(inc, 4), "challenger": None, "surplus": None}
                continue
            surplus = ch - inc
            diffs[app] = {"champion": round(inc, 4), "challenger": round(ch, 4), "surplus": round(surplus, 4)}
            surpluses.append(surplus)
            # (2b) on-chain OUTPUT no-regression — the unfakeable signal. A challenger that
            #      delivers LESS output (lower scoreIntent BPS) cannot win on the gas-inflated
            #      JS score. The JS discriminator can disagree with the on-chain anchor; here
            #      the honest output metric vetoes a JS win that hurts users.
            co = _app_onchain_mean(champ_oc.get(app, []))
            cco = _app_onchain_mean(chal_oc.get(app, []))
            if co is not None:
                diffs[app]["onchain"] = {"champion": round(co, 1),
                                         "challenger": (round(cco, 1) if cco is not None else None)}
                if cco is None:
                    adopt = False
                    reasons.append(f"{app}: no on-chain score (champion {co:.0f})")
                elif cco < co - cfg.onchain_regression_bps:
                    adopt = False
                    reasons.append(f"{app}: on-chain output regresses {co:.0f}->{cco:.0f} BPS")
            # (3) per-app JS no-regression
            if inc > 0 and ch < inc * (1 - cfg.max_app_regression):
                adopt = False
                reasons.append(f"{app}: regress {inc:.3f}->{ch:.3f}")
            # (4) quote accuracy (Phase 2b) — sandbag = challenger under-quotes (mean_err>0)
            #     MORE than the champion → inflates the CoW fee = share of (gained - quoted).
            if chal_qa is not None:
                cq = (chal_qa.get(app) or {}).get("mean_err")
                pq = (champ_qa.get(app) or {}).get("mean_err") if champ_qa else None
                if cq is not None:
                    diffs[app]["quote_err"] = round(cq, 4)
                    if pq is not None:
                        extra = cq - pq
                        diffs[app]["extra_sandbag"] = round(extra, 4)
                        if cfg.max_extra_sandbag is not None and extra > cfg.max_extra_sandbag:
                            adopt = False
                            reasons.append(f"{app}: sandbags quote +{extra:.3f} > {cfg.max_extra_sandbag:g} vs champion")
                        elif extra > 0.02:
                            flags.append(f"{app}: under-quotes +{extra:.3f} vs champion")

        # (5) net gain: usage-weighted (equal for now) mean surplus must beat the margin
        net = sum(surpluses) / len(surpluses) if surpluses else 0.0
        if net <= cfg.dethrone_margin:
            adopt = False
            reasons.append(f"net surplus {net:+.4f} <= margin {cfg.dethrone_margin:g}")

        rec = StageRecord(
            stage="adopt", scenario="(all)", ok=True,
            summary=(f"rule=p2ref -> {'ADOPT' if adopt else 'REJECT'} (net surplus {net:+.4f})"
                     + ("" if adopt else f" :: {'; '.join(reasons)}")
                     + (f"  [flags: {'; '.join(flags)}]" if flags else "")),
            inputs={"on_chain_floor": floor, "margin": cfg.dethrone_margin,
                    "max_app_regression": cfg.max_app_regression, "max_extra_sandbag": cfg.max_extra_sandbag},
            outputs={"adopt": adopt, "net_surplus": round(net, 6),
                     "per_app_diff": diffs, "reasons": reasons, "flags": flags},
            meta={"ports_to": "epoch/manager.py:_should_adopt (reference-anchored target)",
                  "note": "decision = per-app surplus above on-chain floor; usage weights TODO (Phase 8)"},
            duration_ms=_ms(t0),
        )
        return adopt, rec


ADOPT_RULES = {"current": CurrentAdoptRule, "p2": P2AdoptRule, "p2ref": P2RefAdoptRule}
