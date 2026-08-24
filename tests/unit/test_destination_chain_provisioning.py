"""A declared destination chain must be provisioned, or the row must defer.

The benchmark's chain set was built from each intent's own ``chain_id`` — the
SOURCE chain. A cross-chain order's delivery chain lives in its params, and for
the entire seeded wTAO -> wAlpha corpus that is Bittensor EVM (964), a chain no
order is native to. So 964 never entered ``build_rpc_url_map``, never got a
simulator, and ``simulate_cross_chain`` skipped every destination leg via its
``sim is None`` branch — no interactions, no Transfer logs, no token_transfers.

The row then read as ``nothing_delivered``, which is exactly what a bad plan
looks like. Miners debugged correct plans for rounds (gimly/UID 118: seven rows
across two rounds, zero credited, against a destination call that verifies on
live mainnet state at every seeded amount).
"""
from __future__ import annotations

import pytest

from minotaur_subnet.harness.benchmark_worker import benchmark_chain_ids
from minotaur_subnet.harness.orchestrator import (
    RealSimulationUnavailable,
    _assert_destination_backends_usable,
)
from minotaur_subnet.shared.types import ExecutionPlan


class _State:
    def __init__(self, chain_id, dest=None):
        self.chain_id = chain_id
        self._dest = dest

    def raw_params_view(self):
        return {"dest_chain_id": self._dest} if self._dest is not None else {}


def _intents(*states):
    return [(None, s, None) for s in states]


class TestChainSet:
    def test_declared_destination_chain_is_provisioned(self):
        """The regression: 964 is reachable only as a DESTINATION."""
        assert benchmark_chain_ids(_intents(_State(1, 964))) == [1, 964]

    def test_source_only_slate_is_unchanged(self):
        assert benchmark_chain_ids(_intents(_State(1), _State(8453))) == [1, 8453]

    def test_destination_equal_to_source_adds_nothing(self):
        """Not a cross-chain order — the predicate compares, it does not test presence."""
        assert benchmark_chain_ids(_intents(_State(1, 1))) == [1]

    @pytest.mark.parametrize("dest", ["", 0, "0", None, "abc", object()])
    def test_unusable_destination_never_raises(self, dest):
        """This path only ever ADDS chains to provision; it must not break a round."""
        assert benchmark_chain_ids(_intents(_State(1, dest))) == [1]

    def test_empty_slate_keeps_the_legacy_default(self):
        assert benchmark_chain_ids([]) == [1]


class _Backend:
    def __init__(self, connected=True):
        self._c = connected

    def is_connected(self):
        return self._c


class SubtensorSimulator(_Backend):
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


class TestAbsentBackendDefers:
    def test_unprovisioned_destination_chain_defers(self):
        """Was `continue` — the silent skip that produced the whole outage."""
        sim = _Multi({1: _Backend()})          # 964 absent entirely
        with pytest.raises(RealSimulationUnavailable, match="NO simulator"):
            _assert_destination_backends_usable(sim, _plan(964))

    def test_a_provisioned_healthy_destination_passes(self):
        sim = _Multi({1: _Backend(), 964: SubtensorSimulator()})
        _assert_destination_backends_usable(sim, _plan(964))

    def test_a_chain_with_no_destination_leg_is_not_checked(self):
        """A dead sidecar for a chain this plan never touches must not defer it."""
        sim = _Multi({1: _Backend()})
        plan = ExecutionPlan(
            intent_id="i", interactions=[], deadline=0, nonce=0,
            metadata={"legs": [{"leg_id": 0, "chain_id": 1, "type": "source"}]},
        )
        _assert_destination_backends_usable(sim, plan)
