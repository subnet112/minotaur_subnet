"""The destination-leg measurement reaches the app's scorer — and nothing else.

Three invariants:

1. ``context.simulation`` carries ``destination_delivered`` /
   ``destination_amount_source`` EXACTLY when the platform set them (the
   benchmark path), as decimal STRINGS — and is byte-identical to before when
   they are unset (every live / quote / follower call, and every single-leg
   plan). A scorer that ignores them cannot tell this change happened.

2. The synthesized solver-shape deposit and the mocked real-calldata deposit
   are byte-identical transfers (one shared encoder) — the observed amount can
   never depend on which shape the plan arrived in.

3. ``bridge_execution_plan`` falls back to None (→ "declared") only when
   there is genuinely nothing executable to observe.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.cross_chain

from minotaur_subnet.engine.context import JsContext
from minotaur_subnet.shared.types import (
    ExecutionPlan,
    IntentState,
    Interaction,
    SimulationResult,
    _MOCK_BRIDGE_TARGET,
    mock_bridge_deposit,
    mock_bridge_interactions,
)
from minotaur_subnet.simulator.cross_chain_bench import bridge_execution_plan

WETH = "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"
AMOUNT = 10**18


def _context(sim: SimulationResult) -> dict:
    state = IntentState(
        contract_address="0x" + "22" * 20, chain_id=8453,
        nonce=0, owner="0x" + "33" * 20, raw_params={"dest_chain_id": 1},
    )
    return JsContext(chain_id=8453, contract_address="0x" + "22" * 20,
                     ).build_context(sim, state)


class TestContextExposure:
    def test_absent_when_unset(self):
        # The live / quote / follower / single-leg case: nothing set, nothing
        # emitted — a scorer reading the field sees undefined, and the
        # context is byte-identical to before this change.
        ctx = _context(SimulationResult(success=True, gas_used=21000))
        assert "destination_delivered" not in ctx["simulation"]
        assert "destinationDelivered" not in ctx["simulation"]
        assert "destination_amount_source" not in ctx["simulation"]

    def test_present_as_exact_strings_when_set(self):
        # 2**160 cannot survive a float round-trip; the context must carry
        # the decimal string untouched.
        big = str(2**160)
        sim = SimulationResult(
            success=True, gas_used=21000,
            destination_delivered=big,
            destination_amount_source="simulated",
        )
        ctx = _context(sim)["simulation"]
        assert ctx["destination_delivered"] == big
        assert ctx["destinationDelivered"] == big
        assert isinstance(ctx["destination_delivered"], str)
        assert ctx["destination_amount_source"] == "simulated"
        assert ctx["destinationAmountSource"] == "simulated"

    def test_simulation_result_defaults_are_none(self):
        sim = SimulationResult(success=True)
        assert sim.destination_delivered is None
        assert sim.destination_amount_source is None


class TestSharedDepositEncoder:
    def test_synthesized_equals_mocked(self):
        # depositV3 calldata being mocked vs a calldata-less leg being
        # synthesized must produce the same transfer, byte for byte.
        real = Interaction(
            target="0x" + "44" * 20, value="0",
            call_data="0x7b939232" + "00" * 64, chain_id=1,
        )
        mocked = mock_bridge_interactions([real], WETH, AMOUNT)[0]
        synthesized = mock_bridge_deposit(WETH, AMOUNT, 1)
        assert mocked.call_data == synthesized.call_data
        assert mocked.target == synthesized.target == WETH
        assert synthesized.call_data.startswith("0xa9059cbb")
        assert _MOCK_BRIDGE_TARGET[2:].lower() in synthesized.call_data.lower()


def _bridge_leg(**over) -> dict:
    leg = {"leg_id": 1, "chain_id": 1, "type": "bridge",
           "interaction_indices": [], "bridge_amount": AMOUNT,
           "token_in": WETH, "token_out": WETH}
    leg.update(over)
    return leg


def _plan(legs, interactions=None) -> ExecutionPlan:
    return ExecutionPlan(
        intent_id="app-1", interactions=interactions or [],
        deadline=0, nonce=0, metadata={"legs": legs},
    )


class TestBridgeExecutionPlanFallbacks:
    def test_no_token_is_not_executable(self):
        leg = _bridge_leg(token_in="")
        assert bridge_execution_plan(_plan([leg]), leg) is None

    def test_no_amount_is_not_executable(self):
        leg = _bridge_leg(bridge_amount=0)
        assert bridge_execution_plan(_plan([leg]), leg) is None

    def test_garbage_amount_is_not_executable(self):
        leg = _bridge_leg(bridge_amount="not-a-number")
        assert bridge_execution_plan(_plan([leg]), leg) is None

    def test_synthesis_when_token_and_amount_present(self):
        leg = _bridge_leg()
        out = bridge_execution_plan(_plan([leg]), leg)
        assert out is not None
        assert len(out.interactions) == 1
        assert out.interactions[0].call_data.startswith("0xa9059cbb")

    def test_substrate_and_wait_legs_stay_out(self):
        legs = [
            {"leg_id": 0, "chain_id": 1, "type": "source",
             "runtime": "substrate", "interaction_indices": []},
            {"leg_id": 1, "chain_id": 1, "type": "wait",
             "interaction_indices": []},
            _bridge_leg(leg_id=2),
        ]
        out = bridge_execution_plan(_plan(legs), legs[2])
        assert out is not None
        assert len(out.interactions) == 1  # only the synthesized deposit
