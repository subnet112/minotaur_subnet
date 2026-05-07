"""Unit tests for relayer submodules.

Tests ChainDeployment, get_supported_chains, GasManager, MockRelayer,
EvmRelayer (mocked Web3), and ValidatorSync.
"""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

# Ensure repo root is importable
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from minotaur_subnet.relayer.base import MockRelayer, SubmitResult
from minotaur_subnet.relayer.chain_config import ChainDeployment, get_supported_chains
from minotaur_subnet.relayer.gas_manager import GasManager
from minotaur_subnet.relayer.evm_relayer import EvmRelayer
from minotaur_subnet.relayer.validator_sync import ValidatorSync


# ═══════════════════════════════════════════════════════════════════════════════
#                          CHAIN DEPLOYMENT TESTS
# ═══════════════════════════════════════════════════════════════════════════════


class TestChainDeployment(unittest.TestCase):
    """Tests for ChainDeployment dataclass."""

    def test_defaults(self):
        cd = ChainDeployment(chain_id=1, name="Ethereum", rpc_url="http://localhost:8545")
        self.assertEqual(cd.chain_id, 1)
        self.assertEqual(cd.name, "Ethereum")
        self.assertEqual(cd.app_intent_base_address, "")
        self.assertEqual(cd.relayer_wallet, "")
        self.assertEqual(cd.gas_price_gwei, 0.0)
        self.assertEqual(cd.max_gas_price_gwei, 100.0)
        self.assertEqual(cd.gas_buffer_pct, 20)
        self.assertEqual(cd.confirmations, 1)

    def test_custom_fields(self):
        cd = ChainDeployment(
            chain_id=8453,
            name="Base",
            rpc_url="https://base-rpc.example.com",
            app_intent_base_address="0xABC",
            relayer_wallet="0xDEF",
            gas_price_gwei=0.01,
            max_gas_price_gwei=50.0,
            gas_buffer_pct=10,
            confirmations=3,
        )
        self.assertEqual(cd.chain_id, 8453)
        self.assertEqual(cd.app_intent_base_address, "0xABC")
        self.assertEqual(cd.gas_buffer_pct, 10)
        self.assertEqual(cd.confirmations, 3)


# ═══════════════════════════════════════════════════════════════════════════════
#                         GET SUPPORTED CHAINS TESTS
# ═══════════════════════════════════════════════════════════════════════════════


class TestGetSupportedChains(unittest.TestCase):
    """Tests for get_supported_chains() env-var parsing."""

    @patch.dict("os.environ", {}, clear=True)
    def test_no_env_vars_returns_empty(self):
        chains = get_supported_chains()
        self.assertEqual(len(chains), 0)

    @patch.dict("os.environ", {"ETHEREUM_RPC_URL": "http://eth.example.com"}, clear=True)
    def test_single_chain(self):
        chains = get_supported_chains()
        self.assertIn(1, chains)
        self.assertEqual(chains[1].name, "Ethereum")
        self.assertEqual(chains[1].rpc_url, "http://eth.example.com")

    @patch.dict("os.environ", {
        "ETHEREUM_RPC_URL": "http://eth.example.com",
        "BASE_RPC_URL": "http://base.example.com",
    }, clear=True)
    def test_multiple_chains(self):
        chains = get_supported_chains()
        self.assertEqual(len(chains), 2)
        self.assertIn(1, chains)
        self.assertIn(8453, chains)

    @patch.dict("os.environ", {
        "ETHEREUM_RPC_URL": "http://eth.example.com",
        "RELAYER_WALLET_1": "0xChainSpecific",
    }, clear=True)
    def test_per_chain_wallet_override(self):
        chains = get_supported_chains()
        self.assertEqual(chains[1].relayer_wallet, "0xChainSpecific")

    @patch.dict("os.environ", {
        "ETHEREUM_RPC_URL": "http://eth.example.com",
        "RELAYER_WALLET": "0xFallback",
    }, clear=True)
    def test_fallback_wallet(self):
        chains = get_supported_chains()
        self.assertEqual(chains[1].relayer_wallet, "0xFallback")

    @patch.dict("os.environ", {
        "ETHEREUM_RPC_URL": "http://eth.example.com",
        "APP_INTENT_BASE_1": "0xContract",
    }, clear=True)
    def test_app_intent_base_address(self):
        chains = get_supported_chains()
        self.assertEqual(chains[1].app_intent_base_address, "0xContract")

    @patch.dict("os.environ", {
        "ETHEREUM_RPC_URL": "http://eth.example.com",
        "BASE_RPC_URL": "http://base.example.com",
        "ARBITRUM_RPC_URL": "http://arb.example.com",
        "OPTIMISM_RPC_URL": "http://op.example.com",
    }, clear=True)
    def test_all_four_chains(self):
        chains = get_supported_chains()
        self.assertEqual(len(chains), 4)
        self.assertIn(1, chains)
        self.assertIn(8453, chains)
        self.assertIn(42161, chains)
        self.assertIn(10, chains)


# ═══════════════════════════════════════════════════════════════════════════════
#                            GAS MANAGER TESTS
# ═══════════════════════════════════════════════════════════════════════════════


class TestGasManager(unittest.TestCase):
    """Tests for GasManager gas estimation and balance checking."""

    def setUp(self):
        self.chains = {
            1: ChainDeployment(
                chain_id=1, name="Ethereum",
                rpc_url="http://eth.example.com",
                relayer_wallet="0xRelayer",
                gas_price_gwei=20.0,
                gas_buffer_pct=20,
            ),
        }
        self.gm = GasManager(self.chains)

    def test_estimate_execution_cost_single_call(self):
        plan = SimpleNamespace(interactions=[SimpleNamespace()])
        cost = self.gm.estimate_execution_cost(chain_id=1, plan=plan)
        # base_gas=100000 + proxy_deploy_gas=32000 + 1*80000 = 212000
        # with 20% buffer: 212000 * 120 // 100 = 254400
        # price=20 gwei: 254400 * 20 * 1e9 = 5088000000000000
        self.assertEqual(cost, 254400 * 20 * 10**9)

    def test_estimate_execution_cost_multiple_calls(self):
        plan = SimpleNamespace(interactions=[SimpleNamespace(), SimpleNamespace(), SimpleNamespace()])
        cost = self.gm.estimate_execution_cost(chain_id=1, plan=plan)
        # base=100000 + proxy=32000 + 3*80000=240000 → 372000
        # buffer: 372000 * 120 // 100 = 446400
        expected = int(446400 * 20 * 1e9)
        self.assertEqual(cost, expected)

    def test_estimate_unknown_chain_returns_zero(self):
        plan = SimpleNamespace(interactions=[])
        cost = self.gm.estimate_execution_cost(chain_id=999, plan=plan)
        self.assertEqual(cost, 0)

    def test_estimate_with_override_gas_price(self):
        plan = SimpleNamespace(interactions=[SimpleNamespace()])
        cost = self.gm.estimate_execution_cost(chain_id=1, plan=plan, gas_price_gwei=50)
        expected = int(254400 * 50 * 1e9)
        self.assertEqual(cost, expected)

    def test_estimate_no_interactions_attribute(self):
        plan = SimpleNamespace()  # no .interactions
        cost = self.gm.estimate_execution_cost(chain_id=1, plan=plan)
        # n_calls defaults to 1
        expected = int(254400 * 20 * 1e9)
        self.assertEqual(cost, expected)

    def test_check_sufficient_balance_true(self):
        # 1 ETH balance, cost = 0.1 ETH → need 0.15 ETH → True
        result = self.gm.check_sufficient_balance(1, 1.0, int(0.1 * 1e18))
        self.assertTrue(result)

    def test_check_sufficient_balance_false(self):
        # 0.1 ETH balance, cost = 0.1 ETH → need 0.15 ETH → False
        result = self.gm.check_sufficient_balance(1, 0.1, int(0.1 * 1e18))
        self.assertFalse(result)

    def test_check_sufficient_balance_exactly_threshold(self):
        # balance = cost * 1.5 exactly → True
        cost_wei = int(1e18)
        balance_eth = 1.5
        result = self.gm.check_sufficient_balance(1, balance_eth, cost_wei)
        self.assertTrue(result)

    @patch("minotaur_subnet.blockchain.chains.get_web3")
    def test_get_balances_success(self, mock_get_web3):
        mock_w3 = MagicMock()
        mock_w3.eth.get_balance.return_value = int(2.5 * 1e18)
        mock_get_web3.return_value = mock_w3

        balances = asyncio.run(self.gm.get_balances())
        self.assertAlmostEqual(balances[1], 2.5)

    @patch("minotaur_subnet.blockchain.chains.get_web3")
    def test_get_balances_failure_returns_negative(self, mock_get_web3):
        mock_get_web3.side_effect = Exception("RPC down")

        balances = asyncio.run(self.gm.get_balances())
        self.assertEqual(balances[1], -1.0)

    def test_get_balances_skips_chains_without_wallet(self):
        chains = {
            1: ChainDeployment(
                chain_id=1, name="Ethereum",
                rpc_url="http://eth.example.com",
                relayer_wallet="",  # no wallet
            ),
        }
        gm = GasManager(chains)
        balances = asyncio.run(gm.get_balances())
        self.assertNotIn(1, balances)


# ═══════════════════════════════════════════════════════════════════════════════
#                           MOCK RELAYER TESTS
# ═══════════════════════════════════════════════════════════════════════════════


class TestMockRelayer(unittest.TestCase):
    """Tests for MockRelayer."""

    def setUp(self):
        self.relayer = MockRelayer()

    def test_submit_plan_returns_success(self):
        order = SimpleNamespace(order_id="order_1", chain_id=1)
        plan = SimpleNamespace()
        result = asyncio.run(
            self.relayer.submit_plan(order, plan, 0.95)
        )
        self.assertIsInstance(result, SubmitResult)
        self.assertTrue(result.success)
        self.assertIsNotNone(result.tx_hash)
        self.assertTrue(result.tx_hash.startswith("0x"))

    def test_submit_plan_records_submission(self):
        order = SimpleNamespace(order_id="order_2", chain_id=8453)
        asyncio.run(
            self.relayer.submit_plan(order, None, 0.8)
        )
        self.assertEqual(len(self.relayer.submissions), 1)
        self.assertEqual(self.relayer.submissions[0]["order_id"], "order_2")
        self.assertEqual(self.relayer.submissions[0]["chain_id"], 8453)
        self.assertEqual(self.relayer.submissions[0]["score"], 0.8)

    def test_submit_plan_chain_id_defaults_to_1(self):
        order = SimpleNamespace(order_id="order_3")  # no chain_id
        result = asyncio.run(
            self.relayer.submit_plan(order, None, 0.5)
        )
        self.assertEqual(result.chain_id, 1)

    def test_multiple_submissions(self):
        for i in range(5):
            order = SimpleNamespace(order_id=f"order_{i}", chain_id=1)
            asyncio.run(
                self.relayer.submit_plan(order, None, 0.5)
            )
        self.assertEqual(len(self.relayer.submissions), 5)

    def test_on_leader_changed_clears_submissions(self):
        """REL-12: on_leader_changed drops all in-flight submissions."""
        for i in range(3):
            order = SimpleNamespace(order_id=f"order_{i}", chain_id=1)
            asyncio.run(self.relayer.submit_plan(order, None, 0.5))
        self.assertEqual(len(self.relayer.submissions), 3)

        dropped = self.relayer.on_leader_changed("0xNewLeader")
        self.assertEqual(dropped, 3)
        self.assertEqual(len(self.relayer.submissions), 0)
        self.assertEqual(self.relayer._current_leader, "0xNewLeader")

    def test_on_leader_changed_empty(self):
        dropped = self.relayer.on_leader_changed("0xLeader")
        self.assertEqual(dropped, 0)


# ═══════════════════════════════════════════════════════════════════════════════
#                          EVM RELAYER TESTS
# ═══════════════════════════════════════════════════════════════════════════════


class TestEvmRelayer(unittest.TestCase):
    """Tests for EvmRelayer with mocked Web3."""

    def setUp(self):
        self.chains = {
            1: ChainDeployment(
                chain_id=1, name="Ethereum",
                rpc_url="http://eth.example.com",
                app_intent_base_address="0x1234567890abcdef1234567890abcdef12345678",
                relayer_wallet="0xRelayer",
            ),
        }
        self.relayer = EvmRelayer(
            chains=self.chains,
            private_key="0x" + "ab" * 32,
        )

    def test_unconfigured_chain_error(self):
        order = SimpleNamespace(order_id="order_1", chain_id=999)
        result = asyncio.run(
            self.relayer.submit_plan(order, None, 0.5)
        )
        self.assertFalse(result.success)
        self.assertIn("not configured", result.error)
        self.assertEqual(result.chain_id, 999)

    def test_no_contract_address_error(self):
        chains = {
            1: ChainDeployment(
                chain_id=1, name="Ethereum",
                rpc_url="http://eth.example.com",
                app_intent_base_address="",  # empty
            ),
        }
        relayer = EvmRelayer(chains=chains)
        order = SimpleNamespace(order_id="order_1", chain_id=1)
        result = asyncio.run(
            relayer.submit_plan(order, None, 0.5)
        )
        self.assertFalse(result.success)
        self.assertIn("No AppIntentBase", result.error)

    @patch("minotaur_subnet.blockchain.chains.get_web3")
    @patch("minotaur_subnet.relayer.evm_relayer.encode_intent_order")
    @patch("minotaur_subnet.relayer.evm_relayer.encode_execution_plan")
    def test_success_flow(self, mock_encode_plan, mock_encode_order, mock_get_web3):
        mock_w3 = MagicMock()
        mock_get_web3.return_value = mock_w3
        mock_w3.to_checksum_address.return_value = "0x1234567890abcdef1234567890abcdef12345678"
        mock_encode_order.return_value = (b"\x00" * 32,) * 11
        mock_encode_plan.return_value = ([], 0, 0, b"")

        # Mock contract
        mock_contract = MagicMock()
        mock_w3.eth.contract.return_value = mock_contract
        mock_contract.functions.executeIntent.return_value.build_transaction.return_value = {
            "gas": 500000
        }
        mock_w3.eth.get_transaction_count.return_value = 0

        # Mock signing and sending
        mock_signed = MagicMock()
        mock_signed.raw_transaction = b"\x00" * 32
        mock_w3.eth.account.sign_transaction.return_value = mock_signed
        mock_w3.eth.send_raw_transaction.return_value = bytes.fromhex("aa" * 32)

        # Mock receipt
        mock_w3.eth.wait_for_transaction_receipt.return_value = {
            "status": 1,
            "blockNumber": 12345,
            "gasUsed": 150000,
        }

        order = SimpleNamespace(
            order_id="order_1", chain_id=1,
            params={}, submitted_by="0xUser",
            deadline=0, perpetual=False,
            max_executions=1, cooldown=0,
            user_signature="",
        )
        plan = SimpleNamespace(
            interactions=[], deadline=0, nonce=0, metadata=None,
        )
        consensus = SimpleNamespace(approvals=[])

        result = asyncio.run(
            self.relayer.submit_plan(order, plan, 0.95, consensus)
        )
        self.assertTrue(result.success)
        self.assertEqual(result.block_number, 12345)
        self.assertEqual(result.gas_used, 150000)

    @patch("minotaur_subnet.blockchain.chains.get_web3")
    @patch("minotaur_subnet.relayer.evm_relayer.encode_intent_order")
    @patch("minotaur_subnet.relayer.evm_relayer.encode_execution_plan")
    def test_reverted_tx(self, mock_encode_plan, mock_encode_order, mock_get_web3):
        mock_w3 = MagicMock()
        mock_get_web3.return_value = mock_w3
        mock_w3.to_checksum_address.return_value = "0x1234567890abcdef1234567890abcdef12345678"
        mock_encode_order.return_value = (b"\x00" * 32,) * 11
        mock_encode_plan.return_value = ([], 0, 0, b"")

        mock_contract = MagicMock()
        mock_w3.eth.contract.return_value = mock_contract
        mock_contract.functions.executeIntent.return_value.build_transaction.return_value = {
            "gas": 500000
        }
        mock_w3.eth.get_transaction_count.return_value = 0
        mock_signed = MagicMock()
        mock_signed.raw_transaction = b"\x00" * 32
        mock_w3.eth.account.sign_transaction.return_value = mock_signed
        mock_w3.eth.send_raw_transaction.return_value = bytes.fromhex("bb" * 32)

        # Reverted receipt
        mock_w3.eth.wait_for_transaction_receipt.return_value = {
            "status": 0,
            "blockNumber": 12346,
            "gasUsed": 200000,
        }

        order = SimpleNamespace(
            order_id="order_1", chain_id=1,
            params={}, submitted_by="0xUser",
            deadline=0, perpetual=False,
            max_executions=1, cooldown=0,
            user_signature="",
        )
        plan = SimpleNamespace(
            interactions=[], deadline=0, nonce=0, metadata=None,
        )

        result = asyncio.run(
            self.relayer.submit_plan(order, plan, 0.5)
        )
        self.assertFalse(result.success)
        self.assertEqual(result.gas_used, 200000)

    @patch("minotaur_subnet.blockchain.chains.get_web3")
    def test_exception_handling(self, mock_get_web3):
        mock_get_web3.side_effect = Exception("Connection refused")

        order = SimpleNamespace(order_id="order_1", chain_id=1, user_signature="")
        result = asyncio.run(
            self.relayer.submit_plan(order, None, 0.5)
        )
        self.assertFalse(result.success)
        self.assertIn("Connection refused", result.error)


# ═══════════════════════════════════════════════════════════════════════════════
#                          VALIDATOR SYNC TESTS
# ═══════════════════════════════════════════════════════════════════════════════


class TestValidatorSync(unittest.TestCase):
    """Tests for ValidatorSync."""

    def test_init_defaults(self):
        chains = {1: ChainDeployment(chain_id=1, name="Ethereum", rpc_url="http://eth")}
        vs = ValidatorSync(chains)
        self.assertEqual(vs.netuid, 112)
        self.assertEqual(vs.poll_interval, 300.0)
        self.assertFalse(vs._running)
        self.assertEqual(vs._last_validators, [])

    def test_stop(self):
        vs = ValidatorSync({})
        vs._running = True
        vs.stop()
        self.assertFalse(vs._running)

    def test_sync_once_no_change(self):
        """When validators haven't changed, _update_all_chains should not be called."""
        vs = ValidatorSync({})
        vs._last_validators = ["0xAAA", "0xBBB"]

        # Mock _get_metagraph_validators to return the same set
        async def _get_same():
            return ["0xAAA", "0xBBB"]

        vs._get_metagraph_validators = _get_same

        # Mock _update_all_chains to track calls
        update_called = False
        async def _track_update(validators):
            nonlocal update_called
            update_called = True

        vs._update_all_chains = _track_update

        asyncio.run(vs._sync_once())
        self.assertFalse(update_called)

    def test_sync_once_detects_change(self):
        """When validators change, _update_all_chains should be called."""
        vs = ValidatorSync({})
        vs._last_validators = ["0xAAA"]

        async def _get_new():
            return ["0xAAA", "0xBBB", "0xCCC"]

        vs._get_metagraph_validators = _get_new

        updated_with = []
        async def _track_update(validators):
            updated_with.extend(validators)

        vs._update_all_chains = _track_update

        asyncio.run(vs._sync_once())
        self.assertEqual(updated_with, ["0xAAA", "0xBBB", "0xCCC"])
        self.assertEqual(vs._last_validators, ["0xAAA", "0xBBB", "0xCCC"])

    def test_sync_once_empty_validators_noop(self):
        """Empty metagraph response should not update anything."""
        vs = ValidatorSync({})
        vs._last_validators = ["0xAAA"]

        async def _get_empty():
            return []

        vs._get_metagraph_validators = _get_empty

        update_called = False
        async def _track_update(validators):
            nonlocal update_called
            update_called = True

        vs._update_all_chains = _track_update

        asyncio.run(vs._sync_once())
        self.assertFalse(update_called)

    def test_custom_netuid_and_poll(self):
        vs = ValidatorSync({}, netuid=42, poll_interval=60.0)
        self.assertEqual(vs.netuid, 42)
        self.assertEqual(vs.poll_interval, 60.0)


# ═══════════════════════════════════════════════════════════════════════════════
#                          SUBMIT RESULT TESTS
# ═══════════════════════════════════════════════════════════════════════════════


class TestSubmitResult(unittest.TestCase):
    """Tests for SubmitResult dataclass."""

    def test_success_result(self):
        r = SubmitResult(success=True, tx_hash="0xabc", chain_id=1, block_number=100, gas_used=150000)
        self.assertTrue(r.success)
        self.assertIsNone(r.error)

    def test_failure_result(self):
        r = SubmitResult(success=False, error="Chain not configured", chain_id=999)
        self.assertFalse(r.success)
        self.assertIsNone(r.tx_hash)
        self.assertIsNone(r.block_number)
        self.assertEqual(r.gas_used, 0)

    def test_defaults(self):
        r = SubmitResult(success=True)
        self.assertIsNone(r.tx_hash)
        self.assertIsNone(r.error)
        self.assertEqual(r.chain_id, 0)
        self.assertIsNone(r.block_number)
        self.assertEqual(r.gas_used, 0)


if __name__ == "__main__":
    unittest.main()
