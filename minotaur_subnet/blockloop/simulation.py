"""Simulation step of the block loop pipeline."""

from __future__ import annotations

import logging
from typing import Any

from minotaur_subnet.shared.types import ExecutionPlan, SimulationResult
from minotaur_subnet.shared.simulation import build_mock_simulation
from minotaur_subnet.orderbook.orderbook import Order

logger = logging.getLogger(__name__)


class SimulationRunner:
    """Runs Anvil fork simulations for execution plans.

    Handles: mock bridge interaction setup, scoreIntent path,
    fallback to mock simulation when simulator is unavailable.

    Args:
        simulator: AnvilSimulator or MultiChainSimulator (optional, mock if None).
        bridge_registry: BridgeRegistry for cross-chain bridge quoting (optional).
    """

    def __init__(
        self,
        simulator: Any = None,
        bridge_registry: Any = None,
    ) -> None:
        self.simulator = simulator
        self.bridge_registry = bridge_registry

    async def simulate(
        self,
        plan: ExecutionPlan,
        order: Order,
        contract_address: str | None,
        intent_order_dict: dict | None,
        is_cross_chain: bool,
        deployed_contract: str,
    ) -> SimulationResult:
        """Simulate an execution plan.

        Handles token seeding, deposit-model apps, cross-chain simulation,
        and falls back to mock simulation on failure.
        """
        if self.simulator is not None:
            # Seed the simulator with input tokens from order params.
            # For swap-style: tokens go to the executor (user/proxy).
            # For deposit-style (DCA): tokens go to the contract address
            # because scoreIntent uses _fundFromContract.
            token_balances = None
            input_token = (
                order.params.get("input_token")
                or order.params.get("tokenIn")
                or order.params.get("token_in")
                or order.params.get("asset")
            )
            # Strip CAIP-10 prefix if present (e.g. eip155:8453:0x833589...)
            if input_token and input_token.startswith("eip155:"):
                try:
                    from minotaur_subnet.shared.interop_address import parse_address
                    ia = parse_address(input_token, default_chain_id=order.chain_id)
                    input_token = ia.address
                except ValueError:
                    pass
            input_amount = (
                order.params.get("input_amount")
                or order.params.get("amountPerBuy")
                or order.params.get("amount_per_buy")
                or order.params.get("amount")
            )
            if input_token and input_amount:
                try:
                    token_balances = {input_token: int(input_amount)}
                except (ValueError, TypeError):
                    pass

            # For deposit-model apps, seed the contract rather than the executor
            is_deposit_model = order.params.get("amountPerBuy") or order.params.get("amount_per_buy")
            seed_contract = deployed_contract if (is_deposit_model and deployed_contract) else None

            try:
                # For deposit-model apps, seed the contract with input tokens
                # so scoreIntent -> _fundFromContract can transfer to the proxy.
                sim_token_balances = token_balances
                if seed_contract and token_balances and hasattr(self.simulator, '_deal_erc20'):
                    for tok, amt in token_balances.items():
                        self.simulator._deal_erc20(tok, seed_contract, amt)
                    sim_token_balances = None  # Already dealt to contract

                # For standard apps, scoreIntent calls _fundAndExecute which does
                # safeTransferFrom(user, proxy, amount). The user needs both
                # token balance AND allowance for the app contract.
                if not seed_contract and token_balances and deployed_contract:
                    # Get the AnvilSimulator for this chain
                    sim = self.simulator
                    if hasattr(sim, 'simulators'):
                        sim = sim.simulators.get(
                            order.chain_id,
                            sim.simulators.get(31337),
                        )
                    if sim and hasattr(sim, '_deal_erc20'):
                        for tok, amt in token_balances.items():
                            sim._deal_erc20(tok, order.submitted_by, amt)
                            sim._set_erc20_allowance(
                                tok, order.submitted_by, deployed_contract, 2**256 - 1,
                            )

                        # Seed WETH/WTAO for platform fee so scoreIntent can collect it
                        platform_fee = int(order.params.get("platform_fee_wei", 0))
                        if platform_fee > 0:
                            from minotaur_subnet.blockchain.tokens import WRAPPED_NATIVE_TOKEN
                            weth = WRAPPED_NATIVE_TOKEN.get(order.chain_id)
                            if weth:
                                sim._deal_erc20(weth, order.submitted_by, platform_fee)
                                sim._set_erc20_allowance(
                                    weth, order.submitted_by, deployed_contract, 2**256 - 1,
                                )

                if is_cross_chain and hasattr(self.simulator, "simulate_cross_chain"):
                    simulation = await self.simulator.simulate_cross_chain(
                        plan,
                        bridge_registry=self.bridge_registry,
                        contract_address=contract_address,
                        intent_order=intent_order_dict,
                        token_balances=sim_token_balances,
                    )
                else:
                    logger.info("[LOOP] simulate: contract=%s intent_order=%s tokens=%s", contract_address, "yes" if intent_order_dict else "no", sim_token_balances)
                    simulation = await self.simulator.simulate(
                        plan,
                        contract_address=contract_address,
                        intent_order=intent_order_dict,
                        token_balances=sim_token_balances,
                    )
                    logger.info("[LOOP] simulation result: success=%s error=%s transfers=%s", simulation.success, simulation.error, len(simulation.token_transfers or []))
                return simulation
            except Exception as exc:
                logger.error("[LOOP] simulator exception: %s", exc, exc_info=True)
                logger.warning("Simulator failed, falling back to mock: %s", exc)
                return build_mock_simulation(plan, order.params)
        else:
            return build_mock_simulation(plan, order.params)
