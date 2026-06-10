"""The scoring pipeline — threads scenarios through the stages and A/Bs two solvers.

Backend-agnostic: a "solve+simulate backend" yields (plan, simulation, state) for
each scenario (fabricated in fake mode, real solver+fork in fork mode); the
always-real ScoreJS + Aggregate + Adopt stages run identically on top. Produces a
full RunTrace per solver so every stage is inspectable.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Protocol

from minotaur_subnet.engine.js_engine import JsExecutionEngine
from minotaur_subnet.harness.benchmark_worker import BenchmarkScorecard
from minotaur_subnet.harness.orchestrator import BenchmarkResult
from minotaur_subnet.shared.types import ExecutionPlan, IntentState, SimulationResult

from .model import Fill, LabConfig, RunTrace, Scenario, StageRecord
from .stages import ADOPT_RULES, ScoreJsStage, aggregate, build_state, fake_solve_simulate, realized_output


class SolveSimBackend(Protocol):
    """Produces (plan, simulation, state, stage records, solver's own quote) for one scenario."""

    async def run(
        self, scenario: Scenario,
    ) -> tuple[ExecutionPlan, SimulationResult, IntentState, list[StageRecord], int | None]:
        ...


class FakeBackend:
    """Scripted Solve+Simulate from a {scenario_name: Fill} map (no fork/solver)."""

    def __init__(self, fills: dict[str, Fill]) -> None:
        self.fills = fills

    async def run(self, scenario: Scenario):
        fill = self.fills.get(scenario.name, Fill.at_ratio(scenario, 1.0))
        plan, sim, recs = fake_solve_simulate(scenario, fill)
        quoted = (int(fill.quoted_output) if fill.quoted_output
                  else (int(fill.output_amount) if (fill.success and fill.output_amount) else None))
        return plan, sim, build_state(scenario), recs, quoted


@dataclass
class SolverResult:
    label: str
    trace: RunTrace
    card: BenchmarkScorecard
    onchain_by_app: dict[str, list[int | None]]
    per_case: dict[str, dict] = field(default_factory=dict)
    quote_by_app: dict[str, dict] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "scorecard": self.card.to_dict(),
            "onchain_by_app": self.onchain_by_app,
            "quote_by_app": self.quote_by_app,
            "per_case": self.per_case,
            "trace": self.trace.to_dict(),
        }


async def run_solver(
    label: str,
    scenarios: list[Scenario],
    backend: SolveSimBackend,
    engine: JsExecutionEngine,
    cfg: LabConfig,
) -> SolverResult:
    trace = RunTrace(solver=label, config=cfg.to_dict())
    scorejs = ScoreJsStage(engine)
    brs: list[BenchmarkResult] = []
    onchain_by_app: dict[str, list[int | None]] = defaultdict(list)
    per_case: dict[str, dict] = {}
    qerr_by_app: dict[str, list[float]] = defaultdict(list)

    for sc in scenarios:
        plan, sim, state, recs, quoted = await backend.run(sc)
        for r in recs:
            trace.add(r)
        res, srec = await scorejs.run(sc, plan, sim, state)
        trace.add(srec)
        brs.append(BenchmarkResult(
            intent_id=f"{sc.app_id}:{sc.name}",
            plan=object() if res.valid else None,
            score=res.score,
            error=None if res.valid else res.reason,
        ))
        onchain_by_app[sc.app_id].append(sim.on_chain_score)
        # quote accuracy: realized vs the solver's OWN quote (positive = under-quote / sandbag)
        realized = realized_output(sim, sc)
        qerr = ((realized - quoted) / realized) if (quoted and realized) else None
        if qerr is not None:
            qerr_by_app[sc.app_id].append(qerr)
        per_case[f"{sc.app_id}:{sc.name}"] = {
            "app_id": sc.app_id, "scenario": sc.name, "js": round(res.score, 6), "valid": res.valid,
            "on_chain": sim.on_chain_score, "realized": realized, "quoted": quoted,
            "quote_err": (round(qerr, 6) if qerr is not None else None),
        }

    quote_by_app = {app: {"mean_err": sum(v) / len(v), "n": len(v)} for app, v in qerr_by_app.items()}
    card, arec = aggregate(brs)
    trace.add(arec)
    trace.scorecard = card.to_dict()
    return SolverResult(label, trace, card, dict(onchain_by_app), per_case, quote_by_app)


def compare(champion: SolverResult, challenger: SolverResult, cfg: LabConfig) -> tuple[bool, StageRecord]:
    """Run the selected adoption rule over the two solvers' results."""
    rule = ADOPT_RULES[cfg.adopt_rule]()
    adopt, rec = rule.evaluate(
        champion.card, challenger.card,
        champion.onchain_by_app, challenger.onchain_by_app, cfg,
        champ_qa=champion.quote_by_app, chal_qa=challenger.quote_by_app,
    )
    challenger.trace.add(rec)
    return adopt, rec
