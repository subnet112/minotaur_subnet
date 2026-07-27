"""The request/scenario chain is authoritative for anvil selection.

Regression for the split-brain where the contract address was resolved for one
chain (``req.chain_id``) but the simulator was picked from the solver's plan —
so an Ethereum DEX contract could run on the Base fork (no code → empty
``relayer()`` → silent zero). ``_get_simulator`` now routes by the caller's
authoritative ``chain_id`` and only cross-checks the plan's hint.
"""
from __future__ import annotations

import asyncio

from minotaur_subnet.shared.types import ExecutionPlan, Interaction, SimulationResult
from minotaur_subnet.simulator.anvil_simulator import MultiChainSimulator


def _plan(chain=None, interaction_chain=0):
    md = {"chain_id": chain} if chain is not None else {}
    return ExecutionPlan(
        intent_id="t",
        interactions=[
            Interaction(target="0x" + "11" * 20, value="0", call_data="0x",
                        chain_id=interaction_chain),
        ],
        deadline=0, nonce=0, metadata=md,
    )


class _FakeSim:
    def __init__(self, cid):
        self.cid = cid
        self.last_kwargs = None

    async def simulate(self, plan, **kwargs):
        self.last_kwargs = kwargs
        return SimulationResult(success=True, gas_used=self.cid)


def _mc():
    # Bypass __init__ (needs live anvils) — _get_simulator only touches
    # .simulators and .default_chain_id.
    mc = MultiChainSimulator.__new__(MultiChainSimulator)
    mc.simulators = {1: _FakeSim(1), 8453: _FakeSim(8453), 31337: _FakeSim(31337)}
    mc.default_chain_id = 31337
    return mc


class TestAuthoritativeChainSelection:
    def test_authoritative_overrides_mis_stamped_plan(self):
        mc = _mc()
        # plan says Base (8453) but the request/scenario is ETH (1)
        assert mc._get_simulator(_plan(chain=8453), chain_id=1) is mc.simulators[1]

    def test_authoritative_missing_sim_returns_none_not_default(self):
        mc = _mc()
        # chain with no sub-sim must fail CLOSED, never silently route to the
        # local/default fork (the wrong-chain footgun).
        assert mc._get_simulator(_plan(chain=1), chain_id=999) is None

    def test_authoritative_str_chain_id(self):
        mc = _mc()
        assert mc._get_simulator(_plan(chain=8453), chain_id="1") is mc.simulators[1]

    def test_legacy_plan_derived_when_no_chain_id(self):
        mc = _mc()
        assert mc._get_simulator(_plan(chain=8453), chain_id=None) is mc.simulators[8453]

    def test_legacy_default_fallback(self):
        mc = _mc()
        assert mc._get_simulator(_plan(chain=999), chain_id=None) is mc.simulators[31337]

    def test_interaction_chain_used_when_metadata_absent(self):
        mc = _mc()
        p = _plan(chain=None, interaction_chain=8453)
        assert mc._get_simulator(p, chain_id=None) is mc.simulators[8453]

    def test_simulate_routes_by_chain_id_and_does_not_forward_it(self):
        mc = _mc()
        res = asyncio.run(
            mc.simulate(_plan(chain=8453), chain_id=1, contract_address="0xabc")
        )
        assert res.success and res.gas_used == 1        # ran on the ETH sim
        # chain_id is consumed by the router, NOT forwarded to the single sim
        assert mc.simulators[1].last_kwargs == {"contract_address": "0xabc"}
        assert mc.simulators[8453].last_kwargs is None  # Base sim never touched

    def test_simulate_no_sim_for_authoritative_chain_errors_clean(self):
        mc = _mc()
        res = asyncio.run(mc.simulate(_plan(chain=1), chain_id=999))
        assert not res.success and "chain 999" in (res.error or "")
