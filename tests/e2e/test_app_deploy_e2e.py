"""E2E tests for the App Intent deployment pipeline.

Tests the full deploy flow: ForgeCompiler -> DeployService -> on-chain contract.
Requires Anvil running on port 8545 (started by the ``anvil`` fixture).
"""

import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pytest
from eth_abi import encode as abi_encode
from eth_hash.auto import keccak
from web3 import Web3

from minotaur_subnet.deployment.compiler import ForgeCompiler
from minotaur_subnet.deployment.deployer import DeployService
from minotaur_subnet.relayer import EvmRelayer
from minotaur_subnet.relayer.chain_config import ChainDeployment
from minotaur_subnet.shared.types import AppIntentConfig, AppIntentDefinition

CONTRACTS_DIR = Path(__file__).resolve().parents[2] / "contracts"
CHAIN_ID = 31337
RPC_URL = "http://127.0.0.1:8545"

# Anvil deterministic accounts
DEPLOYER_KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
DEPLOYER_ADDR = "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"
VALIDATOR_1 = "0x70997970C51812dc3A010C7d01b50e0d17dc79C8"
VALIDATOR_2 = "0x3C44CdDdB6a900fa2b585dd299e03d12FA4293BC"
DEX_AGGREGATOR_SOURCE = (CONTRACTS_DIR / "src" / "DexAggregatorApp.sol").read_text()
DEX_SCORING_JS = (CONTRACTS_DIR / "src" / "dex_aggregator_scoring.js").read_text()
FEE_COLLECTOR = DEPLOYER_ADDR
FEE_BPS = 5000

# Minimal ABIs for reading deployed contract state
GET_VALIDATORS_ABI = [
    {
        "inputs": [],
        "name": "getValidators",
        "outputs": [{"name": "", "type": "address[]"}],
        "stateMutability": "view",
        "type": "function",
    },
]

REGISTERED_INTENTS_ABI = [
    {
        "inputs": [{"name": "", "type": "bytes4"}],
        "name": "registeredIntents",
        "outputs": [{"name": "", "type": "bool"}],
        "stateMutability": "view",
        "type": "function",
    },
]

SWAP_SELECTOR_ABI = [
    {
        "inputs": [],
        "name": "SWAP_SELECTOR",
        "outputs": [{"name": "", "type": "bytes4"}],
        "stateMutability": "view",
        "type": "function",
    },
]


@pytest.fixture(scope="module")
def compiler() -> ForgeCompiler:
    return ForgeCompiler(contracts_dir=CONTRACTS_DIR)


def _make_dex_aggregator_definition(app_id: str, name: str) -> AppIntentDefinition:
    return AppIntentDefinition(
        app_id=app_id,
        name=name,
        version="1.0.0",
        intent_type="swap",
        js_code=DEX_SCORING_JS,
        solidity_code=DEX_AGGREGATOR_SOURCE,
        constructor_args=[
            ("address", FEE_COLLECTOR),
            ("uint256", str(FEE_BPS)),
        ],
        config=AppIntentConfig(supported_chains=[CHAIN_ID]),
    )


@pytest.fixture(scope="module")
def relayer() -> EvmRelayer:
    chains = {
        CHAIN_ID: ChainDeployment(
            chain_id=CHAIN_ID,
            name="Anvil",
            rpc_url=RPC_URL,
            relayer_wallet=DEPLOYER_ADDR,
        ),
    }
    return EvmRelayer(chains=chains, private_key=DEPLOYER_KEY)


@pytest.fixture(scope="module")
def w3(anvil) -> Web3:
    return Web3(Web3.HTTPProvider(RPC_URL))


@pytest.fixture(scope="module")
def registry_address(anvil, w3) -> str:
    """Deploy a ValidatorRegistry on Anvil and return its address."""
    # Get ValidatorRegistry artifact from forge
    artifact_path = CONTRACTS_DIR / "out" / "ValidatorRegistry.sol" / "ValidatorRegistry.json"
    if not artifact_path.exists():
        subprocess.run(
            ["forge", "build"], cwd=str(CONTRACTS_DIR),
            capture_output=True, timeout=60,
        )
    with open(artifact_path) as f:
        artifact = json.load(f)

    bytecode = artifact["bytecode"]["object"]
    # ABI-encode constructor args: (address owner, address[] validators)
    validators = sorted(
        [VALIDATOR_1, VALIDATOR_2],
        key=lambda a: int(a, 16),
    )
    constructor_args = abi_encode(
        ["address", "address[]"],
        [Web3.to_checksum_address(DEPLOYER_ADDR),
         [Web3.to_checksum_address(v) for v in validators]],
    )

    tx = {
        "from": Web3.to_checksum_address(DEPLOYER_ADDR),
        "data": bytecode + constructor_args.hex(),
        "gas": 2_000_000,
        "gasPrice": w3.eth.gas_price,
        "nonce": w3.eth.get_transaction_count(DEPLOYER_ADDR),
        "chainId": CHAIN_ID,
    }
    signed = w3.eth.account.sign_transaction(tx, DEPLOYER_KEY)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=30)
    return receipt["contractAddress"]


# ── Compiler tests ────────────────────────────────────────────────────────


def test_compile_existing_dex_aggregator_app(compiler: ForgeCompiler):
    """ForgeCompiler extracts DexAggregatorApp bytecode from existing artifact."""
    result = compiler.extract_existing("DexAggregatorApp")
    assert result.error is None, f"Unexpected error: {result.error}"
    assert result.bytecode.startswith("0x")
    assert len(result.bytecode) > 100, "Bytecode too short"
    assert len(result.abi) > 0, "ABI should have entries"


def test_compile_generated_solidity(compiler: ForgeCompiler):
    """ForgeCompiler compiles new .sol source and extracts bytecode."""
    source = '''\
// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import "../AppIntentBase.sol";

contract TestGenerated is AppIntentBase {
    constructor(
        address _relayer,
        address _validatorRegistry,
        uint256 _quorumBps,
        uint256 _scoreThreshold
    ) AppIntentBase(_relayer, _validatorRegistry, _quorumBps, _scoreThreshold) {}

    function _checkIntent(
        IntentOrder calldata,
        ExecutionPlan calldata,
        address
    ) internal pure override returns (uint256 score, bool valid) {
        return (7500, true);
    }
}
'''
    result = compiler.compile("TestGenerated", source)
    assert result.error is None, f"Compilation failed: {result.error}"
    assert result.bytecode.startswith("0x")
    assert len(result.bytecode) > 100


# ── Deploy tests ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_deploy_dex_aggregator_app_to_anvil(
    anvil, compiler: ForgeCompiler, relayer: EvmRelayer, w3: Web3,
    registry_address: str,
):
    """Full pipeline: compile -> deploy -> real address on Anvil."""
    deploy_svc = DeployService(
        compiler=compiler,
        relayer=relayer,
        registry_address=registry_address,
    )

    app = _make_dex_aggregator_definition("test-deploy-swap", "DexAggregatorApp")

    result = await deploy_svc.deploy(app, CHAIN_ID)
    assert result.error is None, f"Deploy failed: {result.error}"
    assert result.status.value == "solving"
    assert result.contract_address is not None
    assert result.contract_address.startswith("0x")

    # Verify contract exists on-chain
    code = w3.eth.get_code(Web3.to_checksum_address(result.contract_address))
    assert len(code) > 0, "No code at deployed address"


@pytest.mark.asyncio
async def test_deployed_contract_has_validators(
    anvil, compiler: ForgeCompiler, relayer: EvmRelayer, w3: Web3,
    registry_address: str,
):
    """Call getValidators() on a freshly deployed contract."""
    deploy_svc = DeployService(
        compiler=compiler,
        relayer=relayer,
        registry_address=registry_address,
    )

    app = _make_dex_aggregator_definition(
        "test-deploy-validators", "DexAggregatorValidators"
    )

    result = await deploy_svc.deploy(app, CHAIN_ID)
    assert result.error is None

    contract = w3.eth.contract(
        address=Web3.to_checksum_address(result.contract_address),
        abi=GET_VALIDATORS_ABI,
    )
    on_chain_validators = contract.functions.getValidators().call()

    assert len(on_chain_validators) == 2
    expected = {
        Web3.to_checksum_address(VALIDATOR_1),
        Web3.to_checksum_address(VALIDATOR_2),
    }
    assert set(on_chain_validators) == expected


@pytest.mark.asyncio
async def test_deployed_contract_has_registered_intent(
    anvil, compiler: ForgeCompiler, relayer: EvmRelayer, w3: Web3,
    registry_address: str,
):
    """DexAggregatorApp auto-registers SWAP_SELECTOR in its constructor."""
    deploy_svc = DeployService(
        compiler=compiler,
        relayer=relayer,
        registry_address=registry_address,
    )

    app = _make_dex_aggregator_definition(
        "test-deploy-selector", "DexAggregatorSelector"
    )

    result = await deploy_svc.deploy(app, CHAIN_ID)
    assert result.error is None

    contract = w3.eth.contract(
        address=Web3.to_checksum_address(result.contract_address),
        abi=SWAP_SELECTOR_ABI + REGISTERED_INTENTS_ABI,
    )

    swap_selector = contract.functions.SWAP_SELECTOR().call()
    is_registered = contract.functions.registeredIntents(swap_selector).call()
    assert is_registered, "SWAP_SELECTOR should be registered in constructor"
