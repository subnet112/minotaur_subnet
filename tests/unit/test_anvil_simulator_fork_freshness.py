"""Tests for the per-sim fork-freshness behavior of AnvilSimulator.

Background: ``anvil_reset`` with empty params is a no-op in Foundry —
the fork stays at its initial block. The simulator must pass an explicit
``forking.blockNumber`` to actually advance the fork. These tests verify
that:

  1. When no upstream RPC is configured, _reset_fork(None) skips silently
     (local-testnet path, fork stays static — acceptable).
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


def test_reset_fork_none_no_upstream_is_noop(sim_no_upstream):
    """Without upstream URL, _reset_fork(None) silently skips."""
    sim_no_upstream._reset_fork(block_number=None)
    sim_no_upstream.w3.provider.make_request.assert_not_called()


def test_reset_fork_none_with_upstream_fetches_head_and_resets(sim_with_upstream):
    """With upstream URL, _reset_fork(None) fetches head + resets to it."""
    with patch("minotaur_subnet.simulator.anvil_simulator.requests.post") as mock_post:
        mock_post.return_value.json.return_value = {"result": "0xabcdef"}  # 11259375
        mock_post.return_value.raise_for_status = MagicMock()

        sim_with_upstream._reset_fork(block_number=None)

        mock_post.assert_called_once()
        # Verify it called the upstream URL
        assert mock_post.call_args[0][0] == "https://base-mainnet.example/v2/key"
        # Verify it called anvil_reset with the fetched block
        sim_with_upstream.w3.provider.make_request.assert_called_once_with(
            "anvil_reset",
            [{"forking": {"blockNumber": 11259375}}],
        )


def test_reset_fork_explicit_block_does_not_fetch_upstream(sim_with_upstream):
    """Historical-replay path: when block_number is given, upstream isn't queried."""
    with patch("minotaur_subnet.simulator.anvil_simulator.requests.post") as mock_post:
        sim_with_upstream._reset_fork(block_number=5_000_000)

        mock_post.assert_not_called()
        sim_with_upstream.w3.provider.make_request.assert_called_once_with(
            "anvil_reset",
            [{"forking": {"blockNumber": 5_000_000}}],
        )


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
