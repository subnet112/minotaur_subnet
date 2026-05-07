"""Direct on-chain E2E tests against Anvil.

Tests the Python EIP-712 signing against the real Solidity contracts:
  1. Mint tokens → construct order → sign → build plan → validator sign → execute
  2. Verify on-chain state changes (balances, nonces, executed flags)
  3. Replay protection
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pytest
from eth_hash.auto import keccak
from web3 import Web3

from minotaur_subnet.consensus.eip712 import (
    address_from_key,
    hash_plan_eip712,
    sign_plan_approval_eip712,
)
from tests.e2e.dex_test_helpers import (
    fund_and_approve_erc20,
    sign_dex_swap_order,
)

# Minimal ABIs for interacting with deployed contracts
MOCK_TOKEN_ABI = [
    {"inputs": [{"name": "to", "type": "address"}, {"name": "amount", "type": "uint256"}], "name": "mint", "outputs": [], "stateMutability": "nonpayable", "type": "function"},
    {"inputs": [{"name": "spender", "type": "address"}, {"name": "amount", "type": "uint256"}], "name": "approve", "outputs": [{"name": "", "type": "bool"}], "stateMutability": "nonpayable", "type": "function"},
    {"inputs": [{"name": "account", "type": "address"}], "name": "balanceOf", "outputs": [{"name": "", "type": "uint256"}], "stateMutability": "view", "type": "function"},
]

DEX_AGGREGATOR_APP_ABI = [
    {"inputs": [], "name": "SWAP_SELECTOR", "outputs": [{"name": "", "type": "bytes4"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "scoreThreshold", "outputs": [{"name": "", "type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"name": "", "type": "address"}], "name": "nonces", "outputs": [{"name": "", "type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"name": "", "type": "bytes32"}], "name": "executedOrders", "outputs": [{"name": "", "type": "bool"}], "stateMutability": "view", "type": "function"},
    {"anonymous": False, "inputs": [{"indexed": True, "name": "orderId", "type": "bytes32"}, {"indexed": True, "name": "submittedBy", "type": "address"}, {"indexed": False, "name": "score", "type": "uint256"}, {"indexed": False, "name": "planHash", "type": "bytes32"}, {"indexed": False, "name": "gasUsed", "type": "uint256"}], "name": "IntentExecuted", "type": "event"},
    {
        "inputs": [
            {
                "components": [
                    {"name": "orderId", "type": "bytes32"},
                    {"name": "app", "type": "address"},
                    {"name": "intentSelector", "type": "bytes4"},
                    {"name": "intentParams", "type": "bytes"},
                    {"name": "submittedBy", "type": "address"},
                    {"name": "chainId", "type": "uint256"},
                    {"name": "deadline", "type": "uint256"},
                    {"name": "nonce", "type": "uint256"},
                    {"name": "perpetual", "type": "bool"},
                    {"name": "maxExecutions", "type": "uint256"},
                    {"name": "cooldown", "type": "uint256"},
                ],
                "name": "order",
                "type": "tuple",
            },
            {
                "components": [
                    {
                        "components": [
                            {"name": "target", "type": "address"},
                            {"name": "value", "type": "uint256"},
                            {"name": "callData", "type": "bytes"},
                        ],
                        "name": "calls",
                        "type": "tuple[]",
                    },
                    {"name": "deadline", "type": "uint256"},
                    {"name": "nonce", "type": "uint256"},
                    {"name": "metadata", "type": "bytes"},
                ],
                "name": "plan",
                "type": "tuple",
            },
            {"name": "userSignature", "type": "bytes"},
            {"name": "validatorSignatures", "type": "bytes[]"},
        ],
        "name": "executeIntent",
        "outputs": [],
        "stateMutability": "payable",
        "type": "function",
    },
]

TEST_SWAP_ROUTER_ABI = [
    {"inputs": [{"name": "outputToken", "type": "address"}, {"name": "outputAmount", "type": "uint256"}, {"name": "recipient", "type": "address"}], "name": "swapExact", "outputs": [], "stateMutability": "nonpayable", "type": "function"},
]


class TestFullExecuteIntent:
    """Full executeIntent flow: mint → sign → plan → validate → execute."""

    def test_full_execute_intent(
        self, web3_client, deployed_contracts, test_accounts, eip712_domain,
    ):
        w3 = web3_client
        dc = deployed_contracts
        accts = test_accounts

        # Contract instances
        usdc = w3.eth.contract(address=dc.usdc, abi=MOCK_TOKEN_ABI)
        app = w3.eth.contract(address=dc.dex_app, abi=DEX_AGGREGATOR_APP_ABI)

        # 1. Mint WETH to user
        fund_and_approve_erc20(
            w3,
            token_address=dc.weth,
            token_abi=MOCK_TOKEN_ABI,
            recipient=accts.user_addr,
            spender=dc.dex_app,
            amount=10**18,
            funder_key=accts.deployer_key,
            owner_key=accts.user_key,
            chain_id=31337,
        )

        # 2. Construct IntentOrder
        order_id = keccak(b"test_order_e2e_1")
        score_threshold = app.functions.scoreThreshold().call()
        user_nonce = app.functions.nonces(accts.user_addr).call()
        signed = sign_dex_swap_order(
            w3=w3,
            app_address=dc.dex_app,
            app_abi=DEX_AGGREGATOR_APP_ABI,
            user_key=accts.user_key,
            submitted_by=accts.user_addr,
            domain_separator=eip712_domain,
            chain_id=31337,
            order_id_bytes=order_id,
            user_nonce=user_nonce,
            input_token=dc.weth,
            output_token=dc.usdc,
            input_amount=10**18,
            min_output_amount=1800 * 10**6,
            receiver=accts.user_addr,
        )

        # 4. Build execution plan: router.swapExact(usdc, 1800e6, app)
        router_contract = w3.eth.contract(address=dc.router, abi=TEST_SWAP_ROUTER_ABI)
        swap_calldata = router_contract.encode_abi(
            "swapExact", args=[dc.usdc, 1800 * 10**6, dc.dex_app],
        )
        swap_calldata_bytes = bytes.fromhex(swap_calldata[2:])

        plan_calls = [(dc.router, 0, swap_calldata_bytes)]
        plan_deadline = signed.deadline
        plan_nonce = 0
        plan_metadata = b""

        # 5. Compute plan hash
        plan_hash = hash_plan_eip712(plan_calls, plan_deadline, plan_nonce, plan_metadata)

        # 6. Sign validator approvals (sorted by address)
        sorted_vals = accts.sorted_validators
        validator_sigs = []
        for addr, key in sorted_vals:
            sig = sign_plan_approval_eip712(
                key, order_id, plan_hash, score_threshold, eip712_domain,
            )
            validator_sigs.append(sig)

        # 7. Build and send executeIntent tx
        order_tuple = (
            order_id,
            dc.dex_app,
            signed.swap_selector,
            signed.intent_params,
            accts.user_addr,
            31337,
            signed.deadline,
            user_nonce,
            False,  # perpetual
            1,      # maxExecutions
            0,      # cooldown
        )
        plan_tuple = (
            [(dc.router, 0, swap_calldata_bytes)],
            plan_deadline,
            plan_nonce,
            plan_metadata,
        )

        tx = app.functions.executeIntent(
            order_tuple,
            plan_tuple,
            signed.user_signature,
            validator_sigs,
        ).build_transaction({
            "from": accts.deployer_addr,
            "nonce": w3.eth.get_transaction_count(accts.deployer_addr),
            "gas": 1_000_000,
            "chainId": 31337,
        })

        signed_tx = w3.eth.account.sign_transaction(tx, accts.deployer_key)
        tx_hash = w3.eth.send_raw_transaction(signed_tx.raw_transaction)
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=30)

        # 8. Assertions
        assert receipt["status"] == 1, f"Transaction reverted: {receipt}"

        # User should have USDC
        usdc_balance = usdc.functions.balanceOf(accts.user_addr).call()
        assert usdc_balance >= 1800 * 10**6, f"Expected >= 1800 USDC, got {usdc_balance}"

        # Nonce should increment
        new_nonce = app.functions.nonces(accts.user_addr).call()
        assert new_nonce == user_nonce + 1

        # Order should be marked as executed
        assert app.functions.executedOrders(order_id).call() is True

        # Check IntentExecuted event (verify via raw logs if ABI parsing is flaky)
        intent_executed_topic = keccak(b"IntentExecuted(bytes32,address,uint256,bytes32,uint256)")
        event_logs = [l for l in receipt["logs"] if l["topics"][0] == intent_executed_topic]
        assert len(event_logs) >= 1, "IntentExecuted event not found"


class TestReplayProtection:
    """Same orderId should revert on second attempt."""

    def test_replay_reverts(
        self, web3_client, deployed_contracts, test_accounts, eip712_domain,
    ):
        w3 = web3_client
        dc = deployed_contracts
        accts = test_accounts

        app = w3.eth.contract(address=dc.dex_app, abi=DEX_AGGREGATOR_APP_ABI)
        score_threshold = app.functions.scoreThreshold().call()

        # Use a unique order ID
        order_id = keccak(b"replay_test_e2e")
        user_nonce = app.functions.nonces(accts.user_addr).call()

        fund_and_approve_erc20(
            w3,
            token_address=dc.weth,
            token_abi=MOCK_TOKEN_ABI,
            recipient=accts.user_addr,
            spender=dc.dex_app,
            amount=10**18,
            funder_key=accts.deployer_key,
            owner_key=accts.user_key,
            chain_id=31337,
        )
        signed = sign_dex_swap_order(
            w3=w3,
            app_address=dc.dex_app,
            app_abi=DEX_AGGREGATOR_APP_ABI,
            user_key=accts.user_key,
            submitted_by=accts.user_addr,
            domain_separator=eip712_domain,
            chain_id=31337,
            order_id_bytes=order_id,
            user_nonce=user_nonce,
            input_token=dc.weth,
            output_token=dc.usdc,
            input_amount=10**18,
            min_output_amount=1800 * 10**6,
            receiver=accts.user_addr,
        )

        # Build plan
        router_contract = w3.eth.contract(address=dc.router, abi=TEST_SWAP_ROUTER_ABI)
        swap_calldata = bytes.fromhex(router_contract.encode_abi(
            "swapExact", args=[dc.usdc, 1800 * 10**6, dc.dex_app],
        )[2:])

        plan_hash = hash_plan_eip712([(dc.router, 0, swap_calldata)], signed.deadline, 0)

        sorted_vals = accts.sorted_validators
        validator_sigs = [
            sign_plan_approval_eip712(key, order_id, plan_hash, score_threshold, eip712_domain)
            for _, key in sorted_vals
        ]

        order_tuple = (
            order_id,
            dc.dex_app,
            signed.swap_selector,
            signed.intent_params,
            accts.user_addr,
            31337,
            signed.deadline,
            user_nonce,
            False,
            1,
            0,
        )
        plan_tuple = ([(dc.router, 0, swap_calldata)], signed.deadline, 0, b"")

        # First execution
        receipt = _execute_intent(
            w3,
            app,
            order_tuple,
            plan_tuple,
            signed.user_signature,
            validator_sigs,
            accts.deployer_key,
        )
        assert receipt["status"] == 1

        # Second execution with same orderId should fail
        # Re-sign with updated nonce
        new_nonce = app.functions.nonces(accts.user_addr).call()
        order_tuple2 = (
            order_id,
            dc.dex_app,
            signed.swap_selector,
            signed.intent_params,
            accts.user_addr,
            31337,
            signed.deadline,
            new_nonce,
            False,
            1,
            0,
        )
        signed2 = sign_dex_swap_order(
            w3=w3,
            app_address=dc.dex_app,
            app_abi=DEX_AGGREGATOR_APP_ABI,
            user_key=accts.user_key,
            submitted_by=accts.user_addr,
            domain_separator=eip712_domain,
            chain_id=31337,
            order_id_bytes=order_id,
            user_nonce=new_nonce,
            input_token=dc.weth,
            output_token=dc.usdc,
            input_amount=10**18,
            min_output_amount=1800 * 10**6,
            receiver=accts.user_addr,
        )

        receipt2 = _execute_intent(
            w3,
            app,
            order_tuple2,
            plan_tuple,
            signed2.user_signature,
            validator_sigs,
            accts.deployer_key,
        )
        assert receipt2["status"] == 0, "Replay tx should revert"


def _execute_intent(w3, app, order_tuple, plan_tuple, user_sig, validator_sigs, relayer_key):
    """Send executeIntent transaction."""
    relayer_addr = address_from_key(relayer_key)
    tx = app.functions.executeIntent(
        order_tuple, plan_tuple, user_sig, validator_sigs,
    ).build_transaction({
        "from": relayer_addr,
        "nonce": w3.eth.get_transaction_count(relayer_addr),
        "gas": 1_000_000,
        "chainId": 31337,
    })
    signed = w3.eth.account.sign_transaction(tx, relayer_key)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    return w3.eth.wait_for_transaction_receipt(tx_hash, timeout=30)
