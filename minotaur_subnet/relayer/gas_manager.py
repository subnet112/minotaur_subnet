"""Multi-chain gas tracking for the relayer.

Monitors relayer wallet balances and estimates execution costs across
all supported chains.
"""

from __future__ import annotations

import logging
from typing import Any

from .chain_config import ChainDeployment

logger = logging.getLogger(__name__)


class GasManager:
    """Tracks gas balances and estimates execution costs."""

    def __init__(self, chains: dict[int, ChainDeployment]) -> None:
        self.chains = chains

    async def get_balances(self) -> dict[int, float]:
        """Get relayer wallet balance on each chain (in native token)."""
        balances: dict[int, float] = {}
        for chain_id, config in self.chains.items():
            if not config.relayer_wallet or not config.rpc_url:
                continue
            try:
                from minotaur_subnet.blockchain.chains import get_web3
                w3 = get_web3(chain_id)
                balance_wei = w3.eth.get_balance(config.relayer_wallet)
                balances[chain_id] = balance_wei / 1e18
            except Exception as exc:
                logger.warning(
                    "Failed to get balance on chain %d: %s", chain_id, exc,
                )
                balances[chain_id] = -1.0
        return balances

    def estimate_execution_cost(
        self, chain_id: int, plan: Any, gas_price_gwei: float = 0,
    ) -> int:
        """Estimate gas cost for executing a plan (in wei).

        Args:
            chain_id: Target chain.
            plan: ExecutionPlan with interactions.
            gas_price_gwei: Override gas price. 0 = use chain config.

        Returns:
            Estimated cost in wei.
        """
        config = self.chains.get(chain_id)
        if config is None:
            return 0

        # Base gas + per-call estimate
        base_gas = 100_000  # executeIntent overhead
        per_call_gas = 80_000
        proxy_deploy_gas = 32_000

        n_calls = len(plan.interactions) if hasattr(plan, "interactions") else 1
        estimated_gas = base_gas + proxy_deploy_gas + n_calls * per_call_gas

        # Apply buffer
        estimated_gas = estimated_gas * (100 + config.gas_buffer_pct) // 100

        # Price
        price = gas_price_gwei or config.gas_price_gwei or 20.0
        return int(estimated_gas * price * 1e9)

    def compute_platform_fee(
        self, chain_id: int, plan: Any, margin_bps: int = 2000,
    ) -> int:
        """Compute platform fee = estimated gas cost + margin (in native token wei).

        Args:
            chain_id: Target chain.
            plan: ExecutionPlan with interactions.
            margin_bps: Margin above gas cost in BPS (default 20%).

        Returns:
            Platform fee in native token wei (e.g., ETH wei).
        """
        gas_cost_wei = self.estimate_execution_cost(chain_id, plan)
        margin = gas_cost_wei * margin_bps // 10000
        return gas_cost_wei + margin

    def check_sufficient_balance(
        self, chain_id: int, balance_eth: float, estimated_cost_wei: int,
    ) -> bool:
        """Check if balance covers estimated cost with safety margin."""
        cost_eth = estimated_cost_wei / 1e18
        return balance_eth >= cost_eth * 1.5  # 50% safety margin
