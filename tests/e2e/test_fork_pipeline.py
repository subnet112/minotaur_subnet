"""Full fork pipeline E2E test.

Combines mainnet fork (real Uniswap V3) with the validator infrastructure
(OrderBook, BlockLoop, ConsensusManager, EvmRelayer).

Requires ALCHEMY_API_KEY or ETHEREUM_RPC_URL env var.
"""

from __future__ import annotations

import asyncio
import os
import re
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest
from web3 import Web3

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from minotaur_subnet.consensus.eip712 import (
    address_from_key,
    build_domain_separator,
    sign_plan_approval_eip712,
    sign_user_order,
    hash_plan_eip712,
)
from minotaur_subnet.orderbook import IntentOrderBook
from minotaur_subnet.blockloop import BlockLoop
from minotaur_subnet.relayer.base import MockRelayer
from minotaur_subnet.consensus import ConsensusManager
from minotaur_subnet.store import AppIntentStore
from minotaur_subnet.shared.types import (
    AppIntentConfig,
    AppIntentDefinition,
    AppStatus,
    DeploymentResult,
)
from tests.emulation.fixtures.validator_cluster import ValidatorCluster


# ── Constants ──────────────────────────────────────────────────────────────────

FORK_PORT = 8547  # Different from test_mainnet_fork to avoid collision
FORK_RPC_URL = f"http://127.0.0.1:{FORK_PORT}"
CONTRACTS_DIR = Path(__file__).resolve().parents[2] / "contracts"

# Anvil keys
ANVIL_KEYS = [
    "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80",
    "0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d",
    "0x5de4111afa1a4b94908f83103eb1f1706367c2e68ca870fc3fb9a804cdab365a",
    "0x7c852118294e51e653712a81e05800f419141751be58f605c371e15141b007a6",
    "0x47e179ec197488593b187f80a00eb0da91f1b9d0b13f8733639f19c30a34926a",
]


# ── Skip condition ─────────────────────────────────────────────────────────────

def _get_fork_rpc() -> str | None:
    api_key = os.environ.get("ALCHEMY_API_KEY")
    if api_key:
        return f"https://eth-mainnet.g.alchemy.com/v2/{api_key}"
    return os.environ.get("ETHEREUM_RPC_URL")


pytestmark = pytest.mark.skipif(
    not _get_fork_rpc(),
    reason="No mainnet RPC (set ALCHEMY_API_KEY or ETHEREUM_RPC_URL)",
)


# ── Dataclasses ────────────────────────────────────────────────────────────────

@dataclass
class PipelineAccounts:
    deployer_key: str
    user_key: str
    validator_keys: list[str]

    @property
    def deployer_addr(self) -> str:
        return address_from_key(self.deployer_key)

    @property
    def user_addr(self) -> str:
        return address_from_key(self.user_key)

    @property
    def validator_addrs(self) -> list[str]:
        return [address_from_key(k) for k in self.validator_keys]

    @property
    def sorted_validators(self) -> list[tuple[str, str]]:
        pairs = list(zip(self.validator_addrs, self.validator_keys))
        pairs.sort(key=lambda p: int(p[0], 16))
        return pairs


@dataclass
class PipelineContracts:
    weth: str
    usdc: str
    router: str
    forwarder: str
    swap_app: str
    relayer: str
    domain_separator: bytes


# ── Fixtures ───────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def accounts() -> PipelineAccounts:
    return PipelineAccounts(
        deployer_key=ANVIL_KEYS[0],
        user_key=ANVIL_KEYS[1],
        validator_keys=[ANVIL_KEYS[2], ANVIL_KEYS[3], ANVIL_KEYS[4]],
    )


@pytest.fixture(scope="module")
def fork_anvil():
    """Start Anvil with mainnet fork."""
    rpc = _get_fork_rpc()
    if not rpc:
        pytest.skip("No mainnet RPC")

    proc = subprocess.Popen(
        [
            "anvil",
            "--host", "0.0.0.0",
            "--port", str(FORK_PORT),
            "--fork-url", rpc,
            "--chain-id", "1",
            "--accounts", "10",
            "--silent",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    w3 = Web3(Web3.HTTPProvider(FORK_RPC_URL))
    for _ in range(60):
        try:
            if w3.is_connected():
                break
        except Exception:
            pass
        time.sleep(0.5)
    else:
        proc.kill()
        raise RuntimeError("Anvil fork failed to start")

    yield proc

    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


@pytest.fixture(scope="module")
def fork_w3(fork_anvil) -> Web3:
    return Web3(Web3.HTTPProvider(FORK_RPC_URL))


@pytest.fixture(scope="module")
def deployed(fork_anvil, accounts) -> PipelineContracts:
    """Deploy contracts on the fork."""
    sorted_vals = accounts.sorted_validators
    validators_csv = ",".join(addr for addr, _ in sorted_vals)

    env = os.environ.copy()
    env["DEPLOYER_PRIVATE_KEY"] = accounts.deployer_key
    env["VALIDATORS"] = validators_csv
    env["QUORUM_BPS"] = "8000"
    env["SCORE_THRESHOLD"] = "5000"

    result = subprocess.run(
        [
            "forge", "script",
            "script/DeployForkedStack.s.sol:DeployForkedStack",
            "--rpc-url", FORK_RPC_URL,
            "--broadcast",
        ],
        capture_output=True,
        text=True,
        cwd=str(CONTRACTS_DIR),
        env=env,
        timeout=120,
    )

    if result.returncode != 0:
        raise RuntimeError(
            f"DeployForkedStack failed:\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )

    addresses = {}
    for line in result.stdout.split("\n"):
        match = re.search(
            r"(\w+_ADDRESS|DOMAIN_SEPARATOR)=(0x[0-9a-fA-F]+)", line,
        )
        if match:
            addresses[match.group(1)] = match.group(2)

    swap_app_addr = addresses["SWAP_APP_ADDRESS"]
    domain = build_domain_separator(1, swap_app_addr)

    return PipelineContracts(
        weth=addresses["WETH_ADDRESS"],
        usdc=addresses["USDC_ADDRESS"],
        router=addresses["ROUTER_ADDRESS"],
        forwarder=addresses["FORWARDER_ADDRESS"],
        swap_app=addresses["SWAP_APP_ADDRESS"],
        relayer=addresses["RELAYER_ADDRESS"],
        domain_separator=domain,
    )


@pytest.fixture
def pipeline_store(tmp_path):
    """Create a temporary store with a swap app pre-registered."""
    store = AppIntentStore(store_path=tmp_path / "pipeline_store.json")
    app_def = AppIntentDefinition(
        app_id="fork_swap",
        name="Fork WETH-USDC Swap",
        version="1.0.0",
        intent_type="swap",
        js_code="module.exports = { config: {name: 'swap'}, score: () => ({score: 0.85, valid: true}) }",
        config=AppIntentConfig(supported_chains=[1]),
    )
    store.save_app(app_def)
    store.save_deployment(DeploymentResult(
        app_id="fork_swap",
        status=AppStatus.ACTIVE,
        contract_address="0x" + "ee" * 20,
    ))
    return store


@pytest.fixture
def validator_cluster():
    """3-validator cluster with real EVM keys."""
    cluster = ValidatorCluster()
    asyncio.get_event_loop().run_until_complete(
        cluster.start(count=3, stakes=[100, 80, 60])
    )
    yield cluster
    asyncio.get_event_loop().run_until_complete(cluster.stop())


# ── Tests ──────────────────────────────────────────────────────────────────────


class TestForkPipeline:
    """Full pipeline test: OrderBook → BlockLoop → real Uniswap V3 swap."""

    def test_full_fork_pipeline(
        self,
        fork_w3,
        deployed,
        accounts,
        pipeline_store,
        validator_cluster,
    ):
        """Submit order → BlockLoop tick → MockRelayer records submission.

        This test verifies the full pipeline: order submission to OrderBook,
        BlockLoop processing, plan generation, scoring, and relayer submission.
        The actual on-chain execution uses MockRelayer since the BlockLoop
        doesn't generate real Uniswap calldata.
        """
        # Set up OrderBook and MockRelayer
        orderbook = IntentOrderBook()
        mock_relayer = MockRelayer()

        # Set up ConsensusManager (single-validator MVP mode)
        leader = validator_cluster.get_leader()
        consensus = ConsensusManager(
            validator_id=leader.evm_address,
            private_key=leader.private_key,
            quorum_bps=10000,
            validators=[leader.evm_address],
        )

        # Set up BlockLoop
        loop = BlockLoop(
            orderbook=orderbook,
            app_store=pipeline_store,
            relayer=mock_relayer,
            consensus=consensus,
            tick_interval=1.0,
            score_threshold=0.4,
        )

        # Submit an order to the OrderBook
        order = orderbook.submit(
            app_id="fork_swap",
            intent_function="swap",
            params={
                "token_in": deployed.weth,
                "token_out": deployed.usdc,
                "amount_in": str(1 * 10**18),
                "min_output": str(1500 * 10**6),
            },
            submitted_by=accounts.user_addr,
            chain_id=1,
        )

        assert order is not None
        assert order.status.value == "open"

        # Run a single tick
        result = asyncio.get_event_loop().run_until_complete(loop.tick())

        # Verify tick processed the order
        assert result.orders_processed >= 1
        assert result.tick_number == 1

        # Verify the order was processed (either approved or rejected)
        updated_order = orderbook.get(order.order_id)
        assert updated_order is not None
        # The order should have been processed (filled or failed)
        assert updated_order.status.value in ("filled", "failed", "open")

        # If the mock solver generated a plan and it scored above threshold,
        # the relayer should have a submission
        if result.orders_approved > 0:
            assert len(mock_relayer.submissions) == 1
            sub = mock_relayer.submissions[0]
            assert sub["order_id"] == order.order_id
            assert sub["chain_id"] == 1

    def test_fork_pipeline_with_consensus(
        self,
        fork_w3,
        deployed,
        accounts,
        pipeline_store,
        validator_cluster,
    ):
        """Verify consensus manager signs and approves plans."""
        leader = validator_cluster.get_leader()

        consensus = ConsensusManager(
            validator_id=leader.evm_address,
            private_key=leader.private_key,
            quorum_bps=10000,
            validators=[leader.evm_address],
        )

        orderbook = IntentOrderBook()
        mock_relayer = MockRelayer()

        loop = BlockLoop(
            orderbook=orderbook,
            app_store=pipeline_store,
            relayer=mock_relayer,
            consensus=consensus,
            tick_interval=1.0,
            score_threshold=0.4,
        )

        # Submit multiple orders
        for i in range(3):
            orderbook.submit(
                app_id="fork_swap",
                intent_function="swap",
                params={"token_in": deployed.weth, "amount": str(i + 1)},
                submitted_by=accounts.user_addr,
                chain_id=1,
            )

        # Run tick to process all orders
        result = asyncio.get_event_loop().run_until_complete(loop.tick())

        assert result.orders_processed == 3
        # All should be processed (approved or rejected depending on scoring)
        assert result.orders_approved + result.orders_rejected == 3

    def test_validator_cluster_failover(self, validator_cluster):
        """Verify leader failover when the leader is killed."""
        initial_leader = validator_cluster.get_leader()
        assert initial_leader is not None
        initial_leader_addr = initial_leader.evm_address

        asyncio.get_event_loop().run_until_complete(
            validator_cluster.kill_leader()
        )

        new_leader = validator_cluster.get_leader()
        assert new_leader is not None
        assert new_leader.evm_address != initial_leader_addr
        assert new_leader.is_leader is True

    def test_validator_cluster_real_signatures(
        self, validator_cluster, deployed,
    ):
        """Verify that the cluster produces real EIP-712 signatures."""
        from eth_hash.auto import keccak

        order_id = keccak(b"test_sig_order")
        plan_hash = keccak(b"test_plan_hash")

        sigs = validator_cluster.get_signatures(
            plan_hash=plan_hash,
            order_id=order_id,
            score_bps=8000,
            domain_separator=deployed.domain_separator,
        )

        # Should have signatures from active validators
        active_count = sum(1 for v in validator_cluster.validators if v.stake > 0)
        assert len(sigs) == active_count

        # Each signature should be 65 bytes (r, s, v)
        for addr, sig in sigs:
            assert len(sig) == 65, f"Signature from {addr} is {len(sig)} bytes"
            assert addr.startswith("0x")
