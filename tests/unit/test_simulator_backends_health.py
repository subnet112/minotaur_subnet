"""/health says which chains this node can SIMULATE, not just which it can PIN.

Those are different questions with different answers. Fork pins come off an
archive RPC; simulation needs a live backend — and for chain 964 that backend
is a Chopsticks sidecar behind a compose profile that is INERT by default
("followers on :stable get this file but never start it").

The gap is load-bearing. 964 is a WIRED chain, so
``_assert_destination_backends_usable`` raises ``RealSimulationUnavailable`` —
deferring the WHOLE ROUND, not just the row — on a node with no backend for it,
while a node running the profile scores normally. Nothing in /health
distinguished the two, so the only way to find the split was a round already
stalling on it.
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import pytest  # noqa: E402

from minotaur_subnet.api.routes import apps  # noqa: E402


class _Sim:
    def __init__(self, sims):
        self.simulators = sims


class _Anvil:
    def is_connected(self):
        return True


class SubtensorSimulator:  # the name IS the check — mirrors the orchestrator
    def is_connected(self):
        return True


@pytest.fixture(autouse=True)
def _restore_simulator():
    before = apps._simulator
    yield
    apps.set_simulator(before)


def test_dormant_chains_are_not_reported():
    """Arbitrum/Optimism are registry metadata, never expected to simulate —
    listing them as missing backends would cry wolf on every node."""
    apps.set_simulator(_Sim({}))
    out = apps.simulator_backends_health()
    assert "42161" not in out and "10" not in out
    assert "964" in out and "1" in out


def test_a_missing_backend_is_flagged_as_round_deferring():
    """The exact condition the defer branch tests, surfaced before it fires."""
    apps.set_simulator(_Sim({1: _Anvil(), 8453: _Anvil()}))
    out = apps.simulator_backends_health()
    assert out["964"]["present"] is False
    assert out["964"]["would_defer_destination_leg"] is True
    assert out["1"]["would_defer_destination_leg"] is False


def test_the_wrong_KIND_of_backend_also_defers():
    """A substrate chain routed to anvil cannot execute native precompiles, so
    it is unusable even though a backend is present — present != usable."""
    apps.set_simulator(_Sim({964: _Anvil()}))
    out = apps.simulator_backends_health()
    assert out["964"]["present"] is True
    assert out["964"]["would_defer_destination_leg"] is True


def test_a_correctly_wired_substrate_backend_is_clean():
    apps.set_simulator(_Sim({964: SubtensorSimulator()}))
    out = apps.simulator_backends_health()
    assert out["964"]["backend"] == "SubtensorSimulator"
    assert out["964"]["would_defer_destination_leg"] is False
    assert out["964"]["connected"] is True


def test_expected_backend_comes_from_the_registry():
    """A node reports not only what it HAS but what it SHOULD have, so the
    split is readable from one node's payload without a second to compare."""
    apps.set_simulator(_Sim({}))
    out = apps.simulator_backends_health()
    assert out["964"]["expected_backend"] == "substrate_chopsticks"
    assert out["1"]["expected_backend"] == "evm"


def test_no_simulator_at_all_does_not_raise():
    """/health must never 500: the container healthcheck fails only on an
    exception, and a restart loop fixes nothing here."""
    apps.set_simulator(None)
    out = apps.simulator_backends_health()
    assert out["964"]["present"] is False


def test_a_backend_whose_probe_raises_reports_disconnected():
    class _Flaky:
        def is_connected(self):
            raise RuntimeError("sidecar down")

    apps.set_simulator(_Sim({964: _Flaky()}))
    out = apps.simulator_backends_health()
    assert out["964"]["connected"] is False
