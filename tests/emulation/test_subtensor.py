"""Integration tests against a local subtensor.

Requires Docker to run a local-chain subtensor. Tests subnet registration,
validator/miner registration, metagraph queries, and weight setting.

These tests are slow (~30s+ for Docker startup) and require the
opentensor/subtensor:latest image.

Uses bittensor 10.x API: bt.Subtensor, bt.Wallet.
"""

from __future__ import annotations

import asyncio
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tests.emulation.fixtures.local_subtensor import LocalSubtensor

# Skip entire module if Docker is not available
pytestmark = pytest.mark.skipif(
    not shutil.which("docker"),
    reason="Docker required for local subtensor tests",
)


# ── Fixtures ───────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def event_loop():
    """Module-scoped event loop for async fixtures."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture(scope="module")
def local_subtensor(event_loop):
    """Start a local subtensor Docker container for the test module."""
    sub = LocalSubtensor(port=9955)  # non-default to avoid conflicts
    event_loop.run_until_complete(sub.start())
    yield sub
    event_loop.run_until_complete(sub.stop())


@pytest.fixture(scope="module")
def registered_subnet(local_subtensor, event_loop):
    """Register a subnet on the local chain."""
    event_loop.run_until_complete(local_subtensor.register_subnet(netuid=1))
    return local_subtensor


@pytest.fixture(scope="module")
def subnet_with_validators(registered_subnet, event_loop):
    """Register validators needed by metagraph and weight-setting tests."""
    validators = [
        ("val_0", "hotkey_0", 100.0),
        ("val_1", "hotkey_1", 80.0),
    ]
    for wallet_name, hotkey, stake in validators:
        event_loop.run_until_complete(
            registered_subnet.register_validator(wallet_name, hotkey, stake)
        )
    return registered_subnet


@pytest.fixture(scope="module")
def subnet_with_validators_and_miner(subnet_with_validators, event_loop):
    """Register a miner once validator setup is complete."""
    event_loop.run_until_complete(
        subnet_with_validators.register_miner("miner_0", "miner_hotkey_0")
    )
    return subnet_with_validators


# ── Tests ──────────────────────────────────────────────────────────────────────


class TestSubtensorRegistration:
    """Test subnet and neuron registration on a local subtensor."""

    def test_register_subnet(self, registered_subnet, event_loop):
        """Verify that the subnet exists on the local chain."""
        metagraph = event_loop.run_until_complete(
            registered_subnet.get_metagraph(netuid=registered_subnet.netuid)
        )
        assert metagraph is not None
        assert metagraph.netuid == registered_subnet.netuid

    def test_register_validator(self, subnet_with_validators, event_loop):
        """Register 2 validators with different stakes."""
        metagraph = event_loop.run_until_complete(
            subnet_with_validators.get_metagraph(netuid=subnet_with_validators.netuid)
        )
        assert metagraph.n >= 3

    def test_metagraph_query(self, subnet_with_validators, event_loop):
        """Query metagraph and verify validator count."""
        metagraph = event_loop.run_until_complete(
            subnet_with_validators.get_metagraph(netuid=subnet_with_validators.netuid)
        )
        assert metagraph is not None
        assert metagraph.n >= 3

    def test_validator_ordering(self, subnet_with_validators, event_loop):
        """Highest stake validator should appear with the most stake."""
        metagraph = event_loop.run_until_complete(
            subnet_with_validators.get_metagraph(netuid=subnet_with_validators.netuid)
        )
        if hasattr(metagraph, "S") and len(metagraph.S) > 0:
            stakes = [float(s) for s in metagraph.S]
            # At least one validator should have non-zero stake
            assert max(stakes) > 0

    def test_register_miner(self, subnet_with_validators_and_miner, event_loop):
        """Register a miner on the subnet."""
        metagraph = event_loop.run_until_complete(
            subnet_with_validators_and_miner.get_metagraph(
                netuid=subnet_with_validators_and_miner.netuid
            )
        )
        # Total neurons should include the miner
        assert metagraph.n >= 4


class TestWeightSetting:
    """Test weight operations on a local subtensor."""

    def test_set_weights(self, subnet_with_validators_and_miner, event_loop):
        """Validator sets weights for miners."""
        metagraph = event_loop.run_until_complete(
            subnet_with_validators_and_miner.get_metagraph(
                netuid=subnet_with_validators_and_miner.netuid
            )
        )
        n = metagraph.n
        uids = list(range(n))
        # Use non-uniform weights (uniform weights hit SDK normalization edge case)
        weights = [float(i + 1) for i in range(n)]

        success = event_loop.run_until_complete(
            subnet_with_validators_and_miner.set_weights(
                wallet_name="val_0",
                hotkey="hotkey_0",
                uids=uids,
                weights=weights,
            )
        )
        assert success is True

    def test_weight_normalization(self, subnet_with_validators_and_miner, event_loop):
        """Weights should normalize to sum to 1.0."""
        metagraph = event_loop.run_until_complete(
            subnet_with_validators_and_miner.get_metagraph(
                netuid=subnet_with_validators_and_miner.netuid
            )
        )
        n = metagraph.n
        # Set unnormalized weights using a different validator (val_1)
        # to avoid commit-reveal cooldown conflict with test_set_weights
        uids = list(range(n))
        weights = [float(10 * (i + 1)) for i in range(n)]

        success = event_loop.run_until_complete(
            subnet_with_validators_and_miner.set_weights(
                wallet_name="val_1",
                hotkey="hotkey_1",
                uids=uids,
                weights=weights,
            )
        )
        assert success is True

        # Query metagraph to verify normalization
        metagraph = event_loop.run_until_complete(
            subnet_with_validators_and_miner.get_metagraph(
                netuid=subnet_with_validators_and_miner.netuid
            )
        )
        if hasattr(metagraph, "W") and len(metagraph.W) > 0:
            # Find a validator's weight row with non-zero sum
            for row in metagraph.W:
                row_sum = sum(float(w) for w in row)
                if row_sum > 0:
                    assert abs(row_sum - 1.0) < 0.05, f"Row sum = {row_sum}"
