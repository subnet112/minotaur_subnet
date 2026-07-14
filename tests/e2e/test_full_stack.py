"""Phase E: Full Stack Integration ("God Test").

Everything together: Anvil + all Python components in one ordered test class.
Each test builds on the state left by the previous test.

Requires: Anvil (Foundry) for on-chain execution.
Docker is optional (subtensor tests skipped if not available).
"""

import asyncio
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from eth_hash.auto import keccak
from web3 import Web3

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from minotaur_subnet.blockchain.chains import _web3_cache
from minotaur_subnet.blockloop.loop import BlockLoop
from minotaur_subnet.consensus.eip712 import (
    build_domain_separator,
    hash_plan_eip712,
    sign_plan_approval_eip712,
)
from minotaur_subnet.consensus.manager import ConsensusManager
from minotaur_subnet.consensus.protocol_config import ProtocolConfig
from minotaur_subnet.consensus.peer_network import ValidatorPeerNetwork, PeerEndpoint
from minotaur_subnet.consensus.signatures import sign_plan_approval
from minotaur_subnet.validator.metagraph_sync import PeerInfo, elect_leader
from minotaur_subnet.epoch.manager import EpochManager
from minotaur_subnet.epoch.relative_scoring import has_delivered_value_rows
from minotaur_subnet.harness.submission_store import SubmissionStatus, SubmissionStore
from minotaur_subnet.store import AppIntentStore
from minotaur_subnet.orderbook.orderbook import IntentOrderBook, OrderStatus
from minotaur_subnet.relayer.base import MockRelayer
from minotaur_subnet.relayer.chain_config import ChainDeployment
from minotaur_subnet.relayer.evm_relayer import EvmRelayer
from minotaur_subnet.relayer.validator_sync import ValidatorSync
from minotaur_subnet.sdk.intent_solver import IntentSolver, MarketSnapshot, SolverMetadata
from minotaur_subnet.sdk.solvers.anvil_swap_solver import AnvilSwapSolver
from minotaur_subnet.shared.types import (
    AppIntentConfig,
    AppIntentDefinition,
    ExecutionPlan,
    Interaction,
    IntentState,
    SignedApproval,
)
from tests.e2e.dex_test_helpers import (
    StaticMockSolver,
    fund_and_approve_erc20,
    save_active_deployment,
    submit_and_sign_dex_swap_order,
)

from conftest import ANVIL_KEYS, CHAIN_ID, RPC_URL, TestAccounts, DeployedContracts

pytestmark = pytest.mark.skipif(
    not shutil.which("anvil"), reason="Foundry (anvil) required"
)

# ── Minimal ABIs ──────────────────────────────────────────────────────────

MOCK_TOKEN_ABI = [
    {"inputs": [{"name": "to", "type": "address"}, {"name": "amount", "type": "uint256"}], "name": "mint", "outputs": [], "stateMutability": "nonpayable", "type": "function"},
    {"inputs": [{"name": "spender", "type": "address"}, {"name": "amount", "type": "uint256"}], "name": "approve", "outputs": [{"name": "", "type": "bool"}], "stateMutability": "nonpayable", "type": "function"},
    {"inputs": [{"name": "account", "type": "address"}], "name": "balanceOf", "outputs": [{"name": "", "type": "uint256"}], "stateMutability": "view", "type": "function"},
]

SWAP_APP_ABI = [
    {"inputs": [], "name": "SWAP_SELECTOR", "outputs": [{"name": "", "type": "bytes4"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "scoreThreshold", "outputs": [{"name": "", "type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"name": "", "type": "address"}], "name": "nonces", "outputs": [{"name": "", "type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"name": "", "type": "bytes32"}], "name": "executedOrders", "outputs": [{"name": "", "type": "bool"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "getValidators", "outputs": [{"name": "", "type": "address[]"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "getQuorumRequired", "outputs": [{"name": "", "type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"anonymous": False, "inputs": [{"indexed": True, "name": "orderId", "type": "bytes32"}, {"indexed": True, "name": "submittedBy", "type": "address"}, {"indexed": False, "name": "score", "type": "uint256"}, {"indexed": False, "name": "planHash", "type": "bytes32"}, {"indexed": False, "name": "gasUsed", "type": "uint256"}], "name": "IntentExecuted", "type": "event"},
]

TEST_SWAP_ROUTER_ABI = [
    {"inputs": [{"name": "outputToken", "type": "address"}, {"name": "outputAmount", "type": "uint256"}, {"name": "recipient", "type": "address"}], "name": "swapExact", "outputs": [], "stateMutability": "nonpayable", "type": "function"},
]


# ── AutoQuorumConsensus ───────────────────────────────────────────────────


class AutoQuorumConsensus(ConsensusManager):
    """Signs with all validators automatically for testing.

    Bypasses multi-validator coordination by signing with all validator keys
    at propose() time, ensuring the on-chain quorum check passes.
    """

    def __init__(self, all_keys: list[str], all_addrs: list[str], **kwargs):
        super().__init__(
            validator_id=all_addrs[0],
            private_key=all_keys[0],
            validators=all_addrs,
            **kwargs,
        )
        self._all_keys = all_keys
        self._all_addrs = all_addrs

    async def propose(self, order_id, plan, score, plan_hash):
        """Auto-sign with all validators and return quorum immediately."""
        approvals = []
        for key, addr in zip(self._all_keys, self._all_addrs):
            sig = sign_plan_approval(
                key, order_id, plan_hash, score,
                domain_separator=self.domain_separator,
                score_bps=self.score_threshold_bps,
            )
            approvals.append(SignedApproval(
                validator_id=addr,
                order_id=order_id,
                plan_hash=plan_hash,
                score=score,
                signature=sig,
            ))
        approvals.sort(key=lambda a: int(a.validator_id.replace("0x", ""), 16))
        from minotaur_subnet.shared.types import ConsensusResult
        return ConsensusResult(
            reached=True,
            approvals=approvals,
            quorum=len(approvals),
            collected=len(approvals),
            combined_score=score,
        )


# ── Mock solvers for benchmarking ─────────────────────────────────────────


class SimpleSolver(IntentSolver):
    """Simple solver for initial adoption."""

    def __init__(self, router="", usdc=""):
        self._router = router
        self._usdc = usdc

    def initialize(self, config: dict[str, Any]) -> None:
        pass

    def generate_plan(
        self, intent: AppIntentDefinition, state: IntentState, snapshot: MarketSnapshot,
    ) -> ExecutionPlan:
        return ExecutionPlan(
            intent_id=intent.app_id,
            interactions=[
                Interaction(target="0x" + "11" * 20, value="0", call_data="0xdeadbeef"),
            ],
            deadline=int(time.time()) + 300,
            nonce=state.nonce,
        )

    def metadata(self) -> SolverMetadata:
        return SolverMetadata(name="simple-solver", version="1.0.0", author="miner-1")


class ImprovedSolver(IntentSolver):
    """Improved solver that generates two interactions."""

    def initialize(self, config: dict[str, Any]) -> None:
        pass

    def generate_plan(
        self, intent: AppIntentDefinition, state: IntentState, snapshot: MarketSnapshot,
    ) -> ExecutionPlan:
        return ExecutionPlan(
            intent_id=intent.app_id,
            interactions=[
                Interaction(target="0x" + "11" * 20, value="0", call_data="0xdeadbeef"),
                Interaction(target="0x" + "22" * 20, value="0", call_data="0xcafe"),
            ],
            deadline=int(time.time()) + 300,
            nonce=state.nonce,
            metadata={"optimized": True},
        )

    def metadata(self) -> SolverMetadata:
        return SolverMetadata(name="improved-solver", version="2.0.0", author="miner-2")


class MockOrchestrator:
    """Returns solver instances directly."""

    def __init__(self):
        self._solvers: dict[str, IntentSolver] = {}

    def register(self, tag: str, solver: IntentSolver):
        self._solvers[tag] = solver

    async def start_docker(self, image_tag: str):
        solver = self._solvers.get(image_tag)
        if solver is None:
            raise ValueError(f"No solver for {image_tag}")
        return _MockSession(solver)


class _MockSession:
    def __init__(self, solver):
        self._solver = solver

    async def initialize(self, config):
        self._solver.initialize(config)

    async def restore_state(self, data):
        pass

    async def serialize_state(self):
        return b""

    async def shutdown(self):
        pass

    def generate_plan(self, i, s, sn):
        return self._solver.generate_plan(i, s, sn)

    def metadata(self):
        return self._solver.metadata()


# Single benchmark order the mock worker records delivered value for. The scalar
# composite score is gone: benchmarking now records the RAW delivered output per
# order (an exact decimal wei string), and the relative net-better rule decides
# adoption off those rows.
BENCH_INTENT_ID = "swap-app:benchmark"


class MockBenchmarkWorker:
    """In-process benchmark worker for testing.

    Records per-order delivered value (``benchmark_details.per_intent[*].raw_output``)
    instead of the retired scalar ``benchmark_score``. A richer plan (more
    interactions) models proportionally more delivered value, so an improved solver
    wins the per-order relative net-better comparison the EpochManager runs.
    """

    def __init__(self, sub_store, orchestrator):
        self._sub_store = sub_store
        self._orch = orchestrator

    async def run_once(self):
        pending = self._sub_store.list_by_status(SubmissionStatus.BENCHMARKING)
        for sub in pending:
            if not sub.image_tag:
                self._sub_store.reject(sub.submission_id, "No image")
                continue
            try:
                session = await self._orch.start_docker(sub.image_tag)
                await session.initialize({})
                # Model delivered value from plan richness: more interactions ->
                # more raw output delivered on the benchmark order.
                plan = session.generate_plan(
                    AppIntentDefinition(
                        app_id="test", name="t", version="1", intent_type="swap", js_code="",
                    ),
                    IntentState(contract_address="", chain_id=1, nonce=0, owner=""),
                    MarketSnapshot(chain_id=1, block_number=1, timestamp=int(time.time())),
                )
                raw_output = str(1000 * len(plan.interactions))
                # valid=True + a per-order row with raw_output > 0 == SCORED-with-value
                # (the delivered-value validity gate that replaced score > 0).
                self._sub_store.set_benchmark_result(
                    sub.submission_id,
                    valid=True,
                    details={"per_intent": [
                        {"intent_id": BENCH_INTENT_ID, "raw_output": raw_output},
                    ]},
                )
            except Exception:
                # No usable plan -> delivered no value -> validity gate rejects it
                # (old: score <= 0).
                self._sub_store.set_benchmark_result(sub.submission_id, valid=False)

        # Adoption is owned by the EpochManager (relative net-better vs the
        # champion), not by the benchmark worker — it no longer picks a winner
        # off a scalar score.
        return len(pending)


# ── Champion runtime builder ──────────────────────────────────────────────


def _mock_runtime_builder(submission, epoch):
    """Build the live champion runtime for a hot-swap.

    Returning a plain object is enough here: BlockLoop is not wired into these
    EpochManagers, so ``_hot_swap`` only records champion metadata. Using the
    runtime-builder path (instead of the orchestrator/Docker path) avoids a live
    ``docker inspect`` of the image_id required for champion eligibility.
    """
    return object()


# ── Shared state class ────────────────────────────────────────────────────


class FullStackState:
    """Shared mutable state across ordered tests."""

    def __init__(self):
        self.w3: Web3 = None
        self.dc: DeployedContracts = None
        self.accts: TestAccounts = None
        self.domain: bytes = None
        self.score_threshold: int = 0

        # Components
        self.ob: IntentOrderBook = None
        self.app_store: AppIntentStore = None
        self.consensus: ConsensusManager = None
        self.relayer: EvmRelayer = None
        self.solver: AnvilSwapSolver = None
        self.loop: BlockLoop = None
        self.sub_store: SubmissionStore = None
        self.epoch_mgr: EpochManager = None


# Module-scoped state
_state = FullStackState()


# ── Fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture(scope="module", autouse=True)
def setup_full_stack(anvil, deployed_contracts, test_accounts, eip712_domain, tmp_path_factory):
    """Initialize all components for the full stack test."""
    # Champion eligibility enforces signed provenance by default; these in-process
    # mock submissions carry no provenance, so relax it for the test (same as
    # tests/unit/test_epoch_manager.py). The delivered-value validity gate and the
    # relative net-better adoption rule are still fully exercised.
    _prov_env = {
        "REQUIRE_SIGNED_PROVENANCE": "0",
        "REQUIRE_ASYMMETRIC_PROVENANCE": "0",
    }
    _saved_env = {k: os.environ.get(k) for k in _prov_env}
    os.environ.update(_prov_env)

    w3 = Web3(Web3.HTTPProvider(RPC_URL))
    # Seed both cache variants (get_web3 keys on (chain_id, install_retry)).
    _web3_cache[(CHAIN_ID, True)] = w3
    _web3_cache[(CHAIN_ID, False)] = w3

    dc = deployed_contracts
    accts = test_accounts

    _state.w3 = w3
    _state.dc = dc
    _state.accts = accts
    _state.domain = eip712_domain

    app_contract = w3.eth.contract(address=dc.dex_app, abi=SWAP_APP_ABI)
    _state.score_threshold = app_contract.functions.scoreThreshold().call()

    # OrderBook
    _state.ob = IntentOrderBook()

    # App store
    store_path = tmp_path_factory.mktemp("fullstack") / "store.json"
    _state.app_store = AppIntentStore(store_path=store_path)

    # Submission store
    _state.sub_store = SubmissionStore()

    yield

    _web3_cache.pop((CHAIN_ID, True), None)
    _web3_cache.pop((CHAIN_ID, False), None)
    for k, v in _saved_env.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


# ── Ordered Tests ─────────────────────────────────────────────────────────


class TestFullStack:
    """End-to-end test of the complete Minotaur v2 pipeline."""

    def test_01_anvil_ready(self):
        """Anvil is running and responsive."""
        assert _state.w3.is_connected()
        assert _state.w3.eth.chain_id == CHAIN_ID

    def test_02_contracts_deployed(self):
        """All test stack contracts are deployed."""
        dc = _state.dc
        w3 = _state.w3

        assert w3.eth.get_code(dc.dex_app) != b""
        assert w3.eth.get_code(dc.router) != b""
        assert w3.eth.get_code(dc.weth) != b""
        assert w3.eth.get_code(dc.usdc) != b""

    def test_03_validators_registered(self):
        """Validators are registered on-chain."""
        app_contract = _state.w3.eth.contract(
            address=_state.dc.dex_app, abi=SWAP_APP_ABI,
        )
        validators = app_contract.functions.getValidators().call()
        assert len(validators) == 3

        for addr in _state.accts.validator_addrs:
            assert _state.w3.to_checksum_address(addr) in [
                _state.w3.to_checksum_address(v) for v in validators
            ]

    def test_04_create_app(self):
        """Create swap app in AppIntentStore."""
        app_def = AppIntentDefinition(
            app_id="swap-app",
            name="Full Stack Swap App",
            version="1.0.0",
            intent_type="swap",
            js_code="// fullstack test",
            config=AppIntentConfig(supported_chains=[CHAIN_ID]),
        )
        _state.app_store.save_app(app_def)
        save_active_deployment(
            _state.app_store,
            app_id="swap-app",
            contract_address=_state.dc.dex_app,
            chain_id=CHAIN_ID,
        )

        retrieved = _state.app_store.get_app("swap-app")
        assert retrieved is not None
        assert retrieved.name == "Full Stack Swap App"

    def test_05_setup_consensus(self):
        """Set up AutoQuorumConsensus that signs with all 3 validators."""
        accts = _state.accts

        # Sort validators by address (contract requires ascending order)
        sorted_vals = accts.sorted_validators
        all_addrs = [addr for addr, _ in sorted_vals]
        all_keys = [key for _, key in sorted_vals]

        _state.consensus = AutoQuorumConsensus(
            all_keys=all_keys,
            all_addrs=all_addrs,
            protocol_config=ProtocolConfig(quorum_bps=10000, rpc_url="", registry_address=""),
            chain_id=CHAIN_ID,
            contract_address=_state.dc.dex_app,
            domain_separator=_state.domain,
            score_threshold_bps=_state.score_threshold,
        )

    def test_06_setup_relayer(self):
        """Set up EvmRelayer connected to Anvil."""
        chain_config = ChainDeployment(
            chain_id=CHAIN_ID,
            name="Anvil",
            rpc_url=RPC_URL,
            app_intent_base_address=_state.dc.dex_app,
            relayer_wallet=_state.accts.deployer_addr,
        )
        _state.relayer = EvmRelayer(
            chains={CHAIN_ID: chain_config},
            private_key=_state.accts.deployer_key,
        )

    def test_07_miner_submits_solver(self):
        """Miner submits solver → screening passes."""
        sub = _state.sub_store.create(
            repo_url="https://github.com/miner1/solver",
            commit_hash="aaa111",
            epoch=1,
            hotkey="5Gminer1...",
        )

        # Pass screening stages
        _state.sub_store.update_status(sub.submission_id, SubmissionStatus.SCREENING_STAGE_1)
        _state.sub_store.set_screening_result(sub.submission_id, 1, True, 500)
        _state.sub_store.update_status(sub.submission_id, SubmissionStatus.SCREENING_STAGE_2)
        _state.sub_store.set_screening_result(sub.submission_id, 2, True, 3000)
        _state.sub_store.set_solver_info(sub.submission_id, "simple-solver", "1.0.0")
        _state.sub_store.set_image_tag(sub.submission_id, "simple-solver:latest")
        # Stage 3 captures the immutable image_id (sha256) — required for champion
        # eligibility (is_submission_champion_eligible).
        _state.sub_store.set_image_id(sub.submission_id, "sha256:" + "a" * 64)
        _state.sub_store.update_status(sub.submission_id, SubmissionStatus.SCREENING_STAGE_3)
        _state.sub_store.set_screening_result(sub.submission_id, 3, True, 10000)
        _state.sub_store.update_status(sub.submission_id, SubmissionStatus.BENCHMARKING)

        refreshed = _state.sub_store.get(sub.submission_id)
        assert refreshed.status == SubmissionStatus.BENCHMARKING

    def test_08_benchmark_and_adopt(self):
        """Benchmark solver → EpochManager adopts as champion."""
        orch = MockOrchestrator()
        orch.register("simple-solver:latest", SimpleSolver())
        bw = MockBenchmarkWorker(_state.sub_store, orch)

        epoch_mgr = EpochManager(
            benchmark_worker=bw,
            submission_store=_state.sub_store,
            orchestrator=orch,
            runtime_builder=_mock_runtime_builder,
        )
        _state.epoch_mgr = epoch_mgr

        result = asyncio.get_event_loop().run_until_complete(
            epoch_mgr.on_epoch_boundary(epoch=1)
        )

        assert result["champion_changed"] is True
        # The scalar benchmark_score is gone; a champion is a submission that
        # delivered value (>= 1 per-order raw_output > 0) and won the relative
        # net-better contest. Assert both that a champion was adopted and that it
        # is the benchmarked submission with delivered value on record.
        champ_sub = _state.sub_store.get(epoch_mgr.champion.submission_id)
        assert champ_sub is not None
        assert has_delivered_value_rows(
            (champ_sub.benchmark_details or {}).get("per_intent")
        )

    def test_09_setup_solver_and_blockloop(self):
        """Wire AnvilSwapSolver into BlockLoop."""
        solver = AnvilSwapSolver()
        solver.initialize({
            "router_address": _state.dc.router,
            "weth_address": _state.dc.weth,
            "usdc_address": _state.dc.usdc,
        })
        _state.solver = solver

        _state.loop = BlockLoop(
            orderbook=_state.ob,
            app_store=_state.app_store,
            solver=solver,
            relayer=_state.relayer,
            consensus=_state.consensus,
            score_threshold=0.3,
        )

    def test_10_submit_intent_order(self):
        """User submits signed swap order to OrderBook."""
        w3 = _state.w3
        dc = _state.dc
        accts = _state.accts

        # Mint WETH to user
        fund_and_approve_erc20(
            w3,
            token_address=dc.weth,
            token_abi=MOCK_TOKEN_ABI,
            recipient=accts.user_addr,
            spender=dc.dex_app,
            amount=10**18,
            funder_key=accts.deployer_key,
            owner_key=accts.user_key,
            chain_id=CHAIN_ID,
        )

        # Get on-chain state
        app_contract = w3.eth.contract(address=dc.dex_app, abi=SWAP_APP_ABI)
        user_nonce = app_contract.functions.nonces(accts.user_addr).call()

        order, _signed = submit_and_sign_dex_swap_order(
            w3=w3,
            orderbook=_state.ob,
            app_id="swap-app",
            app_address=dc.dex_app,
            app_abi=SWAP_APP_ABI,
            user_key=accts.user_key,
            submitted_by=accts.user_addr,
            domain_separator=_state.domain,
            chain_id=CHAIN_ID,
            user_nonce=user_nonce,
            input_token=dc.weth,
            output_token=dc.usdc,
            input_amount=10**18,
            min_output_amount=1800 * 10**6,
        )

        assert order.status == OrderStatus.OPEN
        _state._last_order_id = order.order_id

    def test_11_blockloop_processes(self):
        """BlockLoop.tick() → solver generates plan → AutoQuorumConsensus signs → relayer executes."""
        loop = _state.loop

        result = asyncio.get_event_loop().run_until_complete(loop.tick())

        # Debug: check order error if rejected
        order = _state.ob.get(_state._last_order_id)
        assert result.orders_processed == 1
        assert result.orders_approved == 1, f"Order rejected. Status: {order.status}, Error: {order.error}"

    def test_12_verify_execution(self):
        """Check on-chain: order filled, USDC received, IntentExecuted event."""
        order = _state.ob.get(_state._last_order_id)
        assert order.status == OrderStatus.FILLED
        assert order.tx_hash is not None

        # Verify USDC balance
        usdc = _state.w3.eth.contract(address=_state.dc.usdc, abi=MOCK_TOKEN_ABI)
        balance = usdc.functions.balanceOf(_state.accts.user_addr).call()
        assert balance >= 1800 * 10**6

    def test_13_replay_protection(self):
        """Same order ID is marked executed on-chain."""
        # The orderId on-chain is keccak256 of the Order.order_id string
        order_id_bytes = keccak(_state._last_order_id.encode())
        app_contract = _state.w3.eth.contract(
            address=_state.dc.dex_app, abi=SWAP_APP_ABI,
        )
        assert app_contract.functions.executedOrders(order_id_bytes).call() is True

    def test_14_perpetual_order(self):
        """Perpetual order fills and re-opens."""
        ob = IntentOrderBook()

        order = ob.submit(
            app_id="swap-app",
            intent_function="execute",
            params={
                "input_token": "0x" + "aa" * 20,
                "output_token": "0x" + "bb" * 20,
                "input_amount": "1000000",
            },
            submitted_by=_state.accts.user_addr,
            chain_id=CHAIN_ID,
            perpetual=True,
            max_executions=3,
            cooldown=0,
        )

        loop = BlockLoop(
            orderbook=ob,
            app_store=_state.app_store,
            solver=StaticMockSolver(),
            relayer=MockRelayer(),
            score_threshold=0.1,
        )

        # First fill
        result = asyncio.get_event_loop().run_until_complete(loop.tick())
        assert result.orders_approved == 1

        final = ob.get(order.order_id)
        assert final.status == OrderStatus.OPEN
        assert final.execution_count == 1

        # Second fill
        result2 = asyncio.get_event_loop().run_until_complete(loop.tick())
        assert result2.orders_approved == 1

        final2 = ob.get(order.order_id)
        assert final2.execution_count == 2

    def test_15_miner_improves_solver(self):
        """Second miner submits better solver → dethroned → hot-swapped."""
        orch = MockOrchestrator()
        orch.register("simple-solver:latest", SimpleSolver())
        orch.register("improved-solver:latest", ImprovedSolver())
        bw = MockBenchmarkWorker(_state.sub_store, orch)

        epoch_mgr = EpochManager(
            benchmark_worker=bw,
            submission_store=_state.sub_store,
            orchestrator=orch,
            runtime_builder=_mock_runtime_builder,
        )

        # Re-run epoch 1 to set champion (the simple solver)
        asyncio.get_event_loop().run_until_complete(
            epoch_mgr.on_epoch_boundary(epoch=1)
        )
        old_champion_id = epoch_mgr.champion.submission_id
        assert epoch_mgr.champion.solver_name == "simple-solver"

        # Epoch 2: improved solver (2 interactions -> more delivered value)
        sub2 = _state.sub_store.create(
            "https://github.com/m2/solver", "bbb222",
            epoch=2, hotkey="5Gminer2...",
        )
        _state.sub_store.set_solver_info(sub2.submission_id, "improved-solver", "2.0.0")
        _state.sub_store.set_image_tag(sub2.submission_id, "improved-solver:latest")
        _state.sub_store.set_image_id(sub2.submission_id, "sha256:" + "b" * 64)
        _state.sub_store.update_status(sub2.submission_id, SubmissionStatus.BENCHMARKING)

        result = asyncio.get_event_loop().run_until_complete(
            epoch_mgr.on_epoch_boundary(epoch=2)
        )

        # No scalar score to compare: the improved solver dethrones the incumbent
        # by delivering strictly more value per order (relative net-better wins),
        # so the champion is hot-swapped to it.
        assert result["champion_changed"] is True
        assert epoch_mgr.champion.submission_id == sub2.submission_id
        assert epoch_mgr.champion.submission_id != old_champion_id
        assert epoch_mgr.champion.solver_name == "improved-solver"

    def test_16_peer_network_wired_into_blockloop(self):
        """ValidatorPeerNetwork wires into BlockLoop alongside consensus."""
        accts = _state.accts
        sorted_vals = accts.sorted_validators
        leader_addr, leader_key = sorted_vals[0]

        # Create peer network with the other validators as mock peers
        peer_endpoints = [
            PeerEndpoint(validator_id=addr, url=f"http://127.0.0.1:910{i}")
            for i, (addr, _) in enumerate(sorted_vals[1:], start=1)
        ]

        peer_network = ValidatorPeerNetwork(
            validator_id=leader_addr,
            private_key=leader_key,
            consensus=_state.consensus,
            peers=peer_endpoints,
        )

        # Wire into a fresh BlockLoop
        ob = IntentOrderBook()
        ob.submit(
            app_id="swap-app",
            intent_function="execute",
            params={
                "input_token": "0x" + "aa" * 20,
                "output_token": "0x" + "bb" * 20,
                "input_amount": "1000",
            },
            submitted_by=accts.user_addr,
            chain_id=CHAIN_ID,
        )

        loop = BlockLoop(
            orderbook=ob,
            app_store=_state.app_store,
            solver=None,
            relayer=MockRelayer(),
            consensus=_state.consensus,
            score_threshold=0.1,
        )
        loop.set_peer_network(peer_network)

        assert loop.peer_network is peer_network

        # Tick should still work — peer broadcast will fail (no real HTTP)
        # but consensus (AutoQuorumConsensus) auto-approves
        result = asyncio.get_event_loop().run_until_complete(loop.tick())
        assert result.orders_processed == 1

    def test_17_leader_election_deterministic(self):
        """elect_leader picks highest-stake validator deterministically."""
        accts = _state.accts

        # Build PeerInfo list with fake stakes
        peers = [
            PeerInfo(uid=i, hotkey=f"hotkey_{i}", stake=float((i + 1) * 30),
                     evm_address=addr)
            for i, addr in enumerate(accts.validator_addrs)
        ]

        leader = elect_leader(peers)
        assert leader is not None
        # Highest stake = validator index 2 (stake=90)
        assert leader.stake == 90.0

        # Repeated calls return the same leader
        leader2 = elect_leader(peers)
        assert leader2.hotkey == leader.hotkey
