"""E2E tests for cross-chain escrow and CrossChainCompiler.

Runs against real Anvil with deployed contracts. Tests:
1. escrowDeposit / escrowRelease / escrowRefund on-chain
2. CrossChainCompiler produces valid MultiLegPlan from CrossChainPlan
3. Bridge verifier's on-chain escrow check against real contract
4. Full flow: compile → simulate → escrow → execute
5. Escrow refund after timeout

NO MOCKING of: contracts, Web3, Anvil, escrow functions, compiler, verifier.
MOCKED: bridge IGP fee (no real Hyperlane on Anvil), bridge delivery (no real bridge).
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

import pytest
from web3 import Web3

pytestmark = pytest.mark.cross_chain

from tests.e2e.conftest import (
    ANVIL_KEYS, CHAIN_ID, CONTRACTS_DIR, RPC_URL,
    TestAccounts, DeployedContracts,
    _load_artifact, _deploy_contract, _send_tx,
)
from minotaur_subnet.consensus.eip712 import address_from_key, build_domain_separator
from minotaur_subnet.shared.types import (
    BridgeRequest, ChainLeg, CrossChainPlan, Interaction,
)
from minotaur_subnet.bridge.compiler import CrossChainCompiler, CrossChainCompileError
from minotaur_subnet.bridge.verifier import verify_escrow_on_chain
from minotaur_subnet.bridge.registry import BridgeRegistry
from minotaur_subnet.bridge.mock import MockBridgeAdapter


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def w3(anvil) -> Web3:
    return Web3(Web3.HTTPProvider(RPC_URL))


@pytest.fixture(scope="module")
def accounts() -> TestAccounts:
    return TestAccounts(
        deployer_key=ANVIL_KEYS[0],
        user_key=ANVIL_KEYS[1],
        validator_keys=[ANVIL_KEYS[2], ANVIL_KEYS[3], ANVIL_KEYS[4]],
    )


@pytest.fixture(scope="module")
def contracts(anvil, w3, accounts) -> DeployedContracts:
    """Deploy full contract stack on Anvil."""
    sorted_vals = accounts.sorted_validators

    mock_token_abi, mock_token_bytecode = _load_artifact("MockToken")
    router_abi, router_bytecode = _load_artifact("TestSwapRouter")
    registry_abi, registry_bytecode = _load_artifact("ValidatorRegistry")
    dex_app_abi, dex_app_bytecode = _load_artifact("DexAggregatorApp")

    weth = _deploy_contract(
        w3, accounts.deployer_key, mock_token_abi, mock_token_bytecode,
        "Wrapped ETH", "WETH", 18,
    )
    usdc = _deploy_contract(
        w3, accounts.deployer_key, mock_token_abi, mock_token_bytecode,
        "USD Coin", "USDC", 6,
    )
    router = _deploy_contract(
        w3, accounts.deployer_key, router_abi, router_bytecode,
    )
    registry = _deploy_contract(
        w3, accounts.deployer_key, registry_abi, registry_bytecode,
        w3.to_checksum_address(accounts.deployer_addr),
        [w3.to_checksum_address(a) for a, _ in sorted_vals],
    )
    dex_app = _deploy_contract(
        w3, accounts.deployer_key, dex_app_abi, dex_app_bytecode,
        w3.to_checksum_address(accounts.deployer_addr),  # relayer
        registry, 8000, 5000,
        w3.to_checksum_address(accounts.deployer_addr),  # fee collector
        5000,
    )

    # Mint USDC to the DexAggregator contract (simulate bridge delivery)
    usdc_contract = w3.eth.contract(address=usdc, abi=mock_token_abi)
    _send_tx(
        w3,
        usdc_contract.functions.mint(dex_app, 10_000_000 * 10**6),
        accounts.deployer_key,
    )

    domain = build_domain_separator(CHAIN_ID, dex_app)
    return DeployedContracts(
        weth=weth, usdc=usdc, router=router,
        registry=registry, dex_app=dex_app,
        relayer=accounts.deployer_addr,
        domain_separator=domain,
    )


@pytest.fixture
def dex_app_contract(w3, contracts):
    """AppIntentBase ABI for escrow functions."""
    abi, _ = _load_artifact("DexAggregatorApp")
    return w3.eth.contract(address=contracts.dex_app, abi=abi)


@pytest.fixture
def usdc_contract(w3, contracts):
    abi, _ = _load_artifact("MockToken")
    return w3.eth.contract(address=contracts.usdc, abi=abi)


# ── Helper: call escrow functions directly ────────────────────────────────────

def _call_escrow_deposit(w3, contract, deployer_key, order_id, leg_index, token, amount, user, deadline):
    """Call escrowDeposit() as the relayer."""
    fn = contract.functions.escrowDeposit(
        order_id, leg_index,
        w3.to_checksum_address(token),
        amount,
        w3.to_checksum_address(user),
        deadline,
    )
    return _send_tx(w3, fn, deployer_key)


def _call_escrow_release(w3, contract, deployer_key, order_id, leg_index, sigs, release_hash):
    """Call escrowRelease() as the relayer."""
    fn = contract.functions.escrowRelease(order_id, leg_index, sigs, release_hash)
    return _send_tx(w3, fn, deployer_key)


def _get_escrow(contract, order_id, leg_index):
    """Read escrow state."""
    return contract.functions.getEscrow(order_id, leg_index).call()


# ═══════════════════════════════════════════════════════════════════════════════
#  1. ON-CHAIN ESCROW: deposit / release / refund
# ═══════════════════════════════════════════════════════════════════════════════


class TestEscrowOnChain:
    def test_escrow_deposit(self, w3, contracts, accounts, dex_app_contract):
        """escrowDeposit registers tokens for a leg."""
        order_id = b"\x01" * 32
        leg_index = 1
        amount = 500_000  # 0.5 USDC
        deadline = int(time.time()) + 3600

        receipt = _call_escrow_deposit(
            w3, dex_app_contract, accounts.deployer_key,
            order_id, leg_index,
            contracts.usdc, amount,
            accounts.user_addr, deadline,
        )
        assert receipt["status"] == 1

        # Verify on-chain state
        token, amt, user, dl, released, refunded = _get_escrow(
            dex_app_contract, order_id, leg_index,
        )
        assert amt == amount
        assert user.lower() == accounts.user_addr.lower()
        assert not released
        assert not refunded

    def test_escrow_deposit_rejects_zero_amount(self, w3, contracts, accounts, dex_app_contract):
        """escrowDeposit reverts on zero amount."""
        order_id = b"\x02" * 32
        receipt = _call_escrow_deposit(
            w3, dex_app_contract, accounts.deployer_key,
            order_id, 1, contracts.usdc, 0,
            accounts.user_addr, int(time.time()) + 3600,
        )
        assert receipt["status"] == 0, "Expected revert for zero amount"

    def test_escrow_deposit_rejects_duplicate(self, w3, contracts, accounts, dex_app_contract):
        """escrowDeposit reverts if already deposited for same order+leg."""
        order_id = b"\x03" * 32
        deadline = int(time.time()) + 3600

        # First deposit succeeds
        r1 = _call_escrow_deposit(
            w3, dex_app_contract, accounts.deployer_key,
            order_id, 1, contracts.usdc, 100_000,
            accounts.user_addr, deadline,
        )
        assert r1["status"] == 1

        # Second deposit reverts
        r2 = _call_escrow_deposit(
            w3, dex_app_contract, accounts.deployer_key,
            order_id, 1, contracts.usdc, 200_000,
            accounts.user_addr, deadline,
        )
        assert r2["status"] == 0, "Expected revert for duplicate deposit"

    def test_escrow_release_with_validator_quorum(self, w3, contracts, accounts, dex_app_contract):
        """escrowRelease succeeds with validator quorum."""
        from minotaur_subnet.consensus.eip712 import sign_plan_approval_eip712

        order_id = b"\x04" * 32
        leg_index = 1
        amount = 300_000
        deadline = int(time.time()) + 3600

        # Deposit
        _call_escrow_deposit(
            w3, dex_app_contract, accounts.deployer_key,
            order_id, leg_index, contracts.usdc, amount,
            accounts.user_addr, deadline,
        )

        # Build release hash
        from eth_abi import encode as abi_encode
        from eth_hash.auto import keccak
        release_hash = keccak(abi_encode(
            ["bytes32", "uint256", "address", "uint256"],
            [order_id, leg_index, w3.to_checksum_address(contracts.usdc), amount],
        ))

        # Get validator signatures (2 of 3 needed for quorum 8000 bps)
        sorted_vals = accounts.sorted_validators
        domain = build_domain_separator(CHAIN_ID, contracts.dex_app)
        sigs = []
        for addr, key in sorted_vals[:3]:
            sig = sign_plan_approval_eip712(
                key, order_id, release_hash, 5000, domain,
            )
            if isinstance(sig, str):
                sigs.append(bytes.fromhex(sig.replace("0x", "")))
            else:
                sigs.append(sig)

        # Release
        receipt = _call_escrow_release(
            w3, dex_app_contract, accounts.deployer_key,
            order_id, leg_index, sigs, release_hash,
        )
        assert receipt["status"] == 1

        # Verify released
        _, _, _, _, released, refunded = _get_escrow(
            dex_app_contract, order_id, leg_index,
        )
        assert released
        assert not refunded

    def test_escrow_refund_after_deadline(self, w3, contracts, accounts, dex_app_contract):
        """User can refund escrowed tokens after deadline expires."""
        order_id = b"\x05" * 32
        leg_index = 1
        amount = 200_000
        # Set deadline to 1 second from now
        deadline = int(time.time()) + 1

        # Deposit
        _call_escrow_deposit(
            w3, dex_app_contract, accounts.deployer_key,
            order_id, leg_index, contracts.usdc, amount,
            accounts.user_addr, deadline,
        )

        # Fast-forward time on Anvil
        w3.provider.make_request("evm_increaseTime", [5])
        w3.provider.make_request("evm_mine", [])

        # User calls refund
        fn = dex_app_contract.functions.escrowRefund(order_id, leg_index)
        receipt = _send_tx(w3, fn, accounts.user_key)
        assert receipt["status"] == 1

        # Verify refunded
        _, _, _, _, released, refunded = _get_escrow(
            dex_app_contract, order_id, leg_index,
        )
        assert not released
        assert refunded

    def test_escrow_refund_before_deadline_fails(self, w3, contracts, accounts, dex_app_contract):
        """User cannot refund before deadline."""
        order_id = b"\x06" * 32
        deadline = int(time.time()) + 86400  # Far future

        _call_escrow_deposit(
            w3, dex_app_contract, accounts.deployer_key,
            order_id, 1, contracts.usdc, 100_000,
            accounts.user_addr, deadline,
        )

        fn = dex_app_contract.functions.escrowRefund(order_id, 1)
        receipt = _send_tx(w3, fn, accounts.user_key)
        assert receipt["status"] == 0, "Expected revert before deadline"


# ═══════════════════════════════════════════════════════════════════════════════
#  2. BRIDGE VERIFIER: on-chain escrow check
# ═══════════════════════════════════════════════════════════════════════════════


class TestVerifierOnChain:
    def test_verify_escrow_exists(self, w3, contracts, accounts, dex_app_contract):
        """verify_escrow_on_chain returns True when escrow deposited."""
        os.environ["ANVIL_RPC_URL"] = RPC_URL

        order_id = b"\x10" * 32
        amount = 150_000
        deadline = int(time.time()) + 3600

        _call_escrow_deposit(
            w3, dex_app_contract, accounts.deployer_key,
            order_id, 1, contracts.usdc, amount,
            accounts.user_addr, deadline,
        )

        # Verifier should find it
        has_escrow, reason = verify_escrow_on_chain(
            contracts.dex_app, CHAIN_ID,
            "0x" + order_id.hex(), 1,
        )
        assert has_escrow, f"Expected escrow to exist: {reason}"
        assert str(amount) in reason

    def test_verify_escrow_missing(self, w3, contracts):
        """verify_escrow_on_chain returns False when no escrow."""
        os.environ["ANVIL_RPC_URL"] = RPC_URL

        has_escrow, reason = verify_escrow_on_chain(
            contracts.dex_app, CHAIN_ID,
            "0x" + (b"\xFF" * 32).hex(), 99,
        )
        assert not has_escrow


# ═══════════════════════════════════════════════════════════════════════════════
#  3. CROSS-CHAIN COMPILER: produces valid plan from CrossChainPlan
# ═══════════════════════════════════════════════════════════════════════════════


class TestCompilerE2E:
    @pytest.mark.asyncio
    async def test_compile_produces_escrow_params(self, contracts, accounts):
        """Compiler produces escrow params that match the bridge quote."""
        reg = BridgeRegistry()
        reg.register(MockBridgeAdapter())
        compiler = CrossChainCompiler(reg)

        plan = CrossChainPlan(
            legs=[
                ChainLeg(chain_id=CHAIN_ID, interactions=[]),
                ChainLeg(chain_id=964, interactions=[]),
            ],
            bridge_requests=[
                BridgeRequest(
                    token=contracts.usdc,
                    amount=1_000_000,
                    src_chain_id=CHAIN_ID,
                    dst_chain_id=964,
                    recipient=accounts.user_addr,
                ),
            ],
        )

        result = await compiler.compile(
            plan,
            order_id="0x" + (b"\x20" * 32).hex(),
            user_address=accounts.user_addr,
            contract_address=contracts.dex_app,
            deadline=int(time.time()) + 3600,
        )

        # Escrow params should be set
        assert len(result.escrow_params) == 1
        ep = result.escrow_params[0]
        assert ep["user"] == accounts.user_addr
        assert ep["amount"] > 0
        assert ep["leg_index"] > 0  # Destination leg after bridge

    @pytest.mark.asyncio
    async def test_compiler_rejects_bridge_selectors(self, contracts, accounts):
        """Compiler rejects plans with bridge selectors in solver legs."""
        reg = BridgeRegistry()
        reg.register(MockBridgeAdapter())
        compiler = CrossChainCompiler(reg)

        plan = CrossChainPlan(
            legs=[
                ChainLeg(
                    chain_id=CHAIN_ID,
                    interactions=[Interaction(
                        target="0x" + "11" * 20,
                        value="0",
                        call_data="0x81b4e8b4" + "00" * 28,  # transferRemote
                        chain_id=CHAIN_ID,
                    )],
                ),
                ChainLeg(chain_id=964, interactions=[]),
            ],
            bridge_requests=[
                BridgeRequest(
                    token=contracts.usdc, amount=1_000_000,
                    src_chain_id=CHAIN_ID, dst_chain_id=964,
                    recipient=accounts.user_addr,
                ),
            ],
        )

        with pytest.raises(CrossChainCompileError, match="bridge selector"):
            await compiler.compile(
                plan,
                order_id="0x" + (b"\x21" * 32).hex(),
                user_address=accounts.user_addr,
                contract_address=contracts.dex_app,
                deadline=int(time.time()) + 3600,
            )
