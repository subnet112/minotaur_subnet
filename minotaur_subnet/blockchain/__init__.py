"""
Blockchain interaction layer for the App Intents system.

Provides wallet management, smart contract deployment/interaction,
chain configuration, and token utilities.
"""

from minotaur_subnet.blockchain.wallet import (
    WalletManager,
    LocalWalletManager,
    LitProtocolWalletManager,
)
from minotaur_subnet.blockchain.bittensor_proxy_executor import (
    BittensorProxyExecutor,
    ProxyVerificationResult,
)
from minotaur_subnet.blockchain.contracts import ContractManager
from minotaur_subnet.blockchain.chains import CHAIN_CONFIG, get_web3, get_chain_name
from minotaur_subnet.blockchain.tokens import (
    TOKENS,
    get_token_address,
    get_erc20_balance,
    ERC20_ABI,
)

__all__ = [
    # Wallet
    "WalletManager",
    "LocalWalletManager",
    "LitProtocolWalletManager",
    "BittensorProxyExecutor",
    "ProxyVerificationResult",
    # Contracts
    "ContractManager",
    # Chains
    "CHAIN_CONFIG",
    "get_web3",
    "get_chain_name",
    # Tokens
    "TOKENS",
    "get_token_address",
    "get_erc20_balance",
    "ERC20_ABI",
]
