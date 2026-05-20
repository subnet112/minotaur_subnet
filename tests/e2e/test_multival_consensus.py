"""Phase C: Multi-Validator Consensus + ValidatorSync E2E tests.

Tests multi-validator EIP-712 signing, quorum enforcement, signature ordering,
and ValidatorSync bridge between metagraph and on-chain contracts.

Requires: Anvil (Foundry) for on-chain signature verification.
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

from minotaur_subnet.blockchain.chains import _web3_cache
from minotaur_subnet.consensus.eip712 import (
    address_from_key,
    build_domain_separator,
    sign_plan_approval_eip712,
    hash_plan_eip712,
)
from minotaur_subnet.consensus.manager import ConsensusManager
from minotaur_subnet.consensus.protocol_config import ProtocolConfig
from minotaur_subnet.consensus.peer_network import ValidatorPeerNetwork, PeerEndpoint
from minotaur_subnet.consensus.signatures import hash_plan, sign_plan_approval, verify_plan_approval
from minotaur_subnet.validator.metagraph_sync import PeerInfo, elect_leader
from minotaur_subnet.relayer.chain_config import ChainDeployment
from minotaur_subnet.relayer.evm_relayer import EvmRelayer
from minotaur_subnet.relayer.validator_sync import ValidatorSync
from minotaur_subnet.shared.types import (
    ConsensusResult,
    ExecutionPlan,
    Interaction,
    SignedApproval,
)
from tests.e2e.dex_test_helpers import (
    fund_and_approve_erc20,
    sign_dex_swap_order,
)

from conftest import ANVIL_KEYS, CHAIN_ID, RPC_URL

pytestmark = pytest.mark.skipif(
    not shutil.which("anvil"), reason="Foundry (anvil) required"
)

# Minimal ABI for reading DexAggregatorApp state
DEX_AGGREGATOR_APP_ABI_MINIMAL = [
    {"inputs": [], "name": "scoreThreshold", "outputs": [{"name": "", "type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "getValidators", "outputs": [{"name": "", "type": "address[]"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "getQuorumRequired", "outputs": [{"name": "", "type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"name": "", "type": "address"}], "name": "isValidator", "outputs": [{"name": "", "type": "bool"}], "stateMutability": "view", "type": "function"},
]

VALIDATOR_REGISTRY_ABI = [
    {
        "inputs": [{"name": "_validators", "type": "address[]"}],
        "name": "updateValidators",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "getValidators",
        "outputs": [{"name": "", "type": "address[]"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"name": "addr", "type": "address"}],
        "name": "isValidator",
        "outputs": [{"name": "", "type": "bool"}],
        "stateMutability": "view",
        "type": "function",
    },
]


# ── Fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def w3(anvil) -> Web3:
    """Web3 connected to Anvil."""
    w = Web3(Web3.HTTPProvider(RPC_URL))
    _web3_cache[CHAIN_ID] = w
    yield w
    _web3_cache.pop(CHAIN_ID, None)


@pytest.fixture(scope="module")
def validator_cluster(test_accounts, eip712_domain, deployed_contracts, w3):
    """Three-validator cluster with independent consensus managers."""
    dc = deployed_contracts
    accts = test_accounts
    score_threshold = w3.eth.contract(
        address=dc.dex_app, abi=DEX_AGGREGATOR_APP_ABI_MINIMAL,
    ).functions.scoreThreshold().call()

    all_addrs = accts.validator_addrs
    all_keys = accts.validator_keys

    # Share one ProtocolConfig across all three managers — mirrors production
    # where every off-chain component reads the same canonical value.
    shared_cfg = ProtocolConfig(quorum_bps=5000, rpc_url="", registry_address="")
    managers = []
    for key, addr in zip(all_keys, all_addrs):
        mgr = ConsensusManager(
            validator_id=addr,
            private_key=key,
            protocol_config=shared_cfg,  # 50% → ceil(1.5) = 2 of 3
            validators=all_addrs,
            timeout=5.0,
            chain_id=CHAIN_ID,
            contract_address=dc.dex_app,
            domain_separator=eip712_domain,
            score_threshold_bps=score_threshold,
        )
        managers.append(mgr)

    return {
        "managers": managers,
        "keys": all_keys,
        "addrs": all_addrs,
        "score_threshold": score_threshold,
        "domain": eip712_domain,
    }


@pytest.fixture
def test_plan():
    """A simple ExecutionPlan for testing."""
    return ExecutionPlan(
        intent_id="test-consensus",
        interactions=[
            Interaction(target="0x" + "11" * 20, value="0", call_data="0xdeadbeef"),
        ],
        deadline=int(time.time()) + 3600,
        nonce=42,
    )


# ── Tests ─────────────────────────────────────────────────────────────────


class TestMultiValidatorConsensus:
    """Multi-validator EIP-712 signing and quorum tests."""

    def test_three_validator_quorum(self, validator_cluster, test_plan):
        """3 validators sign, quorum reached (2/3 at 67%)."""
        cluster = validator_cluster
        managers = cluster["managers"]

        plan_hash = hash_plan(test_plan)
        order_id = "test-quorum-order-1"

        async def _run():
            # Validator 0 proposes (starts collect)
            propose_task = asyncio.create_task(
                managers[0].propose(order_id, test_plan, 0.85, plan_hash)
            )

            # Give the proposal time to register
            await asyncio.sleep(0.1)

            # Validator 1 signs and sends approval to validator 0
            approval_1 = managers[1].sign_approval(order_id, plan_hash, 0.85)
            result_from_receive = managers[0].receive_approval(approval_1)

            # Wait for propose to complete
            result = await propose_task
            return result, result_from_receive

        result, result_from_receive = asyncio.get_event_loop().run_until_complete(_run())

        # Either propose returned with quorum or receive_approval did
        assert result.reached or (result_from_receive is not None and result_from_receive.reached)

    def test_signatures_sorted_ascending(self, validator_cluster, test_plan):
        """Consensus result has signatures sorted by address ascending."""
        cluster = validator_cluster
        managers = cluster["managers"]
        plan_hash = hash_plan(test_plan)
        order_id = "test-sort-order"

        async def _run():
            propose_task = asyncio.create_task(
                managers[0].propose(order_id, test_plan, 0.85, plan_hash)
            )
            await asyncio.sleep(0.1)

            # Both other validators approve
            for i in [1, 2]:
                approval = managers[i].sign_approval(order_id, plan_hash, 0.85)
                managers[0].receive_approval(approval)

            return await propose_task

        result = asyncio.get_event_loop().run_until_complete(_run())
        assert result.reached

        # Check signatures are sorted by validator address
        addrs = [a.validator_id for a in result.approvals]
        addr_ints = [int(a.replace("0x", ""), 16) for a in addrs]
        assert addr_ints == sorted(addr_ints), "Approvals must be sorted by address ascending"

    def test_quorum_not_reached(self, validator_cluster, test_plan):
        """Only 1/3 signs → quorum not reached (timeout)."""
        cluster = validator_cluster
        managers = cluster["managers"]
        plan_hash = hash_plan(test_plan)
        order_id = "test-no-quorum"

        # Use a very short timeout
        managers[0].timeout = 0.3

        async def _run():
            result = await managers[0].propose(order_id, test_plan, 0.85, plan_hash)
            return result

        result = asyncio.get_event_loop().run_until_complete(_run())

        # Reset timeout
        managers[0].timeout = 5.0

        # Only 1 of 3 validators signed, need 2 → not reached
        assert result.reached is False
        assert result.collected == 1

    def test_invalid_validator_rejected(self, validator_cluster, test_plan):
        """Non-registered validator signature is ignored."""
        cluster = validator_cluster
        managers = cluster["managers"]
        plan_hash = hash_plan(test_plan)
        order_id = "test-invalid-val"

        # Create a fake validator
        fake_key = ANVIL_KEYS[9]  # Not in the validator set
        fake_addr = address_from_key(fake_key)

        fake_approval = SignedApproval(
            validator_id=fake_addr,
            order_id=order_id,
            plan_hash=plan_hash,
            score=0.85,
            signature=sign_plan_approval(
                fake_key, order_id, plan_hash, 0.85,
                domain_separator=cluster["domain"],
                score_bps=cluster["score_threshold"],
            ),
            timestamp=time.time(),
        )

        async def _run():
            propose_task = asyncio.create_task(
                managers[0].propose(order_id, test_plan, 0.85, plan_hash)
            )
            await asyncio.sleep(0.1)

            # Send fake approval — should be ignored
            result = managers[0].receive_approval(fake_approval)
            assert result is None  # Rejected

            # Send real approval from validator 1
            real_approval = managers[1].sign_approval(order_id, plan_hash, 0.85)
            managers[0].receive_approval(real_approval)

            return await propose_task

        result = asyncio.get_event_loop().run_until_complete(_run())
        assert result.reached

        # Only real validators should be in approvals
        val_ids = {a.validator_id.lower() for a in result.approvals}
        assert fake_addr.lower() not in val_ids


class TestPeerNetworkWiring:
    """Tests for ValidatorPeerNetwork integration with consensus."""

    def test_peer_network_excludes_self(self, validator_cluster):
        """PeerNetwork excludes the leader validator from its peer list."""
        cluster = validator_cluster
        leader_addr = cluster["addrs"][0]
        leader_key = cluster["keys"][0]

        all_peers = [
            PeerEndpoint(validator_id=addr, url=f"http://validator-{i}:9100")
            for i, addr in enumerate(cluster["addrs"])
        ]

        network = ValidatorPeerNetwork(
            validator_id=leader_addr,
            private_key=leader_key,
            consensus=cluster["managers"][0],
            peers=all_peers,
        )

        # Self should be excluded
        assert len(network.peers) == len(cluster["addrs"]) - 1
        peer_ids = {p.validator_id.lower() for p in network.peers}
        assert leader_addr.lower() not in peer_ids

    def test_leader_election_with_validator_addrs(self, validator_cluster):
        """elect_leader matches consensus manager's validator ordering."""
        cluster = validator_cluster
        addrs = cluster["addrs"]
        keys = cluster["keys"]

        peers = [
            PeerInfo(uid=i, hotkey=f"hotkey_{i}", stake=float(100 - i * 20),
                     evm_address=addr)
            for i, addr in enumerate(addrs)
        ]

        leader = elect_leader(peers)
        assert leader is not None
        assert leader.stake == 100.0
        assert leader.evm_address == addrs[0]

    def test_set_peers_updates_list(self, validator_cluster):
        """set_peers() dynamically updates the peer list."""
        cluster = validator_cluster
        leader_addr = cluster["addrs"][0]
        leader_key = cluster["keys"][0]

        network = ValidatorPeerNetwork(
            validator_id=leader_addr,
            private_key=leader_key,
            consensus=cluster["managers"][0],
        )

        assert len(network.peers) == 0

        new_peers = [
            PeerEndpoint(validator_id=cluster["addrs"][1], url="http://v1:9100"),
            PeerEndpoint(validator_id=cluster["addrs"][2], url="http://v2:9100"),
        ]
        network.set_peers(new_peers)
        assert len(network.peers) == 2


class TestValidatorSync:
    """ValidatorSync bridge between metagraph and on-chain contracts."""

    def test_validator_sync_manual(self, w3, deployed_contracts, test_accounts):
        """Manually set validators → sync to Anvil registry."""
        dc = deployed_contracts
        accts = test_accounts

        chain_config = ChainDeployment(
            chain_id=CHAIN_ID,
            name="Anvil",
            rpc_url=RPC_URL,
            app_intent_base_address=dc.dex_app,
            validator_registry_address=dc.registry,
            relayer_wallet=accts.deployer_addr,
        )

        relayer = EvmRelayer(
            chains={CHAIN_ID: chain_config},
            private_key=accts.deployer_key,
        )

        sync = ValidatorSync(
            chains={CHAIN_ID: chain_config},
            relayer=relayer,
        )

        # Set known validators
        new_validators = accts.validator_addrs[:2]  # Only first 2
        sync.set_validators(new_validators)

        # Trigger sync by changing the set
        sync._last_validators = []  # Reset
        sync.set_validators([])  # Will be picked up on next sync

        async def _run():
            # Direct update via relayer
            await relayer.sync_validators(CHAIN_ID, new_validators)

        asyncio.get_event_loop().run_until_complete(_run())

        # Verify on-chain via registry
        registry_contract = w3.eth.contract(
            address=dc.registry,
            abi=VALIDATOR_REGISTRY_ABI,
        )
        on_chain_validators = registry_contract.functions.getValidators().call()
        assert len(on_chain_validators) == 2

    def test_validator_set_update(self, w3, deployed_contracts, test_accounts):
        """Change validator set → registry reflects new set."""
        dc = deployed_contracts
        accts = test_accounts

        chain_config = ChainDeployment(
            chain_id=CHAIN_ID,
            name="Anvil",
            rpc_url=RPC_URL,
            app_intent_base_address=dc.dex_app,
            validator_registry_address=dc.registry,
            relayer_wallet=accts.deployer_addr,
        )
        relayer = EvmRelayer(
            chains={CHAIN_ID: chain_config},
            private_key=accts.deployer_key,
        )

        # Restore all 3 validators
        all_validators = accts.validator_addrs

        async def _run():
            await relayer.sync_validators(CHAIN_ID, all_validators)

        asyncio.get_event_loop().run_until_complete(_run())

        # Verify all 3 are registered via registry
        registry_contract = w3.eth.contract(
            address=dc.registry, abi=VALIDATOR_REGISTRY_ABI,
        )
        on_chain_validators = registry_contract.functions.getValidators().call()
        assert len(on_chain_validators) == 3

        for addr in all_validators:
            assert registry_contract.functions.isValidator(
                w3.to_checksum_address(addr)
            ).call() is True

    def test_full_pipeline_with_consensus(
        self, w3, deployed_contracts, test_accounts, eip712_domain,
    ):
        """Order → 3 validators sign → relayer executes → on-chain success."""
        dc = deployed_contracts
        accts = test_accounts

        # Get contract state
        app_contract = w3.eth.contract(address=dc.dex_app, abi=[
            {"inputs": [], "name": "SWAP_SELECTOR", "outputs": [{"name": "", "type": "bytes4"}], "stateMutability": "view", "type": "function"},
            {"inputs": [], "name": "scoreThreshold", "outputs": [{"name": "", "type": "uint256"}], "stateMutability": "view", "type": "function"},
            {"inputs": [{"name": "", "type": "address"}], "name": "nonces", "outputs": [{"name": "", "type": "uint256"}], "stateMutability": "view", "type": "function"},
            {"inputs": [{"name": "", "type": "bytes32"}], "name": "executedOrders", "outputs": [{"name": "", "type": "bool"}], "stateMutability": "view", "type": "function"},
            {
                "inputs": [
                    {"components": [{"name": "orderId", "type": "bytes32"}, {"name": "app", "type": "address"}, {"name": "intentSelector", "type": "bytes4"}, {"name": "intentParams", "type": "bytes"}, {"name": "submittedBy", "type": "address"}, {"name": "chainId", "type": "uint256"}, {"name": "deadline", "type": "uint256"}, {"name": "nonce", "type": "uint256"}, {"name": "perpetual", "type": "bool"}, {"name": "maxExecutions", "type": "uint256"}, {"name": "cooldown", "type": "uint256"}], "name": "order", "type": "tuple"},
                    {"components": [{"components": [{"name": "target", "type": "address"}, {"name": "value", "type": "uint256"}, {"name": "callData", "type": "bytes"}], "name": "calls", "type": "tuple[]"}, {"name": "deadline", "type": "uint256"}, {"name": "nonce", "type": "uint256"}, {"name": "metadata", "type": "bytes"}], "name": "plan", "type": "tuple"},
                    {"name": "userSignature", "type": "bytes"},
                    {"name": "validatorSignatures", "type": "bytes[]"},
                ],
                "name": "executeIntent",
                "outputs": [],
                "stateMutability": "payable",
                "type": "function",
            },
        ])

        score_threshold = app_contract.functions.scoreThreshold().call()
        user_nonce = app_contract.functions.nonces(accts.user_addr).call()

        order_id = keccak(b"test_full_pipeline_3val")
        fund_and_approve_erc20(
            w3,
            token_address=dc.weth,
            token_abi=[
                {"inputs": [{"name": "to", "type": "address"}, {"name": "amount", "type": "uint256"}], "name": "mint", "outputs": [], "stateMutability": "nonpayable", "type": "function"},
                {"inputs": [{"name": "spender", "type": "address"}, {"name": "amount", "type": "uint256"}], "name": "approve", "outputs": [{"name": "", "type": "bool"}], "stateMutability": "nonpayable", "type": "function"},
            ],
            recipient=accts.user_addr,
            spender=dc.dex_app,
            amount=10**18,
            funder_key=accts.deployer_key,
            owner_key=accts.user_key,
            chain_id=CHAIN_ID,
        )
        signed_order = sign_dex_swap_order(
            w3=w3,
            app_address=dc.dex_app,
            app_abi=[
                {"inputs": [], "name": "SWAP_SELECTOR", "outputs": [{"name": "", "type": "bytes4"}], "stateMutability": "view", "type": "function"},
            ],
            user_key=accts.user_key,
            submitted_by=accts.user_addr,
            domain_separator=eip712_domain,
            chain_id=CHAIN_ID,
            order_id_bytes=order_id,
            user_nonce=user_nonce,
            input_token=dc.weth,
            output_token=dc.usdc,
            input_amount=10**18,
            min_output_amount=1800 * 10**6,
            receiver=accts.user_addr,
        )

        # Build plan
        router_contract = w3.eth.contract(address=dc.router, abi=[
            {"inputs": [{"name": "outputToken", "type": "address"}, {"name": "outputAmount", "type": "uint256"}, {"name": "recipient", "type": "address"}], "name": "swapExact", "outputs": [], "stateMutability": "nonpayable", "type": "function"},
        ])
        swap_calldata = bytes.fromhex(router_contract.encode_abi(
            "swapExact", args=[dc.usdc, 1800 * 10**6, dc.dex_app],
        )[2:])

        plan_hash = hash_plan_eip712([(dc.router, 0, swap_calldata)], signed_order.deadline, 0)

        # All 3 validators sign (sorted by address)
        sorted_vals = accts.sorted_validators
        validator_sigs = [
            sign_plan_approval_eip712(key, order_id, plan_hash, score_threshold, eip712_domain)
            for _, key in sorted_vals
        ]

        # Execute
        order_tuple = (
            order_id,
            dc.dex_app,
            signed_order.swap_selector,
            signed_order.intent_params,
            accts.user_addr,
            CHAIN_ID,
            signed_order.deadline,
            user_nonce,
            False,
            1,
            0,
        )
        plan_tuple = ([(dc.router, 0, swap_calldata)], signed_order.deadline, 0, b"")

        tx = app_contract.functions.executeIntent(
            order_tuple,
            plan_tuple,
            signed_order.user_signature,
            validator_sigs,
        ).build_transaction({
            "from": accts.deployer_addr,
            "nonce": w3.eth.get_transaction_count(accts.deployer_addr),
            "gas": 1_000_000, "chainId": CHAIN_ID,
        })
        signed = w3.eth.account.sign_transaction(tx, accts.deployer_key)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=30)

        assert receipt["status"] == 1, f"Transaction reverted: {receipt}"
        assert app_contract.functions.executedOrders(order_id).call() is True
