"""Bridge mocking in the BENCHMARK scoring path.

Bridge contracts can't execute on an Anvil fork (no relayer to fill an
Across deposit, no attestation service to mint a CCTP burn), so a plan
carrying real bridge calldata reverts and ``require_real_sim`` fail-closes
it to 0 — scoring a correct cross-chain answer exactly like no answer at
all. The validator's re-simulation and the live multi-leg path both already
mock these calls; the benchmark was the one scoring path that didn't.

The load-bearing test here is the NEGATIVE one: a single-chain plan must
come back as the *same object*, so every existing champion score stays
bit-identical (docs/architecture/cross-chain-intents.md §8 trap 2). This is
a consensus-relevant path — a scoring change that leaked into single-chain
plans would diverge the fleet.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.cross_chain

from minotaur_subnet.harness.orchestrator import _mock_bridge_for_benchmark
from minotaur_subnet.shared.types import (
    ExecutionPlan,
    IntentState,
    Interaction,
    _MOCK_BRIDGE_TARGET,
)

WETH = "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"
AMOUNT = 10**18

# Real bridge selectors the compiler injects (shared.types._BRIDGE_CALL_SELECTORS)
ACROSS_DEPOSIT_V3 = "7b939232"
CCTP_DEPOSIT_FOR_BURN = "8e0250ee"
HYPERLANE_TRANSFER_REMOTE = "81b4e8b4"
ERC20_TRANSFER = "a9059cbb"


def _ix(selector: str, target: str = "0x" + "11" * 20) -> Interaction:
    return Interaction(
        target=target, value="0", call_data=f"0x{selector}" + "00" * 28, chain_id=1,
    )


def _plan(interactions: list[Interaction], metadata: dict) -> ExecutionPlan:
    return ExecutionPlan(
        intent_id="app-1", interactions=interactions,
        deadline=0, nonce=0, metadata=metadata,
    )


def _state() -> IntentState:
    return IntentState(
        contract_address="0x" + "cc" * 20, chain_id=1, nonce=0, owner="",
        raw_params={"input_token": WETH, "input_amount": str(AMOUNT)},
    )


# ═══════════════════════════════════════════════════════════════════════════
#  Single-chain plans must be untouched — the consensus-safety property
# ═══════════════════════════════════════════════════════════════════════════


class TestSingleChainUnchanged:
    def test_plain_swap_returns_the_same_object(self):
        plan = _plan([_ix(ERC20_TRANSFER)], {"route": "univ3"})
        # Identity, not equality: nothing was rebuilt, so nothing can drift.
        assert _mock_bridge_for_benchmark(plan, _state()) is plan

    def test_no_metadata_returns_the_same_object(self):
        plan = _plan([_ix(ERC20_TRANSFER)], {})
        assert _mock_bridge_for_benchmark(plan, _state()) is plan

    def test_bridge_calldata_without_cross_chain_metadata_is_untouched(self):
        # The gate is the DECLARATION, not the calldata. A single-chain plan
        # that happens to carry a matching selector must not be rewritten —
        # widening on calldata alone would silently rescore live champions.
        plan = _plan([_ix(ACROSS_DEPOSIT_V3)], {"route": "univ3"})
        assert _mock_bridge_for_benchmark(plan, _state()) is plan

    def test_none_state_is_safe(self):
        plan = _plan([_ix(ERC20_TRANSFER)], {})
        assert _mock_bridge_for_benchmark(plan, None) is plan


# ═══════════════════════════════════════════════════════════════════════════
#  Declared cross-chain plans get their bridge calls mocked
# ═══════════════════════════════════════════════════════════════════════════


class TestCrossChainMocked:
    @pytest.mark.parametrize("flag", [
        "cross_chain", "multi_leg_plan", "cross_chain_plan",
    ])
    def test_every_declaration_form_triggers(self, flag):
        plan = _plan([_ix(ACROSS_DEPOSIT_V3)], {flag: True})
        out = _mock_bridge_for_benchmark(plan, _state())
        assert out is not plan
        assert out.interactions[0].call_data.startswith("0x" + ERC20_TRANSFER)

    @pytest.mark.parametrize("selector", [
        ACROSS_DEPOSIT_V3, CCTP_DEPOSIT_FOR_BURN, HYPERLANE_TRANSFER_REMOTE,
    ])
    def test_all_three_rails_are_mocked(self, selector):
        plan = _plan([_ix(selector)], {"cross_chain": True})
        out = _mock_bridge_for_benchmark(plan, _state())
        assert out.interactions[0].target == WETH
        assert _MOCK_BRIDGE_TARGET.lower()[2:] in out.interactions[0].call_data.lower()

    def test_mock_transfers_the_declared_input_amount(self):
        plan = _plan([_ix(ACROSS_DEPOSIT_V3)], {"cross_chain": True})
        out = _mock_bridge_for_benchmark(plan, _state())
        # trailing uint256 word of transfer(address,uint256)
        assert int(out.interactions[0].call_data[-64:], 16) == AMOUNT

    def test_non_bridge_calls_in_a_cross_chain_plan_survive(self):
        swap = _ix(ERC20_TRANSFER, target="0x" + "22" * 20)
        plan = _plan([swap, _ix(ACROSS_DEPOSIT_V3)], {"cross_chain": True})
        out = _mock_bridge_for_benchmark(plan, _state())
        assert out.interactions[0] == swap          # untouched
        assert out.interactions[1].target == WETH   # mocked

    def test_declared_but_no_bridge_calldata_returns_same_object(self):
        # A destination-only leg declares cross-chain but bridges nothing.
        plan = _plan([_ix(ERC20_TRANSFER)], {"cross_chain": True})
        assert _mock_bridge_for_benchmark(plan, _state()) is plan

    def test_original_plan_is_never_mutated(self):
        original = _ix(ACROSS_DEPOSIT_V3)
        plan = _plan([original], {"cross_chain": True})
        _mock_bridge_for_benchmark(plan, _state())
        # br.plan (and its hash) must still describe what the solver returned.
        assert plan.interactions[0] is original
        assert plan.interactions[0].call_data.startswith("0x" + ACROSS_DEPOSIT_V3)

    def test_metadata_is_carried_onto_the_sim_plan(self):
        meta = {"cross_chain": True, "chain_id": 8453}
        plan = _plan([_ix(ACROSS_DEPOSIT_V3)], meta)
        out = _mock_bridge_for_benchmark(plan, _state())
        # The simulator routes on metadata.chain_id — losing it would send the
        # plan to the wrong fork.
        assert out.metadata == meta
        assert out.deadline == plan.deadline and out.nonce == plan.nonce


class TestDeterminism:
    """This runs inside the scored benchmark: same input → same bytes, on
    every validator, with no network in the path."""

    def test_repeated_calls_are_byte_identical(self):
        plan = _plan([_ix(ACROSS_DEPOSIT_V3)], {"cross_chain": True})
        a = _mock_bridge_for_benchmark(plan, _state())
        b = _mock_bridge_for_benchmark(plan, _state())
        assert [i.call_data for i in a.interactions] == [
            i.call_data for i in b.interactions
        ]

    def test_missing_amount_degrades_to_zero_not_an_error(self):
        state = IntentState(
            contract_address="0x" + "cc" * 20, chain_id=1, nonce=0, owner="",
            raw_params={"input_token": WETH},   # no input_amount
        )
        plan = _plan([_ix(ACROSS_DEPOSIT_V3)], {"cross_chain": True})
        out = _mock_bridge_for_benchmark(plan, state)
        assert int(out.interactions[0].call_data[-64:], 16) == 0

    def test_garbage_amount_does_not_raise(self):
        state = IntentState(
            contract_address="0x" + "cc" * 20, chain_id=1, nonce=0, owner="",
            raw_params={"input_token": WETH, "input_amount": "not-a-number"},
        )
        plan = _plan([_ix(ACROSS_DEPOSIT_V3)], {"cross_chain": True})
        assert int(
            _mock_bridge_for_benchmark(plan, state).interactions[0].call_data[-64:], 16
        ) == 0
