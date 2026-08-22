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

    def __init__(self, control=None, **params):
        self._params = params
        self._control = control or {}
        self.contract_address = None

    def raw_params_view(self) -> dict:
        return dict(self._params)

    def control_view(self) -> dict:
        return dict(self._control)


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
        assert out == (None, None, None)

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
        assert out == (None, None, None)

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
        assert out == (None, None, None)

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
        assert out[:2] == (str(delivered), "simulated")


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
        assert out[:2] == ("320000000", "simulated")

    def test_bridged_but_unswapped_delivers_nothing(self):
        """THE inversion this filter exists to stop.

        A plan that bridges WETH and skips the destination swap used to be
        credited its raw WETH amount — ~1e12x the honest USDC answer purely on
        decimals — which won the order outright and logged the honest plan as a
        regression. It delivered none of what was asked for; it scores zero.
        """
        from minotaur_subnet.harness.orchestrator import _ANVIL_DEFAULT_ACCOUNT
        honest, _, _ = _measure(
            [_transfer(USDC_BASE, _ANVIL_DEFAULT_ACCOUNT, 320_000_000)],
            output_token=USDC_BASE,
        )
        dumper, _, _ = _measure(
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
        assert out[:2] == ("5", "simulated")

    def test_receiver_filter_still_applies(self):
        # Right token, wrong recipient — the pre-existing guard must survive.
        out = _measure(
            [_transfer(USDC_BASE, "0x" + "99" * 20, 320_000_000)],
            output_token=USDC_BASE,
        )
        assert out[:2] == ("0", "simulated")

    def test_no_requested_token_fails_closed(self):
        """Unmeasurable must mean uncredited, never unfiltered.

        Falling back to the blind sum when the intent declares no output token
        would hand the dumping plan its inflated number back through the gap.
        """
        from minotaur_subnet.harness.orchestrator import _ANVIL_DEFAULT_ACCOUNT
        delivered, source, _diag = _measure(
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
        assert out[:2] == ("0", "simulated")


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


APP_BASE = "0xE0D97941103C30799fa0AA9d54a34246846C73bF"
APP_ETH = "0xcD42Cf6FD6E0C539CaE038Fe6a73C67f8c1c7A52"


class TestDeliveryRecipients:
    """WHO counts as delivery on the destination chain.

    Crediting only ``params['receiver']`` is why the reference solver measured
    zero on every case: bench state is built with ``owner=""`` and quote cases
    carry no ``receiver``, so the solver's ``receiver_default =
    state.contract_address or state.owner`` addressed the APP while the
    platform watched the anvil default account.
    """

    def _plan(self, dst=8453):
        return _plan({"cross_chain": True, "dst_chain_id": dst})

    def test_bare_state_keeps_the_historical_default(self):
        from minotaur_subnet.harness.orchestrator import (
            _ANVIL_DEFAULT_ACCOUNT, _delivery_recipients,
        )
        got = _delivery_recipients(_State(), self._plan())
        assert got == {_ANVIL_DEFAULT_ACCOUNT.lower()}

    def test_destination_app_is_credited(self):
        from minotaur_subnet.harness.orchestrator import _delivery_recipients
        got = _delivery_recipients(
            _State(control={"_app_addresses": {8453: APP_BASE, 1: APP_ETH}}),
            self._plan(8453),
        )
        assert APP_BASE.lower() in got

    def test_source_chain_app_is_NOT_credited(self):
        """The far side's address, never the near side's.

        A transfer to the source-chain address on the destination fork reaches
        an account with no code there — stranded funds. Crediting it would be a
        mis-credit dressed as a fix.
        """
        from minotaur_subnet.harness.orchestrator import _delivery_recipients
        got = _delivery_recipients(
            _State(control={"_app_addresses": {8453: APP_BASE, 1: APP_ETH}}),
            self._plan(8453),
        )
        assert APP_ETH.lower() not in got

    def test_string_keyed_map_resolves(self):
        # The map survives a JSON round-trip in some callers, which stringifies
        # the int chain ids.
        from minotaur_subnet.harness.orchestrator import _delivery_recipients
        got = _delivery_recipients(
            _State(control={"_app_addresses": {"8453": APP_BASE}}), self._plan(8453),
        )
        assert APP_BASE.lower() in got

    def test_explicit_receiver_replaces_the_default_but_not_the_app(self):
        from minotaur_subnet.harness.orchestrator import (
            _ANVIL_DEFAULT_ACCOUNT, _delivery_recipients,
        )
        user = "0x" + "99" * 20
        got = _delivery_recipients(
            _State(receiver=user, control={"_app_addresses": {8453: APP_BASE}}),
            self._plan(8453),
        )
        assert got == {user, APP_BASE.lower()}
        assert _ANVIL_DEFAULT_ACCOUNT.lower() not in got

    def test_unresolvable_destination_adds_nothing(self):
        from minotaur_subnet.harness.orchestrator import (
            _ANVIL_DEFAULT_ACCOUNT, _delivery_recipients,
        )
        for plan_meta in ({}, {"dst_chain_id": None}, {"dst_chain_id": "junk"}):
            got = _delivery_recipients(
                _State(control={"_app_addresses": {8453: APP_BASE}}),
                _plan(plan_meta),
            )
            assert got == {_ANVIL_DEFAULT_ACCOUNT.lower()}, plan_meta

    def test_delivery_into_the_destination_app_is_measured(self):
        """End-to-end: the case that read 0 before this fix."""
        from minotaur_subnet.harness.orchestrator import _measure_destination_delivery
        import asyncio
        out = asyncio.run(_measure_destination_delivery(
            _dest_sim([_transfer(USDC_BASE, APP_BASE, 320_000_000)]),
            _plan({**_cross_chain_plan_meta(), "dst_chain_id": 8453}),
            _State(output_token=USDC_BASE,
                   control={"_app_addresses": {8453: APP_BASE}}),
            None, None,
        ))
        assert out[:2] == ("320000000", "simulated")

    def test_widening_never_lowers_a_measurement(self):
        """Superset property: every previously-credited transfer still counts."""
        from minotaur_subnet.harness.orchestrator import (
            _ANVIL_DEFAULT_ACCOUNT, _measure_destination_delivery,
        )
        import asyncio
        out = asyncio.run(_measure_destination_delivery(
            _dest_sim([_transfer(USDC_BASE, _ANVIL_DEFAULT_ACCOUNT, 5),
                       _transfer(USDC_BASE, APP_BASE, 7)]),
            _plan({**_cross_chain_plan_meta(), "dst_chain_id": 8453}),
            _State(output_token=USDC_BASE,
                   control={"_app_addresses": {8453: APP_BASE}}),
            None, None,
        ))
        assert out[:2] == ("12", "simulated")


class TestDeliveryDiagnosis:
    """A zero delivery must say WHICH zero it is.

    Three causes need three different fixes; a bare 0 told a miner nothing and
    the distinction only ever reached a validator-side log.
    """

    def test_credited_delivery_has_no_diagnosis(self):
        from minotaur_subnet.harness.orchestrator import _ANVIL_DEFAULT_ACCOUNT
        delivered, _, diag = _measure(
            [_transfer(USDC_BASE, _ANVIL_DEFAULT_ACCOUNT, 5)],
            output_token=USDC_BASE,
        )
        assert delivered == "5"
        assert diag is None, "presence of a diagnosis IS the failure signal"

    def test_wrong_recipient_is_named(self):
        _, _, diag = _measure(
            [_transfer(USDC_BASE, "0x" + "99" * 20, 320_000_000)],
            output_token=USDC_BASE,
        )
        assert diag["code"] == "wrong_recipient"
        assert diag["delivered_to_others"] == "320000000"

    def test_wrong_token_is_named(self):
        from minotaur_subnet.harness.orchestrator import _ANVIL_DEFAULT_ACCOUNT
        _, _, diag = _measure(
            [_transfer(WETH_BASE, _ANVIL_DEFAULT_ACCOUNT, 10**18)],
            output_token=USDC_BASE,
        )
        assert diag["code"] == "wrong_token"
        assert diag["other_tokens_delivered"] == str(10**18)

    def test_nothing_delivered_is_named(self):
        _, _, diag = _measure([], output_token=USDC_BASE)
        assert diag["code"] == "nothing_delivered"

    def test_wrong_recipient_outranks_wrong_token(self):
        """The closer miss and the cheaper fix is the more useful thing to say."""
        from minotaur_subnet.harness.orchestrator import _ANVIL_DEFAULT_ACCOUNT
        _, _, diag = _measure(
            [_transfer(USDC_BASE, "0x" + "99" * 20, 1),
             _transfer(WETH_BASE, _ANVIL_DEFAULT_ACCOUNT, 10**18)],
            output_token=USDC_BASE,
        )
        assert diag["code"] == "wrong_recipient"

    def test_missing_output_token_is_named(self):
        from minotaur_subnet.harness.orchestrator import _ANVIL_DEFAULT_ACCOUNT
        delivered, _, diag = _measure(
            [_transfer(WETH_BASE, _ANVIL_DEFAULT_ACCOUNT, 10**18)],
        )
        assert delivered is None
        assert diag["code"] == "no_output_token"

    def test_diagnosis_is_deterministic_across_validators(self):
        """It rides a persisted row two validators compare byte-for-byte.

        ``credited_recipients`` comes from a set, whose iteration order is not
        stable — unsorted, two builds could emit different bytes for the same
        observation and a wording difference would read as a data difference.
        """
        import json
        from minotaur_subnet.harness.orchestrator import _delivery_diagnosis
        a = _delivery_diagnosis("0xtok", {"0xb", "0xa", "0xc"}, 0, 7)
        b = _delivery_diagnosis("0xtok", {"0xc", "0xa", "0xb"}, 0, 7)
        assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)
        assert a["credited_recipients"] == ["0xa", "0xb", "0xc"]

    def test_code_vocabulary_is_closed(self):
        from minotaur_subnet.api.routes.submissions.report import (
            _DELIVERY_REASON_HINTS,
        )
        from minotaur_subnet.harness.orchestrator import _delivery_diagnosis
        produced = {
            _delivery_diagnosis("t", set(), 0, 1)["code"],
            _delivery_diagnosis("t", set(), 1, 0)["code"],
            _delivery_diagnosis("t", set(), 0, 0)["code"],
            "no_output_token",
        }
        # Every code the platform can emit has miner-facing guidance.
        assert produced <= set(_DELIVERY_REASON_HINTS), produced


class TestCrossChainReportBlock:
    """The miner-facing aggregate on /v1/submissions/{id}/status."""

    def _block(self, rows):
        from minotaur_subnet.api.routes.submissions.report import (
            _cross_chain_delivery_block,
        )
        return _cross_chain_delivery_block({"per_intent": rows})

    def test_single_chain_submissions_are_untouched(self):
        # Every submission today. The report must stay byte-identical until a
        # solver actually emits a cross-chain plan.
        assert self._block([{"raw_output": "5"}, {"raw_output": "0"}]) is None
        assert self._block([]) is None

    def test_counts_and_hint(self):
        b = self._block([
            {"destination_delivered": "0",
             "destination_delivery_reason": "wrong_recipient"},
            {"destination_delivered": "0",
             "destination_delivery_reason": "wrong_recipient"},
            {"destination_delivered": "0",
             "destination_delivery_reason": "wrong_token"},
            {"destination_delivered": "900"},
            {"raw_output": "5"},
        ])
        assert b["orders"] == 4
        assert b["credited"] == 1
        assert b["reasons"] == {"wrong_recipient": 2, "wrong_token": 1}
        assert "receiver" in b["hint"]

    def test_hint_follows_the_most_common_cause(self):
        b = self._block([
            {"destination_delivered": "0",
             "destination_delivery_reason": "wrong_token"},
            {"destination_delivered": "0",
             "destination_delivery_reason": "wrong_token"},
            {"destination_delivered": "0",
             "destination_delivery_reason": "wrong_recipient"},
        ])
        assert "destination swap" in b["hint"]

    def test_fully_credited_reports_no_hint(self):
        b = self._block([{"destination_delivered": "900"}])
        assert b == {"orders": 1, "credited": 1, "reasons": {}}


class TestBridgeCapabilityDescriptor:
    """The scored path had NO answer to 'what can a bridge carry?'.

    A solver could only get cross-chain right by memorising the four canonical
    addresses, which selects for hardcoded tables over general competence.
    """

    def _d(self):
        from minotaur_subnet.simulator.cross_chain_bench import (
            bridge_capability_descriptor,
        )
        return bridge_capability_descriptor()

    def test_survives_the_solver_wire(self):
        """The whole reason this is a dict and not a BridgeRegistry.

        Solvers are reached over a JSON line protocol. A registry object
        serialises to "<BridgeRegistry object at 0x…>" — a TRUTHY string that
        passes a solver's `is not None` guard and then raises on use, which is
        strictly worse than sending nothing.
        """
        import json
        from minotaur_subnet.harness.protocol import make_initialize_request

        d = self._d()
        assert json.loads(json.dumps(d)) == d, "descriptor must round-trip as JSON"
        wire = make_initialize_request({"bridge_capability": d}).to_json()
        assert "object at 0x" not in wire
        assert json.loads(wire)["config"]["bridge_capability"] == d

    def test_is_deterministic(self):
        """It feeds a scored path, so two validators must derive it identically.

        The live registry prices routes over HTTP; this must not.
        """
        import json
        a, b = self._d(), self._d()
        assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)

    def test_describes_what_is_actually_creditable(self):
        """Not what a rail *could* carry — what the benchmark will CREDIT.

        An unmapped token passes through map_bridged_token unchanged, so no
        destination balance is seeded and delivery measures zero. The descriptor
        must not promise those.
        """
        from minotaur_subnet.simulator.cross_chain_bench import (
            _CANONICAL_TOKEN_BY_CHAIN, map_bridged_token,
        )
        d = self._d()
        assert d["routes"], "must describe at least one route"
        for route in d["routes"]:
            src, dst = route["src_chain_id"], route["dst_chain_id"]
            assert src != dst
            for t in route["tokens"]:
                # every advertised pair is one the seeding path can actually map
                assert map_bridged_token(t["token_in"], src, dst).lower() == \
                    t["token_out"].lower(), (t, src, dst)
        advertised = {t["symbol"] for r in d["routes"] for t in r["tokens"]}
        assert advertised == set(_CANONICAL_TOKEN_BY_CHAIN)

    def test_fee_is_the_benchmark_constant_not_a_live_quote(self):
        """A solver planning against this gets the number the scorer will use."""
        from minotaur_subnet.simulator.cross_chain_bench import (
            BENCHMARK_BRIDGE_FEE_BPS,
        )
        d = self._d()
        assert d["fee_bps"] == BENCHMARK_BRIDGE_FEE_BPS
        assert d["fee_model"] == "benchmark_constant"

    def test_reaches_the_scored_benchmark_init_config(self):
        """The defect: benchmark_worker/orchestrator never told the solver anything.

        Guards the wiring, not the value — the descriptor being correct is
        useless if the scored path still omits it.
        """
        import inspect
        from minotaur_subnet.harness import orchestrator
        src = inspect.getsource(orchestrator)
        assert "bridge_capability_descriptor()" in src
        assert '"bridge_capability"' in src


class TestSolverInitConfigSurvivesTheWire:
    """Every value a solver is handed must still be its declared type on arrival.

    This bug class is invisible until a solver TRUSTS the field. A
    ``BridgeRegistry`` put in a solver's init config arrived as its repr —
    ``"<BridgeRegistry object at 0x…>"`` — which is TRUTHY, so a solver's
    ``if registry is not None`` guard passed and the next attribute access
    raised. Worse than sending nothing: ``None`` fails that guard cleanly.
    """

    def _roundtrip(self, config):
        import json
        from minotaur_subnet.harness.protocol import HarnessRequest
        line = HarnessRequest(command="initialize",
                              params={"config": config}).to_json()
        return json.loads(line)["config"]

    def test_a_registry_cannot_survive_the_hop(self):
        """Pin the hazard itself, so nobody 'fixes' this by passing the object."""
        from minotaur_subnet.bridge.registry import BridgeRegistry
        got = self._roundtrip({"bridge_registry": BridgeRegistry()})["bridge_registry"]
        assert isinstance(got, str) and got, "repr'd to a truthy string"
        assert not hasattr(got, "find_bridge")

    def test_the_descriptor_does_survive(self):
        from minotaur_subnet.simulator.cross_chain_bench import (
            bridge_capability_descriptor,
        )
        d = bridge_capability_descriptor()
        assert self._roundtrip({"bridge_capability": d})["bridge_capability"] == d

    def test_every_value_keeps_its_type(self):
        """The general guard: no value may change type crossing the wire."""
        from minotaur_subnet.harness.runtime_solver import (
            bridge_capability_descriptor,
        )
        cfg = {
            "chain_ids": [1, 8453],
            "rpc_urls": {"1": "http://x", "8453": "http://y"},
            "timeout_per_plan_ms": 5000,
            "bridge_capability": bridge_capability_descriptor(),
        }
        out = self._roundtrip(cfg)
        assert out == cfg
        for k, v in cfg.items():
            assert type(out[k]) is type(v), f"{k} changed type on the wire"

    def test_live_and_scored_paths_send_the_same_shape(self):
        """One contract everywhere — not a registry field on one path and a
        descriptor on another."""
        import inspect
        from minotaur_subnet.harness import orchestrator, runtime_solver
        live = inspect.getsource(runtime_solver)
        scored = inspect.getsource(orchestrator)
        assert '"bridge_capability"' in live and '"bridge_capability"' in scored
        # and the un-sendable field is gone from the live path entirely
        assert '"bridge_registry"' not in live
class TestFailClosedRowsStillGetDiagnosed:
    """A reverting plan is the MOST common cross-chain outcome, and the one a
    miner most needs explained.

    The measurement used to sit inside the ``if not fail_closed_miss:`` guard,
    so a plan whose scoreIntent reverts recorded no delivered amount and no
    reason. Live, 154 of the first 172 cross-chain rows were exactly that: the
    `cross_chain_delivery` block shipped to explain a zero was silent for the
    failure that actually happens.
    """

    def test_measurement_is_outside_the_fail_closed_guard(self):
        """Structural: the call must not be nested under the guard."""
        import inspect, re
        from minotaur_subnet.harness import orchestrator

        src = inspect.getsource(orchestrator._process_scenario)
        guard = src.index("if not fail_closed_miss:")
        call = src.index("await _measure_destination_delivery(")
        assert call < guard, (
            "_measure_destination_delivery moved back inside the fail-closed "
            "guard — reverting plans would stop being diagnosed"
        )

    def test_scorer_only_sees_it_on_the_scored_path(self):
        """The safety property. A fail-closed row never reaches score_fn, and
        the values are attached to `sim` only inside the guard — so diagnosing
        a reverting plan cannot become a way to earn credit for one."""
        import inspect
        from minotaur_subnet.harness import orchestrator

        src = inspect.getsource(orchestrator._process_scenario)
        guard = src.index("if not fail_closed_miss:")
        assert src.index("sim.destination_delivered =") > guard
        assert src.index("await score_fn(") > guard

    def test_mock_sims_are_still_never_measured(self):
        """A fabricated mock sim has no real fork to observe — the `used_mock`
        gate must survive the move out of the fail-closed guard."""
        import inspect
        from minotaur_subnet.harness import orchestrator

        src = inspect.getsource(orchestrator._process_scenario)
        call = src.index("await _measure_destination_delivery(")
        # the gate immediately preceding the call is `if not used_mock:`
        between = src[src.index("_delivery_diag = None"):call]
        assert "if not used_mock:" in between
        assert "if not fail_closed_miss:" not in between


class TestOrderAskedCrossChainButPlanDidNot:
    """The 83% case, and the one that carried no signal at all until now.

    Measured on the leader over 51 rounds and 43 miners: of 578 benched
    cross-chain rows, 482 were a plan that never declared cross-chain (the
    solver simply did not route the order) and 78 more had no plan; only 18
    declared one. The 18 got a diagnosis. The 482 got
    ``scoreIntent reverted: (empty revert)`` — what ANY broken plan produces —
    so the most common way to score zero on cross-chain demand was also the one
    that never mentioned cross-chain.
    """

    @staticmethod
    def _measure_single_chain_plan(**params):
        import asyncio
        from minotaur_subnet.harness.orchestrator import (
            _measure_destination_delivery,
        )
        return asyncio.run(_measure_destination_delivery(
            _dest_sim([]), _plan({"route": "univ3"}),  # NOT a cross-chain plan
            _State(**params), None, None,
        ))

    def test_single_chain_plan_for_a_cross_chain_order_is_named(self):
        delivered, source, diag = self._measure_single_chain_plan(
            output_token=USDC_BASE, dest_chain_id="8453",
        )
        assert diag["code"] == "no_cross_chain_plan"
        assert diag["requested_chain"] == "8453"
        # Nothing is measured — there is no journey to run — so this cannot
        # move a score: both measurement fields stay exactly as they were.
        assert delivered is None and source is None

    def test_nothing_delivered_is_not_reused_for_it(self):
        """``nothing_delivered`` would be a lie: it says the destination legs
        moved nothing, and here there are no destination legs to blame. The two
        need opposite fixes — build a leg vs fix the leg you built."""
        _, _, diag = self._measure_single_chain_plan(
            output_token=USDC_BASE, dest_chain_id="8453",
        )
        assert diag["code"] != "nothing_delivered"

    def test_ordinary_single_chain_order_is_untouched(self):
        """Byte-identical for every non-cross-chain order — which is ~97% of
        the corpus, so a false positive here would be a fleet-wide row change."""
        assert self._measure_single_chain_plan(output_token=USDC_BASE) == (
            None, None, None,
        )

    def test_dest_chain_equal_to_source_is_not_cross_chain(self):
        """An order that names its own chain as the destination is single-chain;
        a presence test on dest_chain_id would diagnose the whole corpus."""
        import asyncio
        from minotaur_subnet.harness.orchestrator import (
            _measure_destination_delivery,
        )

        class _StateOn8453(_State):
            chain_id = 8453

        out = asyncio.run(_measure_destination_delivery(
            _dest_sim([]), _plan({"route": "univ3"}),
            _StateOn8453(output_token=USDC_BASE, dest_chain_id="8453"),
            None, None,
        ))
        assert out == (None, None, None)


class TestIntentRequestsCrossChain:
    """The USER's question, kept separate from the SOLVER's answer."""

    def test_reads_the_declared_destination(self):
        from minotaur_subnet.simulator.cross_chain_bench import (
            intent_requests_cross_chain,
        )
        assert intent_requests_cross_chain({"dest_chain_id": "8453"}, 1) is True
        assert intent_requests_cross_chain({"dest_chain_id": 8453}, 1) is True
        assert intent_requests_cross_chain({"dest_chain_id": "1"}, 1) is False
        assert intent_requests_cross_chain({}, 1) is False
        assert intent_requests_cross_chain(None, 1) is False

    def test_junk_reads_as_single_chain(self):
        """This only decides whether to EXPLAIN a zero, so guessing wrong must
        cost nothing — every unparseable value falls back to silence."""
        from minotaur_subnet.simulator.cross_chain_bench import (
            intent_requests_cross_chain,
        )
        for junk in ("", "0", 0, None, "base", {}, []):
            assert intent_requests_cross_chain({"dest_chain_id": junk}, 1) is False


class TestScoredPathSeesTheSourceLeg:
    """The scored sim must execute a cross-chain plan's SOURCE-side work.

    A solver's ``cross_chain_plan`` keeps its interactions in LEGS and leaves
    the top level empty. The destination measurement was taught this (it calls
    ``normalize_to_legs`` first); the scored path was not — so it handed the
    simulator a plan with ZERO interactions, scoreIntent reverted "(empty
    revert)", and the row scored 0 however good the plan was.

    Measured on the leader 2026-08-18: the one submission that demonstrably
    delivered on the destination chain (499750000000000000 — exactly the
    5bps-haircut amount) still scored 0.0 with ``interactions: []``. Replayed
    on a throwaway fork: ``_mock_bridge_for_benchmark`` returned the plan
    UNCHANGED with 0 interactions, while the same plan normalized carried 1
    executable source-chain interaction (45836 gas).
    """

    @staticmethod
    def _state(chain_id, **params):
        """``_State`` funnels kwargs into params, so chain_id must be set as a
        real ATTRIBUTE — the helper reads ``state.chain_id`` to decide which
        legs execute here, and a params entry would silently exercise the
        no-chain fallback instead."""
        st = _State(**params)
        st.chain_id = chain_id
        return st

    @staticmethod
    def _legs_plan(source_chain=1, dest_chain=8453):
        return _plan({"cross_chain_plan": {
            "legs": [
                {"chain_id": source_chain, "interactions": [asdict(_ix(source_chain))]},
                {"chain_id": dest_chain, "interactions": [asdict(_ix(dest_chain))]},
            ],
            "bridge_requests": [{
                "token": WETH_ETH, "amount": AMOUNT,
                "src_chain_id": source_chain, "dst_chain_id": dest_chain,
            }],
        }})

    def test_source_leg_is_recovered_when_the_top_level_is_empty(self):
        from minotaur_subnet.harness.orchestrator import _mock_bridge_for_benchmark

        plan = self._legs_plan()
        assert plan.interactions == [], "precondition: the solver shape"
        out = _mock_bridge_for_benchmark(plan, self._state(1, input_token=WETH_ETH))
        assert len(out.interactions) >= 1, (
            "the scored sim must run the source leg, not an empty plan"
        )

    def test_destination_legs_are_never_scored_here(self):
        """They belong to another fork; crediting them would score work on the
        wrong chain."""
        from minotaur_subnet.harness.orchestrator import _mock_bridge_for_benchmark

        out = _mock_bridge_for_benchmark(
            self._legs_plan(), self._state(1, input_token=WETH_ETH),
        )
        for ix in out.interactions:
            assert int(getattr(ix, "chain_id", 1) or 1) == 1, (
                f"destination-chain interaction leaked into the scored sim: {ix}"
            )

    def test_a_plan_with_top_level_interactions_is_untouched(self):
        """STRICTLY ADDITIVE: anything that scores today must keep byte-identical
        inputs. Only the empty-top-level case — which scores 0 today — changes."""
        from minotaur_subnet.harness.orchestrator import _mock_bridge_for_benchmark

        plan = _plan(_cross_chain_plan_meta(), interactions=[_ix(1)])
        before = list(plan.interactions)
        out = _mock_bridge_for_benchmark(plan, self._state(1, input_token=WETH_ETH))
        assert list(out.interactions) == before

    def test_single_chain_plan_returns_the_same_object(self):
        """~97% of rows. Not merely equal — the SAME object, as before."""
        from minotaur_subnet.harness.orchestrator import _mock_bridge_for_benchmark

        plan = _plan({"route": "univ3"}, interactions=[_ix(1)])
        assert _mock_bridge_for_benchmark(plan, self._state(1)) is plan

    def test_destination_only_plan_still_returns_the_same_object(self):
        """Nothing recoverable on this chain ⇒ unchanged, hence score 0 exactly
        as today — never a guess."""
        from minotaur_subnet.harness.orchestrator import _mock_bridge_for_benchmark

        plan = self._legs_plan()
        # scored on the DESTINATION chain: no source-side work belongs here
        out = _mock_bridge_for_benchmark(plan, self._state(999, input_token=WETH_ETH))
        assert out is plan

    def test_helper_returns_empty_for_a_non_multileg_plan(self):
        from minotaur_subnet.harness.orchestrator import _source_leg_interactions

        assert _source_leg_interactions(_plan({"route": "univ3"}), self._state(1)) == []


class TestTaoBridgeTokenMapping:
    """wTAO on Ethereum -> native TAO on chain 964.

    The two sides of this row are different KINDS of asset, which is why the row
    has to exist at all: without it `map_bridged_token` passes the token through
    and the destination fork is seeded with wTAO's Ethereum address, a contract
    that does not exist on 964.
    """

    WTAO_ETH = "0x77E06c9eCCf2E797fd462A92B6D7642EF85b0A44"

    def test_wtao_becomes_native_on_bittensor(self):
        from minotaur_subnet.simulator.cross_chain_bench import map_bridged_token
        from minotaur_subnet.blockchain.tokens import NATIVE_SENTINEL
        assert map_bridged_token(self.WTAO_ETH, 1, 964) == NATIVE_SENTINEL

    def test_the_reverse_leg_becomes_the_erc20(self):
        from minotaur_subnet.simulator.cross_chain_bench import map_bridged_token
        from minotaur_subnet.blockchain.tokens import NATIVE_SENTINEL
        assert map_bridged_token(NATIVE_SENTINEL, 964, 1).lower() == self.WTAO_ETH.lower()

    def test_it_is_case_insensitive_on_the_source_address(self):
        from minotaur_subnet.simulator.cross_chain_bench import map_bridged_token
        from minotaur_subnet.blockchain.tokens import NATIVE_SENTINEL
        assert map_bridged_token(self.WTAO_ETH.lower(), 1, 964) == NATIVE_SENTINEL

    def test_existing_routes_are_untouched(self):
        from minotaur_subnet.simulator.cross_chain_bench import map_bridged_token
        weth_eth = "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"
        assert map_bridged_token(weth_eth, 1, 8453) == "0x4200000000000000000000000000000000000006"

    def test_an_unmapped_token_still_passes_through(self):
        """Fail-closed: an unknown asset seeds nothing usable and the leg fails,
        which is the no-credit outcome, never a mis-credit."""
        from minotaur_subnet.simulator.cross_chain_bench import map_bridged_token
        unknown = "0x1234567890123456789012345678901234567890"
        assert map_bridged_token(unknown, 1, 964) == unknown

    def test_the_bridge_adapter_and_the_map_agree(self):
        """The adapter's quote and the seeding map must name the same asset, or
        the leg is seeded with something the plan does not spend."""
        import asyncio
        from minotaur_subnet.bridge.tensorplex import TensorplexAdapter
        from minotaur_subnet.simulator.cross_chain_bench import map_bridged_token
        q = asyncio.run(TensorplexAdapter().quote(self.WTAO_ETH, 7_000_000_000, 1, 964))
        assert q.token_out.lower() == map_bridged_token(self.WTAO_ETH, 1, 964).lower()
