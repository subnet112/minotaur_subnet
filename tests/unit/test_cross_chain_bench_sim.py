"""Deterministic destination-leg measurement for the benchmark (Phase 0).

Covers the three pieces that stand between a cross-chain plan and a
destination number the champion contest could eventually trust:

  - ``normalize_to_legs`` — projecting the modern ``cross_chain_plan`` /
    ``multi_leg_plan`` shapes onto the legacy ``legs`` convention that
    ``simulate_cross_chain`` walks. Without it a modern plan falls through
    to single-chain and the destination chain is never touched.
  - ``benchmark_bridge_estimate`` — the fixed-fee model. The live quote path
    is non-deterministic across validators; the solver's declared output is
    self-reported. Neither may feed a scored path.
  - ``observed_bridged_amount`` — reading what actually left the source fork
    off the simulation rather than off the plan.

The Phase-0 contract is that none of this moves a score, so the tests also
pin ``destination_delivered`` as measurement-only.
"""

from __future__ import annotations

from dataclasses import asdict

import pytest

pytestmark = pytest.mark.cross_chain

from minotaur_subnet.simulator.cross_chain_bench import (
    BENCHMARK_BRIDGE_FEE_BPS,
    benchmark_bridge_estimate,
    is_cross_chain_plan,
    normalize_to_legs,
    observed_bridged_amount,
)
from minotaur_subnet.shared.types import (
    ExecutionPlan,
    Interaction,
    TokenTransfer,
    _MOCK_BRIDGE_TARGET,
)

WETH_ETH = "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"
WETH_BASE = "0x4200000000000000000000000000000000000006"
AMOUNT = 10**18


def _ix(chain_id: int = 1, selector: str = "a9059cbb") -> Interaction:
    return Interaction(
        target="0x" + "11" * 20, value="0",
        call_data=f"0x{selector}" + "00" * 28, chain_id=chain_id,
    )


def _plan(metadata: dict, interactions=None) -> ExecutionPlan:
    return ExecutionPlan(
        intent_id="app-1", interactions=interactions or [],
        deadline=0, nonce=0, metadata=metadata,
    )


def _cross_chain_plan_meta() -> dict:
    """Solver shape: two legs, one bridge request between them."""
    return {
        "cross_chain_plan": {
            "legs": [
                {"chain_id": 1, "interactions": [asdict(_ix(1))]},
                {"chain_id": 8453, "interactions": [asdict(_ix(8453))]},
            ],
            "bridge_requests": [{
                "token": WETH_ETH, "amount": AMOUNT,
                "src_chain_id": 1, "dst_chain_id": 8453,
            }],
        },
    }


def _multi_leg_plan_meta() -> dict:
    """Compiler shape: solver leg, bridge leg (real calldata), solver leg."""
    return {
        "multi_leg_plan": {
            "forward_legs": [
                {"leg_index": 0, "chain_id": 1,
                 "interactions": [asdict(_ix(1))],
                 "metadata": {"type": "solver_leg"}},
                {"leg_index": 1, "chain_id": 1,
                 "interactions": [asdict(_ix(1, "7b939232"))],
                 "metadata": {"type": "bridge", "bridge_amount": AMOUNT,
                              "bridge_token_out": WETH_BASE}},
                {"leg_index": 2, "chain_id": 8453,
                 "interactions": [asdict(_ix(8453))],
                 "metadata": {"type": "solver_leg"}},
            ],
            "rollback_legs": [
                {"leg_index": 100, "chain_id": 8453, "interactions": [],
                 "metadata": {"type": "rollback_bridge"}},
            ],
        },
    }


# ═══════════════════════════════════════════════════════════════════════════
#  Normalization — modern shapes reach the destination fork at all
# ═══════════════════════════════════════════════════════════════════════════


class TestNormalize:
    def test_legacy_legs_pass_through_untouched(self):
        plan = _plan({"legs": [{"leg_id": 0, "chain_id": 1}]})
        assert normalize_to_legs(plan) is plan

    def test_single_chain_plan_returns_none(self):
        # None = "keep your single-chain path"; nothing to measure elsewhere.
        assert normalize_to_legs(_plan({"route": "univ3"})) is None

    def test_cross_chain_plan_becomes_legs(self):
        out = normalize_to_legs(_plan(_cross_chain_plan_meta()))
        legs = out.metadata["legs"]
        # solver leg → bridge → solver leg
        assert [l["type"] for l in legs] == ["source", "bridge", "destination"]
        assert [l["chain_id"] for l in legs] == [1, 1, 8453]

    def test_multi_leg_plan_becomes_legs(self):
        out = normalize_to_legs(_plan(_multi_leg_plan_meta()))
        legs = out.metadata["legs"]
        assert [l["type"] for l in legs] == ["source", "bridge", "destination"]

    def test_rollback_legs_are_excluded(self):
        # The revert path is recovery, not the forward outcome being measured.
        out = normalize_to_legs(_plan(_multi_leg_plan_meta()))
        assert len(out.metadata["legs"]) == 3

    def test_interaction_indices_address_the_flat_list(self):
        out = normalize_to_legs(_plan(_multi_leg_plan_meta()))
        legs = out.metadata["legs"]
        # extract_leg_plan slices plan.interactions by these indices.
        for leg in legs:
            for i in leg["interaction_indices"]:
                assert i < len(out.interactions)
        assert legs[0]["interaction_indices"] == [0]
        assert legs[1]["interaction_indices"] == [1]   # the bridge calldata
        assert legs[2]["interaction_indices"] == [2]

    def test_compiled_shape_wins_over_solver_shape(self):
        # If both are present the compiled plan is what would execute.
        meta = {**_cross_chain_plan_meta(), **_multi_leg_plan_meta()}
        out = normalize_to_legs(_plan(meta))
        assert out.metadata["legs"][1]["bridge_amount"] == AMOUNT

    def test_original_plan_is_not_mutated(self):
        plan = _plan(_multi_leg_plan_meta())
        normalize_to_legs(plan)
        assert "legs" not in plan.metadata

    def test_single_leg_is_not_multi_chain(self):
        meta = {"cross_chain_plan": {
            "legs": [{"chain_id": 1, "interactions": []}],
            "bridge_requests": [],
        }}
        assert normalize_to_legs(_plan(meta)) is None


class TestIsCrossChain:
    @pytest.mark.parametrize("meta", [
        {"legs": [1]}, {"multi_leg_plan": {"forward_legs": []}},
        {"cross_chain_plan": {"legs": []}},
        {"cross_chain": True},
    ])
    def test_declaration_forms(self, meta):
        assert is_cross_chain_plan(_plan(meta)) is True

    def test_single_chain_is_false(self):
        assert is_cross_chain_plan(_plan({"route": "univ3"})) is False


# ═══════════════════════════════════════════════════════════════════════════
#  The bridge model — deterministic and not solver-controlled
# ═══════════════════════════════════════════════════════════════════════════


class TestBridgeEstimate:
    def test_applies_the_constant_haircut(self):
        est = benchmark_bridge_estimate(AMOUNT, WETH_BASE, "simulated")
        assert est["fee"] == AMOUNT * BENCHMARK_BRIDGE_FEE_BPS // 10_000
        assert est["estimated_output"] == AMOUNT - est["fee"]

    def test_is_pure_no_network(self):
        a = benchmark_bridge_estimate(AMOUNT, WETH_BASE, "simulated")
        b = benchmark_bridge_estimate(AMOUNT, WETH_BASE, "simulated")
        assert a == b

    def test_fee_is_conservative_vs_live_rails(self):
        # Measured live 2026-07-26: Across 1.03–2.78 bps, CCTP 1.00 bps. The
        # benchmark must not FLATTER a bridged route against a single-chain one.
        assert BENCHMARK_BRIDGE_FEE_BPS >= 3

    def test_records_where_the_amount_came_from(self):
        assert benchmark_bridge_estimate(
            1, "", "declared")["amount_source"] == "declared"

    def test_negative_amount_clamps(self):
        assert benchmark_bridge_estimate(-5, "", "simulated")["amount_in"] == 0


class TestObservedBridgedAmount:
    def test_reads_the_mocked_bridge_transfer(self):
        transfers = [
            TokenTransfer(token=WETH_ETH, from_addr="0x" + "aa" * 20,
                          to_addr=_MOCK_BRIDGE_TARGET, amount=AMOUNT),
        ]
        assert observed_bridged_amount(transfers) == AMOUNT

    def test_ignores_unrelated_transfers(self):
        transfers = [
            TokenTransfer(token=WETH_ETH, from_addr="0x" + "aa" * 20,
                          to_addr="0x" + "cc" * 20, amount=999),
        ]
        assert observed_bridged_amount(transfers) == 0

    def test_accepts_dict_rows(self):
        # leg_results carries plain dicts, not TokenTransfer objects.
        rows = [{"token": WETH_ETH, "from": "0x" + "aa" * 20,
                 "to": _MOCK_BRIDGE_TARGET, "amount": AMOUNT}]
        assert observed_bridged_amount(rows) == AMOUNT

    def test_case_insensitive_address_match(self):
        rows = [{"to": _MOCK_BRIDGE_TARGET.upper(), "amount": 7}]
        assert observed_bridged_amount(rows) == 7

    def test_empty_and_garbage_are_zero(self):
        assert observed_bridged_amount(None) == 0
        assert observed_bridged_amount([{"to": _MOCK_BRIDGE_TARGET,
                                         "amount": "junk"}]) == 0


# ═══════════════════════════════════════════════════════════════════════════
#  The bridge deposit is EXECUTED, so a declaration can't be inflated
# ═══════════════════════════════════════════════════════════════════════════


class _FakeChainSim:
    """Stands in for one chain's AnvilSimulator."""

    def __init__(self, success=True, moved=0, raises=False):
        self.success, self.moved, self.raises = success, moved, raises
        self.calls: list = []

    async def simulate(self, plan, **kwargs):
        self.calls.append(plan)
        if self.raises:
            raise RuntimeError("fork gone")
        transfers = (
            [TokenTransfer(token=WETH_ETH, from_addr="0x" + "aa" * 20,
                           to_addr=_MOCK_BRIDGE_TARGET, amount=self.moved)]
            if self.moved else []
        )
        from minotaur_subnet.shared.types import SimulationResult
        return SimulationResult(
            success=self.success, gas_used=1, error=None,
            token_transfers=transfers,
        )


def _multichain(sim_by_chain):
    from minotaur_subnet.simulator.anvil_simulator import MultiChainSimulator
    mc = MultiChainSimulator.__new__(MultiChainSimulator)
    mc.simulators = sim_by_chain
    mc.default_chain_id = 1
    return mc


class TestBridgeDepositIsExecuted:
    def _bridge_leg(self, amount=AMOUNT):
        return {"leg_id": 1, "chain_id": 1, "type": "bridge",
                "interaction_indices": [0], "bridge_amount": amount,
                "token_in": WETH_ETH, "token_out": WETH_BASE}

    def _plan_with_bridge_calldata(self):
        return _plan(
            {"legs": [self._bridge_leg()]},
            interactions=[_ix(1, "7b939232")],
        )

    def test_observed_amount_wins_over_the_declaration(self):
        import asyncio
        # The proxy only moved half of what the plan declared.
        mc = _multichain({1: _FakeChainSim(success=True, moved=AMOUNT // 2)})
        moved, source = asyncio.run(mc._simulate_mocked_bridge(
            self._plan_with_bridge_calldata(), self._bridge_leg(), {},
        ))
        assert (moved, source) == (AMOUNT // 2, "simulated")

    def test_reverted_deposit_earns_nothing(self):
        import asyncio
        # A plan declaring an amount it never earned reverts here — the whole
        # point of executing rather than assuming.
        mc = _multichain({1: _FakeChainSim(success=False)})
        moved, source = asyncio.run(mc._simulate_mocked_bridge(
            self._plan_with_bridge_calldata(), self._bridge_leg(), {},
        ))
        assert (moved, source) == (0, "unfilled")

    def test_successful_but_moved_nothing_earns_nothing(self):
        import asyncio
        mc = _multichain({1: _FakeChainSim(success=True, moved=0)})
        moved, source = asyncio.run(mc._simulate_mocked_bridge(
            self._plan_with_bridge_calldata(), self._bridge_leg(), {},
        ))
        assert (moved, source) == (0, "unfilled")

    def test_simulator_exception_earns_nothing(self):
        import asyncio
        mc = _multichain({1: _FakeChainSim(raises=True)})
        moved, source = asyncio.run(mc._simulate_mocked_bridge(
            self._plan_with_bridge_calldata(), self._bridge_leg(), {},
        ))
        assert (moved, source) == (0, "unfilled")

    def test_no_calldata_falls_back_to_declared(self):
        import asyncio
        # Solver shape: the compiler hasn't injected bridge calldata yet, so
        # there is nothing to execute and the declaration is all there is.
        leg = {**self._bridge_leg(), "interaction_indices": []}
        mc = _multichain({1: _FakeChainSim()})
        moved, source = asyncio.run(mc._simulate_mocked_bridge(
            _plan({"legs": [leg]}), leg, {},
        ))
        assert (moved, source) == (AMOUNT, "declared")

    def test_bridge_calldata_is_mocked_before_execution(self):
        import asyncio
        chain_sim = _FakeChainSim(success=True, moved=AMOUNT)
        asyncio.run(_multichain({1: chain_sim})._simulate_mocked_bridge(
            self._plan_with_bridge_calldata(), self._bridge_leg(), {},
        ))
        # The real depositV3 would revert on a fork with no relayer.
        executed = chain_sim.calls[0].interactions[0].call_data
        assert executed.startswith("0xa9059cbb")


# ═══════════════════════════════════════════════════════════════════════════
#  Phase-0 contract: measurement only
# ═══════════════════════════════════════════════════════════════════════════


class TestObserveOnly:
    def test_result_fields_default_to_none(self):
        from minotaur_subnet.harness.orchestrator import BenchmarkResult
        br = BenchmarkResult(intent_id="app:scenario")
        assert br.destination_delivered is None
        assert br.destination_amount_source is None

    def test_single_chain_measurement_is_skipped(self):
        import asyncio
        from minotaur_subnet.harness.orchestrator import (
            _measure_destination_delivery,
        )

        class ExplodingSimulator:
            async def simulate_cross_chain(self, *a, **kw):
                raise AssertionError("must not touch a single-chain plan")

        out = asyncio.run(_measure_destination_delivery(
            ExplodingSimulator(), _plan({"route": "univ3"}), None, None, None,
        ))
        assert out == (None, None)

    def test_observation_failure_is_swallowed(self):
        import asyncio
        from minotaur_subnet.harness.orchestrator import (
            _measure_destination_delivery,
        )

        class BrokenSimulator:
            async def simulate_cross_chain(self, *a, **kw):
                raise RuntimeError("fork unavailable")

        # A failed observation must not fail the benchmark row.
        out = asyncio.run(_measure_destination_delivery(
            BrokenSimulator(), _plan(_multi_leg_plan_meta()), None, None, None,
        ))
        assert out == (None, None)

    def test_simulator_without_the_method_is_skipped(self):
        import asyncio
        from minotaur_subnet.harness.orchestrator import (
            _measure_destination_delivery,
        )

        class SingleChainOnly:
            pass

        out = asyncio.run(_measure_destination_delivery(
            SingleChainOnly(), _plan(_multi_leg_plan_meta()), None, None, None,
        ))
        assert out == (None, None)
