"""Leader election emulation tests with real local subtensor.

Tests MetagraphSync + WeightsEmitter against a real local subtensor Docker
container to verify:
- Metagraph sync picks up registered peers from subtensor
- The on-chain leader view is deterministic across syncs
- Higher-stake validators win leader election on the local subnet
- WeightsEmitter rejects unauthorized submissions cleanly

Requires Docker.
"""

import asyncio
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tests.emulation.fixtures.local_subtensor import LocalSubtensor
from minotaur_subnet.validator.metagraph_sync import MetagraphSync, PeerInfo, elect_leader

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
    """Start a local subtensor Docker container for leader election tests."""
    sub = LocalSubtensor(port=9957)  # Unique port to avoid conflicts
    event_loop.run_until_complete(sub.start())
    yield sub
    event_loop.run_until_complete(sub.stop())


@pytest.fixture(scope="module")
def registered_validators(local_subtensor, event_loop):
    """Register subnet + 3 validators with different stakes."""
    event_loop.run_until_complete(local_subtensor.register_subnet(netuid=1))

    validators = [
        ("leader_val", "leader_hotkey", 100.0),
        ("mid_val", "mid_hotkey", 50.0),
        ("low_val", "low_hotkey", 10.0),
    ]
    for wallet_name, hotkey, stake in validators:
        event_loop.run_until_complete(
            local_subtensor.register_validator(wallet_name, hotkey, stake)
        )

    return local_subtensor, validators


# ── Tests ──────────────────────────────────────────────────────────────────────


class TestMetagraphLeaderElection:
    """Test leader election using MetagraphSync against real subtensor."""

    def test_metagraph_sync_finds_registered_peers(
        self, registered_validators, event_loop,
    ):
        """MetagraphSync discovers burned-registered peers from subtensor."""
        sub, validators = registered_validators
        import bittensor as bt

        wallet = bt.Wallet(name="leader_val", hotkey="leader_hotkey")
        my_hotkey = wallet.hotkey.ss58_address

        sync = MetagraphSync(
            subtensor_url=sub.url,
            netuid=sub.netuid,
            my_hotkey=my_hotkey,
            poll_interval=60.0,
        )

        state = event_loop.run_until_complete(sync.sync_once())

        assert state is not None
        assert state.block > 0
        assert state.epoch is not None
        assert state.epoch.tempo_blocks > 0
        peer_hotkeys = {peer.hotkey for peer in state.peers}
        expected_hotkeys = {
            bt.Wallet(name=wallet_name, hotkey=hotkey).hotkey.ss58_address
            for wallet_name, hotkey, _stake in validators
        }
        assert expected_hotkeys.issubset(peer_hotkeys)

    def test_chain_leader_matches_highest_stake_validator(
        self, registered_validators, event_loop,
    ):
        """The local chain leader matches the highest-stake validator view."""
        sub, validators = registered_validators
        import bittensor as bt

        wallet = bt.Wallet(name="leader_val", hotkey="leader_hotkey")
        my_hotkey = wallet.hotkey.ss58_address

        sync = MetagraphSync(
            subtensor_url=sub.url,
            netuid=sub.netuid,
            my_hotkey=my_hotkey,
            poll_interval=60.0,
        )

        state = event_loop.run_until_complete(sync.sync_once())

        assert state.leader is not None
        assert state.validators
        max_stake = max(v.stake for v in state.validators)
        assert state.leader.stake == max_stake

    def test_leader_election_deterministic(
        self, registered_validators, event_loop,
    ):
        """Multiple syncs return the same leader."""
        sub, validators = registered_validators
        import bittensor as bt

        wallet = bt.Wallet(name="leader_val", hotkey="leader_hotkey")
        my_hotkey = wallet.hotkey.ss58_address

        sync = MetagraphSync(
            subtensor_url=sub.url,
            netuid=sub.netuid,
            my_hotkey=my_hotkey,
            poll_interval=60.0,
        )

        state1 = event_loop.run_until_complete(sync.sync_once())
        state2 = event_loop.run_until_complete(sync.sync_once())

        assert state1.leader.hotkey == state2.leader.hotkey

    def test_highest_stake_validator_is_leader(
        self, registered_validators, event_loop,
    ):
        """Highest-stake validator recognizes itself as leader."""
        sub, validators = registered_validators
        import bittensor as bt

        wallet = bt.Wallet(name="leader_val", hotkey="leader_hotkey")
        my_hotkey = wallet.hotkey.ss58_address

        sync = MetagraphSync(
            subtensor_url=sub.url,
            netuid=sub.netuid,
            my_hotkey=my_hotkey,
            poll_interval=60.0,
        )

        state = event_loop.run_until_complete(sync.sync_once())
        assert sync.is_leader
        assert state.my_role == "leader"

    def test_my_role_follower(self, registered_validators, event_loop):
        """Another burned-registered wallet also detects itself as follower."""
        sub, validators = registered_validators
        import bittensor as bt

        wallet = bt.Wallet(name="low_val", hotkey="low_hotkey")
        my_hotkey = wallet.hotkey.ss58_address

        sync = MetagraphSync(
            subtensor_url=sub.url,
            netuid=sub.netuid,
            my_hotkey=my_hotkey,
            poll_interval=60.0,
        )

        state = event_loop.run_until_complete(sync.sync_once())
        assert not sync.is_leader
        assert state.my_role == "follower"


class TestWeightsEmitterOnChain:
    """Test WeightsEmitter against real local subtensor."""

    def test_emit_weights_for_registered_wallet(
        self, registered_validators, event_loop,
    ):
        """WeightsEmitter can submit weights for a burned-registered wallet."""
        sub, validators = registered_validators
        import bittensor as bt

        wallet = bt.Wallet(name="leader_val", hotkey="leader_hotkey")
        subtensor = bt.Subtensor(network=sub.url)

        metagraph = subtensor.metagraph(netuid=sub.netuid)

        from minotaur_subnet.validator.weights_emitter import WeightsEmitter
        emitter = WeightsEmitter(
            wallet=wallet,
            subtensor=subtensor,
            netuid=sub.netuid,
            block_time=0.25,
        )

        # Build weight mapping using real hotkeys from metagraph
        hotkeys = [metagraph.hotkeys[i] for i in range(metagraph.n)]
        weights_map = {hk: 1.0 / metagraph.n for hk in hotkeys}

        success = event_loop.run_until_complete(emitter.emit_async(weights_map))
        assert success is True

    def test_emit_skips_unknown_hotkeys(self, registered_validators, event_loop):
        """WeightsEmitter skips hotkeys not in the metagraph."""
        sub, validators = registered_validators
        import bittensor as bt

        wallet = bt.Wallet(name="leader_val", hotkey="leader_hotkey")
        subtensor = bt.Subtensor(network=sub.url)

        from minotaur_subnet.validator.weights_emitter import WeightsEmitter
        emitter = WeightsEmitter(
            wallet=wallet,
            subtensor=subtensor,
            netuid=sub.netuid,
            block_time=0.25,
        )

        # All fake hotkeys — should return False (no valid UIDs)
        weights_map = {
            "5FakeHotkey1111111111111111111111111111111111111": 0.5,
            "5FakeHotkey2222222222222222222222222222222222222": 0.5,
        }

        success = event_loop.run_until_complete(emitter.emit_async(weights_map))
        assert success is False
