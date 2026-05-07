"""Shared fixtures for E2E tests against Anvil local chain.

Provides:
  - anvil: starts/stops an Anvil subprocess
  - deployed_contracts: deploys the canonical mock stack directly from artifacts
  - test_accounts: Anvil's deterministic keys
  - web3_client: Web3 connected to the local chain
  - eip712_domain: computed domain separator from deployed DexAggregatorApp
"""

import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import pytest
from web3 import Web3

# Ensure repo root is importable
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from minotaur_subnet.consensus.eip712 import (
    address_from_key,
    build_domain_separator,
)


# ── Anvil deterministic accounts ────────────────────────────────────────────

ANVIL_KEYS = [
    "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80",  # 0
    "0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d",  # 1
    "0x5de4111afa1a4b94908f83103eb1f1706367c2e68ca870fc3fb9a804cdab365a",  # 2
    "0x7c852118294e51e653712a81e05800f419141751be58f605c371e15141b007a6",  # 3
    "0x47e179ec197488593b187f80a00eb0da91f1b9d0b13f8733639f19c30a34926a",  # 4
    "0x8b3a350cf5c34c9194ca85829a2df0ec3153be0318b5e2d3348e872092edffba",  # 5
    "0x92db14e403b83dfe3df233f83dfa3a0d7096f21ca9b0d6d6b8d88b2b4ec1564e",  # 6
    "0x4bbbf85ce3377467afe5d46f804f221813b2bb87f24d81f60f1fcdbf7cbf4356",  # 7
    "0xdbda1821b80551c9d65939329250298aa3472ba22feea921c0cf5d620ea67b97",  # 8
    "0x2a871d0798f97d79848a013d4936a73bf4cc922c825d33c1cf7073dff6d409c6",  # 9
]

CHAIN_ID = 31337
CONTRACTS_DIR = Path(__file__).resolve().parents[2] / "contracts"
RPC_URL = "http://127.0.0.1:8545"


@dataclass
class TestAccounts:
    """Anvil deterministic accounts with role assignments."""
    __test__ = False
    deployer_key: str   # Account 0 — deploys contracts, acts as relayer
    user_key: str       # Account 1 — the "user" submitting orders
    validator_keys: list[str]  # Accounts 2-4 — validators

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
        """Validators sorted by address (ascending), as the contract requires."""
        pairs = list(zip(self.validator_addrs, self.validator_keys))
        pairs.sort(key=lambda p: int(p[0], 16))
        return pairs


@dataclass
class DeployedContracts:
    """Addresses of deployed test-stack contracts."""
    __test__ = False
    weth: str
    usdc: str
    router: str
    registry: str
    dex_app: str
    relayer: str
    domain_separator: bytes


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture(scope="session")
def test_accounts() -> TestAccounts:
    return TestAccounts(
        deployer_key=ANVIL_KEYS[0],
        user_key=ANVIL_KEYS[1],
        validator_keys=[ANVIL_KEYS[2], ANVIL_KEYS[3], ANVIL_KEYS[4]],
    )


@pytest.fixture(scope="session")
def anvil():
    """Start an Anvil instance for the test session."""
    proc = subprocess.Popen(
        [
            "anvil",
            "--host", "0.0.0.0",
            "--port", "8545",
            "--chain-id", str(CHAIN_ID),
            "--accounts", "10",
            "--silent",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    # Wait for Anvil to be ready
    w3 = Web3(Web3.HTTPProvider(RPC_URL))
    for _ in range(30):
        try:
            if w3.is_connected():
                break
        except Exception:
            pass
        time.sleep(0.2)
    else:
        proc.kill()
        raise RuntimeError("Anvil failed to start")

    # Expose RPC URL so EvmRelayer/DeployService can find Anvil
    os.environ["ANVIL_RPC_URL"] = RPC_URL

    yield proc

    # Teardown
    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


@pytest.fixture(scope="session")
def web3_client(anvil) -> Web3:
    """Web3 instance connected to Anvil."""
    return Web3(Web3.HTTPProvider(RPC_URL))


@pytest.fixture(scope="session")
def deployed_contracts(anvil, test_accounts) -> DeployedContracts:
    """Deploy the local E2E stack directly from compiled artifacts."""
    w3 = Web3(Web3.HTTPProvider(RPC_URL))
    sorted_vals = test_accounts.sorted_validators

    mock_token_abi, mock_token_bytecode = _load_artifact("MockToken")
    router_abi, router_bytecode = _load_artifact("TestSwapRouter")
    registry_abi, registry_bytecode = _load_artifact("ValidatorRegistry")
    dex_app_abi, dex_app_bytecode = _load_artifact("DexAggregatorApp")

    weth_addr = _deploy_contract(
        w3, test_accounts.deployer_key, mock_token_abi, mock_token_bytecode,
        "Wrapped ETH", "WETH", 18,
    )
    usdc_addr = _deploy_contract(
        w3, test_accounts.deployer_key, mock_token_abi, mock_token_bytecode,
        "USD Coin", "USDC", 6,
    )
    router_addr = _deploy_contract(
        w3, test_accounts.deployer_key, router_abi, router_bytecode,
    )
    registry_addr = _deploy_contract(
        w3,
        test_accounts.deployer_key,
        registry_abi,
        registry_bytecode,
        w3.to_checksum_address(test_accounts.deployer_addr),
        [w3.to_checksum_address(addr) for addr, _ in sorted_vals],
    )
    dex_app_addr = _deploy_contract(
        w3,
        test_accounts.deployer_key,
        dex_app_abi,
        dex_app_bytecode,
        w3.to_checksum_address(test_accounts.deployer_addr),
        registry_addr,
        8000,
        5000,
        w3.to_checksum_address(test_accounts.deployer_addr),
        5000,
    )

    usdc = w3.eth.contract(address=usdc_addr, abi=mock_token_abi)
    _send_tx(
        w3,
        usdc.functions.mint(router_addr, 10_000_000 * 10**6),
        test_accounts.deployer_key,
    )

    domain = build_domain_separator(CHAIN_ID, dex_app_addr)

    return DeployedContracts(
        weth=weth_addr,
        usdc=usdc_addr,
        router=router_addr,
        registry=registry_addr,
        dex_app=dex_app_addr,
        relayer=test_accounts.deployer_addr,
        domain_separator=domain,
    )


@pytest.fixture(scope="session")
def eip712_domain(deployed_contracts) -> bytes:
    """EIP-712 domain separator from deployed DexAggregatorApp."""
    return deployed_contracts.domain_separator


def _load_artifact(name: str) -> tuple[list, str]:
    artifact_path = CONTRACTS_DIR / "out" / f"{name}.sol" / f"{name}.json"
    with open(artifact_path) as f:
        artifact = json.load(f)
    bytecode = artifact["bytecode"]["object"]
    if not bytecode.startswith("0x"):
        bytecode = f"0x{bytecode}"
    return artifact["abi"], bytecode


def _deploy_contract(w3: Web3, deployer_key: str, abi: list, bytecode: str, *args) -> str:
    deployer_addr = w3.to_checksum_address(address_from_key(deployer_key))
    contract = w3.eth.contract(abi=abi, bytecode=bytecode)
    tx = contract.constructor(*args).build_transaction(
        {
            "from": deployer_addr,
            "nonce": w3.eth.get_transaction_count(deployer_addr),
            "gas": 5_000_000,
            "chainId": CHAIN_ID,
        }
    )
    signed = w3.eth.account.sign_transaction(tx, deployer_key)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=30)
    assert receipt["status"] == 1, f"Deploy failed for constructor args {args!r}"
    return receipt["contractAddress"]


def _send_tx(w3: Web3, fn, private_key: str) -> dict:
    addr = w3.to_checksum_address(address_from_key(private_key))
    tx = fn.build_transaction(
        {
            "from": addr,
            "nonce": w3.eth.get_transaction_count(addr),
            "gas": 500_000,
            "chainId": CHAIN_ID,
        }
    )
    signed = w3.eth.account.sign_transaction(tx, private_key)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    return w3.eth.wait_for_transaction_receipt(tx_hash, timeout=30)
