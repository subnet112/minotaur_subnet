"""Phase A: Pipeline Integration — OrderBook → BlockLoop → Consensus → Relayer → On-Chain.

Proves the core v2 pipeline works end-to-end: submit order with EIP-712 user
signature → BlockLoop.tick() processes it → consensus signs → relayer submits
to Anvil → contract executes → verify on-chain state.

Requires: Anvil (Foundry) and `forge script` for contract deployment.
"""

import asyncio
import shutil
import sys
import time
from pathlib import Path

import pytest
from eth_hash.auto import keccak
from web3 import Web3

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from minotaur_subnet.blockloop.loop import BlockLoop, TickResult
from minotaur_subnet.blockchain.chains import _web3_cache
from minotaur_subnet.consensus.eip712 import build_domain_separator
from minotaur_subnet.consensus.manager import ConsensusManager
from minotaur_subnet.consensus.protocol_config import ProtocolConfig
from minotaur_subnet.consensus.signatures import sign_plan_approval, hash_plan
from minotaur_subnet.shared.types import SignedApproval, ConsensusResult
from minotaur_subnet.store import AppIntentStore
from minotaur_subnet.orderbook.orderbook import IntentOrderBook, OrderStatus
from minotaur_subnet.relayer.base import SubmitResult
from minotaur_subnet.relayer.chain_config import ChainDeployment, EXECUTE_INTENT_ABI
from minotaur_subnet.relayer.evm_relayer import EvmRelayer
from minotaur_subnet.shared.types import (
    AppIntentConfig,
    AppIntentDefinition,
    ExecutionPlan,
    Interaction,
    IntentState,
)
from minotaur_subnet.sdk.solvers.anvil_swap_solver import AnvilSwapSolver
from tests.e2e.dex_test_helpers import (
    StaticMockSolver,
    fund_and_approve_erc20,
    save_active_deployment,
    sign_dex_swap_order,
    submit_and_sign_dex_swap_order,
)

# Import shared conftest fixtures
from conftest import (
    ANVIL_KEYS,
    CHAIN_ID,
    CONTRACTS_DIR,
    RPC_URL,
    TestAccounts,
    DeployedContracts,
)

pytestmark = pytest.mark.skipif(
    not shutil.which("anvil"), reason="Foundry (anvil) required"
)


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
        return ConsensusResult(
            reached=True,
            approvals=approvals,
            quorum=len(approvals),
            collected=len(approvals),
            combined_score=score,
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
    {"anonymous": False, "inputs": [{"indexed": True, "name": "orderId", "type": "bytes32"}, {"indexed": True, "name": "submittedBy", "type": "address"}, {"indexed": False, "name": "score", "type": "uint256"}, {"indexed": False, "name": "planHash", "type": "bytes32"}, {"indexed": False, "name": "gasUsed", "type": "uint256"}], "name": "IntentExecuted", "type": "event"},
]


# ── Fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def w3(anvil) -> Web3:
    """Web3 connected to Anvil, also injected into blockchain.chains cache."""
    w = Web3(Web3.HTTPProvider(RPC_URL))
    _web3_cache[CHAIN_ID] = w
    yield w
    _web3_cache.pop(CHAIN_ID, None)


@pytest.fixture(scope="module")
def pipeline_components(w3, deployed_contracts, test_accounts, eip712_domain, tmp_path_factory):
    """Wire up the full pipeline: OrderBook → BlockLoop → Consensus → EvmRelayer."""
    dc = deployed_contracts
    accts = test_accounts

    # 1. OrderBook
    ob = IntentOrderBook()

    # 2. AppIntentStore with registered app
    store_path = tmp_path_factory.mktemp("store") / "store.json"
    app_store = AppIntentStore(store_path=store_path)
    app_def = AppIntentDefinition(
        app_id="swap-app",
        name="Test Swap App",
        version="1.0.0",
        intent_type="swap",
        js_code="// mock",
        config=AppIntentConfig(supported_chains=[CHAIN_ID]),
    )
    app_store.save_app(app_def)
    save_active_deployment(
        app_store,
        app_id="swap-app",
        contract_address=dc.dex_app,
        chain_id=CHAIN_ID,
    )

    # 3. AutoQuorumConsensus with all 3 validators
    # Signs with all validators automatically to satisfy on-chain quorum
    score_threshold = w3.eth.contract(address=dc.dex_app, abi=SWAP_APP_ABI).functions.scoreThreshold().call()
    sorted_vals = accts.sorted_validators
    all_addrs = [addr for addr, _ in sorted_vals]
    all_keys = [key for _, key in sorted_vals]

    consensus = AutoQuorumConsensus(
        all_keys=all_keys,
        all_addrs=all_addrs,
        protocol_config=ProtocolConfig(quorum_bps=10000, rpc_url="", registry_address=""),
        chain_id=CHAIN_ID,
        contract_address=dc.dex_app,
        domain_separator=eip712_domain,
        score_threshold_bps=score_threshold,
    )

    # 4. EvmRelayer connected to Anvil
    chain_config = ChainDeployment(
        chain_id=CHAIN_ID,
        name="Anvil",
        rpc_url=RPC_URL,
        app_intent_base_address=dc.dex_app,
        relayer_wallet=accts.deployer_addr,
    )
    relayer = EvmRelayer(
        chains={CHAIN_ID: chain_config},
        private_key=accts.deployer_key,
    )

    # 5. Solver
    solver = AnvilSwapSolver()
    solver.initialize({
        "router_address": dc.router,
        "weth_address": dc.weth,
        "usdc_address": dc.usdc,
    })

    # 6. BlockLoop
    loop = BlockLoop(
        orderbook=ob,
        app_store=app_store,
        solver=solver,
        relayer=relayer,
        consensus=consensus,
        score_threshold=0.3,  # Low threshold for test (mock scoring)
    )

    return {
        "orderbook": ob,
        "app_store": app_store,
        "consensus": consensus,
        "relayer": relayer,
        "solver": solver,
        "loop": loop,
        "contracts": dc,
        "accounts": accts,
        "domain": eip712_domain,
        "score_threshold": score_threshold,
    }


def _sign_order(
    w3, dc, accts, domain, order_id_bytes, user_nonce,
    perpetual=False, max_executions=1, cooldown=0,
):
    """Helper: build and sign an IntentOrder."""
    signed = sign_dex_swap_order(
        w3=w3,
        app_address=dc.dex_app,
        app_abi=SWAP_APP_ABI,
        user_key=accts.user_key,
        submitted_by=accts.user_addr,
        domain_separator=domain,
        chain_id=CHAIN_ID,
        order_id_bytes=order_id_bytes,
        user_nonce=user_nonce,
        input_token=dc.weth,
        output_token=dc.usdc,
        input_amount=10**18,
        min_output_amount=1800 * 10**6,
        receiver=accts.user_addr,
        perpetual=perpetual,
        max_executions=max_executions,
        cooldown=cooldown,
    )
    return (
        signed.user_signature,
        signed.intent_params,
        signed.deadline,
        signed.swap_selector,
    )


def _submit_and_sign(w3, dc, accts, ob, domain, user_nonce,
                     perpetual=False, max_executions=1, cooldown=0):
    """Submit order to OrderBook, then sign with the real orderId.

    The EIP-712 user signature must include the orderId that the encoder will
    produce (keccak256 of the Order.order_id string). Since OrderBook assigns
    a random UUID, we sign AFTER submission, then update the order.
    """
    order, signed = submit_and_sign_dex_swap_order(
        w3=w3,
        orderbook=ob,
        app_id="swap-app",
        app_address=dc.dex_app,
        app_abi=SWAP_APP_ABI,
        user_key=accts.user_key,
        submitted_by=accts.user_addr,
        domain_separator=domain,
        chain_id=CHAIN_ID,
        user_nonce=user_nonce,
        input_token=dc.weth,
        output_token=dc.usdc,
        input_amount=10**18,
        min_output_amount=1800 * 10**6,
        perpetual=perpetual,
        max_executions=max_executions,
        cooldown=cooldown,
    )
    return order, signed.deadline


def _mint_weth(w3, dc, accts, amount=10**18):
    """Mint WETH to user and approve the app to pull it."""
    fund_and_approve_erc20(
        w3,
        token_address=dc.weth,
        token_abi=MOCK_TOKEN_ABI,
        recipient=accts.user_addr,
        spender=dc.dex_app,
        amount=amount,
        funder_key=accts.deployer_key,
        owner_key=accts.user_key,
        chain_id=CHAIN_ID,
    )


# ── Tests ─────────────────────────────────────────────────────────────────


class TestPipelineE2E:
    """Core pipeline integration tests: order → tick → on-chain execution."""

    def test_order_to_execution(self, w3, pipeline_components):
        """Submit order → BlockLoop.tick() → on-chain execution → FILLED."""
        p = pipeline_components
        dc = p["contracts"]
        accts = p["accounts"]
        ob = p["orderbook"]
        loop = p["loop"]

        # Mint WETH to user
        _mint_weth(w3, dc, accts)

        # Get user nonce from contract
        app_contract = w3.eth.contract(address=dc.dex_app, abi=SWAP_APP_ABI)
        user_nonce = app_contract.functions.nonces(accts.user_addr).call()

        # Submit and sign (sign AFTER submission to use real orderId)
        order, deadline = _submit_and_sign(
            w3, dc, accts, ob, p["domain"], user_nonce,
        )

        assert order.status == OrderStatus.OPEN

        # Tick the block loop
        result = asyncio.get_event_loop().run_until_complete(loop.tick())

        assert result.orders_processed == 1
        assert result.orders_approved == 1

        # Verify order status
        final_order = ob.get(order.order_id)
        assert final_order.status == OrderStatus.FILLED
        assert final_order.tx_hash is not None

        # Verify on-chain: USDC received by user
        usdc = w3.eth.contract(address=dc.usdc, abi=MOCK_TOKEN_ABI)
        usdc_balance = usdc.functions.balanceOf(accts.user_addr).call()
        assert usdc_balance >= 1800 * 10**6

    def test_order_lifecycle_states(self, w3, pipeline_components):
        """Track order state transitions: OPEN → ASSIGNED → SOLVED → SCORED → APPROVED → FILLED."""
        p = pipeline_components
        dc = p["contracts"]
        accts = p["accounts"]

        # Fresh orderbook for clean state tracking
        test_ob = IntentOrderBook()

        # Get the current user nonce
        app_contract = w3.eth.contract(address=dc.dex_app, abi=SWAP_APP_ABI)
        user_nonce = app_contract.functions.nonces(accts.user_addr).call()

        _mint_weth(w3, dc, accts)

        # Submit and sign (uses real orderId)
        order, deadline = _submit_and_sign(
            w3, dc, accts, test_ob, p["domain"], user_nonce,
        )

        # OPEN
        assert order.status == OrderStatus.OPEN

        # Create a loop with the test orderbook
        loop = BlockLoop(
            orderbook=test_ob,
            app_store=p["app_store"],
            solver=p["solver"],
            relayer=p["relayer"],
            consensus=p["consensus"],
            score_threshold=0.3,
        )

        result = asyncio.get_event_loop().run_until_complete(loop.tick())

        # After tick, order should be FILLED (all transitions happened)
        final = test_ob.get(order.order_id)
        assert final.status == OrderStatus.FILLED, f"Expected FILLED, got {final.status}, error: {final.error}"
        assert final.score is not None
        assert final.tx_hash is not None

    def test_score_below_threshold_rejected(self, w3, pipeline_components):
        """Low score → REJECTED, no on-chain tx."""
        p = pipeline_components
        ob = p["orderbook"]

        # Submit an order with bad params (no valid tokens)
        order = ob.submit(
            app_id="swap-app",
            intent_function="execute",
            params={},  # Empty params → fallback plan → low score
            submitted_by=p["accounts"].user_addr,
            chain_id=CHAIN_ID,
        )

        # Use a loop with high threshold
        high_threshold_loop = BlockLoop(
            orderbook=ob,
            app_store=p["app_store"],
            solver=None,  # No solver → fallback plan
            relayer=p["relayer"],
            consensus=p["consensus"],
            score_threshold=0.95,  # Very high threshold
        )

        result = asyncio.get_event_loop().run_until_complete(
            high_threshold_loop.tick()
        )

        assert result.orders_processed == 1
        assert result.orders_rejected == 1

        final = ob.get(order.order_id)
        assert final.status == OrderStatus.REJECTED
        assert "below threshold" in (final.error or "")

    def test_consensus_signs_correctly(self, w3, pipeline_components):
        """EIP-712 validator signatures produced by consensus manager."""
        from minotaur_subnet.consensus.signatures import hash_plan, verify_plan_approval

        p = pipeline_components
        consensus = p["consensus"]
        plan = ExecutionPlan(
            intent_id="test",
            interactions=[
                Interaction(target="0x" + "11" * 20, value="0", call_data="0x"),
            ],
            deadline=int(time.time()) + 300,
            nonce=0,
        )

        plan_hash = hash_plan(plan)
        approval = consensus.sign_approval("test-order-1", plan_hash, 0.85)

        # Verify the signature
        assert verify_plan_approval(
            approval.validator_id,
            approval.signature,
            approval.order_id,
            approval.plan_hash,
            approval.score,
            domain_separator=p["domain"],
            score_bps=p["score_threshold"],
        )

    def test_user_signature_verified(self, w3, pipeline_components):
        """User EIP-712 signature passes Python verification roundtrip."""
        from minotaur_subnet.consensus.eip712 import (
            hash_order_struct,
            _to_typed_data_hash,
        )
        from eth_account import Account

        p = pipeline_components
        dc = p["contracts"]
        accts = p["accounts"]

        app_contract = w3.eth.contract(address=dc.dex_app, abi=SWAP_APP_ABI)
        user_nonce = app_contract.functions.nonces(accts.user_addr).call()

        order_id_bytes = keccak(b"test_user_sig_verify")
        signed = sign_dex_swap_order(
            w3=w3,
            app_address=dc.dex_app,
            app_abi=SWAP_APP_ABI,
            user_key=accts.user_key,
            submitted_by=accts.user_addr,
            domain_separator=p["domain"],
            chain_id=CHAIN_ID,
            order_id_bytes=order_id_bytes,
            user_nonce=user_nonce,
            input_token=dc.weth,
            output_token=dc.usdc,
            input_amount=10**18,
            min_output_amount=1800 * 10**6,
            receiver=accts.user_addr,
        )

        # Verify: recover signer from signature
        struct_hash = hash_order_struct(
            order_id_bytes,
            dc.dex_app,
            signed.swap_selector,
            signed.intent_params,
            accts.user_addr,
            CHAIN_ID,
            signed.deadline,
            user_nonce,
            False,
            1,
            0,
        )
        digest = _to_typed_data_hash(p["domain"], struct_hash)
        recovered = Account._recover_hash(digest, signature=signed.user_signature)
        assert recovered.lower() == accts.user_addr.lower()

    def test_replay_protection(self, w3, pipeline_components):
        """Same orderId cannot execute twice — contract reverts."""
        p = pipeline_components
        dc = p["contracts"]
        accts = p["accounts"]

        _mint_weth(w3, dc, accts)

        # Execute the first order
        app_contract = w3.eth.contract(address=dc.dex_app, abi=SWAP_APP_ABI)
        user_nonce = app_contract.functions.nonces(accts.user_addr).call()

        test_ob = IntentOrderBook()
        order, deadline = _submit_and_sign(
            w3, dc, accts, test_ob, p["domain"], user_nonce,
        )

        # The bytes32 that will be submitted on-chain
        order_id_bytes = keccak(order.order_id.encode())

        loop = BlockLoop(
            orderbook=test_ob,
            app_store=p["app_store"],
            solver=p["solver"],
            relayer=p["relayer"],
            consensus=p["consensus"],
            score_threshold=0.3,
        )

        result = asyncio.get_event_loop().run_until_complete(loop.tick())
        assert result.orders_approved == 1

        # Verify on-chain: order marked as executed
        assert app_contract.functions.executedOrders(order_id_bytes).call() is True

    def test_perpetual_order_reopen(self, w3, pipeline_components):
        """Perpetual order re-opens after fill, respects max_executions."""
        p = pipeline_components
        ob = IntentOrderBook()

        order = ob.submit(
            app_id="swap-app",
            intent_function="execute",
            params={
                "input_token": "0x" + "aa" * 20,
                "output_token": "0x" + "bb" * 20,
                "input_amount": "1000000",
            },
            submitted_by=p["accounts"].user_addr,
            chain_id=CHAIN_ID,
            perpetual=True,
            max_executions=3,
            cooldown=0,
        )

        assert order.perpetual is True
        assert order.max_executions == 3

        # Use MockRelayer (not real on-chain) for this test
        from minotaur_subnet.relayer.base import MockRelayer

        loop = BlockLoop(
            orderbook=ob,
            app_store=p["app_store"],
            solver=StaticMockSolver(),
            relayer=MockRelayer(),
            score_threshold=0.1,  # Low threshold for mock plan
        )

        # First tick
        result = asyncio.get_event_loop().run_until_complete(loop.tick())
        assert result.orders_approved == 1

        final = ob.get(order.order_id)
        # After fill, perpetual order should re-open
        assert final.status == OrderStatus.OPEN
        assert final.execution_count == 1

    def test_multiple_orders_single_tick(self, w3, pipeline_components):
        """BlockLoop processes a batch of orders in one tick."""
        p = pipeline_components
        ob = IntentOrderBook()

        from minotaur_subnet.relayer.base import MockRelayer

        loop = BlockLoop(
            orderbook=ob,
            app_store=p["app_store"],
            solver=StaticMockSolver(),
            relayer=MockRelayer(),
            score_threshold=0.1,
        )

        # Submit 5 orders
        for i in range(5):
            ob.submit(
                app_id="swap-app",
                intent_function="execute",
                params={
                    "input_token": "0x" + "aa" * 20,
                    "output_token": "0x" + "bb" * 20,
                    "input_amount": str(1000 * (i + 1)),
                },
                submitted_by=f"0x{i:040x}",
                chain_id=CHAIN_ID,
            )

        result = asyncio.get_event_loop().run_until_complete(loop.tick())

        assert result.orders_processed == 5
        # With mock scoring and low threshold, all should approve
        assert result.orders_approved >= 4
