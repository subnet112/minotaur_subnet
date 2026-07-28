"""Simulation step of the block loop pipeline."""

from __future__ import annotations

import logging
from typing import Any

from minotaur_subnet.v3.manifest import spend_token_balances
from minotaur_subnet.shared.types import ExecutionPlan, SimulationResult
from minotaur_subnet.shared.simulation import build_mock_simulation
from minotaur_subnet.orderbook.orderbook import Order

logger = logging.getLogger(__name__)


def _funds_from_contract(manifest: Any, intent_function: str) -> bool:
    """Does this app declare that the intent is funded ON THE CONTRACT?

    Deposit-model apps (their scoreIntent pulls via _fundFromContract) set
    ``funds_from_contract: true`` on the intent in their manifest. This used
    to be inferred from an "amountPerBuy" param existing — one DCA app's
    param name standing in for a platform concept. Absent declaration means
    the executor is funded, which is the common case.
    """
    if manifest is None:
        return False
    fns = (
        manifest.get("intent_functions", []) or []
        if isinstance(manifest, dict)
        else list(getattr(manifest, "intents", []) or [])
    )
    for fn in fns:
        name = fn.get("name") if isinstance(fn, dict) else getattr(fn, "name", None)
        if name != intent_function:
            continue
        meta = (
            fn.get("metadata") if isinstance(fn, dict)
            else getattr(fn, "metadata", None)
        ) or {}
        if isinstance(fn, dict) and fn.get("funds_from_contract") is not None:
            return bool(fn["funds_from_contract"])
        return bool(meta.get("funds_from_contract"))
    return False


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
        manifest: Any = None,
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
            # Seed the simulator with whatever this order SPENDS, read from
            # the app's own manifest (v3.manifest.spend_token_balances).
            #
            # This used to guess from a fixed alias list —
            # input_token/tokenIn/token_in/asset paired with
            # input_amount/amountPerBuy/amount_per_buy/amount — i.e. the
            # platform holding a table of app archetypes (swap / DCA / yield)
            # and pattern-matching orders against it. An app outside the table
            # was simply not seeded.
            token_balances, reason = spend_token_balances(
                manifest, order.intent_function, order.params,
                default_chain_id=order.chain_id,
            )
            if token_balances is None and order.params:
                # LOUD on purpose. An unseeded order reverts in
                # safeTransferFrom and scores 0, which reads as a bad solver
                # rather than a manifest gap. Measured against the live corpus
                # (tools/seeding_replay.py) this fires for undeclared intent
                # functions only — 1 of 2367 quotes, itself already broken.
                logger.warning(
                    "Not seeding order %s (app %s, intent %r): %s — its "
                    "simulation will revert if the intent spends tokens",
                    order.order_id, order.app_id, order.intent_function, reason,
                )

            # Deposit-model apps are funded on the CONTRACT rather than the
            # executor, because their scoreIntent pulls via _fundFromContract.
            # An app declares that in its manifest; it used to be inferred
            # from an "amountPerBuy" param existing, which is one DCA app's
            # vocabulary rather than a platform concept.
            seed_contract = (
                deployed_contract
                if (deployed_contract and _funds_from_contract(manifest, order.intent_function))
                else None
            )

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

                # The ORDER's chain is authoritative for which anvil to run on —
                # the same chain that resolved ``contract_address``. Pass it to a
                # MultiChainSimulator so an ETH contract can never be simulated on
                # the Base fork (the split-brain: contract from the request,
                # anvil from the plan). Only a MultiChainSimulator accepts it;
                # a single-chain AnvilSimulator's simulate() does not, so gate on
                # the router method. Scoped to the single-chain branch — the
                # cross-chain path routes per-leg and must not receive it.
                _chain_kwargs: dict[str, Any] = {}
                if hasattr(self.simulator, "_get_simulator"):
                    _chain_kwargs["chain_id"] = getattr(order, "chain_id", None)

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
                        **_chain_kwargs,
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
