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
    return SimpleNamespace(
        params=params, chain_id=chain_id, submitted_by=_USER,
        intent_function="spend", order_id="ord_test", app_id="app_test",
    )


def _manifest(*, funds_from_contract: bool = False) -> dict:
    """A minimal APP-SHAPED manifest: one user-supplied address + one
    user-supplied amount is the spend side. Deliberately not named after any
    app's vocabulary — the platform reads types and sources, not names."""
    fn: dict = {
        "name": "spend",
        "params": {
            "input_token": {"type": "address", "source": "user"},
            "input_amount": {"type": "uint256", "source": "user"},
            # source=quote / source=system params must NOT be mistaken for the
            # spend side, so include one of each.
            "min_output_amount": {"type": "uint256", "source": "quote"},
            "receiver": {"type": "address", "source": "system"},
        },
    }
    if funds_from_contract:
        fn["funds_from_contract"] = True
    return {"intent_functions": [fn]}


def _run(sim, order, deployed=_CONTRACT, manifest=None):
    runner = SimulationRunner(simulator=sim)
    return asyncio.run(runner.simulate(
        object(), order, deployed, None, False, deployed,
        manifest=manifest if manifest is not None else _manifest(),
    ))


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
    # Deposit-model is now DECLARED by the app, not inferred from an
    # "amountPerBuy" param existing (one DCA app's vocabulary).
    _run(sim, _order({"input_token": _TOK, "input_amount": "1000"}),
         manifest=_manifest(funds_from_contract=True))
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


def test_unseedable_order_is_logged_not_silent(caplog):
    """An app whose manifest declares no unambiguous spend pair must produce a
    LOUD warning. Silently declining shows up as a score of 0, which reads as
    a bad solver rather than a manifest gap."""
    import logging

    sim = _RecSim()
    ambiguous = {"intent_functions": [{
        "name": "spend",
        "params": {
            "a_token": {"type": "address", "source": "user"},
            "amount_one": {"type": "uint256", "source": "user"},
            "amount_two": {"type": "uint256", "source": "user"},
        },
    }]}
    with caplog.at_level(logging.WARNING):
        _run(sim, _order({"a_token": _TOK, "amount_one": "1"}), manifest=ambiguous)
    assert any("Not seeding order" in r.message for r in caplog.records)
    assert sim.sim_kwargs["token_balances"] is None


def test_quote_sourced_amount_is_not_the_spend_side():
    """min_output_amount is source=quote — a slippage guard, not what the
    order spends. Seeding it would fund the wrong leg."""
    sim = _RecSim()
    _run(sim, _order({
        "input_token": _TOK, "input_amount": "1000", "min_output_amount": "990",
    }))
    assert sim.sim_kwargs["token_balances"] == {_TOK: 1000}
