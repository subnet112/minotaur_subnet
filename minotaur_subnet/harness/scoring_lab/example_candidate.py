"""Example candidate solver for the scoring lab.

Extends the genesis ``BaselineSwapSolver`` — so it inherits Uniswap V3 **and
Aerodrome** routing plus multi-hop for free — and is where you make a change you
hope BEATS the genesis on real Base liquidity. The lab runs genesis vs this
candidate on a pinned Base fork and shows the adoption verdict.

By default this is parity with genesis (so the lab runs end-to-end and you see a
fair A/B). Edit ``generate_plan`` / the routing knobs to actually improve, then::

    python -m minotaur_subnet.harness.scoring_lab bench --candidate <this file>

Run as a harness subprocess; requires the solver-oss repo on PYTHONPATH (the lab
sets this for you when it launches the solver).
"""
from __future__ import annotations

from strategies.dex_aggregator.baseline_solver import BaselineSwapSolver  # genesis baseline
from minotaur_subnet.sdk.intent_solver import SolverMetadata


class CandidateSwapSolver(BaselineSwapSolver):
    def metadata(self) -> SolverMetadata:
        base = super().metadata()
        return SolverMetadata(
            name="candidate", version="0.1.0", author="lab",
            description="lab candidate — edit me to beat genesis",
            supported_chains=base.supported_chains,
            supported_intent_types=base.supported_intent_types,
        )

    # def generate_plan(self, intent, state, snapshot=None):
    #     # TODO: your improved routing here. Default = baseline best-executable
    #     # route across Uni V3 + Aerodrome. Example levers to try:
    #     #   - self._processor.slippage_bps (tighter/looser min-out)
    #     #   - widen pool discovery (more fee tiers / intermediary tokens)
    #     #   - a smarter split/route than _find_best_executable_route
    #     return super().generate_plan(intent, state, snapshot)


SOLVER_CLASS = CandidateSwapSolver
