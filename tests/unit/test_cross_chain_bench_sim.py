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


class _State:
    """Minimal IntentState stand-in: the measurement only needs params."""

    def __init__(self, **params):
        self._params = params
        self.contract_address = None

    def raw_params_view(self) -> dict:
        return dict(self._params)


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

    def test_solver_shape_bridge_token_out_is_destination_mapped(self):
        # Soak finding 2026-07-28: the request carries the SOURCE token;
        # seeding the destination fork with it deals an address that has no
        # code there (Base WETH on Ethereum) and honest plans measure null.
        meta = {
            "cross_chain_plan": {
                "legs": [
                    {"chain_id": 8453, "interactions": []},
                    {"chain_id": 1, "interactions": [asdict(_ix(1))]},
                ],
                "bridge_requests": [{
                    "token": WETH_BASE, "amount": AMOUNT,
                    "src_chain_id": 8453, "dst_chain_id": 1,
                }],
            },
        }
        legs = normalize_to_legs(_plan(meta)).metadata["legs"]
        bridge = [l for l in legs if l["type"] == "bridge"][0]
        assert bridge["token_out"] == WETH_ETH   # destination-chain address
        assert bridge["token_in"] == WETH_BASE   # deposit stays source-side

    def test_unmapped_bridge_token_passes_through(self):
        from minotaur_subnet.simulator.cross_chain_bench import map_bridged_token
        exotic = "0x" + "ab" * 20
        assert map_bridged_token(exotic, 8453, 1) == exotic
        assert map_bridged_token("", 8453, 1) == ""
        assert map_bridged_token(WETH_BASE, "garbage", 1) == WETH_BASE
        # Case-insensitive on the source side, canonical casing out.
        assert map_bridged_token(WETH_BASE.upper().replace("0X", "0x"), 8453, 1) == WETH_ETH

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

    def test_no_calldata_synthesizes_the_deposit(self):
        import asyncio
        # Solver shape: no bridge calldata, but token+amount are declared —
        # the deposit is SYNTHESIZED and executed, so the amount is observed
        # off the sim (here: the fake moved it all), never self-reported.
        leg = {**self._bridge_leg(), "interaction_indices": []}
        chain_sim = _FakeChainSim(success=True, moved=AMOUNT)
        moved, source = asyncio.run(
            _multichain({1: chain_sim})._simulate_mocked_bridge(
                _plan({"legs": [leg]}), leg, {},
            ))
        assert (moved, source) == (AMOUNT, "simulated")
        executed = chain_sim.calls[0].interactions
        assert len(executed) == 1
        assert executed[0].call_data.startswith("0xa9059cbb")
        assert executed[0].target == WETH_ETH

    def test_synthesized_deposit_that_reverts_earns_nothing(self):
        import asyncio
        # A solver-shape declaration the journey never earned: the
        # synthesized transfer reverts. Inflating the declaration now COSTS
        # the credit instead of granting it.
        leg = {**self._bridge_leg(), "interaction_indices": []}
        mc = _multichain({1: _FakeChainSim(success=False)})
        moved, source = asyncio.run(mc._simulate_mocked_bridge(
            _plan({"legs": [leg]}), leg, {},
        ))
        assert (moved, source) == (0, "unfilled")

    def test_nothing_to_synthesize_falls_back_to_declared(self):
        import asyncio
        # No calldata AND no token to transfer (e.g. a native-asset bridge):
        # nothing executable to observe, so the declaration is all there is —
        # and it stays LABELLED as such.
        leg = {**self._bridge_leg(), "interaction_indices": [],
               "token_in": ""}
        mc = _multichain({1: _FakeChainSim()})
        moved, source = asyncio.run(mc._simulate_mocked_bridge(
            _plan({"legs": [leg]}), leg, {},
        ))
        assert (moved, source) == (AMOUNT, "declared")

    def test_preceding_same_chain_legs_run_before_the_deposit(self):
        import asyncio
        # Swap-then-bridge: the source leg's interactions must execute in the
        # SAME simulation as the deposit, or an honest deposit reverts
        # against the fork's seeded balances (simulate() is
        # snapshot-isolated per call).
        source_leg = {"leg_id": 0, "chain_id": 1, "type": "source",
                      "interaction_indices": [0]}
        bridge_leg = {**self._bridge_leg(), "leg_id": 1,
                      "interaction_indices": [1]}
        plan = _plan(
            {"legs": [source_leg, bridge_leg]},
            interactions=[_ix(1, "38ed1739"), _ix(1, "7b939232")],
        )
        chain_sim = _FakeChainSim(success=True, moved=AMOUNT)
        moved, source = asyncio.run(
            _multichain({1: chain_sim})._simulate_mocked_bridge(
                plan, bridge_leg, {},
            ))
        assert (moved, source) == (AMOUNT, "simulated")
        executed = chain_sim.calls[0].interactions
        assert len(executed) == 2
        # Source leg first, untouched; then the mocked deposit.
        assert executed[0].call_data.startswith("0x38ed1739")
        assert executed[1].call_data.startswith("0xa9059cbb")

    def test_other_chain_legs_stay_out_of_the_deposit_sim(self):
        import asyncio
        # A destination leg (other chain) must not leak into the source-side
        # journey.
        dest_leg = {"leg_id": 0, "chain_id": 8453, "type": "source",
                    "interaction_indices": [0]}
        bridge_leg = {**self._bridge_leg(), "leg_id": 1,
                      "interaction_indices": [1]}
        plan = _plan(
            {"legs": [dest_leg, bridge_leg]},
            interactions=[_ix(8453), _ix(1, "7b939232")],
        )
        chain_sim = _FakeChainSim(success=True, moved=AMOUNT)
        asyncio.run(_multichain({1: chain_sim})._simulate_mocked_bridge(
            plan, bridge_leg, {},
        ))
        assert len(chain_sim.calls[0].interactions) == 1

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

    def test_solver_shape_delivery_is_extracted(self):
        # Soak finding 2026-07-28: the extraction walked plan.metadata["legs"]
        # (legacy-only), while the solver's cross_chain_plan — the shape
        # miners emit — was normalized on a COPY inside the simulator. A
        # perfectly delivered journey measured null. The measurement must
        # normalize the plan it extracts from.
        import asyncio
        from minotaur_subnet.harness.orchestrator import (
            _ANVIL_DEFAULT_ACCOUNT,
            _measure_destination_delivery,
        )
        from minotaur_subnet.shared.types import SimulationResult, TokenTransfer

        delivered = AMOUNT - 10**15

        class RecordingSimulator:
            async def simulate_cross_chain(self, plan, **kw):
                legs = plan.metadata["legs"]
                dest = [l for l in legs if l["type"] == "destination"]
                assert dest, "normalized legs must reach the simulator"
                return SimulationResult(
                    success=True,
                    leg_results={dest[0]["leg_id"]: {
                        "success": True,
                        "token_transfers": [{
                            "token": WETH_ETH,
                            "from": "0x" + "12" * 20,
                            "to": _ANVIL_DEFAULT_ACCOUNT,
                            "amount": str(delivered),
                        }],
                    }},
                    bridge_estimate={"amount_source": "simulated"},
                )

        out = asyncio.run(_measure_destination_delivery(
            RecordingSimulator(), _plan(_cross_chain_plan_meta()),
            _State(output_token=WETH_ETH), None, None,
        ))
        assert out == (str(delivered), "simulated")


USDC_BASE = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"


def _dest_sim(transfers, amount_source="simulated"):
    """Simulator stub whose destination leg reports ``transfers``."""
    from minotaur_subnet.shared.types import SimulationResult

    class _Sim:
        async def simulate_cross_chain(self, plan, **kw):
            legs = plan.metadata["legs"]
            dest = [l for l in legs if l["type"] == "destination"]
            return SimulationResult(
                success=True,
                leg_results={dest[0]["leg_id"]: {
                    "success": True, "token_transfers": transfers,
                }},
                bridge_estimate={"amount_source": amount_source},
            )

    return _Sim()


def _transfer(token, to, amount):
    return {"token": token, "from": "0x" + "12" * 20,
            "to": to, "amount": str(amount)}


def _measure(transfers, **params):
    import asyncio
    from minotaur_subnet.harness.orchestrator import _measure_destination_delivery

    return asyncio.run(_measure_destination_delivery(
        _dest_sim(transfers), _plan(_cross_chain_plan_meta()),
        _State(**params), None, None,
    ))


class TestDeliveryIsTokenFiltered:
    """Delivery credit counts the asset the INTENT asked for — nothing else.

    Summing every transfer to the receiver measures arrival, not delivery. The
    two diverge exactly where the incentive lives, so each of these is a way
    the pre-filter measurement mispriced a plan.
    """

    def test_only_the_requested_token_is_credited(self):
        from minotaur_subnet.harness.orchestrator import _ANVIL_DEFAULT_ACCOUNT
        out = _measure(
            [_transfer(USDC_BASE, _ANVIL_DEFAULT_ACCOUNT, 320_000_000),
             _transfer(WETH_BASE, _ANVIL_DEFAULT_ACCOUNT, 10**18)],
            output_token=USDC_BASE,
        )
        # The stray WETH leg is change/dust, not delivery.
        assert out == ("320000000", "simulated")

    def test_bridged_but_unswapped_delivers_nothing(self):
        """THE inversion this filter exists to stop.

        A plan that bridges WETH and skips the destination swap used to be
        credited its raw WETH amount — ~1e12x the honest USDC answer purely on
        decimals — which won the order outright and logged the honest plan as a
        regression. It delivered none of what was asked for; it scores zero.
        """
        from minotaur_subnet.harness.orchestrator import _ANVIL_DEFAULT_ACCOUNT
        honest, _ = _measure(
            [_transfer(USDC_BASE, _ANVIL_DEFAULT_ACCOUNT, 320_000_000)],
            output_token=USDC_BASE,
        )
        dumper, _ = _measure(
            [_transfer(WETH_BASE, _ANVIL_DEFAULT_ACCOUNT, 10**18)],
            output_token=USDC_BASE,
        )
        assert dumper == "0"
        assert int(honest) > int(dumper)

    def test_token_match_is_case_insensitive(self):
        from minotaur_subnet.harness.orchestrator import _ANVIL_DEFAULT_ACCOUNT
        out = _measure(
            [_transfer(USDC_BASE.lower(), _ANVIL_DEFAULT_ACCOUNT, 5)],
            output_token=USDC_BASE.upper(),
        )
        assert out == ("5", "simulated")

    def test_receiver_filter_still_applies(self):
        # Right token, wrong recipient — the pre-existing guard must survive.
        out = _measure(
            [_transfer(USDC_BASE, "0x" + "99" * 20, 320_000_000)],
            output_token=USDC_BASE,
        )
        assert out == ("0", "simulated")

    def test_no_requested_token_fails_closed(self):
        """Unmeasurable must mean uncredited, never unfiltered.

        Falling back to the blind sum when the intent declares no output token
        would hand the dumping plan its inflated number back through the gap.
        """
        from minotaur_subnet.harness.orchestrator import _ANVIL_DEFAULT_ACCOUNT
        delivered, source = _measure(
            [_transfer(WETH_BASE, _ANVIL_DEFAULT_ACCOUNT, 10**18)],
        )
        assert delivered is None
        assert source == "simulated"

    def test_solver_metadata_cannot_choose_the_credited_token(self):
        """The filter reads intent params, never the solver's own declaration.

        Sourcing it from plan metadata would restore the vector in one move:
        declare the cheap token, dump the cheap token, get credited for it.
        """
        from minotaur_subnet.harness.orchestrator import _ANVIL_DEFAULT_ACCOUNT
        meta = _cross_chain_plan_meta()
        meta["cross_chain_plan"]["legs"][-1]["token_out"] = WETH_BASE
        meta["token_out"] = WETH_BASE
        import asyncio
        from minotaur_subnet.harness.orchestrator import (
            _measure_destination_delivery,
        )
        out = asyncio.run(_measure_destination_delivery(
            _dest_sim([_transfer(WETH_BASE, _ANVIL_DEFAULT_ACCOUNT, 10**18)]),
            _plan(meta), _State(output_token=USDC_BASE), None, None,
        ))
        assert out == ("0", "simulated")


class TestCrossChainGatesAgree:
    """Every gate asks one predicate, so none can disagree with another.

    Each historical disagreement failed the same way — one gate saw cross-chain
    where another saw single-chain — and every one of them read to a miner as
    "cross-chain earns nothing".
    """

    SHAPES = [
        ("cross_chain", {"cross_chain": True}),
        ("cross_chain_plan", {"cross_chain_plan": {"legs": [{}, {}]}}),
        ("multi_leg_plan", {"multi_leg_plan": {"forward_legs": [{}]}}),
        ("legs", {"legs": [{"leg_id": 0}]}),
    ]

    def test_every_declared_shape_is_recognised(self):
        from minotaur_subnet.simulator.cross_chain_bench import (
            declares_cross_chain, is_cross_chain_plan,
        )
        for name, meta in self.SHAPES:
            assert declares_cross_chain(meta), name
            assert is_cross_chain_plan(_plan(meta)), name

    def test_single_chain_is_untouched_by_every_gate(self):
        from minotaur_subnet.harness.orchestrator import _mock_bridge_for_benchmark
        from minotaur_subnet.simulator.cross_chain_bench import declares_cross_chain

        single = _plan({"route": "uniswap_v3"}, interactions=[_ix(1)])
        assert not declares_cross_chain(single.metadata)
        # Bit-identical: the same object, not merely an equal one.
        assert _mock_bridge_for_benchmark(single, None) is single

    def test_legacy_legs_shape_reaches_the_bridge_mocker(self):
        """The gap that made a legacy plan measurable but unscoreable.

        ``legs`` was measured by the destination observer yet skipped by the
        bridge mocker, so its real bridge calldata reverted in the scored sim —
        measured as delivering, scored as failing.
        """
        from minotaur_subnet.harness.orchestrator import _mock_bridge_for_benchmark

        plan = _plan(
            {"legs": [{"leg_id": 0, "chain_id": 1, "type": "bridge"}]},
            interactions=[_ix(1, "7b939232")],  # a real bridge selector
        )
        mocked = _mock_bridge_for_benchmark(plan, None)
        assert mocked is not plan, "bridge calldata must be rewritten"
        assert mocked.interactions != plan.interactions

    def test_widening_is_inert_without_bridge_calldata(self):
        """Adding ``legs`` to the mock gate cannot move an existing score.

        The rewrite is selector-matched, so a declared plan carrying no bridge
        calldata returns the identical object however it declared itself.
        """
        from minotaur_subnet.harness.orchestrator import _mock_bridge_for_benchmark

        for name, meta in self.SHAPES:
            plan = _plan(dict(meta), interactions=[_ix(1, "a9059cbb")])
            assert _mock_bridge_for_benchmark(plan, None) is plan, name
