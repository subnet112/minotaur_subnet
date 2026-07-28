"""SimulationRunner no longer seeds the fork on the event loop.

The deposit-model contract fund and the user platform-fee fund used to be dealt
via ``_deal_erc20`` / ``_set_erc20_allowance`` on the loop BEFORE ``await
simulate()`` — outside the sim locks (racing offloaded sims per the
``SIM_OFFLOAD_TO_THREAD`` gate) and wiped by the per-sim re-fork anyway. They are
now forwarded as ``deposit_contract_seeds`` / ``fee_seeds`` kwargs and applied
inside ``_simulate_via_score_intent`` under the lock, after the re-fork. These
tests pin: (a) SimulationRunner never seeds on the loop, and (b) it forwards the
right kwargs, only when set (so standard swaps / quotes stay byte-identical).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from minotaur_subnet.blockchain.tokens import WRAPPED_NATIVE_TOKEN
from minotaur_subnet.blockloop.simulation import SimulationRunner
from minotaur_subnet.shared.types import SimulationResult

_TOK = "0x" + "11" * 20
_CONTRACT = "0x" + "22" * 20
_USER = "0x" + "33" * 20


class _RecSim:
    """Records simulate() kwargs and flags any loop-side seed call (there must be
    none — a real AnvilSimulator's _deal_erc20 mutates the shared fork)."""

    def __init__(self) -> None:
        self.sim_kwargs: dict | None = None
        self.loop_side_seed_calls: list = []

    def _deal_erc20(self, *a, **k):  # pragma: no cover - must never be hit
        self.loop_side_seed_calls.append(("deal", a, k))
        return True

    def _set_erc20_allowance(self, *a, **k):  # pragma: no cover - must never be hit
        self.loop_side_seed_calls.append(("approve", a, k))

    async def simulate(self, plan, **kwargs):
        self.sim_kwargs = kwargs
        return SimulationResult(success=True, gas_used=1)


def _order(params: dict, chain_id: int = 1):
    return SimpleNamespace(params=params, chain_id=chain_id, submitted_by=_USER)


def _run(sim, order, deployed=_CONTRACT):
    runner = SimulationRunner(simulator=sim)
    # signature: simulate(plan, order, contract_address, intent_order_dict,
    #                     is_cross_chain, deployed_contract)
    return asyncio.run(runner.simulate(object(), order, deployed, None, False, deployed))


def test_standard_order_passes_no_seed_kwargs():
    sim = _RecSim()
    res = _run(sim, _order({"input_token": _TOK, "input_amount": "1000"}))
    assert res.success
    assert sim.loop_side_seed_calls == []                       # nothing on the loop
    assert sim.sim_kwargs is not None
    assert "deposit_contract_seeds" not in sim.sim_kwargs        # byte-identical path
    assert "fee_seeds" not in sim.sim_kwargs
    assert sim.sim_kwargs["token_balances"] == {_TOK: 1000}      # dealt inside, as before


def test_deposit_order_forwards_deposit_seeds_and_nulls_executor_balance():
    sim = _RecSim()
    _run(sim, _order({"input_token": _TOK, "input_amount": "1000", "amountPerBuy": "100"}))
    assert sim.loop_side_seed_calls == []
    assert sim.sim_kwargs["deposit_contract_seeds"] == {_TOK: 1000}
    assert sim.sim_kwargs["token_balances"] is None             # not dealt to the executor
    assert "fee_seeds" not in sim.sim_kwargs


def test_user_fee_order_forwards_fee_seeds():
    sim = _RecSim()
    _run(sim, _order({"input_token": _TOK, "input_amount": "1000", "platform_fee_wei": "500"}))
    assert sim.loop_side_seed_calls == []
    assert sim.sim_kwargs["fee_seeds"] == {WRAPPED_NATIVE_TOKEN[1]: 500}
    assert sim.sim_kwargs["token_balances"] == {_TOK: 1000}     # executor balance unchanged
    assert "deposit_contract_seeds" not in sim.sim_kwargs


def test_zero_platform_fee_forwards_no_fee_seeds():
    sim = _RecSim()
    _run(sim, _order({"input_token": _TOK, "input_amount": "1000", "platform_fee_wei": "0"}))
    assert "fee_seeds" not in sim.sim_kwargs
