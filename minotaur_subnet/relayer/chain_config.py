"""Per-chain deployment configuration for the EVM relayer.

Extends the existing blockchain/chains.py patterns with relayer-specific
config: wallet addresses, gas settings, AppIntentBase contract addresses.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ChainDeployment:
    """Configuration for a single chain deployment."""
    chain_id: int
    name: str
    rpc_url: str
    app_intent_base_address: str = ""
    validator_registry_address: str = ""
    relayer_wallet: str = ""           # Relayer's EOA on this chain
    gas_price_gwei: float = 0.0        # 0 = use provider estimate
    max_gas_price_gwei: float = 100.0
    gas_buffer_pct: int = 20           # Extra gas estimation buffer
    confirmations: int = 1             # Blocks to wait for receipt


# Default chain configs loaded from environment
def get_supported_chains() -> dict[int, ChainDeployment]:
    """Build chain configs from environment variables.

    Env vars:
        ETHEREUM_RPC_URL, BASE_RPC_URL, ARBITRUM_RPC_URL, OPTIMISM_RPC_URL
        RELAYER_WALLET_{CHAIN_ID}  — relayer wallet address per chain
        APP_INTENT_BASE_{CHAIN_ID} — deployed AppIntentBase address
    """
    chains: dict[int, ChainDeployment] = {}

    chain_specs = [
        (1, "Ethereum", "ETHEREUM_RPC_URL"),
        (8453, "Base", "BASE_RPC_URL"),
        (42161, "Arbitrum", "ARBITRUM_RPC_URL"),
        (10, "Optimism", "OPTIMISM_RPC_URL"),
        (31337, "Anvil", "ANVIL_RPC_URL"),
        (964, "Bittensor EVM", "BITTENSOR_EVM_RPC_URL"),
    ]

    for chain_id, name, rpc_env in chain_specs:
        rpc_url = os.environ.get(rpc_env, "")
        if not rpc_url:
            continue

        chains[chain_id] = ChainDeployment(
            chain_id=chain_id,
            name=name,
            rpc_url=rpc_url,
            app_intent_base_address=os.environ.get(
                f"APP_INTENT_BASE_{chain_id}", "",
            ),
            validator_registry_address=os.environ.get(
                f"VALIDATOR_REGISTRY_{chain_id}", "",
            ),
            relayer_wallet=os.environ.get(
                f"RELAYER_WALLET_{chain_id}",
                os.environ.get("RELAYER_WALLET", ""),
            ),
        )

    return chains


# Well-known contract ABIs (subset for the relayer)
EXECUTE_INTENT_ABI = [
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
            {"name": "legIndex", "type": "uint256"},
            {"name": "userSignature", "type": "bytes"},
            {"name": "validatorSignatures", "type": "bytes[]"},
        ],
        "name": "executeCrossChainLeg",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
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
            {"name": "legIndex", "type": "uint256"},
            {"name": "userSignature", "type": "bytes"},
            {"name": "validatorSignatures", "type": "bytes[]"},
        ],
        "name": "executeLeg",
        "outputs": [],
        "stateMutability": "payable",
        "type": "function",
    },
]


# ABI for the shared ValidatorRegistry contract
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
        "inputs": [],
        "name": "getValidatorCount",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
]
