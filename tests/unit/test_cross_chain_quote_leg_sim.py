"""Per-leg simulation in the unified cross-chain quote (§10 step 2).

The contract under test:

- With a simulator, a clean destination-leg run REPLACES the solver's
  declared estimate with the observed delivery
  (``estimated_output_source: "leg_simulation"``, ``simulated: true``).
- Without one — or on ANY failure (leg revert, sim exception, nothing
  delivered) — the payload degrades to exactly the pre-simulation shape,
  labelled, never raising.
"""

from __future__ import annotations

import asyncio

import pytest

pytestmark = pytest.mark.cross_chain

from minotaur_subnet.api.services.cross_chain_quote import build_cross_chain_quote
from minotaur_subnet.bridge.compiler import CrossChainCompiler
from minotaur_subnet.bridge.mock import MockBridgeAdapter
from minotaur_subnet.bridge.registry import BridgeRegistry
from minotaur_subnet.shared.types import (
    BridgeRequest,
    ChainLeg,
    CrossChainPlan,
    Interaction,
)

WETH_ETH = "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"
OUT_TOKEN = "0x" + "77" * 20
RECEIVER = "0x" + "bb" * 20
AMOUNT = 10**18


def _run(coro):
    return asyncio.run(coro)


def _ix(chain_id: int) -> Interaction:
    return Interaction(
        target="0x" + "11" * 20, value="0",
        call_data="0xa9059cbb" + "00" * 28, chain_id=chain_id,
    )


def _meta(**extra) -> dict:
    plan = CrossChainPlan(
        legs=[
            ChainLeg(chain_id=1, interactions=[_ix(1)]),
            ChainLeg(chain_id=8453, interactions=[_ix(8453)]),
        ],
        bridge_requests=[BridgeRequest(
            token=WETH_ETH, amount=AMOUNT, src_chain_id=1, dst_chain_id=8453,
            recipient=RECEIVER,
        )],
    )
    return {"cross_chain": True, "cross_chain_plan": plan.to_dict(), **extra}


@pytest.fixture
def compiler():
    reg = BridgeRegistry()
    reg.register(MockBridgeAdapter())
    return CrossChainCompiler(reg)


class _FakeSim:
    """MultiChainSimulator stand-in: canned leg_results, records kwargs."""

    def __init__(self, leg_results, raises=False):
        self._leg_results = leg_results
        self._raises = raises
        self.calls: list[dict] = []

    async def simulate_cross_chain(self, plan, **kwargs):
        self.calls.append(kwargs)
        if self._raises:
            raise RuntimeError("fork down")
        import types
        return types.SimpleNamespace(
            success=True, leg_results=self._leg_results, bridge_estimate={},
        )


def _dest_transfer(amount, to=RECEIVER, token=OUT_TOKEN):
    return {"token": token, "from": "0x" + "12" * 20, "to": to,
            "amount": str(amount)}


def _healthy_legs(delivered=AMOUNT - 10**15, to=RECEIVER):
    return {
        0: {"success": True, "gas_used": 100, "error": None,
            "token_transfers": []},
        1: {"success": True, "type": "bridge", "bridge_estimate": {}},
        2: {"success": True, "gas_used": 200, "error": None,
            "token_transfers": [_dest_transfer(delivered, to=to)]},
    }


PARAMS = {"input_token": WETH_ETH, "input_amount": str(AMOUNT),
          "output_token": OUT_TOKEN, "receiver": RECEIVER}


class TestLegSimulationWinsTheEstimate:
    def test_observed_delivery_replaces_declaration(self, compiler):
        delivered = AMOUNT - 10**15
        sim = _FakeSim(_healthy_legs(delivered))
        payload = _run(build_cross_chain_quote(
            _meta(dst_amount=str(AMOUNT * 2)),  # inflated declaration
            compiler, simulator=sim, bridge_registry=None, params=PARAMS,
        ))
        assert payload["estimated_output"] == delivered
        assert payload["estimated_output_source"] == "leg_simulation"
        assert payload["simulated"] is True
        assert payload["leg_simulation"]["destination_success"] is True

    def test_source_leg_is_funded_with_the_input(self, compiler):
        sim = _FakeSim(_healthy_legs())
        _run(build_cross_chain_quote(
            _meta(), compiler, simulator=sim, params=PARAMS,
        ))
        assert sim.calls[0]["token_balances"] == {WETH_ETH: AMOUNT}

    def test_placeholder_recipient_falls_back_to_largest_transfer(self, compiler):
        # Quote-time calldata carries placeholder addresses; a transfer to an
        # unmatched address still counts via the largest-transfer fallback.
        sim = _FakeSim(_healthy_legs(to="0x" + "99" * 20))
        payload = _run(build_cross_chain_quote(
            _meta(), compiler, simulator=sim, params=PARAMS,
        ))
        assert payload["estimated_output_source"] == "leg_simulation"
        assert payload["estimated_output"] == AMOUNT - 10**15


class TestDegradation:
    def test_no_simulator_is_the_old_payload(self, compiler):
        payload = _run(build_cross_chain_quote(_meta(), compiler))
        fee = AMOUNT * 10 // 10_000
        assert payload["estimated_output"] == AMOUNT - fee
        assert payload["estimated_output_source"] == "bridge_quote"
        assert payload["simulated"] is False
        assert "leg_simulation" not in payload

    def test_reverted_destination_degrades_labelled(self, compiler):
        legs = _healthy_legs()
        legs[2] = {"success": False, "gas_used": 0,
                   "error": "execution reverted", "token_transfers": []}
        sim = _FakeSim(legs)
        payload = _run(build_cross_chain_quote(
            _meta(dst_amount=str(AMOUNT // 2)),
            compiler, simulator=sim, params=PARAMS,
        ))
        # Falls back to the declaration, and says why.
        assert payload["estimated_output"] == AMOUNT // 2
        assert payload["estimated_output_source"] == "solver_declared"
        assert payload["simulated"] is False
        assert payload["leg_simulation"]["destination_success"] is False

    def test_sim_exception_degrades_silently(self, compiler):
        sim = _FakeSim({}, raises=True)
        payload = _run(build_cross_chain_quote(
            _meta(), compiler, simulator=sim, params=PARAMS,
        ))
        assert payload is not None
        assert payload["simulated"] is False
        assert "leg_simulation" not in payload

    def test_wrong_token_delivery_earns_nothing(self, compiler):
        legs = _healthy_legs()
        legs[2]["token_transfers"] = [
            _dest_transfer(AMOUNT, token="0x" + "de" * 20),
        ]
        sim = _FakeSim(legs)
        payload = _run(build_cross_chain_quote(
            _meta(), compiler, simulator=sim, params=PARAMS,
        ))
        assert payload["estimated_output_source"] == "bridge_quote"
        assert payload["simulated"] is False
