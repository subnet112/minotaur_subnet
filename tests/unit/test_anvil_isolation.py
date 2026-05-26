"""Tests for the snapshot/revert isolation behavior (PR-7, audit C4).

Verifies that AnvilSimulator's per-simulation snapshot+revert wrapper
and the baseline-snapshot recovery path actually contain state
mutations from leaking across simulation boundaries.

The unit tests below use a mocked web3 provider so they run anywhere
without a live anvil. A live-integration check that hits a real anvil
is gated on the ``ANVIL_RPC_URL`` env var.
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

from minotaur_subnet.simulator.anvil_simulator import (
    AnvilSimulator,
    SimulatorStateError,
)


def _make_sim(upstream: str | None = None) -> AnvilSimulator:
    """Build a simulator with a mocked web3 provider, baseline pre-set."""
    with patch("minotaur_subnet.simulator.anvil_simulator.Web3") as MockWeb3:
        MockWeb3.HTTPProvider.return_value = MagicMock()
        MockWeb3.to_checksum_address = lambda x: x
        instance = MagicMock()
        instance.is_connected.return_value = True
        instance.eth.block_number = 100
        # Storage probe returns a fixed baseline value.
        instance.eth.get_storage_at = MagicMock(return_value=b"\x00" * 32)
        MockWeb3.return_value = instance
        sim = AnvilSimulator(
            rpc_url="http://anvil:8545",
            default_executor="0x" + "00" * 20,
            upstream_rpc_url=upstream,
        )
    # Replace provider with a fresh spy so we can drive responses.
    sim.w3 = MagicMock()
    sim.w3.eth.get_storage_at = MagicMock(return_value=b"\x00" * 32)
    sim._baseline_snapshot_id = "0x1"
    sim._baseline_probe_value = b"\x00" * 32
    return sim


def test_reset_fork_no_upstream_reverts_to_baseline_and_resnapshots():
    """No-upstream path: revert succeeds → new baseline snapshot taken."""
    sim = _make_sim(upstream=None)
    # evm_revert returns True; the follow-up evm_snapshot returns a new ID.
    sim.w3.provider.make_request = MagicMock(side_effect=[
        {"result": True},   # evm_revert
        {"result": "0x2"},  # evm_snapshot (new baseline)
    ])

    sim._reset_fork(block_number=None)

    calls = sim.w3.provider.make_request.call_args_list
    assert calls[0][0][0] == "evm_revert"
    assert calls[0][0][1] == ["0x1"]
    assert calls[1][0][0] == "evm_snapshot"
    assert sim._baseline_snapshot_id == "0x2"


def test_reset_fork_no_upstream_raises_when_revert_fails():
    """No-upstream path + revert fails → SimulatorStateError, baseline cleared."""
    sim = _make_sim(upstream=None)
    sim.w3.provider.make_request = MagicMock(return_value={"result": False})

    with pytest.raises(SimulatorStateError, match="evm_revert to baseline"):
        sim._reset_fork(block_number=None)

    assert sim._baseline_snapshot_id is None


def test_reset_fork_no_upstream_lazy_init_when_no_baseline():
    """No-upstream + no baseline yet → take one and return (no revert)."""
    sim = _make_sim(upstream=None)
    sim._baseline_snapshot_id = None
    sim.w3.provider.make_request = MagicMock(return_value={"result": "0x5"})

    sim._reset_fork(block_number=None)

    # Only the snapshot was taken; no revert was attempted.
    assert sim.w3.provider.make_request.call_args_list[0][0][0] == "evm_snapshot"
    assert sim._baseline_snapshot_id == "0x5"


def test_evm_revert_returns_false_on_stale_snapshot():
    """evm_revert returning false (stale ID) is surfaced as False, not raise."""
    sim = _make_sim(upstream=None)
    sim.w3.provider.make_request = MagicMock(return_value={"result": False})
    assert sim._evm_revert("0xdead") is False


def test_assert_baseline_alive_raises_on_probe_mismatch_no_upstream():
    """Probe disagrees + no upstream to recover → SimulatorStateError."""
    sim = _make_sim(upstream=None)
    # Force probe to fire on next call.
    from minotaur_subnet.simulator import anvil_simulator as mod
    sim._sim_count = mod.BASELINE_PROBE_EVERY - 1
    # Probe slot read returns a DIFFERENT value than the recorded baseline.
    sim.w3.eth.get_storage_at = MagicMock(return_value=b"\xff" * 32)

    with pytest.raises(SimulatorStateError, match="Baseline storage probe mismatch"):
        sim._assert_baseline_alive()


def test_assert_baseline_alive_skips_when_counter_not_at_interval():
    """Probe only fires every N sims — early calls are no-ops."""
    sim = _make_sim(upstream=None)
    sim._sim_count = 0
    sim.w3.eth.get_storage_at = MagicMock(return_value=b"\xff" * 32)  # mismatch

    # Should NOT raise — probe doesn't fire yet.
    sim._assert_baseline_alive()
    assert sim._sim_count == 1


def test_assert_baseline_alive_force_refork_when_upstream_available():
    """Probe disagrees + upstream configured → triggers _reset_fork (no raise)."""
    sim = _make_sim(upstream="https://upstream.example")
    from minotaur_subnet.simulator import anvil_simulator as mod
    sim._sim_count = mod.BASELINE_PROBE_EVERY - 1
    sim.w3.eth.get_storage_at = MagicMock(return_value=b"\xff" * 32)
    # _reset_fork on upstream path calls anvil_reset + evm_snapshot.
    sim.w3.provider.make_request = MagicMock(return_value={"result": "0xnew"})
    with patch.object(sim, "_fetch_upstream_head", return_value=12345):
        # Should NOT raise — recovery happens via re-fork.
        sim._assert_baseline_alive()
    # anvil_reset was called as part of recovery.
    methods = [c[0][0] for c in sim.w3.provider.make_request.call_args_list]
    assert "anvil_reset" in methods


# ── Live integration ────────────────────────────────────────────────
# Run against a real anvil with ANVIL_RPC_URL=http://localhost:8545
# pytest tests/unit/test_anvil_isolation.py -k live

@pytest.mark.skipif(
    not os.environ.get("ANVIL_RPC_URL"),
    reason="set ANVIL_RPC_URL to run live anvil isolation checks",
)
def test_live_simulate_does_not_leak_state_across_calls():
    """Two back-to-back simulates: state set in #1 is gone by #2.

    Uses anvil_setStorageAt INSIDE a (manually invoked) simulation to
    mutate a slot the per-sim revert MUST undo. Confirms the value
    is back to baseline after the simulate() returns.
    """
    from minotaur_subnet.shared.types import ExecutionPlan

    sim = AnvilSimulator(rpc_url=os.environ["ANVIL_RPC_URL"])

    # Read baseline of a high-numbered slot of address(0) — picked to
    # avoid collisions with anything real on the fork.
    addr = "0x0000000000000000000000000000000000000000"
    slot = 0xDEADBEEF
    before = sim.w3.eth.get_storage_at(addr, slot)

    # Manually poison the slot OUTSIDE simulate(), then run an empty
    # simulate() and verify the next reset_fork brought it back.
    sim.w3.provider.make_request(
        "anvil_setStorageAt",
        [addr, hex(slot), "0x" + "ff" * 32],
    )
    poisoned = sim.w3.eth.get_storage_at(addr, slot)
    assert poisoned != before, "anvil_setStorageAt did not stick (sanity)"

    # Empty plan — exercises the snapshot/reset/revert path.
    plan = ExecutionPlan(interactions=[], deadline=0, nonce=0, metadata={})
    import asyncio
    asyncio.run(sim.simulate(plan))

    after = sim.w3.eth.get_storage_at(addr, slot)
    assert after == before, (
        f"State leaked across simulate() — slot was {poisoned.hex()} "
        f"before sim, {after.hex()} after (baseline {before.hex()})"
    )
