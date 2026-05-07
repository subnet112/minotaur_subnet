"""Plan builders — generate real EVM interactions for simulation.

These are used when no miner-submitted solver is available (fallback).
Each builder knows how to create interactions for a specific app type
using real on-chain protocol addresses.

Mainnet addresses (used on Anvil mainnet fork):
- WETH:              0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2
- USDC:              0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48
- Uniswap V3 Router: 0xE592427A0AEce92De3Edee1F18E0157C05861564
"""

from __future__ import annotations

import logging
import time
from typing import Any

from eth_abi import encode

from minotaur_subnet.shared.types import (
    AppIntentDefinition,
    ExecutionPlan,
    Interaction,
    IntentState,
)

logger = logging.getLogger(__name__)

# ── Mainnet addresses ──────────────────────────────────────────────────
WETH = "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"
USDC = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
UNISWAP_V3_ROUTER = "0xE592427A0AEce92De3Edee1F18E0157C05861564"

# Function selectors
_DEPOSIT_SELECTOR = "d0e30db0"            # WETH.deposit()
_APPROVE_SELECTOR = "095ea7b3"            # ERC20.approve(address,uint256)
_EXACT_INPUT_SINGLE = "414bf389"          # SwapRouter.exactInputSingle(...)


def build_vault_buydip_plan(
    app: AppIntentDefinition,
    state: IntentState,
    buy_amount_wei: int | None = None,
    executor: str = "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266",
) -> ExecutionPlan:
    """Build a WETH → USDC swap plan for the vault's buyDip function.

    Creates 3 interactions on Uniswap V3:
    1. Wrap ETH → WETH
    2. Approve Uniswap V3 Router to spend WETH
    3. Swap WETH → USDC via exactInputSingle (0.3% fee tier)

    For the local testnet, USDC stands in for WTAO since there's no
    WTAO on mainnet. The scoring JS evaluates the swap quality regardless
    of the specific output token.

    Args:
        app: The app intent definition.
        state: Current intent state (contains order params in raw_params).
        buy_amount_wei: ETH to spend (default: 1 ETH).
        executor: Address executing the plan (for Anvil impersonation).
    """
    if buy_amount_wei is None:
        # Default: 1 ETH worth of swap
        buy_amount_wei = 10**18

    chain_id = state.chain_id or 1
    deadline = int(time.time()) + 3600  # 1 hour from now

    # ── Interaction 1: Wrap ETH → WETH ──────────────────────────────
    deposit_calldata = "0x" + _DEPOSIT_SELECTOR

    # ── Interaction 2: Approve Router to spend WETH ─────────────────
    approve_encoded = encode(
        ["address", "uint256"],
        [UNISWAP_V3_ROUTER, buy_amount_wei],
    )
    approve_calldata = "0x" + _APPROVE_SELECTOR + approve_encoded.hex()

    # ── Interaction 3: Swap WETH → USDC via Uniswap V3 ─────────────
    # exactInputSingle params: tokenIn, tokenOut, fee, recipient,
    #   deadline, amountIn, amountOutMinimum, sqrtPriceLimitX96
    swap_params = encode(
        [
            "address",     # tokenIn
            "address",     # tokenOut
            "uint24",      # fee
            "address",     # recipient
            "uint256",     # deadline
            "uint256",     # amountIn
            "uint256",     # amountOutMinimum
            "uint160",     # sqrtPriceLimitX96
        ],
        [
            WETH,                    # tokenIn
            USDC,                    # tokenOut
            3000,                    # fee (0.3%)
            executor,                # recipient
            deadline,                # deadline
            buy_amount_wei,          # amountIn
            0,                       # amountOutMinimum (0 for simulation)
            0,                       # sqrtPriceLimitX96 (0 = no limit)
        ],
    )
    swap_calldata = "0x" + _EXACT_INPUT_SINGLE + swap_params.hex()

    plan = ExecutionPlan(
        intent_id=app.app_id,
        interactions=[
            Interaction(
                target=WETH,
                value=str(buy_amount_wei),
                call_data=deposit_calldata,
                chain_id=chain_id,
            ),
            Interaction(
                target=WETH,
                value="0",
                call_data=approve_calldata,
                chain_id=chain_id,
            ),
            Interaction(
                target=UNISWAP_V3_ROUTER,
                value="0",
                call_data=swap_calldata,
                chain_id=chain_id,
            ),
        ],
        deadline=deadline,
        nonce=state.nonce,
        metadata={
            "executor": executor,
            "token_in": WETH,
            "token_out": USDC,
            "amount_in": str(buy_amount_wei),
            "swap_route": "WETH → USDC (UniV3 0.3%)",
            "plan_type": "vault_buydip",
        },
    )

    logger.info(
        "Built vault buyDip plan: %s ETH via %s",
        buy_amount_wei / 10**18,
        plan.metadata["swap_route"],
    )
    return plan


def is_vault_app(app: AppIntentDefinition) -> bool:
    """Check if an app looks like a vault (for plan builder selection)."""
    name_lower = (app.name or "").lower()
    desc_lower = (app.description or "").lower()
    return (
        "vault" in name_lower
        or "dip" in name_lower
        or "vault" in desc_lower
        or "dip" in desc_lower
    )
