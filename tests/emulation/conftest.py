"""Pytest fixtures for full-stack emulation tests.

These fixtures provide pre-configured infrastructure for integration testing:
- Anvil forks (ETH, Base)
- Local subtensor
- Validator/miner clusters
- Consensus + peer network
- API client
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import asyncio
import pytest
from unittest.mock import AsyncMock

from minotaur_subnet.orderbook import IntentOrderBook
from minotaur_subnet.blockloop import BlockLoop
from minotaur_subnet.relayer import MockRelayer
from minotaur_subnet.consensus import ConsensusManager, ValidatorPeerNetwork, PeerEndpoint
from minotaur_subnet.store import AppIntentStore
from minotaur_subnet.shared.types import (
    AppIntentConfig,
    AppIntentDefinition,
    AppStatus,
    DeploymentResult,
)
from minotaur_subnet.consensus.eip712 import address_from_key

from .fixtures import ValidatorCluster, MinerCluster


# Anvil deterministic keys (accounts 5-9 for validators, matching validator_cluster.py)
VALIDATOR_KEYS = [
    "0x8b3a350cf5c34c9194ca85829a2df0ec3153be0318b5e2d3348e872092edffba",  # 5
    "0x92db14e403b83dfe3df233f83dfa3a0d7096f21ca9b0d6d6b8d88b2b4ec1564e",  # 6
    "0x4bbbf85ce3377467afe5d46f804f221813b2bb87f24d81f60f1fcdbf7cbf4356",  # 7
]


@pytest.fixture
def temp_store(tmp_path):
    """Create a temporary AppIntentStore."""
    return AppIntentStore(store_path=tmp_path / "test_store.json")


@pytest.fixture
def sample_swap_app():
    """A pre-configured swap App Intent definition."""
    return AppIntentDefinition(
        app_id="emulation_swap",
        name="Emulation WETH-USDC Swap",
        version="1.0.0",
        intent_type="swap",
        js_code="module.exports = { config: {name: 'swap'}, score: () => ({score: 0.8, valid: true}) }",
        config=AppIntentConfig(supported_chains=[1, 8453]),
    )


@pytest.fixture
def orderbook():
    """Fresh IntentOrderBook."""
    return IntentOrderBook()


@pytest.fixture
def mock_relayer():
    """MockRelayer that logs submissions."""
    return MockRelayer()


@pytest.fixture
def block_loop(orderbook, temp_store, mock_relayer, sample_swap_app):
    """BlockLoop with mock relayer and pre-loaded app."""
    temp_store.save_app(sample_swap_app)
    temp_store.save_deployment(DeploymentResult(
        app_id="emulation_swap",
        status=AppStatus.ACTIVE,
        contract_address="0x" + "ee" * 20,
    ))

    loop = BlockLoop(
        orderbook=orderbook,
        app_store=temp_store,
        relayer=mock_relayer,
        tick_interval=1.0,
        score_threshold=0.4,
    )
    return loop


@pytest.fixture
def bridge_registry():
    """BridgeRegistry with MockBridgeAdapter."""
    from minotaur_subnet.bridge.registry import BridgeRegistry
    from minotaur_subnet.bridge.mock import MockBridgeAdapter
    reg = BridgeRegistry()
    reg.register(MockBridgeAdapter())
    return reg


@pytest.fixture
def bridge_tracker(bridge_registry, orderbook, mock_relayer):
    """BridgeTracker for cross-chain tests."""
    from minotaur_subnet.relayer.bridge_tracker import BridgeTracker
    return BridgeTracker(
        bridge_registry=bridge_registry,
        orderbook=orderbook,
        relayer=mock_relayer,
    )


@pytest.fixture
def cross_chain_block_loop(
    orderbook, temp_store, mock_relayer, sample_swap_app,
    bridge_registry, bridge_tracker,
):
    """BlockLoop with bridge support and solver for cross-chain tests."""
    # BaselineSwapSolver lives in the external solver repo
    # (subnet112/minotaur-solver); skip dependent tests when absent.
    baseline_mod = pytest.importorskip("minotaur_subnet.sdk.solvers.baseline_solver")
    BaselineSwapSolver = baseline_mod.BaselineSwapSolver

    # Use a low score threshold so mock-scored plans pass
    sample_swap_app.config.score_threshold = 0.3

    temp_store.save_app(sample_swap_app)
    temp_store.save_deployment(DeploymentResult(
        app_id="emulation_swap",
        status=AppStatus.ACTIVE,
        contract_address="0x" + "ee" * 20,
    ))

    solver = BaselineSwapSolver()
    solver.initialize({
        "chain_ids": [1, 8453],
        "rpc_urls": {},
        "bridge_registry": bridge_registry,
    })

    loop = BlockLoop(
        orderbook=orderbook,
        app_store=temp_store,
        relayer=mock_relayer,
        solver=solver,
        tick_interval=1.0,
        score_threshold=0.3,
        bridge_registry=bridge_registry,
        bridge_tracker=bridge_tracker,
    )
    return loop


@pytest.fixture
def consensus_manager():
    """Single-validator ConsensusManager for testing."""
    key = VALIDATOR_KEYS[0]
    addr = address_from_key(key)
    return ConsensusManager(
        validator_id=addr,
        private_key=key,
    )


@pytest.fixture
def multi_validator_consensus():
    """3-validator ConsensusManager with real EVM keys."""
    addrs = [address_from_key(k) for k in VALIDATOR_KEYS]
    return ConsensusManager(
        validator_id=addrs[0],
        private_key=VALIDATOR_KEYS[0],
        quorum_bps=6700,  # 2 of 3
        validators=addrs,
    )


@pytest.fixture
def peer_network(multi_validator_consensus):
    """ValidatorPeerNetwork with mock peers (no real HTTP)."""
    addr0 = address_from_key(VALIDATOR_KEYS[0])
    peers = [
        PeerEndpoint(
            validator_id=address_from_key(VALIDATOR_KEYS[i]),
            url=f"http://validator-{i}:9100",
        )
        for i in range(1, len(VALIDATOR_KEYS))
    ]
    return ValidatorPeerNetwork(
        validator_id=addr0,
        private_key=VALIDATOR_KEYS[0],
        consensus=multi_validator_consensus,
        peers=peers,
    )


@pytest.fixture
def validator_cluster():
    """A 3-validator cluster."""
    cluster = ValidatorCluster()
    asyncio.get_event_loop().run_until_complete(
        cluster.start(count=3, stakes=[100, 80, 60])
    )
    yield cluster
    asyncio.get_event_loop().run_until_complete(cluster.stop())


@pytest.fixture
def miner_cluster():
    """A single-miner cluster."""
    cluster = MinerCluster()
    asyncio.get_event_loop().run_until_complete(cluster.start(count=1))
    yield cluster
    asyncio.get_event_loop().run_until_complete(cluster.stop())
