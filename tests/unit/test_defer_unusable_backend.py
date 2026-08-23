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
