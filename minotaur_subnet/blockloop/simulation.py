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
        fork_block: int | None = None,
        pin_only: bool = False,
    ) -> SimulationResult:
        """Simulate an execution plan.

        Handles token seeding, deposit-model apps, cross-chain simulation,
        and falls back to mock simulation on failure.

        ``fork_block`` / ``pin_only`` are the QUOTE path's fork-pin controls:
        pin the single-chain sim to a stable, cache-warm block instead of
        re-forking to upstream head on every call (see AnvilSimulator._simulate_inner).
        They are ignored on the cross-chain branch and default to the prior
        head-refork behaviour, so scoring / order-processing are unchanged.
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
                # Fork seeds are applied INSIDE simulate() now — after the
                # per-sim re-fork and under the sim locks — via the
                # deposit_contract_seeds / fee_seeds params below. They used to
                # be dealt HERE on the event loop before `await simulate()`,
                # which (a) mutated the shared fork outside _sim_lock /
                # _fork_mutation_lock and raced offloaded sims (see
                # anvil_simulator._sim_offload_enabled), and (b) was silently
                # WIPED by this sim's own re-fork before scoreIntent ran on the
                # order rail. The standard-app executor balance + allowance are
                # already re-dealt inside _simulate_via_score_intent from
                # token_balances, so only the deposit-contract fund and the
                # user platform-fee fund are forwarded explicitly.
                sim_token_balances = token_balances
                deposit_contract_seeds: dict[str, int] | None = None
                fee_seeds: dict[str, int] | None = None

                # Deposit-model apps: scoreIntent -> _fundFromContract pulls the
                # input token from the APP CONTRACT, so fund the contract (not
                # the executor) inside the sim.
                if seed_contract and token_balances:
                    deposit_contract_seeds = dict(token_balances)
                    sim_token_balances = None  # not dealt to the executor

                # Standard apps: a user-paid platform fee (when set) is pulled
                # from the USER in WETH — fund it + approve the app inside the sim.
                if not seed_contract and token_balances and deployed_contract:
                    platform_fee = int(order.params.get("platform_fee_wei", 0))
                    if platform_fee > 0:
                        from minotaur_subnet.blockchain.tokens import WRAPPED_NATIVE_TOKEN
                        weth = WRAPPED_NATIVE_TOKEN.get(order.chain_id)
                        if weth:
                            fee_seeds = {weth: platform_fee}

                # Only forward the seed kwargs when set: keeps standard swaps /
                # quotes byte-identical, and never passes them to a simulator
                # whose simulate() signature doesn't accept them.
                _seed_kwargs: dict[str, Any] = {}
                if deposit_contract_seeds:
                    _seed_kwargs["deposit_contract_seeds"] = deposit_contract_seeds
                if fee_seeds:
                    _seed_kwargs["fee_seeds"] = fee_seeds

                if is_cross_chain and hasattr(self.simulator, "simulate_cross_chain"):
                    simulation = await self.simulator.simulate_cross_chain(
                        plan,
                        bridge_registry=self.bridge_registry,
                        contract_address=contract_address,
                        intent_order=intent_order_dict,
                        token_balances=sim_token_balances,
                        **_seed_kwargs,
                    )
                else:
                    logger.info("[LOOP] simulate: contract=%s intent_order=%s tokens=%s", contract_address, "yes" if intent_order_dict else "no", sim_token_balances)
                    simulation = await self.simulator.simulate(
                        plan,
                        contract_address=contract_address,
                        intent_order=intent_order_dict,
                        token_balances=sim_token_balances,
                        fork_block=fork_block,
                        pin_only=pin_only,
                        **_seed_kwargs,
                    )
                    logger.info("[LOOP] simulation result: success=%s error=%s transfers=%s", simulation.success, simulation.error, len(simulation.token_transfers or []))
                return simulation
            except Exception as exc:
                logger.error("[LOOP] simulator exception: %s", exc, exc_info=True)
                logger.warning("Simulator failed, falling back to mock: %s", exc)
                return build_mock_simulation(plan, order.params)
        else:
            return build_mock_simulation(plan, order.params)
