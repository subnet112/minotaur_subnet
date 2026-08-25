"""A destination chain that cannot be simulated must DEFER, not score zero.

On 2026-08-23 both Chopsticks sidecars were dead — docker reported them Up,
they were running=false with no IP — and the whole fleet logged
`nothing_delivered` on every chain-964 destination leg for hours. Miners spent
rounds debugging correct plans against a backend that could never run them.

Recording nothing_delivered blames the solver for our outage. These tests pin
the two ways a chain is unscoreable by construction.
"""
from __future__ import annotations

import pytest

from minotaur_subnet.harness.orchestrator import (
    RealSimulationUnavailable,
    _assert_destination_backends_usable,
)
from minotaur_subnet.shared.types import ExecutionPlan


class _Backend:
    def __init__(self, connected=True):
        self._c = connected
    def is_connected(self):
        return self._c


class SubtensorSimulator(_Backend):  # name is what the check keys on
    pass


class AnvilSimulator(_Backend):
    pass


class _Multi:
    def __init__(self, sims):
        self.simulators = sims


def _plan(dest_chain):
    return ExecutionPlan(
        intent_id="i", interactions=[], deadline=0, nonce=0,
        metadata={"legs": [
            {"leg_id": 0, "chain_id": 1, "type": "source"},
            {"leg_id": 1, "chain_id": dest_chain, "type": "destination"},
        ]},
    )


def test_a_dead_sidecar_defers_instead_of_scoring_zero():
    sim = _Multi({964: SubtensorSimulator(connected=False)})
    with pytest.raises(RealSimulationUnavailable, match="not reachable"):
        _assert_destination_backends_usable(sim, _plan(964))


def test_the_wrong_backend_kind_defers():
    """964 on Anvil: the staking precompile has no bytecode to fork, so
    purchaseWrapped can never mint however correct the plan."""
    sim = _Multi({964: AnvilSimulator(connected=True)})
    with pytest.raises(RealSimulationUnavailable, match="substrate backend"):
        _assert_destination_backends_usable(sim, _plan(964))


def test_a_healthy_substrate_backend_passes():
    sim = _Multi({964: SubtensorSimulator(connected=True)})
    _assert_destination_backends_usable(sim, _plan(964))


def test_an_evm_destination_on_anvil_is_fine():
    """Base does not want the substrate backend; Anvil is correct there."""
    sim = _Multi({8453: AnvilSimulator(connected=True)})
    _assert_destination_backends_usable(sim, _plan(8453))


def test_a_dead_sidecar_for_an_UNUSED_chain_does_not_defer():
    """Only chains carrying a destination leg are checked."""
    sim = _Multi({964: SubtensorSimulator(connected=False),
                  8453: AnvilSimulator(connected=True)})
    _assert_destination_backends_usable(sim, _plan(8453))


def test_no_destination_legs_is_a_no_op():
    sim = _Multi({964: SubtensorSimulator(connected=False)})
    p = ExecutionPlan(intent_id="i", interactions=[], deadline=0, nonce=0,
                      metadata={"legs": [{"leg_id": 0, "chain_id": 1, "type": "source"}]})
    _assert_destination_backends_usable(sim, p)


def test_an_unknown_simulator_shape_is_tolerated():
    """Never let the guard itself break scoring."""
    class Bare: pass
    _assert_destination_backends_usable(Bare(), _plan(964))


# ── The hole this guard shipped with ────────────────────────────────────────
#
# The original loop did `if backend is None: continue` — so the case the guard
# most needed to catch, a destination chain with NO simulator at all, was the
# one it waved through. The leg is then never dispatched, the leg result carries
# no token_transfers, every delivery bucket lands on zero, and the row reads
# `nothing_delivered` exactly as if the plan were empty.

def test_a_wired_chain_with_no_simulator_defers():
    """964 is wired: we claim to simulate it, so its absence is OUR outage."""
    sim = _Multi({1: AnvilSimulator(connected=True)})
    with pytest.raises(RealSimulationUnavailable, match="no simulator was built"):
        _assert_destination_backends_usable(sim, _plan(964))


def test_the_defer_message_names_the_env_to_set():
    sim = _Multi({1: AnvilSimulator(connected=True)})
    with pytest.raises(RealSimulationUnavailable) as ei:
        _assert_destination_backends_usable(sim, _plan(964))
    assert "BITTENSOR_CHOPSTICKS_SIM_RPC_URL" in str(ei.value)


def test_an_UNWIRED_destination_chain_never_defers():
    """A solver-declared chain we never claimed to simulate must NOT stall the
    round.

    The destination chain comes off a solver-authored plan. If any chain we
    cannot simulate deferred, one submission declaring `dest_chain_id: 42161`
    would stall every round it landed in — a denial of service costing the
    attacker one submission. Arbitrum is registered but `wired=False`, so it is
    diagnosed per-row (destination_unsimulated) and costs that row only.
    """
    sim = _Multi({1: AnvilSimulator(connected=True)})
    _assert_destination_backends_usable(sim, _plan(42161))


def test_an_UNREGISTERED_destination_chain_never_defers():
    sim = _Multi({1: AnvilSimulator(connected=True)})
    _assert_destination_backends_usable(sim, _plan(1337000))


# ── A zero has four causes, and they need four different fixes ──────────────

from minotaur_subnet.harness.orchestrator import _delivery_diagnosis  # noqa: E402

_TOKEN = "0xtoken"
_RECIPIENTS = {"0xme"}


def _code(**kw):
    return _delivery_diagnosis(_TOKEN, _RECIPIENTS, 0, 0, **kw)["code"]


def test_an_empty_leg_that_ran_is_still_nothing_delivered():
    assert _code() == "nothing_delivered"


def test_a_reverting_leg_says_so():
    assert _code(legs_reverted=1) == "destination_leg_reverted"


def test_a_leg_that_never_ran_says_so():
    assert _code(legs_unsimulated=1) == "destination_unsimulated"


def test_never_dispatched_outranks_everything():
    """No advice about tokens or recipients is honest when nothing ran."""
    d = _delivery_diagnosis(
        _TOKEN, _RECIPIENTS,
        wrong_token_to_recipient=5, right_token_elsewhere=7,
        legs_reverted=1, legs_unsimulated=1,
    )
    assert d["code"] == "destination_unsimulated"


def test_a_closer_miss_still_outranks_a_revert():
    """One leg reverted but another delivered the right token elsewhere: the
    recipient fix is the actionable one."""
    d = _delivery_diagnosis(
        _TOKEN, _RECIPIENTS,
        wrong_token_to_recipient=0, right_token_elsewhere=7, legs_reverted=1,
    )
    assert d["code"] == "wrong_recipient"


def test_leg_counts_are_stringified_like_every_other_field():
    """The row is fleet-compared, so every value is a stable string."""
    d = _delivery_diagnosis(_TOKEN, _RECIPIENTS, 0, 0, legs_reverted=2)
    assert d["legs_reverted"] == "2"
    assert d["legs_unsimulated"] == "0"


def test_every_code_carries_a_miner_facing_hint():
    """The vocabulary is closed; a code with no hint is a code that explains
    nothing to the miner it is shown to."""
    from minotaur_subnet.api.routes.submissions.report import (
        _DELIVERY_REASON_HINTS,
    )
    for code in (
        "wrong_recipient", "wrong_token", "nothing_delivered",
        "no_output_token", "no_cross_chain_plan",
        "destination_leg_reverted", "destination_unsimulated",
    ):
        assert _DELIVERY_REASON_HINTS.get(code), code
