"""Scoring-lab CLI: demo (fake), run (fake from JSON), bench (real fork)."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
from typing import Any

from . import make_engine
from .model import DEFAULT_DEX_SCORER, DEFAULT_GENESIS_IMAGE, Fill, LabConfig, Scenario
from .stages import _card_global
from .pipeline import FakeBackend, SolverResult, compare, run_solver


# ── pretty printing ──────────────────────────────────────────────────────────
def _print_solver(res: SolverResult, floor: int | None = None) -> None:
    print(f"\n=== {res.label} ===")
    for rec in res.trace.stage("score_js"):
        oc = rec.meta.get("on_chain_score")
        oc_s = "—" if oc is None else str(oc)
        flag = ""
        if floor is not None:
            flag = "  [floor FAIL]" if (oc is None or oc < floor) else "  [floor ok]"
        print(f"  {rec.scenario:<24} {rec.summary}  on_chain={oc_s}{flag}")
    apps = ", ".join(f"{k}={v:.4f}" for k, v in sorted(res.card.app_scores.items()))
    print(f"  per-app: {apps}")
    print(f"  global : {_card_global(res.card):.4f}  "
          f"(coverage {res.card.coverage:.0%}, {res.card.total} scenarios)")
    if res.quote_by_app:
        qa = ", ".join(f"{k}={v['mean_err']:+.3f}" for k, v in sorted(res.quote_by_app.items()))
        print(f"  quote-err (realized−quoted)/realized: {qa}   (+ = under-quote/sandbag)")


def _print_adopt(rec) -> None:
    print(f"\n  ADOPTION  {rec.summary}")
    for k, v in rec.inputs.items():
        print(f"    {k}: {v}")
    diff = (rec.outputs or {}).get("per_app_diff")
    if diff:
        print("    head-to-head (champion → challenger, surplus):")
        for app, d in sorted(diff.items()):
            ch = "—" if d.get("challenger") is None else f"{d['challenger']:.4f}"
            sp = "—" if d.get("surplus") is None else f"{d['surplus']:+.4f}"
            extra = d.get("extra_sandbag")
            qstr = f"   sandbag {extra:+.3f}" if extra is not None else ""
            oc = d.get("onchain")
            ocstr = (f"   on-chain {oc['champion']:.0f}→"
                     f"{('%.0f' % oc['challenger']) if oc.get('challenger') is not None else '—'} BPS") if oc else ""
            print(f"      {app:<10} JS {d['champion']:.4f} → {ch}   surplus {sp}{ocstr}{qstr}")
    flags = (rec.outputs or {}).get("flags")
    if flags:
        print("    flags: " + "; ".join(flags))


# ── built-in fake demo ───────────────────────────────────────────────────────
def _demo_scenarios() -> list[Scenario]:
    WETH = "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"
    USDC = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
    WBTC = "0x2260FAC5E5542a773Aa44fBCfeDf7C193bc2C599"
    return [
        Scenario("WETH_to_USDC", WETH, USDC, "1000000000000000000", "1800000000", app_id="dexA", chain_id=1),
        Scenario("WBTC_to_USDC", WBTC, USDC, "10000000", "4000000000", app_id="dexA", chain_id=1),
        Scenario("WBTC_to_WETH", WBTC, WETH, "10000000", "2300000000000000000", app_id="dexB", chain_id=1),
        Scenario("WETH_to_USDC_2", WETH, USDC, "2000000000000000000", "3600000000", app_id="dexB", chain_id=1),
    ]


async def _demo(cfg: LabConfig) -> None:
    scenarios = _demo_scenarios()
    engine = await make_engine(cfg.scorer_path, [s.app_id for s in scenarios])
    print("Scoring lab — real dex scorer:", cfg.scorer_path)
    print("outputScore = min(1, 0.5 + (output/min-1)*0.5);  final = 0.7*output + 0.15*gas + 0.15*impact")

    champ = await run_solver("champion (1.2x min)", scenarios,
                             FakeBackend({s.name: Fill.at_ratio(s, 1.2, gas_used=160_000, on_chain_score=6000)
                                          for s in scenarios}), engine, cfg)

    # CASE 1 — challenger strictly better everywhere -> ADOPT
    print("\n" + "─" * 80 + "\nCASE 1 — challenger improves every app (1.5x)")
    chal1 = await run_solver("challenger (1.5x min)", scenarios,
                             FakeBackend({s.name: Fill.at_ratio(s, 1.5, gas_used=150_000, on_chain_score=7000)
                                          for s in scenarios}), engine, cfg)
    _print_solver(champ); _print_solver(chal1)
    adopt, rec = compare(champ, chal1, cfg); _print_adopt(rec)

    # CASE 2 — great on dexA, fails a dexB scenario (below min -> 0) -> per-app regression REJECT
    print("\n" + "─" * 80 + "\nCASE 2 — challenger great on dexA, breaks one dexB swap (below-min)")
    mixed: dict[str, Fill] = {}
    for s in scenarios:
        if s.app_id == "dexA":
            mixed[s.name] = Fill.at_ratio(s, 2.0, gas_used=150_000, on_chain_score=9000)
        else:
            mixed[s.name] = (Fill.at_ratio(s, 1.2, gas_used=150_000, on_chain_score=6000)
                             if s.name == "WBTC_to_WETH"
                             else Fill.at_ratio(s, 0.97, gas_used=150_000, on_chain_score=0))  # below min -> 0
    chal2 = await run_solver("challenger (dexA 2.0x / dexB 1 broken)", scenarios,
                             FakeBackend(mixed), engine, cfg)
    _print_solver(champ); _print_solver(chal2)
    adopt, rec = compare(champ, chal2, cfg); _print_adopt(rec)


# ── JSON-driven run (fake or fork) ───────────────────────────────────────────
async def _run_file(path: str, cfg: LabConfig, json_out: str | None) -> None:
    # `run` drives the FAKE backend over custom JSON scenarios + scripted fills.
    # The real-fork genesis-vs-candidate comparison is `bench`.
    spec = json.loads(Path(path).read_text())
    scenarios = [Scenario(**s) for s in spec["scenarios"]]
    engine = await make_engine(cfg.scorer_path, [s.app_id for s in scenarios])

    results: dict[str, SolverResult] = {}
    for label, fills_spec in spec.get("solvers", {}).items():
        backend = FakeBackend({s.name: Fill.from_spec(s, fills_spec.get(s.name, {"output_ratio": 1.0}))
                               for s in scenarios})
        res = await run_solver(label, scenarios, backend, engine, cfg)
        results[label] = res
        _print_solver(res, floor=cfg.on_chain_floor)

    if "champion" in results and "challenger" in results:
        adopt, rec = compare(results["champion"], results["challenger"], cfg)
        _print_adopt(rec)

    if json_out:
        Path(json_out).write_text(json.dumps({k: v.to_dict() for k, v in results.items()}, indent=2))
        print(f"\n  trace dumped -> {json_out}")


# ── fork bench: genesis vs candidate ─────────────────────────────────────────
async def _bench(cfg: LabConfig, candidate: str | None, json_out: str | None, limit: int | None) -> None:
    from .fork import run_bench
    await run_bench(cfg, candidate_solver=candidate, json_out=json_out,
                    print_solver=_print_solver, print_adopt=_print_adopt, limit=limit)


# ── arg parsing ──────────────────────────────────────────────────────────────
def _cfg_from_args(a: argparse.Namespace) -> LabConfig:
    return LabConfig(
        sim=getattr(a, "sim", "fake"),
        scorer_path=getattr(a, "scorer", DEFAULT_DEX_SCORER),
        adopt_rule=getattr(a, "rule", "current"),
        on_chain_floor=getattr(a, "on_chain_floor", None),
        base_upstream_rpc=getattr(a, "base_rpc", None) or os.environ.get("BASE_ALCHEMY_RPC_URL"),
        anvil_rpc=getattr(a, "anvil_rpc", None),
        fork_block=getattr(a, "fork_block", None),
        contract_address=getattr(a, "contract", "") or "",
        app_registry=getattr(a, "app_registry", None),
        registry_app_id=getattr(a, "app_id", None),
        genesis_image=getattr(a, "genesis_image", DEFAULT_GENESIS_IMAGE),
        requote=getattr(a, "requote", True),
        slippage_bps=getattr(a, "slippage_bps", 50),
        grade_quote=getattr(a, "grade_quote", True),
        max_extra_sandbag=getattr(a, "max_extra_sandbag", None),
        dethrone_margin=getattr(a, "margin", None) if getattr(a, "margin", None) is not None else LabConfig().dethrone_margin,
    )


def main() -> None:
    p = argparse.ArgumentParser(prog="scoring_lab", description=__doc__)
    sub = p.add_subparsers(dest="cmd")

    pd = sub.add_parser("demo", help="built-in fake A/B demo")
    pd.add_argument("--scorer", default=DEFAULT_DEX_SCORER)

    pr = sub.add_parser("run", help="run scenarios from a JSON file (fake or fork)")
    pr.add_argument("--scenarios", required=True)
    pr.add_argument("--scorer", default=DEFAULT_DEX_SCORER)
    pr.add_argument("--sim", choices=["fake", "fork"], default="fake")
    pr.add_argument("--rule", choices=["current"], default="current")
    pr.add_argument("--on-chain-floor", type=int, default=None)
    pr.add_argument("--max-extra-sandbag", type=float, default=None)
    pr.add_argument("--margin", type=float, default=None)
    pr.add_argument("--base-rpc", default=None)
    pr.add_argument("--anvil-rpc", default=None)
    pr.add_argument("--fork-block", type=int, default=None)
    pr.add_argument("--contract", default="", help="explicit app contract (else resolved from AppRegistry)")
    pr.add_argument("--app-registry", default=None, help="AppRegistry address (else $APP_REGISTRY_<chain>)")
    pr.add_argument("--app-id", default=None, help="on-chain bytes32 appId to resolve via AppRegistry")
    pr.add_argument("--json", dest="json_out", default=None)

    pb = sub.add_parser("bench", help="real fork: genesis vs candidate solver")
    pb.add_argument("--scorer", default=DEFAULT_DEX_SCORER)
    pb.add_argument("--base-rpc", default=None, help="live Base RPC to fork (default: $BASE_ALCHEMY_RPC_URL)")
    pb.add_argument("--anvil-rpc", default=None, help="existing anvil RPC (else the lab starts one)")
    pb.add_argument("--fork-block", type=int, default=None, help="pin a block (sealed) vs live head")
    pb.add_argument("--contract", default="", help="explicit app contract (else resolved from AppRegistry)")
    pb.add_argument("--app-registry", default=None, help="AppRegistry address (else $APP_REGISTRY_<chain>)")
    pb.add_argument("--app-id", default=None, help="on-chain bytes32 appId to resolve via AppRegistry")
    pb.add_argument("--genesis-image", default=DEFAULT_GENESIS_IMAGE)
    pb.add_argument("--candidate", default=None, help="path to a candidate solver.py (else the example candidate)")
    pb.add_argument("--rule", choices=["current"], default="current")
    pb.add_argument("--on-chain-floor", type=int, default=None)
    pb.add_argument("--margin", type=float, default=None)
    pb.add_argument("--limit", type=int, default=None, help="cap number of scenarios (quick smoke runs)")
    pb.add_argument("--no-requote", dest="requote", action="store_false",
                    help="use the stale hardcoded mins instead of fresh reference quotes")
    pb.add_argument("--slippage-bps", type=int, default=50, help="slippage for quote-derived min (default 50)")
    pb.add_argument("--no-grade-quote", dest="grade_quote", action="store_false",
                    help="skip per-solver quote grading (faster fork runs)")
    pb.add_argument("--max-extra-sandbag", type=float, default=None,
                    help="block adoption if the challenger under-quotes > this fraction MORE than the champion")
    pb.add_argument("--json", dest="json_out", default=None)

    a = p.parse_args()
    cmd = a.cmd or "demo"
    cfg = _cfg_from_args(a)

    if cmd == "demo":
        if not os.path.exists(cfg.scorer_path):
            raise SystemExit(f"dex scorer not found: {cfg.scorer_path}\n  pass --scorer or set MINOTAUR_DEX_SCORER")
        asyncio.run(_demo(cfg))
    elif cmd == "run":
        asyncio.run(_run_file(a.scenarios, cfg, a.json_out))
    elif cmd == "bench":
        cfg.sim = "fork"
        asyncio.run(_bench(cfg, a.candidate, a.json_out, a.limit))
    else:
        p.print_help()


if __name__ == "__main__":
    main()
