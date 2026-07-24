"""Tests for the per-sim fork-freshness behavior of AnvilSimulator.

Background: ``anvil_reset`` with empty params is a no-op in Foundry —
the fork stays at its initial block. The simulator must pass an explicit
``forking.blockNumber`` to actually advance the fork. These tests verify
that:

  1. When no upstream RPC is configured, _reset_fork(None) reverts to
     the startup baseline snapshot (PR-7 / audit C4 — was a no-op
     before, but that allowed fork-state poisoning to persist across
     simulations on local-testnet chain 31337).
  2. When upstream is configured, _reset_fork(None) fetches the upstream
     head and calls anvil_reset with that block number.
  3. When an explicit block_number is passed, the upstream is NOT queried
     (historical replay path).
  4. Upstream fetch failure leaves the fork untouched (best-effort, no
     half-reset).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from minotaur_subnet.simulator.anvil_simulator import AnvilSimulator


@pytest.fixture
def sim_no_upstream():
    """Simulator without upstream RPC configured (local-testnet path)."""
    with patch("minotaur_subnet.simulator.anvil_simulator.Web3") as MockWeb3:
        MockWeb3.HTTPProvider.return_value = MagicMock()
        MockWeb3.to_checksum_address = lambda x: x  # bypass checksum
        instance = MagicMock()
        instance.is_connected.return_value = True
        instance.eth.block_number = 100
        MockWeb3.return_value = instance
        sim = AnvilSimulator(
            rpc_url="http://anvil:8545",
            default_executor="0x" + "00" * 20,
        )
        # Replace provider so we can spy on make_request
        sim.w3 = MagicMock()
        sim.w3.provider.make_request = MagicMock()
        return sim


@pytest.fixture
def sim_with_upstream():
    """Simulator with upstream RPC configured (mainnet-fork path)."""
    with patch("minotaur_subnet.simulator.anvil_simulator.Web3") as MockWeb3:
        MockWeb3.HTTPProvider.return_value = MagicMock()
        MockWeb3.to_checksum_address = lambda x: x
        instance = MagicMock()
        instance.is_connected.return_value = True
        instance.eth.block_number = 100
        MockWeb3.return_value = instance
        sim = AnvilSimulator(
            rpc_url="http://anvil-base:8546",
            default_executor="0x" + "00" * 20,
            upstream_rpc_url="https://base-mainnet.example/v2/key",
        )
        sim.w3 = MagicMock()
        sim.w3.provider.make_request = MagicMock()
        return sim


def test_httpprovider_gets_socket_timeout():
    """The sim's HTTPProvider MUST be built with a finite socket timeout.

    Without request_kwargs the provider inherits requests' default timeout=None
    (infinite wait), so a wedged RPC freezes the event loop and starves the
    benchmark timeout + the round's cert-deadline abort (the 159-min stall +
    'stale incumbent bar' aborts). Regression guard: the provider must carry a
    positive timeout == sim_timeout.
    """
    with patch("minotaur_subnet.simulator.anvil_simulator.Web3") as MockWeb3:
        MockWeb3.HTTPProvider.return_value = MagicMock()
        MockWeb3.to_checksum_address = lambda x: x
        inst = MagicMock()
        inst.is_connected.return_value = True
        inst.eth.block_number = 100
        MockWeb3.return_value = inst
        AnvilSimulator(
            rpc_url="http://anvil:8545",
            default_executor="0x" + "00" * 20,
            sim_timeout=17.0,
        )
        _, kwargs = MockWeb3.HTTPProvider.call_args
        rk = kwargs.get("request_kwargs") or {}
        assert rk.get("timeout") == 17.0, (
            f"HTTPProvider built without a finite socket timeout: {kwargs!r}"
        )


def test_reset_fork_none_no_upstream_reverts_to_baseline(sim_no_upstream):
    """Without upstream URL, _reset_fork(None) reverts to baseline snapshot.

    Previously a no-op (the audit-flagged C4 fork-poisoning vector).
    Now: revert to baseline, take a fresh one.
    """
    # Pre-seed the baseline (init couldn't because make_request is now
    # the spy, not the real connection).
    sim_no_upstream._baseline_snapshot_id = "0x1"
    sim_no_upstream.w3.provider.make_request.side_effect = [
        {"result": True},   # evm_revert
        {"result": "0x2"},  # evm_snapshot
    ]

    sim_no_upstream._reset_fork(block_number=None)

    calls = sim_no_upstream.w3.provider.make_request.call_args_list
    assert calls[0][0][0] == "evm_revert"
    assert calls[0][0][1] == ["0x1"]
    assert calls[1][0][0] == "evm_snapshot"
    assert sim_no_upstream._baseline_snapshot_id == "0x2"


def test_reset_fork_none_with_upstream_fetches_head_and_resets(sim_with_upstream):
    """With upstream URL, _reset_fork(None) fetches head + resets to it.

    After the reset, a fresh baseline snapshot is taken so post-reset
    recovery paths still work.
    """
    with patch("minotaur_subnet.simulator.anvil_simulator.requests.post") as mock_post:
        mock_post.return_value.json.return_value = {"result": "0xabcdef"}  # 11259375
        mock_post.return_value.raise_for_status = MagicMock()

        sim_with_upstream._reset_fork(block_number=None)

        mock_post.assert_called_once()
        # Verify it called the upstream URL
        assert mock_post.call_args[0][0] == "https://base-mainnet.example/v2/key"
        # Two RPC calls now: the anvil_reset, then a fresh evm_snapshot.
        methods = [c[0][0] for c in sim_with_upstream.w3.provider.make_request.call_args_list]
        assert "anvil_reset" in methods
        assert "evm_snapshot" in methods


def test_reset_fork_explicit_block_does_not_fetch_upstream(sim_with_upstream):
    """Historical-replay path: when block_number is given, upstream isn't queried."""
    with patch("minotaur_subnet.simulator.anvil_simulator.requests.post") as mock_post:
        sim_with_upstream._reset_fork(block_number=5_000_000)

        mock_post.assert_not_called()
        # anvil_reset called with the explicit block; baseline re-snapshot follows.
        calls = sim_with_upstream.w3.provider.make_request.call_args_list
        assert calls[0][0] == (
            "anvil_reset",
            [{"forking": {"blockNumber": 5_000_000}}],
        )
        assert calls[1][0][0] == "evm_snapshot"


def test_reset_fork_upstream_fetch_failure_leaves_fork_alone(sim_with_upstream):
    """If upstream fetch fails, anvil_reset is NOT called (no half-reset)."""
    with patch("minotaur_subnet.simulator.anvil_simulator.requests.post") as mock_post:
        mock_post.side_effect = Exception("Alchemy down")

        # Should not raise — best-effort path
        sim_with_upstream._reset_fork(block_number=None)

        sim_with_upstream.w3.provider.make_request.assert_not_called()


def test_upstream_url_blank_string_treated_as_none():
    """Whitespace-only or empty upstream URL is normalized to None."""
    with patch("minotaur_subnet.simulator.anvil_simulator.Web3") as MockWeb3:
        MockWeb3.HTTPProvider.return_value = MagicMock()
        MockWeb3.to_checksum_address = lambda x: x
        instance = MagicMock()
        instance.is_connected.return_value = True
        instance.eth.block_number = 100
        MockWeb3.return_value = instance

        sim = AnvilSimulator(
            rpc_url="http://anvil:8545",
            default_executor="0x" + "00" * 20,
            upstream_rpc_url="   ",  # whitespace-only
        )
        assert sim.upstream_rpc_url is None

        sim2 = AnvilSimulator(
            rpc_url="http://anvil:8545",
            default_executor="0x" + "00" * 20,
            upstream_rpc_url="",
        )
        assert sim2.upstream_rpc_url is None
