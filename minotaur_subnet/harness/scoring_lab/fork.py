"""Real-fork Solve+Simulate backend.

Stands up a local Anvil fork of Base (live read of the deployed DexAggregator),
runs the real genesis + candidate solvers (as harness subprocesses) to generate
plans, and simulates each plan's on-chain ``scoreIntent`` on the fork — the exact
production path (orchestrator + AnvilSimulator), wrapped so every stage is traced.

Sealed by default: the fork is pinned to a single block (capture upstream head once),
so the solver and the simulator both see identical state — the determinism the
champion contest needs. Pass an explicit fork_block to replay a historical block.

External deps: a live Base RPC (``--base-rpc`` / $BASE_ALCHEMY_RPC_URL) to fork from,
anvil on PATH, and the genesis solver source at $MINOTAUR_SOLVER_OSS (your
minotaur-solver-oss checkout).
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable

import minotaur_subnet
from minotaur_subnet.harness.orchestrator import (
    BenchmarkConfig,
    SolverOrchestrator,
    _build_benchmark_intent_order,
    _build_token_balances,
)
from minotaur_subnet.sdk.intent_solver import MarketSnapshot
from minotaur_subnet.shared.types import AppIntentConfig, AppIntentDefinition, IntentState

from . import make_engine
from .model import LabConfig, Scenario, StageRecord
from .pipeline import compare, run_solver
from .stages import _ms, build_state

SOLVER_OSS = os.environ.get("MINOTAUR_SOLVER_OSS", "")  # set to your minotaur-solver-oss checkout
SUBNET_ROOT = str(Path(minotaur_subnet.__file__).resolve().parents[1])
GENESIS_SOLVER_PY = os.path.join(SOLVER_OSS, "solver.py")
EXAMPLE_CANDIDATE = str(Path(__file__).with_name("example_candidate.py"))


# ── RPC helpers ──────────────────────────────────────────────────────────────
def _rpc(url: str, method: str, params: list, timeout: float = 5.0) -> Any:
    body = json.dumps({"jsonrpc": "2.0", "method": method, "params": params, "id": 1}).encode()
    req = urllib.request.Request(url, data=body, headers={"content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read()).get("result")


def _upstream_head(upstream: str) -> int:
    return int(_rpc(upstream, "eth_blockNumber", []), 16)


# ── Anvil fork lifecycle ─────────────────────────────────────────────────────
class AnvilFork:
    def __init__(self, proc, rpc_url: str, block: int) -> None:
        self.proc = proc
        self.rpc_url = rpc_url
        self.block = block

    @classmethod
    async def launch(cls, upstream: str, chain_id: int, fork_block: int, port: int = 18650) -> "AnvilFork":
        anvil = shutil.which("anvil") or os.path.expanduser("~/.foundry/bin/anvil")
        cmd = [anvil, "--fork-url", upstream, "--fork-block-number", str(fork_block),
               "--chain-id", str(chain_id), "--port", str(port), "--no-storage-caching", "--silent"]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
        rpc_url = f"http://127.0.0.1:{port}"
        for _ in range(120):
            try:
                if _rpc(rpc_url, "eth_blockNumber", [], timeout=1.0):
                    return cls(proc, rpc_url, fork_block)
            except Exception:
                await asyncio.sleep(0.5)
        proc.kill()
        raise RuntimeError(f"anvil fork did not become ready on {rpc_url}")

    async def close(self) -> None:
        try:
            self.proc.kill()
            await self.proc.wait()
        except Exception:
            pass


def verify_contract(rpc_url: str, address: str) -> str | None:
    """Confirm a contract exists at *address* on the fork. Returns its relayer() if readable."""
    code = _rpc(rpc_url, "eth_call", [{"to": address, "data": "0x"}, "latest"]) if False else \
        _rpc(rpc_url, "eth_getCode", [address, "latest"])
    if not code or code == "0x":
        raise RuntimeError(
            f"No contract code at {address} on the fork.\n"
            f"  The tracked broadcast address may be retired — pass --contract <current address>.")
    relayer = None
    try:  # relayer() selector = keccak("relayer()")[:4] = 0x8406c079 (soft info check)
        ret = _rpc(rpc_url, "eth_call", [{"to": address, "data": "0x8406c079"}, "latest"])
        if ret and int(ret, 16) != 0:
            relayer = "0x" + ret[-40:]
    except Exception:
        pass
    return relayer


# ── intents from the scorer manifest ─────────────────────────────────────────
def base_scenarios(manifest: dict, cfg: LabConfig, app_id: str, chain_id: int) -> list[Scenario]:
    out: list[Scenario] = []
    for sc in manifest.get("benchmark_scenarios", []):
        chains = sc.get("chains")
        if chains and chain_id not in chains:
            continue
        p = sc.get("params", {})
        if not p.get("input_token") or not p.get("output_token"):
            continue
        out.append(Scenario(
            name=sc.get("name", "?"),
            input_token=p["input_token"], output_token=p["output_token"],
            input_amount=str(p.get("input_amount", "0")),
            min_output_amount=str(p.get("min_output_amount", "0")),
            app_id=app_id, chain_id=chain_id,
            receiver=p.get("receiver", "0x0000000000000000000000000000000000000001"),
            contract_address=cfg.contract_address,
            intent_function=sc.get("intent_function", "swap"),
            quoted_output=str(p.get("quoted_output", p.get("min_output_amount", "0"))),
        ))
    return out


def _app_def(app_id: str) -> AppIntentDefinition:
    # run_benchmark/generate_plan only need app_id + config.trigger_type (USER_TRIGGERED default).
    return AppIntentDefinition(app_id=app_id, name="DexAggregator", version="1.0.0",
                               intent_type="swap", js_code="", config=AppIntentConfig())


# ── the fork backend: real solver plan + real scoreIntent simulation ─────────
class ForkBackend:
    """SolveSimBackend backed by a running SolverSession + AnvilSimulator."""

    def __init__(self, session, simulator, cfg: LabConfig) -> None:
        self.session = session
        self.simulator = simulator
        self.cfg = cfg

    async def run(self, scenario: Scenario):
        state = build_state(scenario)
        intent = _app_def(scenario.app_id)
        snapshot = MarketSnapshot.empty(scenario.chain_id)

        # ── Stage 1: Solve (real solver, routes against the fork) ──
        t0 = time.monotonic()
        plan = await self.session.generate_plan(intent, state, snapshot)
        route = (plan.metadata or {}).get("route", "?") if plan else "none"
        solve_rec = StageRecord(
            stage="solve", scenario=scenario.name, ok=plan is not None,
            summary=f"route={route} interactions={len(plan.interactions) if plan else 0}",
            inputs={"input_amount": scenario.input_amount, "min_output": scenario.min_output_amount},
            outputs={"route": route, "interactions": len(plan.interactions) if plan else 0,
                     "targets": [i.target for i in (plan.interactions if plan else [])][:4]},
            meta={"backend": "fork", "ports_to": "SolverSession.generate_plan"}, duration_ms=_ms(t0))

        # ── Stage 2: Simulate (real scoreIntent on the fork) ──
        t1 = time.monotonic()
        if plan and plan.metadata is not None:
            plan.metadata.setdefault("chain_id", scenario.chain_id)
        token_balances = _build_token_balances(state)
        intent_order = _build_benchmark_intent_order(state, plan) if plan else None
        sim = await self.simulator.simulate(
            plan, contract_address=scenario.contract_address,
            intent_order=intent_order, token_balances=token_balances)
        sim_rec = StageRecord(
            stage="simulate", scenario=scenario.name, ok=sim.success,
            summary=(f"success={sim.success} on_chain={sim.on_chain_score} "
                     f"gas={sim.gas_used} transfers={len(sim.token_transfers)}"
                     + (f" err={sim.error}" if sim.error else "")),
            inputs={"contract": scenario.contract_address, "fork_block": self.cfg.fork_block,
                    "token_balances": token_balances},
            outputs={"success": sim.success, "on_chain_score": sim.on_chain_score,
                     "gas_used": sim.gas_used, "transfers": len(sim.token_transfers), "error": sim.error},
            meta={"backend": "fork", "ports_to": "anvil_simulator._simulate_via_score_intent"},
            duration_ms=_ms(t1))

        # Stage 1b: the solver's OWN quote (Phase 2b — graded vs realized for sandbag detection).
        quoted = None
        if self.cfg.grade_quote:
            try:
                q = await self.session.quote(intent, state, snapshot)
                quoted = int(q.estimated_output) if (q and getattr(q, "estimated_output", None)) else None
            except Exception:
                quoted = None
        return plan, sim, state, [solve_rec, sim_rec], quoted


async def _requote(session, scenarios: list[Scenario], cfg: LabConfig, chain_id: int) -> list[Scenario]:
    """Phase 1 — quote-derived min. Replace each case's stale hardcoded min_output with a
    FRESH reference quote (the champion's quote() at the sealed block): min = estimated*(1-slippage).
    This is what production does at order time (orders.py), which the benchmark never did.
    Ports to: benchmark_worker._enrich_intents_with_manifests honoring manifest source:"quote".
    """
    from dataclasses import replace
    print(f"[requote] reference quote @ pinned block (slippage {cfg.slippage_bps}bps):")
    out: list[Scenario] = []
    for sc in scenarios:
        state = build_state(sc)
        intent = _app_def(sc.app_id)
        snapshot = MarketSnapshot.empty(sc.chain_id)
        try:
            q = await session.quote(intent, state, snapshot)
        except Exception as exc:
            print(f"  {sc.name:<22} quote FAILED ({exc}) — keeping hardcoded min")
            out.append(sc)
            continue
        est = int(q.estimated_output) if (q and getattr(q, "estimated_output", None)) else 0
        if est <= 0:
            print(f"  {sc.name:<22} no quote — keeping hardcoded min {sc.min_output_amount}")
            out.append(sc)
            continue
        fresh_min = est * (10000 - cfg.slippage_bps) // 10000
        print(f"  {sc.name:<22} min {sc.min_output_amount} -> {fresh_min}  (quote est {est})")
        out.append(replace(sc, min_output_amount=str(fresh_min), quoted_output=str(est)))
    return out


async def _start_solver(orch: SolverOrchestrator, solver_path: str, rpc_url: str, chain_id: int):
    """Start a solver subprocess with the right PYTHONPATH and initialize it against the fork."""
    prev = os.environ.get("PYTHONPATH", "")
    os.environ["PYTHONPATH"] = os.pathsep.join(p for p in (SOLVER_OSS, SUBNET_ROOT, prev) if p)
    try:
        session = await orch.start_subprocess(solver_path)
    finally:
        os.environ["PYTHONPATH"] = prev
    await session.initialize({"chain_ids": [chain_id], "rpc_urls": {chain_id: rpc_url}})
    await session.on_benchmark_start(0)
    return session


# ── the entrypoint: genesis vs candidate on a real Base fork ─────────────────
async def run_bench(
    cfg: LabConfig, candidate_solver: str | None, json_out: str | None,
    print_solver: Callable, print_adopt: Callable, limit: int | None = None,
    chain_id: int = 8453, app_id: str = "dex",
) -> None:
    if not cfg.base_upstream_rpc and not cfg.anvil_rpc:
        raise SystemExit("need a Base RPC to fork: pass --base-rpc <url> or set BASE_ALCHEMY_RPC_URL")
    if not os.path.exists(GENESIS_SOLVER_PY):
        raise SystemExit(f"genesis solver source not found at {GENESIS_SOLVER_PY}\n"
                         f"  set MINOTAUR_SOLVER_OSS to the minotaur-solver-oss checkout")
    candidate_path = candidate_solver or EXAMPLE_CANDIDATE

    # Cold pool discovery makes the FIRST quote slow (Base RPC round-trips); the default
    # 5s QUOTE timeout kills the solver. Give it generate_plan's budget for lab runs.
    from minotaur_subnet.harness import protocol as _protocol
    _protocol.TIMEOUTS[_protocol.Command.QUOTE] = max(
        _protocol.TIMEOUTS.get(_protocol.Command.QUOTE, 5.0), 45.0)

    # 1. fork (sealed: pin a single block so solver + sim agree)
    if cfg.anvil_rpc:
        fork = None
        rpc_url = cfg.anvil_rpc
        block = cfg.fork_block or _upstream_head(rpc_url)
        print(f"[fork] using existing anvil at {rpc_url} (block {block})")
    else:
        block = cfg.fork_block or _upstream_head(cfg.base_upstream_rpc)
        print(f"[fork] launching anvil — Base @ block {block} (sealed) ...")
        fork = await AnvilFork.launch(cfg.base_upstream_rpc, chain_id, fork_block=block)
        rpc_url = fork.rpc_url
    cfg.fork_block = block

    try:
        # Resolve the app contract from the on-chain AppRegistry (never hardcoded) —
        # the same source production resolves app_id -> contractAddr from.
        needs_resolve = (not cfg.contract_address) or (
            cfg.contract_address.startswith("0x") and int(cfg.contract_address, 16) == 0)
        if needs_resolve:
            reg = cfg.app_registry or os.environ.get(f"APP_REGISTRY_{chain_id}", "").strip()
            if not reg or not cfg.registry_app_id:
                raise SystemExit(
                    f"no app contract: resolve from the AppRegistry by passing --app-id <bytes32> "
                    f"plus $APP_REGISTRY_{chain_id} / --app-registry, or pass --contract explicitly")
            from web3 import Web3
            from .registry import resolve_contract
            w3 = Web3(Web3.HTTPProvider(rpc_url))
            cfg.contract_address = resolve_contract(w3, reg, cfg.registry_app_id)
            print(f"[registry] {cfg.registry_app_id} -> {cfg.contract_address} (AppRegistry {reg})")
        relayer = verify_contract(rpc_url, cfg.contract_address)
        print(f"[fork] app {cfg.contract_address} OK"
              + (f" (relayer {relayer})" if relayer else " (code present)"))

        # 2. simulator (pinned -> no upstream resets)
        from minotaur_subnet.simulator.anvil_simulator import MultiChainSimulator
        sim = MultiChainSimulator(rpc_urls={chain_id: rpc_url}, default_chain_id=chain_id,
                                  upstream_rpc_urls=None)

        # 3. scenarios from the real scorer manifest (Base only)
        engine = await make_engine(cfg.scorer_path, [app_id])
        manifest = engine.get_manifest(app_id)
        scenarios = base_scenarios(manifest, cfg, app_id, chain_id)
        if limit:
            scenarios = scenarios[:limit]
        if not scenarios:
            raise SystemExit(f"no Base ({chain_id}) scenarios in the scorer manifest")
        print(f"[fork] {len(scenarios)} Base scenarios: {', '.join(s.name for s in scenarios)}")

        # 4. run genesis (the reference: its quote sets each case's fresh min), then candidate
        orch = SolverOrchestrator()
        results = {}

        print(f"\n[solver] starting genesis (reference): {os.path.basename(GENESIS_SOLVER_PY)}")
        genesis_session = await _start_solver(orch, GENESIS_SOLVER_PY, rpc_url, chain_id)
        try:
            if cfg.requote:
                scenarios = await _requote(genesis_session, scenarios, cfg, chain_id)
            results["genesis"] = await run_solver(
                "genesis", scenarios, ForkBackend(genesis_session, sim, cfg), engine, cfg)
        finally:
            await genesis_session.shutdown()
        print_solver(results["genesis"], floor=cfg.on_chain_floor)

        print(f"\n[solver] starting candidate: {os.path.basename(candidate_path)}")
        candidate_session = await _start_solver(orch, candidate_path, rpc_url, chain_id)
        try:
            results["candidate"] = await run_solver(
                "candidate", scenarios, ForkBackend(candidate_session, sim, cfg), engine, cfg)
        finally:
            await candidate_session.shutdown()
        print_solver(results["candidate"], floor=cfg.on_chain_floor)
    finally:
        if fork is not None:
            await fork.close()

    adopt, rec = compare(results["genesis"], results["candidate"], cfg)
    print_adopt(rec)

    # Phase 3 — package both runs into content-addressed Reports + the diff (the adoption inputs).
    from .report import build_report, diff_reports
    ts = int(time.time())
    champ_report = build_report("genesis", results["genesis"], scenarios, cfg, timestamp=ts)
    chal_report = build_report("candidate", results["candidate"], scenarios, cfg, timestamp=ts)
    report_diff = diff_reports(champ_report, chal_report, adopt, rec)
    print(f"\n  REPORT  hash={champ_report['seal']['report_hash']}  "
          f"comparable={report_diff['comparable']}  verdict={report_diff['verdict']}  "
          f"({len(scenarios)} sealed cases @ block {cfg.fork_block})")
    if json_out:
        Path(json_out).write_text(json.dumps(
            {"champion_report": champ_report, "challenger_report": chal_report, "diff": report_diff},
            indent=2))
        print(f"  report dumped -> {json_out}")
