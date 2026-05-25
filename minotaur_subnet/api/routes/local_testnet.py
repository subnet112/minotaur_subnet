"""Local-testnet / development-only routes.

These handlers expose dev-time conveniences that have NO production use case:
faucet funding against the local Anvil forks, direct subtensor stake calls
bypassing the permission system, and arbitrary-Python strategy replay for
miner debugging.

The router is intentionally NOT registered by ``api/server.py`` unless
``LOCAL_TESTNET=1`` is set. Production stacks leave it unset, so these
handlers don't exist on the deployed API — not behind a runtime gate, but
simply not on the route table at all. Defense in depth: even an attacker
inside the Docker network can't reach an endpoint that wasn't registered.

History: faucet handlers previously lived in ``wallets.py``, ``direct_stake``
in ``native_bittensor.py``, and ``replay-debug`` in ``apps.py`` — all
mounted unconditionally in prod. 2026-05-25 audit found this exposed three
critical issues (unauthenticated faucet, unauthenticated subtensor extrinsic
submission, unauthenticated RCE via arbitrary Python). Consolidating here
makes the dev/prod boundary explicit.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from minotaur_subnet.api import services as _tools


router = APIRouter(tags=["local-testnet"])


# ── module-level wiring (set by startup.py when router is mounted) ──────────

_simulator: Any = None


def set_simulator(simulator: Any) -> None:
    """Wire the multi-chain simulator instance used by ``replay-debug``."""
    global _simulator
    _simulator = simulator


def _store():
    """Return the shared App-Intent store from the API server module."""
    from minotaur_subnet.api.server import store
    return store


# ── faucet (Anvil cheatcodes) ───────────────────────────────────────────────


class FaucetRequest(BaseModel):
    address: str = Field(..., description="Ethereum address (plain 0x or CAIP-10)")
    amount_eth: float = Field(10.0, description="Amount of ETH to fund")
    chain_id: int = Field(0, description="Target chain (0=first available, 31337=ETH fork, 8453=Base fork)")


@router.post("/testnet/faucet")
def faucet_eth(body: FaucetRequest) -> dict[str, Any]:
    """Fund an address with ETH on a local Anvil testnet fork."""
    return _tools.faucet_eth(body.address, body.amount_eth, chain_id=body.chain_id)


class FaucetErc20Request(BaseModel):
    token: str = Field(..., description="Token address, symbol (USDC), or chain-qualified (USDC@8453)")
    address: str = Field(..., description="Recipient address (plain 0x or CAIP-10)")
    amount: str = Field(..., description="Amount in token's smallest unit (decimal string)")
    chain_id: int = Field(0, description="Target chain (0=first available)")


@router.post("/testnet/faucet_erc20")
def faucet_erc20(body: FaucetErc20Request) -> dict[str, Any]:
    """Fund an address with ERC-20 tokens on a local Anvil testnet fork."""
    return _tools.faucet_erc20(body.token, body.address, body.amount, chain_id=body.chain_id)


# ── direct stake (bypasses the permission system) ──────────────────────────


class DirectStakeRequest(BaseModel):
    """Direct stake/unstake without requiring a pre-created permission."""
    action: str = Field(..., description="add_stake or remove_stake")
    owner_ss58: str = Field(..., description="User's Bittensor SS58 address")
    hotkey_ss58: str = Field("", description="Validator hotkey (defaults to owner)")
    netuid: int = Field(..., description="Subnet netuid")
    amount_rao: int = Field(..., description="Amount in RAO (1 TAO = 1e9 RAO)")


@router.post("/native-bittensor/stake")
async def direct_stake(body: DirectStakeRequest) -> dict[str, Any]:
    """Execute a stake or unstake directly via the SubstrateRelayer.

    Testnet shortcut: bypasses the permission system and executes
    using the local subtensor.
    """
    import asyncio
    loop = asyncio.get_running_loop()

    def _run():
        from minotaur_subnet.relayer.substrate_relayer import SubstrateRelayer
        from minotaur_subnet.shared.types import SubstrateAction
        from minotaur_subnet.blockchain.bittensor_proxy_executor import BittensorProxyExecutor

        subtensor_url = os.environ.get("SUBTENSOR_URL", "ws://subtensor:9944")
        executor = BittensorProxyExecutor(network=subtensor_url)
        relayer = SubstrateRelayer(executor)

        action = SubstrateAction(
            action=body.action,
            owner_ss58=body.owner_ss58,
            amount_rao=body.amount_rao,
            netuid=body.netuid,
            hotkey_ss58=body.hotkey_ss58 or body.owner_ss58,
        )

        import asyncio as aio
        return aio.run(relayer.submit_action(action))

    try:
        result = await loop.run_in_executor(None, _run)
        return {
            "success": result.success,
            "tx_hash": result.tx_hash,
            "error": result.error,
            "action": body.action,
            "netuid": body.netuid,
            "amount_rao": body.amount_rao,
        }
    except Exception as exc:
        return {"success": False, "error": str(exc)}


# ── replay-debug (arbitrary-Python strategy replay) ────────────────────────


class ReplayDebugRequest(BaseModel):
    """Run a strategy on one scenario and return per-interaction state trace."""
    code: str = Field(..., description="Python source for the strategy")
    params: dict[str, Any] = Field(..., description="Scenario params (input_token, etc.)")
    intent_function: str = Field("execute", description="Intent function name")
    scenario_name: str = Field("", description="Manifest scenario name (for logging)")
    fork_block: int | None = Field(None, description="Optional historical block to rewind to")


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

        in_token = body.params.get("input_token") or ""
        in_amount = int(body.params.get("input_amount") or "0")
        token_balances: dict[str, int] = {}
        if in_token and in_amount:
            token_balances[in_token] = in_amount

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
