"""App Intent CRUD routes."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field

from minotaur_subnet.api import services as _tools

router = APIRouter(tags=["apps"])


def _require_admin(x_admin_key: str | None = Header(None)) -> None:
    """Validate admin API key for protected endpoints.

    If ADMIN_API_KEY is set, the request must include a matching
    X-Admin-Key header. If ADMIN_API_KEY is not set, admin endpoints
    are open (development mode).
    """
    admin_key = os.environ.get("ADMIN_API_KEY", "").strip()
    if admin_key and x_admin_key != admin_key:
        raise HTTPException(
            status_code=401,
            detail="Admin API key required (X-Admin-Key header)",
        )

# Module-level JS engine reference, set by server.py at startup
_js_engine = None

# Module-level simulator reference, set by server.py at startup
_simulator = None


def set_js_engine(js_engine: Any) -> None:
    global _js_engine
    _js_engine = js_engine


def set_simulator(simulator: Any) -> None:
    global _simulator
    _simulator = simulator


# ── request models ───────────────────────────────────────────────────────────


class CreateAppRequest(BaseModel):
    name: str = Field(..., description="Human-readable name")
    description: str = Field("", description="What this app does")
    supported_chains: list[int] = Field(..., description="Chain IDs (e.g. [1, 8453])")
    js_code: str = Field(..., description="JS scoring code (required)")
    solidity_code: str = Field(..., description="Solidity contract code (required)")
    constructor_args: list[list[str]] | None = Field(
        None, description="Extra constructor args: [[abi_type, value], ...]",
    )
    deployer: str = Field("", description="Deployer address (only this address can update JS later)")


class ValidateAppRequest(BaseModel):
    js_code: str = Field(..., description="JavaScript scoring code to validate")
    solidity_code: str = Field("", description="Solidity contract code to validate")
    skip_solidity: bool = Field(False, description="Skip Solidity compilation check")


class UpdateScoringRequest(BaseModel):
    new_js_code: str = Field(..., description="New JavaScript scoring source")
    caller: str = Field("", description="Caller address (must match deployer if one was set at creation)")
    signature: str = Field("", description="EIP-191 signature proving caller owns the deployer address. "
                           "Message = keccak256(abi.encode(app_id, sha256(new_js_code)))")


class ScorePlanRequest(BaseModel):
    plan: dict[str, Any] = Field(..., description="Execution plan to score")
    params: dict[str, Any] = Field(..., description="Order params → state.raw_params")
    chain_id: int = Field(0, description="Chain ID (0 = auto-detect from deployment)")
    intent_function: str = Field("execute", description="Intent function name")
    fork_block: int | None = Field(
        None,
        description=(
            "Optional historical block number to rewind the anvil fork to "
            "before simulating. Used by miner-side Stage-2 replay of "
            "historical filled orders so the plan is evaluated against "
            "the pool state at the time of the original order. Requires "
            "the upstream RPC to support archive reads."
        ),
    )


class ReplayDebugRequest(BaseModel):
    """Run a strategy on one scenario and return per-interaction state trace."""
    code: str = Field(..., description="Python source for the strategy")
    params: dict[str, Any] = Field(..., description="Scenario params (input_token, etc.)")
    intent_function: str = Field("execute", description="Intent function name")
    scenario_name: str = Field("", description="Manifest scenario name (for logging)")
    fork_block: int | None = Field(None, description="Optional historical block to rewind to")


# ── helpers ──────────────────────────────────────────────────────────────────


def _store():
    """Return the shared store from the API server module."""
    from minotaur_subnet.api.server import store
    return store


# ── routes ───────────────────────────────────────────────────────────────────


@router.post("/apps/")
def create_app(
    body: CreateAppRequest,
    x_admin_key: str | None = Header(None),
) -> dict[str, Any]:
    """Create a new App Intent with developer-provided JS and Solidity code.

    Requires X-Admin-Key header when ADMIN_API_KEY is set. Open in development
    mode (when ADMIN_API_KEY is unset). The on-chain AppRegistry gate is the
    final authority — even an unauthenticated app record can't be routed
    against an unregistered contract — but the admin gate prevents wasted
    relayer gas from spurious deploy attempts.
    """
    _require_admin(x_admin_key)
    return _tools.create_app_intent(
        _store(),
        name=body.name,
        description=body.description,
        supported_chains=body.supported_chains,
        js_code=body.js_code or None,
        solidity_code=body.solidity_code or None,
        constructor_args=body.constructor_args,
        deployer=body.deployer,
    )


@router.post("/apps/validate")
async def validate_app(body: ValidateAppRequest) -> dict[str, Any]:
    """Pre-flight validation for App Intent JS and/or Solidity code."""
    return await _tools.validate_app_intent_code(
        js_code=body.js_code,
        solidity_code=body.solidity_code,
        skip_solidity=body.skip_solidity,
    )


@router.post("/apps/{app_id}/deploy")
async def deploy_app(
    app_id: str,
    chain_id: int | None = None,
    x_admin_key: str | None = Header(None),
) -> dict[str, Any]:
    """Deploy an App Intent to a specific chain (or first supported chain).

    Requires X-Admin-Key header when ADMIN_API_KEY is set. Open in development
    mode (when ADMIN_API_KEY is unset). Without this gate, an unauthenticated
    caller could trigger the relayer to spend gas on attacker-defined Solidity;
    the deployed contract still can't execute orders (AppRegistry gate is
    GATED + allowlist) but the gas burn is real.

    Runs in a thread executor so the synchronous compile + deploy chain
    can call asyncio.run() without conflicting with the FastAPI event loop.
    """
    _require_admin(x_admin_key)
    import asyncio
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None, lambda: _tools.deploy_app_intent(_store(), app_id, chain_id=chain_id),
    )


@router.get("/apps/")
def list_apps(deployer: str = "", status: str = "") -> dict[str, Any]:
    """List all App Intents, optionally filtered by deployer address or status."""
    result = _tools.list_minotaur_subnet(_store(), deployer if deployer else None)
    if status:
        allowed = {s.strip().lower() for s in status.split(",")}
        result["apps"] = [
            a for a in result["apps"]
            if (a.get("status") or "").lower() in allowed
        ]
        result["total"] = len(result["apps"])
    return result


@router.get("/apps/{app_id}/status")
def get_status(app_id: str) -> dict[str, Any]:
    """Get an App Intent's status and execution statistics."""
    return _tools.get_app_status(_store(), app_id)


@router.put("/apps/{app_id}/scoring")
def update_scoring(
    app_id: str,
    body: UpdateScoringRequest,
    x_admin_key: str | None = Header(None),
) -> dict[str, Any]:
    """Update the JS scoring code for an App Intent.

    Requires X-Admin-Key header when ADMIN_API_KEY is set.
    """
    _require_admin(x_admin_key)
    return _tools.update_scoring(
        _store(), app_id, body.new_js_code,
        caller=body.caller,
        signature=body.signature,
    )


@router.get("/apps/{app_id}/manifest")
async def get_manifest(app_id: str) -> dict[str, Any]:
    """Extract and return the JS manifest for an app."""
    return await _tools.get_app_manifest(_store(), app_id)


@router.get("/apps/manifests")
async def list_manifests() -> dict[str, Any]:
    """Return manifests for all apps (bulk discovery for miners)."""
    s = _store()
    apps = s.list_apps()
    results: dict[str, Any] = {}
    for app_def in apps:
        result = await _tools.get_app_manifest(s, app_def.app_id)
        if "manifest" in result and result["manifest"] is not None:
            results[app_def.app_id] = result["manifest"]
    return {"manifests": results, "total": len(results)}


@router.get("/apps/{app_id}/historical-scenarios")
async def get_historical_scenarios(
    app_id: str,
    n_per_chain: int = 10,
) -> dict[str, Any]:
    """Return PII-stripped historical filled-order scenarios for an app.

    The live validator benchmark (benchmark_worker._load_historical_scenarios)
    replays real historical orders as Stage 2 of scoring. Miners need to
    be able to preview how their strategy handles those same orders —
    otherwise score_strategy_all gives a misleading "good" reading even
    when the strategy would fail on every historical replay.

    Deterministic: same round_id (implicit: the app_id is used as the
    pseudo-seed here for dry-run purposes) always returns the same
    sample set. Use ``n_per_chain`` to cap sample size.
    """
    from minotaur_subnet.harness.order_sampler import sample_historical_orders

    s = _store()
    # App_id stands in as the pseudo-round-id for deterministic sampling
    # — miners aren't running inside a solver round, but repeatability
    # across dry-runs is still desirable.
    sampled = sample_historical_orders(
        app_store=s,
        round_id=f"dryrun:{app_id}",
        n_per_chain=max(1, min(n_per_chain, 50)),
    )
    # Filter to this app only (the sampler doesn't filter by app_id)
    for_app = [o for o in sampled if o.get("app_id") == app_id]
    return {
        "app_id": app_id,
        "scenarios": for_app,
        "total": len(for_app),
    }


@router.post("/apps/{app_id}/activate")
def activate_app(
    app_id: str,
    chain_id: int = 0,
    x_admin_key: str | None = Header(None),
) -> dict[str, Any]:
    """Admin: promote an app from solving → active (for testing).

    Requires X-Admin-Key header when ADMIN_API_KEY is set.
    """
    _require_admin(x_admin_key)
    from minotaur_subnet.shared.types import AppStatus
    s = _store()
    dep = s.get_deployment(app_id, chain_id=chain_id if chain_id else None)
    if dep is None:
        raise HTTPException(status_code=404, detail=f"No deployment found for {app_id}")
    s.update_deployment_status(app_id, dep.chain_id, AppStatus.ACTIVE)
    return {"app_id": app_id, "chain_id": dep.chain_id, "status": "active"}


@router.post("/apps/{app_id}/score")
async def score_plan(app_id: str, body: ScorePlanRequest) -> dict[str, Any]:
    """Score an execution plan against an app's JS scoring function.

    Used by miners to test how well their generated plans score.
    Runs the full pipeline when Anvil is available (simulation + JS scoring),
    falling back to mock simulation otherwise.
    """
    import logging
    from minotaur_subnet.shared.types import ExecutionPlan
    from minotaur_subnet.shared.builders import build_intent_state, parse_interactions
    from minotaur_subnet.shared.simulation import build_mock_simulation

    _log = logging.getLogger(__name__)

    s = _store()
    app = s.get_app(app_id)
    if app is None:
        raise HTTPException(status_code=404, detail=f"App not found: {app_id}")

    if not app.js_code:
        raise HTTPException(
            status_code=400,
            detail=f"App {app_id} has no JS scoring code",
        )

    if _js_engine is None:
        raise HTTPException(
            status_code=503,
            detail="JS scoring engine not available",
        )

    # Build ExecutionPlan from request
    plan_dict = body.plan
    interactions_raw = plan_dict.get("interactions", [])
    plan = ExecutionPlan(
        intent_id=plan_dict.get("intent_id", app_id),
        interactions=parse_interactions(interactions_raw),
        deadline=plan_dict.get("deadline", 0),
        nonce=plan_dict.get("nonce", 0),
        metadata=dict(plan_dict.get("metadata", {})),
    )

    params = body.params

    # Look up deployment for contract_address and chain_id
    chain_id = body.chain_id
    contract_address = ""
    deployment = s.get_deployment(app_id)
    if deployment:
        contract_address = deployment.contract_address or ""
        if chain_id == 0:
            chain_id = deployment.chain_id or 1
    if chain_id == 0:
        chain_id = 1

    # Ensure the MultiChainSimulator dispatches to the right chain.
    # Without this hint it falls back to default_chain_id (31337/anvil-eth)
    # and the scoreIntent call runs against the wrong fork — the DexAggregator
    # contract isn't deployed there, relayer() returns empty, and the
    # simulator fails with "Unknown format '0x'".
    plan.metadata.setdefault("chain_id", chain_id)

    state = build_intent_state(
        contract_address=contract_address,
        chain_id=chain_id,
        params=params,
        intent_function=body.intent_function,
        owner=params.get("owner", ""),
    )

    # Synthesize a stand-in IntentOrder so the simulator takes the same
    # scoreIntent path production uses. Without this, simulator.simulate()
    # falls through to its manual-interactions fallback (because the
    # intent_order arg is None), which bypasses the app contract's proxy
    # deploy / invariant checks and silently reports "0 token transfers"
    # even for correct strategies. That divergence was the reason the
    # miner's score_strategy tool kept returning 0 for valid code, so
    # Claude couldn't tell its improvements from regressions.
    #
    # The submitted_by address is a throwaway test account (address 0x…01).
    # It gets seeded with input_amount of input_token and a blanket allowance
    # to the app contract, exactly like AppIntentBase's pull-funding flow.
    from minotaur_subnet.api.services import (
        build_swap_intent_params_hex,
        build_intent_params_hex_from_manifest,
        compute_intent_selector,
    )
    _TEST_USER = "0x0000000000000000000000000000000000000001"

    intent_order: dict | None = None
    token_balances: dict[str, int] | None = None
    # Always seed token_balances from scenario params if available — the
    # manual-fallback simulator path needs the executor funded even when
    # intent_params decoding fails. Without this, the executor has 0 of
    # the input token, the swap router's transferFrom reverts with STF,
    # and miners spend hours debugging a strategy that's actually fine.
    _input_token = params.get("input_token", "")
    _input_amount = params.get("input_amount", "0")
    try:
        _amount_wei = int(_input_amount)
    except (ValueError, TypeError):
        _amount_wei = 0
    if _input_token and _amount_wei > 0:
        token_balances = {_input_token: _amount_wei}
    if contract_address:
        # Ensure a min_output_amount so the swap encoder doesn't bail;
        # 0 is fine for a dry-run simulation.
        dry_params = dict(params)
        dry_params.setdefault("min_output_amount", "0")
        intent_params_hex = build_swap_intent_params_hex(dry_params, _TEST_USER)
        if not intent_params_hex:
            intent_params_hex = build_intent_params_hex_from_manifest(
                s, _js_engine, app_id,
                body.intent_function, dry_params, _TEST_USER,
            )
        intent_selector = compute_intent_selector(
            s, _js_engine, app_id, body.intent_function,
        ) or ""
        if intent_params_hex and intent_selector:
            intent_order = {
                "order_id": f"score-dry-run-{app_id}",
                "app": contract_address,
                "intent_selector": intent_selector,
                "intent_params": intent_params_hex,
                "submitted_by": _TEST_USER,
                "chain_id": chain_id,
                "deadline": 0,  # 0 = no deadline check; this is a dry run
                "nonce": 0,
                "perpetual": False,
                "max_executions": 1,
                "cooldown": 0,
            }
            # Seed the test user with input_amount of input_token so
            # AppIntentBase's safeTransferFrom(submitted_by, proxy, ...)
            # actually has tokens to pull. Without this the contract reverts
            # with ERC20: insufficient balance before any swap attempt.
            input_token = params.get("input_token", "")
            input_amount = params.get("input_amount", "0")
            try:
                amount_wei = int(input_amount)
            except (ValueError, TypeError):
                amount_wei = 0
            if input_token and amount_wei > 0:
                token_balances = {input_token: amount_wei}

    # Try Anvil simulation, fall back to mock
    simulation_mode = "mock"
    simulation = None

    if _simulator is not None:
        try:
            simulation = await _simulator.simulate(
                plan,
                contract_address=contract_address or None,
                intent_order=intent_order,
                token_balances=token_balances,
                fork_block=body.fork_block,
            )
            simulation_mode = "anvil"
        except Exception as exc:
            _log.warning("Anvil simulation failed, falling back to mock: %s", exc)

    if simulation is None:
        simulation = build_mock_simulation(plan, params)

    # Ensure JS code is loaded and score
    try:
        if app_id not in _js_engine._intents:
            await _js_engine.load_intent(app_id, app.js_code)
        score_result = await _js_engine.score(app_id, plan, simulation, state)
        return {
            "app_id": app_id,
            "score": score_result.score,
            "valid": score_result.valid,
            "reason": score_result.reason,
            "breakdown": score_result.breakdown,
            "simulation_mode": simulation_mode,
            "simulation": {
                "success": simulation.success,
                "gas_used": simulation.gas_used,
                "on_chain_score": simulation.on_chain_score,
                "token_transfers": len(simulation.token_transfers),
                "error": simulation.error,
            },
        }
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"JS scoring failed: {exc}",
        )


@router.post("/apps/{app_id}/replay-debug")
async def replay_debug(app_id: str, body: ReplayDebugRequest) -> dict[str, Any]:
    """Replay a strategy on one scenario with per-interaction state trace.

    Powers the miner's ``replay_failed_swap`` MCP tool. Loads the strategy
    code, runs ``generate_plan`` against the supplied params, executes the
    plan via the simulator's manual-execution path with state snapshots
    between interactions (executor balances + allowances to each target).
    Returns a structured trace that lets Claude pinpoint *which*
    interaction's state mutation broke the chain.
    """
    import tempfile
    import asyncio
    from minotaur_subnet.shared.types import (
        AppIntentConfig, AppIntentDefinition, IntentState, TriggerType,
    )
    from minotaur_subnet.harness.snapshot import build_synthetic_snapshot
    from minotaur_subnet.miner.agent.strategy_tester import load_strategy

    s = _store()
    app = s.get_app(app_id)
    if app is None:
        raise HTTPException(status_code=404, detail=f"App not found: {app_id}")
    deployment = s.get_deployment(app_id)
    if deployment is None:
        raise HTTPException(
            status_code=400, detail=f"No deployment for {app_id}",
        )

    if _simulator is None:
        raise HTTPException(
            status_code=503, detail="Simulator not available on this node",
        )

    # Load the user's strategy from a temp file
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".py", delete=False, prefix="replay_",
        ) as f:
            f.write(body.code)
            tmp = f.name
        try:
            strategy = load_strategy(tmp)
        except Exception as exc:
            return {"error": f"Strategy load failed: {exc}"}

        chain_id = deployment.chain_id or 1
        snapshot = build_synthetic_snapshot(chain_id=chain_id)
        intent = AppIntentDefinition(
            app_id=app_id, name=app.name or "Replay", version="1.0.0",
            intent_type="swap", js_code="//",
            config=AppIntentConfig(
                supported_chains=[chain_id], trigger_type=TriggerType.USER_TRIGGERED,
            ),
        )
        state = IntentState(
            contract_address=deployment.contract_address or "",
            chain_id=chain_id, nonce=1,
            owner=body.params.get("owner") or "0x0000000000000000000000000000000000000001",
            raw_params=body.params,
            control={"_intent_function": body.intent_function},
        )
        try:
            plan = strategy.generate_plan(intent, state, snapshot)
        except Exception as exc:
            return {"error": f"generate_plan raised: {exc}"}

        # Build token_balances seed: input_token to input_amount
        in_token = body.params.get("input_token") or ""
        in_amount = int(body.params.get("input_amount") or "0")
        token_balances: dict[str, int] = {}
        if in_token and in_amount:
            token_balances[in_token] = in_amount

        # MultiChainSimulator's _get_simulator expects a plan with
        # metadata.chain_id set. Forward the chain hint and let the
        # multi-chain dispatcher pick the right per-chain AnvilSimulator.
        plan.metadata["chain_id"] = chain_id

        def _run_trace():
            if hasattr(_simulator, "_get_simulator"):
                sim = _simulator._get_simulator(plan)
            else:
                sim = _simulator
            return sim.simulate_with_trace(
                plan, token_balances=token_balances,
                focus_tokens=[
                    in_token,
                    body.params.get("output_token") or "",
                ],
            )
        trace = await asyncio.to_thread(_run_trace)

        return {
            "scenario": body.scenario_name,
            "plan_size": len(plan.interactions),
            **trace,
        }
    finally:
        if tmp:
            try:
                Path(tmp).unlink()
            except OSError:
                pass
