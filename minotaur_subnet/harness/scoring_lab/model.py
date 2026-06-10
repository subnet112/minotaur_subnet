"""Scoring-lab data model — controlled inputs, config knobs, and the run trace.

Everything here is JSON-serialisable so a whole run can be dumped, diffed, and
replayed. The lab is a thin orchestration over the REAL scoring callables; this
module only describes *what flows between stages*, never how scoring works.
"""
from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field
from typing import Any

from minotaur_subnet.epoch.manager import DETHRONE_MARGIN

# The DexAggregator scorer JS lives in the apps repo (not in this repo). Point
# MINOTAUR_DEX_SCORER at your checkout's contracts/scoring/dex_aggregator_scoring.js.
DEFAULT_DEX_SCORER = os.environ.get("MINOTAUR_DEX_SCORER", "")
# App contract addresses are NOT hardcoded — they're resolved from the on-chain
# AppRegistry by app_id (see registry.py), exactly like production resolves
# app_id -> contractAddr. ZERO_ADDR is only a fake-backend placeholder (the fake
# sim never touches a real contract).
ZERO_ADDR = "0x0000000000000000000000000000000000000000"
DEFAULT_GENESIS_IMAGE = os.environ.get("GENESIS_SOLVER_IMAGE", "minotaur-genesis:fee-test")


@dataclass
class Scenario:
    """One order to score (the params a user would submit)."""

    name: str
    input_token: str
    output_token: str
    input_amount: str
    min_output_amount: str
    app_id: str = "dex"                 # scorecard grouping key == engine scorer key
    chain_id: int = 8453
    receiver: str = "0x0000000000000000000000000000000000000001"
    contract_address: str = ZERO_ADDR   # fork mode overwrites this from the AppRegistry
    intent_function: str = "swap"
    stage: str = "synthetic"            # "synthetic" (Stage 1, 0.4) | "historical" (Stage 2, 0.6)
    quoted_output: str | None = None    # CoW-fee reference (12th intent param), defaults to min

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Fill:
    """What a solver delivered for a scenario (used by the FAKE simulate backend)."""

    output_amount: str | None = None    # delivered output (wei str); None = nothing delivered
    gas_used: int = 150_000
    price_impact: float = 0.0
    success: bool = True
    on_chain_score: int | None = None   # BPS 0..10000 (informational in current contest)
    quoted_output: str | None = None    # the solver's OWN quote estimate (for quote-accuracy grading)

    @classmethod
    def from_spec(cls, scenario: "Scenario", spec: dict[str, Any]) -> "Fill":
        out = spec.get("output_amount")
        if out is None and "output_ratio" in spec:
            out = str(int(int(scenario.min_output_amount) * float(spec["output_ratio"])))
        q = spec.get("quoted_output")
        if q is None and "quoted_ratio" in spec:
            q = str(int(int(scenario.min_output_amount) * float(spec["quoted_ratio"])))
        return cls(
            output_amount=out,
            gas_used=int(spec.get("gas_used", 150_000)),
            price_impact=float(spec.get("price_impact", 0.0)),
            success=bool(spec.get("success", True)),
            on_chain_score=spec.get("on_chain_score"),
            quoted_output=q,
        )

    @classmethod
    def at_ratio(cls, scenario: "Scenario", ratio: float, **kw: Any) -> "Fill":
        return cls(output_amount=str(int(int(scenario.min_output_amount) * ratio)), **kw)

    @classmethod
    def revert(cls, **kw: Any) -> "Fill":
        kw.setdefault("gas_used", 0)
        return cls(output_amount=None, success=False, **kw)


@dataclass
class LabConfig:
    """Every knob, logged at run start so a run is fully reproducible."""

    # simulation backend
    sim: str = "fake"                          # "fake" | "fork"
    scorer_path: str = DEFAULT_DEX_SCORER
    # fork-mode wiring
    base_upstream_rpc: str | None = None       # live Base RPC the anvil forks from
    anvil_rpc: str | None = None               # local anvil RPC (lab starts one if None)
    fork_block: int | None = None              # pin a block (sealed) vs None (live head)
    # App contract resolution: the lab reads app_id -> contractAddr from the on-chain
    # AppRegistry (like production), never a hardcoded address. Supply the registry via
    # app_registry / $APP_REGISTRY_{chain_id} and the app via registry_app_id; or pass an
    # explicit contract_address to override (it is verified as registered).
    app_registry: str | None = None            # AppRegistry address (else $APP_REGISTRY_{chain})
    registry_app_id: str | None = None         # the on-chain bytes32 appId to resolve
    contract_address: str = ""                 # resolved from the registry in fork mode
    genesis_image: str = DEFAULT_GENESIS_IMAGE
    # adoption-gate knobs (mirror the real env vars in epoch/manager.py)
    adopt_rule: str = "current"                # "current" | "p2"
    dethrone_margin: float = DETHRONE_MARGIN
    min_champion_score: float = 0.5
    per_app_min_score: float = 0.3
    max_app_regression: float = 0.10
    on_chain_floor: int | None = None          # BPS; used by the "p2" rule (and shown by both)
    # p2ref on-chain output no-regression tolerance (BPS): a challenger may not deliver LESS
    # output (lower scoreIntent BPS) than the champion by more than this, regardless of its JS
    # score. Guards against winning on the gas-inflated JS metric while giving users less.
    # Set to 5 from the measured cross-version residual: on-chain is bit-exact same-host but
    # jitters ~2 BPS across anvil versions (1.5.1 vs 1.6.0-nightly), so a strict 0 would be
    # flaky cross-host. 5 absorbs that jitter (2.5x) while still catching real regressions
    # (the V3-only candidate regressed ~10 BPS). Revisit once a true cross-MACHINE residual is known.
    onchain_regression_bps: float = 5.0
    # Phase 1 — quote-derived min: re-quote each case from the reference (champion) solver
    # at the sealed block instead of using the stale hardcoded manifest min.
    requote: bool = True
    slippage_bps: int = 50
    # Phase 2b — quote-accuracy grading: have each solver quote itself; grade estimated vs realized.
    grade_quote: bool = True
    # block adoption if the challenger sandbags (under-quotes) > this much MORE than the champion
    # (fraction; None = surface/flag only, no hard gate). Guards the CoW fee = share of (gained-quoted).
    max_extra_sandbag: float | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["scorer_path"] = os.path.basename(self.scorer_path)
        return d


@dataclass
class StageRecord:
    """One stage's input→output for one scenario (or '(all)' for run-level stages)."""

    stage: str
    scenario: str
    ok: bool
    summary: str
    inputs: dict[str, Any] = field(default_factory=dict)
    outputs: dict[str, Any] = field(default_factory=dict)
    meta: dict[str, Any] = field(default_factory=dict)
    duration_ms: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RunTrace:
    """The full per-stage trace of one solver run + its scorecard."""

    solver: str
    config: dict[str, Any]
    records: list[StageRecord] = field(default_factory=list)
    scorecard: dict[str, Any] | None = None

    def add(self, rec: StageRecord) -> StageRecord:
        self.records.append(rec)
        return rec

    def stage(self, name: str) -> list[StageRecord]:
        return [r for r in self.records if r.stage == name]

    def to_dict(self) -> dict[str, Any]:
        return {
            "solver": self.solver,
            "config": self.config,
            "scorecard": self.scorecard,
            "records": [r.to_dict() for r in self.records],
        }
