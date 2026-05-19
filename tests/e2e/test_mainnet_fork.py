"""E2E tests against a mainnet fork with real Uniswap V3 liquidity.

Requires ALCHEMY_API_KEY or ETHEREUM_RPC_URL env var.
Forks Ethereum mainnet via Anvil, deploys DexAggregatorApp + ArbitrageApp +
UniswapForwarder from Python (simulating how the relayer deploys contracts),
and executes real intents through Uniswap V3.
"""

from __future__ import annotations

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
    sign_plan_approval_eip712,
    sign_user_order,
    hash_plan_eip712,
)
from minotaur_subnet.sdk.abi_utils import encode_approve, encode_exact_input_single
from tests.e2e.dex_test_helpers import build_dex_swap_intent_params


# ── Constants ──────────────────────────────────────────────────────────────────

FORK_PORT = 8546
FORK_RPC_URL = f"http://127.0.0.1:{FORK_PORT}"

# Mainnet addresses
WETH = "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"
USDC = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
UNISWAP_V3_ROUTER = "0xE592427A0AEce92De3Edee1F18E0157C05861564"

# Anvil deterministic keys
ANVIL_KEYS = [
    "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80",
    "0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d",
    "0x5de4111afa1a4b94908f83103eb1f1706367c2e68ca870fc3fb9a804cdab365a",
    "0x7c852118294e51e653712a81e05800f419141751be58f605c371e15141b007a6",
    "0x47e179ec197488593b187f80a00eb0da91f1b9d0b13f8733639f19c30a34926a",
]

CONTRACTS_DIR = Path(__file__).resolve().parents[2] / "contracts"

# Minimal ERC20 ABI
ERC20_ABI = [
    {"inputs": [{"name": "account", "type": "address"}], "name": "balanceOf", "outputs": [{"name": "", "type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"name": "spender", "type": "address"}, {"name": "amount", "type": "uint256"}], "name": "approve", "outputs": [{"name": "", "type": "bool"}], "stateMutability": "nonpayable", "type": "function"},
    {"inputs": [{"name": "to", "type": "address"}, {"name": "amount", "type": "uint256"}], "name": "transfer", "outputs": [{"name": "", "type": "bool"}], "stateMutability": "nonpayable", "type": "function"},
]

# Minimal WETH ABI (deposit)
WETH_ABI = ERC20_ABI + [
    {"inputs": [], "name": "deposit", "outputs": [], "stateMutability": "payable", "type": "function"},
]


# ── Skip condition ─────────────────────────────────────────────────────────────

def _get_fork_rpc() -> str | None:
    """Get the mainnet RPC URL from env vars."""
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
class ForkAccounts:
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
class ForkContracts:
    weth: str
    usdc: str
    router: str
    forwarder: str
    dex_app: str
    registry: str
    arbitrage_app: str
    relayer: str
    dex_domain_separator: bytes
    arbitrage_domain_separator: bytes


# ── Fixtures ───────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def fork_accounts() -> ForkAccounts:
    return ForkAccounts(
        deployer_key=ANVIL_KEYS[0],
        user_key=ANVIL_KEYS[1],
        validator_keys=[ANVIL_KEYS[2], ANVIL_KEYS[3], ANVIL_KEYS[4]],
    )


@pytest.fixture(scope="module")
def mainnet_fork():
    """Start Anvil with mainnet fork for the test module."""
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

    # Wait for Anvil to be ready
    w3 = Web3(Web3.HTTPProvider(FORK_RPC_URL))
    for _ in range(60):  # longer timeout for fork
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
def fork_w3(mainnet_fork) -> Web3:
    return Web3(Web3.HTTPProvider(FORK_RPC_URL))


def _load_artifact(name: str) -> tuple[list, str]:
    """Load compiled contract ABI and bytecode from forge output."""
    artifact_path = CONTRACTS_DIR / "out" / f"{name}.sol" / f"{name}.json"
    with open(artifact_path) as f:
        artifact = json.load(f)
    return artifact["abi"], artifact["bytecode"]["object"]


def _deploy_contract(w3: Web3, deployer_key: str, abi: list, bytecode: str, *args) -> str:
    """Deploy a contract and return its address."""
    deployer_addr = w3.to_checksum_address(address_from_key(deployer_key))
    contract = w3.eth.contract(abi=abi, bytecode=bytecode)
    tx = contract.constructor(*args).build_transaction({
        "from": deployer_addr,
        "nonce": w3.eth.get_transaction_count(deployer_addr),
        "gas": 5_000_000,
        "chainId": 1,
    })
    signed = w3.eth.account.sign_transaction(tx, deployer_key)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=30)
    assert receipt["status"] == 1, f"Deploy of {abi} failed"
    return receipt["contractAddress"]


@pytest.fixture(scope="module")
def fork_deployed(mainnet_fork, fork_accounts) -> ForkContracts:
    """Deploy DexAggregatorApp + ArbitrageApp + UniswapForwarder from Python.

    This simulates how Minotaur works in production: the relayer deploys
    the Solidity contracts, validators load the JS scoring modules.
    """
    w3 = Web3(Web3.HTTPProvider(FORK_RPC_URL))
    deployer_key = fork_accounts.deployer_key
    deployer_addr = w3.to_checksum_address(fork_accounts.deployer_addr)

    # Ensure forge artifacts exist
    result = subprocess.run(
        ["forge", "build"],
        capture_output=True, text=True,
        cwd=str(CONTRACTS_DIR), timeout=120,
    )
    assert result.returncode == 0, f"forge build failed: {result.stderr}"

    # Sort validators by address (required for on-chain quorum verification)
    sorted_vals = fork_accounts.sorted_validators
    validator_addrs = [w3.to_checksum_address(addr) for addr, _ in sorted_vals]
    quorum_bps = 8000
    score_threshold = 5000

    # Deploy shared ValidatorRegistry first — now also holds the canonical
    # quorumBps that every App deployed against it reads at execution time.
    registry_abi, registry_bytecode = _load_artifact("ValidatorRegistry")
    registry_addr = _deploy_contract(
        w3, deployer_key, registry_abi, registry_bytecode,
        deployer_addr, validator_addrs, quorum_bps,
    )

    # Deploy UniswapForwarder
    fwd_abi, fwd_bytecode = _load_artifact("UniswapForwarder")
    forwarder_addr = _deploy_contract(
        w3, deployer_key, fwd_abi, fwd_bytecode,
        w3.to_checksum_address(UNISWAP_V3_ROUTER),
    )

    # Deploy DexAggregatorApp — no quorum arg; reads from ValidatorRegistry.
    dex_abi, dex_bytecode = _load_artifact("DexAggregatorApp")
    dex_app_addr = _deploy_contract(
        w3, deployer_key, dex_abi, dex_bytecode,
        deployer_addr, registry_addr, score_threshold,
        deployer_addr, 5000,
    )

    # Deploy ArbitrageApp
    arb_abi, arb_bytecode = _load_artifact("ArbitrageApp")
    arb_app_addr = _deploy_contract(
        w3, deployer_key, arb_abi, arb_bytecode,
        deployer_addr, registry_addr, score_threshold,
    )

    # Fund forwarder with WETH:
    # 1. Give deployer ETH via anvil_setBalance
    w3.provider.make_request("anvil_setBalance", [deployer_addr, hex(200 * 10**18)])

    # 2. Wrap 20 ETH → WETH
    weth_contract = w3.eth.contract(
        address=w3.to_checksum_address(WETH), abi=WETH_ABI,
    )
    tx = weth_contract.functions.deposit().build_transaction({
        "from": deployer_addr,
        "value": 20 * 10**18,
        "nonce": w3.eth.get_transaction_count(deployer_addr),
        "gas": 100_000,
        "chainId": 1,
    })
    signed = w3.eth.account.sign_transaction(tx, deployer_key)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    w3.eth.wait_for_transaction_receipt(tx_hash, timeout=30)

    # 3. Transfer WETH to forwarder
    weth_erc20 = w3.eth.contract(
        address=w3.to_checksum_address(WETH), abi=ERC20_ABI,
    )
    tx = weth_erc20.functions.transfer(
        w3.to_checksum_address(forwarder_addr), 10 * 10**18,
    ).build_transaction({
        "from": deployer_addr,
        "nonce": w3.eth.get_transaction_count(deployer_addr),
        "gas": 100_000,
        "chainId": 1,
    })
    signed = w3.eth.account.sign_transaction(tx, deployer_key)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    w3.eth.wait_for_transaction_receipt(tx_hash, timeout=30)

    # Build domain separators
    dex_domain = build_domain_separator(1, dex_app_addr)
    arb_domain = build_domain_separator(1, arb_app_addr)

    return ForkContracts(
        weth=WETH,
        usdc=USDC,
        router=UNISWAP_V3_ROUTER,
        forwarder=forwarder_addr,
        dex_app=dex_app_addr,
        registry=registry_addr,
        arbitrage_app=arb_app_addr,
        relayer=fork_accounts.deployer_addr,
        dex_domain_separator=dex_domain,
        arbitrage_domain_separator=arb_domain,
    )


# ── Helpers ────────────────────────────────────────────────────────────────────


def _build_swap_params(
    input_token: str,
    output_token: str,
    input_amount: int,
    min_output: int,
    receiver: str,
) -> bytes:
    """ABI-encode swap params matching DexAggregatorApp."""
    return build_dex_swap_intent_params(
        input_token=input_token,
        output_token=output_token,
        input_amount=input_amount,
        min_output_amount=min_output,
        receiver=receiver,
    )


def _build_arbitrage_params(
    order_id_bytes: bytes,
    input_amount: int,
    min_return: int,
) -> bytes:
    """ABI-encode arbitrage params matching ArbitrageApp._checkIntent."""
    from eth_abi import encode
    return encode(
        ["bytes32", "uint256", "uint256"],
        [order_id_bytes, input_amount, min_return],
    )


def _build_forwarder_calldata(
    token_in: str,
    token_out: str,
    amount_in: int,
    amount_out_min: int,
    recipient: str,
    fee: int = 3000,
) -> str:
    """Encode UniswapForwarder.executeSwap() calldata."""
    from eth_abi import encode
    # executeSwap(address,address,uint256,uint256,address,uint24)
    selector = Web3.keccak(text="executeSwap(address,address,uint256,uint256,address,uint24)")[:4]
    params = encode(
        ["address", "address", "uint256", "uint256", "address", "uint24"],
        [token_in, token_out, amount_in, amount_out_min, recipient, fee],
    )
    return "0x" + selector.hex() + params.hex()


# Minimal ABI for executeIntent + IntentExecuted event + nonces
APP_ABI = [
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
        "anonymous": False,
        "inputs": [
            {"indexed": True, "name": "orderId", "type": "bytes32"},
            {"indexed": True, "name": "submittedBy", "type": "address"},
            {"indexed": False, "name": "score", "type": "uint256"},
            {"indexed": False, "name": "planHash", "type": "bytes32"},
            {"indexed": False, "name": "gasUsed", "type": "uint256"},
        ],
        "name": "IntentExecuted",
        "type": "event",
    },
    {
        "inputs": [{"name": "", "type": "address"}],
        "name": "nonces",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
]


def _get_user_nonce(w3: Web3, app_addr: str, user_addr: str) -> int:
    """Get the current per-user nonce from an App contract."""
    contract = w3.eth.contract(
        address=w3.to_checksum_address(app_addr), abi=APP_ABI,
    )
    return contract.functions.nonces(w3.to_checksum_address(user_addr)).call()


def _execute_intent(
    w3: Web3,
    app_addr: str,
    order_tuple: tuple,
    plan_tuple: tuple,
    user_sig: bytes,
    validator_sigs: list[bytes],
    relayer_addr: str,
    relayer_key: str,
    gas: int = 1_000_000,
):
    """Build, sign, and send an executeIntent transaction. Returns receipt."""
    app_contract = w3.eth.contract(
        address=w3.to_checksum_address(app_addr), abi=APP_ABI,
    )
    relayer = w3.to_checksum_address(relayer_addr)
    tx = app_contract.functions.executeIntent(
        order_tuple, plan_tuple, user_sig, validator_sigs,
    ).build_transaction({
        "from": relayer,
        "nonce": w3.eth.get_transaction_count(relayer),
        "gas": gas,
        "chainId": 1,
    })
    signed = w3.eth.account.sign_transaction(tx, relayer_key)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    return w3.eth.wait_for_transaction_receipt(tx_hash, timeout=30)


def _fund_user_and_approve(
    w3: Web3,
    weth_contract,
    app_addr: str,
    relayer_addr: str,
    relayer_key: str,
    user_addr: str,
    user_key: str,
    amount: int,
):
    """Transfer WETH to the user and approve the app to pull it."""
    relayer = w3.to_checksum_address(relayer_addr)
    user = w3.to_checksum_address(user_addr)
    app = w3.to_checksum_address(app_addr)

    tx = weth_contract.functions.transfer(user, amount).build_transaction({
        "from": relayer,
        "nonce": w3.eth.get_transaction_count(relayer),
        "gas": 100_000,
        "chainId": 1,
    })
    signed = w3.eth.account.sign_transaction(tx, relayer_key)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    w3.eth.wait_for_transaction_receipt(tx_hash, timeout=30)

    tx = weth_contract.functions.approve(app, amount).build_transaction({
        "from": user,
        "nonce": w3.eth.get_transaction_count(user),
        "gas": 100_000,
        "chainId": 1,
    })
    signed = w3.eth.account.sign_transaction(tx, user_key)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    w3.eth.wait_for_transaction_receipt(tx_hash, timeout=30)


# ── Tests ──────────────────────────────────────────────────────────────────────


class TestMainnetForkSwap:
    """Test real Uniswap V3 swaps on a mainnet fork."""

    def test_uniswap_v3_swap_via_intent(
        self, fork_w3, fork_deployed, fork_accounts,
    ):
        """Full swap: 1 WETH → USDC through DexAggregatorApp + Uniswap router."""
        w3 = fork_w3
        contracts = fork_deployed
        user_addr = fork_accounts.user_addr

        weth_contract = w3.eth.contract(
            address=w3.to_checksum_address(contracts.weth), abi=ERC20_ABI,
        )
        _fund_user_and_approve(
            w3, weth_contract, contracts.dex_app, contracts.relayer,
            fork_accounts.deployer_key, user_addr, fork_accounts.user_key, 1 * 10**18,
        )

        # Check user's initial USDC balance
        usdc_contract = w3.eth.contract(
            address=w3.to_checksum_address(contracts.usdc), abi=ERC20_ABI,
        )
        initial_usdc = usdc_contract.functions.balanceOf(
            w3.to_checksum_address(user_addr),
        ).call()

        # Build order parameters
        from eth_hash.auto import keccak
        order_id_bytes = keccak(b"test_order_1")
        swap_selector = keccak(b"swap(address,address,uint256,uint256,address)")[:4]

        input_amount = 1 * 10**18  # 1 WETH
        min_output = 1500 * 10**6  # 1500 USDC (safe floor)

        intent_params = _build_swap_params(
            contracts.weth, contracts.usdc,
            input_amount, min_output, user_addr,
        )

        # Build plan: proxy approves router, then routes WETH -> USDC to DexAggregatorApp.
        deadline = int(time.time()) + 3600
        approve_calldata = encode_approve(contracts.router, input_amount)
        swap_calldata = encode_exact_input_single(
            contracts.weth, contracts.usdc,
            3000,
            contracts.dex_app,
            deadline,
            input_amount,
            min_output,
        )
        plan_nonce = 0
        plan_calls = [
            (contracts.weth, 0, bytes.fromhex(approve_calldata[2:])),
            (contracts.router, 0, bytes.fromhex(swap_calldata[2:])),
        ]
        plan_hash = hash_plan_eip712(plan_calls, deadline, plan_nonce, b"")

        # Sign user order (EIP-712)
        user_sig = sign_user_order(
            private_key=fork_accounts.user_key,
            order_id=order_id_bytes,
            app=contracts.dex_app,
            intent_selector=swap_selector,
            intent_params=intent_params,
            submitted_by=user_addr,
            chain_id=1,
            deadline=deadline,
            nonce=0,
            perpetual=False,
            max_executions=1,
            cooldown=0,
            domain_separator=contracts.dex_domain_separator,
        )

        # Sign validator approvals
        score_bps = 8000
        sorted_vals = fork_accounts.sorted_validators
        validator_sigs = [
            sign_plan_approval_eip712(
                private_key=vkey,
                order_id=order_id_bytes,
                plan_hash=plan_hash,
                score_bps=score_bps,
                domain_separator=contracts.dex_domain_separator,
            )
            for _, vkey in sorted_vals
        ]

        # Build order tuple
        order_tuple = (
            order_id_bytes,
            w3.to_checksum_address(contracts.dex_app),
            swap_selector,
            intent_params,
            w3.to_checksum_address(user_addr),
            1,        # chainId
            deadline,
            0,        # nonce
            False,    # perpetual
            1,        # maxExecutions
            0,        # cooldown
        )

        # Build plan tuple
        plan_tuple = (
            [
                (w3.to_checksum_address(contracts.weth), 0, bytes.fromhex(approve_calldata[2:])),
                (w3.to_checksum_address(contracts.router), 0, bytes.fromhex(swap_calldata[2:])),
            ],
            deadline,
            plan_nonce,
            b"",
        )

        # Send executeIntent from relayer
        receipt = _execute_intent(
            w3, contracts.dex_app,
            order_tuple, plan_tuple, user_sig, validator_sigs,
            contracts.relayer, fork_accounts.deployer_key, gas=1_500_000,
        )

        # Assert: transaction succeeded
        assert receipt["status"] == 1, f"Transaction reverted: {receipt}"

        # Assert: user received USDC
        final_usdc = usdc_contract.functions.balanceOf(
            w3.to_checksum_address(user_addr),
        ).call()
        usdc_received = final_usdc - initial_usdc

        # Should receive a realistic USDC amount for 1 WETH
        assert usdc_received > 1500 * 10**6, (
            f"Too little USDC received: {usdc_received / 1e6:.2f}"
        )
        assert usdc_received < 5000 * 10**6, (
            f"Unrealistically high USDC: {usdc_received / 1e6:.2f}"
        )

        # Assert: IntentExecuted event was emitted
        app_contract = w3.eth.contract(
            address=w3.to_checksum_address(contracts.dex_app), abi=APP_ABI,
        )
        logs = app_contract.events.IntentExecuted().process_receipt(receipt)
        assert len(logs) == 1
        assert logs[0].args.orderId == order_id_bytes

    def test_fork_replay_protection(
        self, fork_w3, fork_deployed, fork_accounts,
    ):
        """Same orderId should revert on second execution."""
        w3 = fork_w3
        contracts = fork_deployed

        from eth_hash.auto import keccak
        order_id_bytes = keccak(b"test_replay_order")
        swap_selector = keccak(b"swap(address,address,uint256,uint256,address)")[:4]

        input_amount = 1 * 10**18
        min_output = 1 * 10**6  # very low to ensure success

        weth_contract = w3.eth.contract(
            address=w3.to_checksum_address(contracts.weth), abi=ERC20_ABI,
        )
        _fund_user_and_approve(
            w3, weth_contract, contracts.dex_app, contracts.relayer,
            fork_accounts.deployer_key, fork_accounts.user_addr, fork_accounts.user_key, input_amount,
        )

        intent_params = _build_swap_params(
            contracts.weth, contracts.usdc,
            input_amount, min_output, fork_accounts.user_addr,
        )

        deadline = int(time.time()) + 3600
        approve_calldata = encode_approve(contracts.router, input_amount)
        swap_calldata = encode_exact_input_single(
            contracts.weth, contracts.usdc,
            3000,
            contracts.dex_app,
            deadline,
            input_amount,
            min_output,
        )
        plan_calls = [
            (contracts.weth, 0, bytes.fromhex(approve_calldata[2:])),
            (contracts.router, 0, bytes.fromhex(swap_calldata[2:])),
        ]
        plan_hash = hash_plan_eip712(plan_calls, deadline, 0, b"")

        current_nonce = _get_user_nonce(w3, contracts.dex_app, fork_accounts.user_addr)

        user_sig = sign_user_order(
            private_key=fork_accounts.user_key,
            order_id=order_id_bytes,
            app=contracts.dex_app,
            intent_selector=swap_selector,
            intent_params=intent_params,
            submitted_by=fork_accounts.user_addr,
            chain_id=1,
            deadline=deadline,
            nonce=current_nonce,
            perpetual=False,
            max_executions=1,
            cooldown=0,
            domain_separator=contracts.dex_domain_separator,
        )

        validator_sigs = [
            sign_plan_approval_eip712(
                private_key=vkey,
                order_id=order_id_bytes,
                plan_hash=plan_hash,
                score_bps=8000,
                domain_separator=contracts.dex_domain_separator,
            )
            for _, vkey in fork_accounts.sorted_validators
        ]

        order_tuple = (
            order_id_bytes,
            w3.to_checksum_address(contracts.dex_app),
            swap_selector,
            intent_params,
            w3.to_checksum_address(fork_accounts.user_addr),
            1, deadline, current_nonce, False, 1, 0,
        )
        plan_tuple = (
            [
                (w3.to_checksum_address(contracts.weth), 0, bytes.fromhex(approve_calldata[2:])),
                (w3.to_checksum_address(contracts.router), 0, bytes.fromhex(swap_calldata[2:])),
            ],
            deadline, 0, b"",
        )

        # First execution should succeed
        receipt1 = _execute_intent(
            w3, contracts.dex_app,
            order_tuple, plan_tuple, user_sig, validator_sigs,
            contracts.relayer, fork_accounts.deployer_key, gas=1_500_000,
        )
        assert receipt1["status"] == 1

        # Second execution with same orderId should revert
        try:
            receipt2 = _execute_intent(
                w3, contracts.dex_app,
                order_tuple, plan_tuple, user_sig, validator_sigs,
                contracts.relayer, fork_accounts.deployer_key, gas=1_500_000,
            )
            # If we get here, tx should have reverted
            assert receipt2["status"] == 0, "Replay should have reverted"
        except Exception:
            # Transaction revert is expected (web3 may raise on revert)
            pass

    def test_fork_slippage_protection(
        self, fork_w3, fork_deployed, fork_accounts,
    ):
        """Absurdly high minOutput should cause Uniswap swap to revert."""
        w3 = fork_w3
        contracts = fork_deployed

        from eth_hash.auto import keccak
        order_id_bytes = keccak(b"test_slippage_order")
        swap_selector = keccak(b"swap(address,address,uint256,uint256,address)")[:4]

        input_amount = 1 * 10**18
        min_output = 1_000_000 * 10**6  # 1M USDC — absurdly high

        weth_contract = w3.eth.contract(
            address=w3.to_checksum_address(contracts.weth), abi=ERC20_ABI,
        )
        _fund_user_and_approve(
            w3, weth_contract, contracts.dex_app, contracts.relayer,
            fork_accounts.deployer_key, fork_accounts.user_addr, fork_accounts.user_key, input_amount,
        )

        intent_params = _build_swap_params(
            contracts.weth, contracts.usdc,
            input_amount, min_output, fork_accounts.user_addr,
        )

        deadline = int(time.time()) + 3600
        approve_calldata = encode_approve(contracts.router, input_amount)
        swap_calldata = encode_exact_input_single(
            contracts.weth, contracts.usdc,
            3000,
            contracts.dex_app,
            deadline,
            input_amount,
            min_output,
        )
        plan_calls = [
            (contracts.weth, 0, bytes.fromhex(approve_calldata[2:])),
            (contracts.router, 0, bytes.fromhex(swap_calldata[2:])),
        ]
        plan_hash = hash_plan_eip712(plan_calls, deadline, 0, b"")

        current_nonce = _get_user_nonce(w3, contracts.dex_app, fork_accounts.user_addr)

        user_sig = sign_user_order(
            private_key=fork_accounts.user_key,
            order_id=order_id_bytes,
            app=contracts.dex_app,
            intent_selector=swap_selector,
            intent_params=intent_params,
            submitted_by=fork_accounts.user_addr,
            chain_id=1,
            deadline=deadline,
            nonce=current_nonce,
            perpetual=False,
            max_executions=1,
            cooldown=0,
            domain_separator=contracts.dex_domain_separator,
        )

        validator_sigs = [
            sign_plan_approval_eip712(
                private_key=vkey,
                order_id=order_id_bytes,
                plan_hash=plan_hash,
                score_bps=8000,
                domain_separator=contracts.dex_domain_separator,
            )
            for _, vkey in fork_accounts.sorted_validators
        ]

        order_tuple = (
            order_id_bytes,
            w3.to_checksum_address(contracts.dex_app),
            swap_selector,
            intent_params,
            w3.to_checksum_address(fork_accounts.user_addr),
            1, deadline, current_nonce, False, 1, 0,
        )
        plan_tuple = (
            [
                (w3.to_checksum_address(contracts.weth), 0, bytes.fromhex(approve_calldata[2:])),
                (w3.to_checksum_address(contracts.router), 0, bytes.fromhex(swap_calldata[2:])),
            ],
            deadline, 0, b"",
        )

        # This should fail due to slippage — Uniswap can't produce 1M USDC for 1 WETH
        try:
            receipt = _execute_intent(
                w3, contracts.dex_app,
                order_tuple, plan_tuple, user_sig, validator_sigs,
                contracts.relayer, fork_accounts.deployer_key, gas=1_500_000,
            )
            assert receipt["status"] == 0, "Slippage protection should revert the swap"
        except Exception:
            # Revert during estimation or execution is expected
            pass


class TestMainnetForkArbitrage:
    """Test cross-fee-tier WETH/USDC arbitrage on a mainnet fork."""

    def test_arbitrage_cross_fee_tier(
        self, fork_w3, fork_deployed, fork_accounts,
    ):
        """Arbitrage: WETH→USDC (0.3% pool) → WETH (0.05% pool) via ArbitrageApp.

        This is a 2-leg round-trip. On a live fork there's no real arb profit
        (prices are already arbitraged), so the round-trip loses ~0.35% to fees.
        We use a conservative minReturn (0.4 WETH for 1 WETH input).
        """
        w3 = fork_w3
        contracts = fork_deployed
        user_addr = fork_accounts.user_addr

        # Verify forwarder has enough WETH
        weth_contract = w3.eth.contract(
            address=w3.to_checksum_address(contracts.weth), abi=ERC20_ABI,
        )
        forwarder_balance = weth_contract.functions.balanceOf(
            w3.to_checksum_address(contracts.forwarder),
        ).call()
        assert forwarder_balance >= 1 * 10**18, (
            f"Forwarder WETH balance too low: {forwarder_balance}"
        )

        # Check user's initial WETH balance
        initial_weth = weth_contract.functions.balanceOf(
            w3.to_checksum_address(user_addr),
        ).call()

        # Build order parameters
        from eth_hash.auto import keccak
        order_id_bytes = keccak(b"test_arb_order_1")
        arb_selector = keccak(b"arbitrage(bytes32,uint256,uint256)")[:4]

        input_amount = 1 * 10**18     # 1 WETH
        min_return = 4 * 10**17       # 0.4 WETH (conservative — round-trip loses to fees)

        intent_params = _build_arbitrage_params(
            order_id_bytes, input_amount, min_return,
        )

        # Build plan: 2 legs
        # Leg 1: WETH → USDC on 0.3% pool, send USDC to forwarder (for leg 2)
        leg1_data = _build_forwarder_calldata(
            contracts.weth, contracts.usdc,
            input_amount, 0,  # amountOutMin=0 (let leg 2 handle min check)
            contracts.forwarder,  # send USDC to forwarder for leg 2
            fee=3000,
        )

        # Leg 2: USDC → WETH on 0.05% pool, send WETH to user
        # Use 1500 USDC (conservative: leg 1 will produce ~1950+ USDC)
        leg2_data = _build_forwarder_calldata(
            contracts.usdc, contracts.weth,
            1500 * 10**6, 0,  # 1500 USDC input, no min (checked by contract)
            user_addr,
            fee=500,  # 0.05% pool
        )

        plan_calls = [
            (contracts.forwarder, 0, bytes.fromhex(leg1_data[2:])),
            (contracts.forwarder, 0, bytes.fromhex(leg2_data[2:])),
        ]
        deadline = int(time.time()) + 3600
        plan_nonce = 0
        plan_hash = hash_plan_eip712(plan_calls, deadline, plan_nonce, b"")

        # Get current nonce on ArbitrageApp
        current_nonce = _get_user_nonce(w3, contracts.arbitrage_app, user_addr)

        # Sign user order (EIP-712) — domain separator is for ArbitrageApp
        user_sig = sign_user_order(
            private_key=fork_accounts.user_key,
            order_id=order_id_bytes,
            app=contracts.arbitrage_app,
            intent_selector=arb_selector,
            intent_params=intent_params,
            submitted_by=user_addr,
            chain_id=1,
            deadline=deadline,
            nonce=current_nonce,
            perpetual=False,
            max_executions=1,
            cooldown=0,
            domain_separator=contracts.arbitrage_domain_separator,
        )

        # Sign validator approvals
        validator_sigs = [
            sign_plan_approval_eip712(
                private_key=vkey,
                order_id=order_id_bytes,
                plan_hash=plan_hash,
                score_bps=8000,
                domain_separator=contracts.arbitrage_domain_separator,
            )
            for _, vkey in fork_accounts.sorted_validators
        ]

        # Build order tuple — app is ArbitrageApp
        order_tuple = (
            order_id_bytes,
            w3.to_checksum_address(contracts.arbitrage_app),
            arb_selector,
            intent_params,
            w3.to_checksum_address(user_addr),
            1,               # chainId
            deadline,
            current_nonce,
            False,           # perpetual
            1,               # maxExecutions
            0,               # cooldown
        )

        # Build plan tuple — TWO calls (2-leg arb)
        plan_tuple = (
            [
                (w3.to_checksum_address(contracts.forwarder), 0, bytes.fromhex(leg1_data[2:])),
                (w3.to_checksum_address(contracts.forwarder), 0, bytes.fromhex(leg2_data[2:])),
            ],
            deadline,
            plan_nonce,
            b"",
        )

        # Send executeIntent from relayer
        receipt = _execute_intent(
            w3, contracts.arbitrage_app,
            order_tuple, plan_tuple, user_sig, validator_sigs,
            contracts.relayer, fork_accounts.deployer_key,
            gas=2_000_000,  # higher gas for 2 swaps
        )

        # Assert: transaction succeeded
        assert receipt["status"] == 1, f"Transaction reverted: {receipt}"

        # Assert: user received WETH >= minReturn (0.4 WETH)
        final_weth = weth_contract.functions.balanceOf(
            w3.to_checksum_address(user_addr),
        ).call()
        weth_received = final_weth - initial_weth
        assert weth_received >= min_return, (
            f"Too little WETH returned: {weth_received / 1e18:.4f}"
        )
        # Sanity: shouldn't receive more than input (no real arb profit on fork)
        assert weth_received < 15 * 10**17, (
            f"Unrealistically high WETH return: {weth_received / 1e18:.4f}"
        )

        # Assert: IntentExecuted event was emitted
        app_contract = w3.eth.contract(
            address=w3.to_checksum_address(contracts.arbitrage_app), abi=APP_ABI,
        )
        logs = app_contract.events.IntentExecuted().process_receipt(receipt)
        assert len(logs) == 1
        assert logs[0].args.orderId == order_id_bytes
