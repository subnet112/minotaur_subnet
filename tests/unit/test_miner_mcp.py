"""Tests for miner agent MCP server tools."""

import json
import pytest
import tempfile
import shutil
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

from minotaur_subnet.miner.agent import mcp_server


# ── Valid strategy code for test_strategy tool ─────────────────────────────

VALID_STRATEGY = '''\
from minotaur_subnet.sdk.strategy import Strategy
from minotaur_subnet.shared.types import ExecutionPlan, Interaction

class TestStrategy(Strategy):
    APP_ID = "app-test-001"

    def generate_plan(self, intent, state, snapshot):
        return ExecutionPlan(
            intent_id=intent.app_id,
            interactions=[
                Interaction(
                    target="0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",
                    value="1000000000000000",
                    call_data="0xd0e30db0",
                    chain_id=1,
                ),
            ],
            deadline=snapshot.timestamp + 300,
            nonce=state.nonce,
        )

STRATEGY_CLASS = TestStrategy
'''

BAD_STRATEGY = "# not a valid strategy\nx = 1\n"


# ── Fixtures ───────────────────────────────────────────────────────────────


@pytest.fixture
def tmp_strategy_dir():
    d = tempfile.mkdtemp(prefix="mcp_strategies_")
    yield d
    shutil.rmtree(d, ignore_errors=True)


# ── Validator proxy tools ──────────────────────────────────────────────────


class TestGetAppDetails:
    @patch("minotaur_subnet.miner.agent.mcp_server._http_get")
    def test_calls_validator(self, mock_get):
        # First call: /v1/apps/{app_id}/status, second: /v1/apps/{app_id}/manifest
        mock_get.side_effect = [
            {
                "app": {"app_id": "app-001", "name": "Test App", "solidity_code": "// sol", "config": {}},
                "deployment": {},
            },
            {"manifest": {"intent_functions": []}},
        ]
        result = mcp_server.get_app_details("app-001")
        assert result["app_id"] == "app-001"
        assert mock_get.call_count == 2
        assert "/v1/apps/app-001/status" in mock_get.call_args_list[0][0][0]
        assert "/v1/apps/app-001/manifest" in mock_get.call_args_list[1][0][0]

    @patch("minotaur_subnet.miner.agent.mcp_server._http_get")
    def test_returns_error(self, mock_get):
        mock_get.return_value = {"error": "HTTP 404: Not Found"}
        result = mcp_server.get_app_details("nonexistent")
        assert "error" in result


class TestGetAppScores:
    @patch("minotaur_subnet.miner.agent.mcp_server._http_get")
    def test_returns_scores(self, mock_get):
        mock_get.return_value = {
            "avg_score": 0.75,
            "best_score": 0.9,
            "execution_count": 10,
            "recent_scores": [0.7, 0.8],
        }
        result = mcp_server.get_app_scores("app-001")
        assert result["avg_score"] == 0.75
        assert "/v1/apps/app-001/status" in mock_get.call_args[0][0]


class TestListAvailableApps:
    @patch("minotaur_subnet.miner.agent.mcp_server._http_get")
    def test_returns_apps(self, mock_get):
        mock_get.return_value = {
            "apps": [{"app_id": "app-001", "name": "Test"}],
        }
        result = mcp_server.list_available_apps()
        assert "apps" in result
        assert result["count"] == 1
        assert "/v1/apps/" in mock_get.call_args[0][0]


class TestListOrders:
    @patch("minotaur_subnet.miner.agent.mcp_server._http_get")
    def test_no_filters(self, mock_get):
        # /v1/orders is paginated — the tool always sends the page params.
        mock_get.return_value = {"orders": [], "count": 0}
        mcp_server.list_orders()
        url = mock_get.call_args[0][0]
        assert "/orders?" in url
        assert "limit=100" in url and "offset=0" in url
        assert "app_id" not in url and "status" not in url

    @patch("minotaur_subnet.miner.agent.mcp_server._http_get")
    def test_with_filters(self, mock_get):
        mock_get.return_value = {"orders": [], "count": 0}
        mcp_server.list_orders(app_id="app-001", status="open", limit=25, offset=50)
        url = mock_get.call_args[0][0]
        assert "app_id=app-001" in url
        assert "status=open" in url
        assert "limit=25" in url and "offset=50" in url


# ── Strategy development tools ─────────────────────────────────────────────


class TestTestStrategy:
    def test_valid_strategy_passes(self):
        result = mcp_server.test_strategy("app-test-001", VALID_STRATEGY)
        assert result["passed"] is True
        assert "OK" in result["message"]

    def test_invalid_strategy_fails(self):
        result = mcp_server.test_strategy("app-test-001", BAD_STRATEGY)
        assert result["passed"] is False

    def test_wrong_app_id_fails(self):
        result = mcp_server.test_strategy("wrong-app-id", VALID_STRATEGY)
        assert result["passed"] is False
        assert "mismatch" in result["message"].lower() or "APP_ID" in result["message"]

    def test_syntax_error_fails(self):
        result = mcp_server.test_strategy("app-001", "def foo(:\n  pass")
        assert result["passed"] is False


class TestStateHelpers:
    def test_prefers_typed_context_params_and_intent_function(self):
        state = SimpleNamespace(
            raw_params={"input_token": "0xlegacy"},
            control={"_intent_function": "execute"},
            typed_context=SimpleNamespace(
                intent_function="swap",
                raw_params={"input_token": "0xtyped"},
            ),
        )

        assert mcp_server._state_params(state) == {"input_token": "0xtyped"}
        assert mcp_server._intent_function_from_state(state) == "swap"


class TestListStrategies:
    def test_empty_dir(self, tmp_strategy_dir):
        with patch.object(mcp_server, "STRATEGY_DIR", Path(tmp_strategy_dir)):
            with patch("minotaur_subnet.miner.agent.mcp_server._strategy_dir",
                        return_value=Path(tmp_strategy_dir)):
                result = mcp_server.list_strategies()
                assert result["count"] == 0
                assert result["strategies"] == []

    def test_with_strategies(self, tmp_strategy_dir):
        # Create a strategy file
        app_dir = Path(tmp_strategy_dir) / "app-001"
        app_dir.mkdir()
        (app_dir / "strategy.py").write_text(VALID_STRATEGY)

        with patch("minotaur_subnet.miner.agent.mcp_server._strategy_dir",
                    return_value=Path(tmp_strategy_dir)):
            result = mcp_server.list_strategies()
            assert result["count"] == 1
            assert result["strategies"][0]["app_id"] == "app-001"


class TestGetScoreFeedback:
    @patch("minotaur_subnet.miner.agent.mcp_server._http_get")
    def test_returns_feedback(self, mock_get):
        mock_get.return_value = {
            "avg_score": 0.5,
            "best_score": 0.7,
            "total_executions": 10,
            "recent_scores": [0.3, 0.4, 0.5, 0.6, 0.7, 0.8],
        }
        result = mcp_server.get_score_feedback("app-001")
        assert result["app_id"] == "app-001"
        assert result["avg_score"] == 0.5
        assert result["trend"] == "improving"

    @patch("minotaur_subnet.miner.agent.mcp_server._http_get")
    def test_declining_trend(self, mock_get):
        mock_get.return_value = {
            "avg_score": 0.5,
            "best_score": 0.7,
            "total_executions": 10,
            "recent_scores": [0.8, 0.7, 0.6, 0.5, 0.4, 0.3],
        }
        result = mcp_server.get_score_feedback("app-001")
        assert result["trend"] == "declining"

    @patch("minotaur_subnet.miner.agent.mcp_server._http_get")
    def test_stable_trend(self, mock_get):
        mock_get.return_value = {
            "avg_score": 0.5,
            "best_score": 0.7,
            "total_executions": 10,
            "recent_scores": [0.5, 0.5],
        }
        result = mcp_server.get_score_feedback("app-001")
        assert result["trend"] == "stable"

    @patch("minotaur_subnet.miner.agent.mcp_server._http_get")
    def test_error_passthrough(self, mock_get):
        mock_get.return_value = {"error": "Connection failed"}
        result = mcp_server.get_score_feedback("app-001")
        assert "error" in result


# ── Contract state tools ──────────────────────────────────────────────────


class TestReadContract:
    def test_no_rpc_url(self):
        with patch.dict("os.environ", {}, clear=False):
            with patch.object(mcp_server, "ANVIL_RPC_URL", ""):
                result = mcp_server.read_contract(
                    "0x1234", "balanceOf(address)", '["0x5678"]',
                )
                assert "error" in result
                assert "ANVIL_RPC_URL" in result["error"]

    @patch("minotaur_subnet.miner.agent.mcp_server._http_post")
    def test_with_rpc_url(self, mock_post):
        mock_post.return_value = {
            "result": "0x0000000000000000000000000000000000000000000000000000000000000064",
        }
        with patch.dict("os.environ", {"ANVIL_RPC_URL": "http://localhost:8545"}):
            result = mcp_server.read_contract(
                "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
                "balanceOf(address)",
                '["0x0000000000000000000000000000000000000001"]',
            )
            assert "raw" in result


class TestReadContractAutoDecode:
    """Tests for auto-decode via return types in function_sig."""

    @patch("minotaur_subnet.miner.agent.mcp_server._http_post")
    def test_uint256_decode(self, mock_post):
        # 100 as uint256
        mock_post.return_value = {
            "result": "0x0000000000000000000000000000000000000000000000000000000000000064",
        }
        with patch.dict("os.environ", {"ANVIL_RPC_URL": "http://localhost:8545"}):
            result = mcp_server.read_contract(
                "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
                "balanceOf(address)(uint256)",
                '["0x0000000000000000000000000000000000000001"]',
            )
            assert "raw" in result
            assert "decoded" in result
            assert result["decoded"] == ["100"]

    @patch("minotaur_subnet.miner.agent.mcp_server._http_post")
    def test_no_return_types_raw_only(self, mock_post):
        mock_post.return_value = {
            "result": "0x0000000000000000000000000000000000000000000000000000000000000064",
        }
        with patch.dict("os.environ", {"ANVIL_RPC_URL": "http://localhost:8545"}):
            result = mcp_server.read_contract(
                "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
                "balanceOf(address)",
                '["0x0000000000000000000000000000000000000001"]',
            )
            assert "raw" in result
            assert "decoded" not in result

    @patch("minotaur_subnet.miner.agent.mcp_server._http_post")
    def test_multiple_return_types(self, mock_post):
        from eth_abi.abi import encode as abi_encode
        # Encode (uint160, int24, uint16) — mock sqrtPriceX96, tick, fee
        encoded = abi_encode(["uint160", "int24", "uint16"], [2**96, -100, 3000])
        mock_post.return_value = {"result": "0x" + encoded.hex()}
        with patch.dict("os.environ", {"ANVIL_RPC_URL": "http://localhost:8545"}):
            result = mcp_server.read_contract(
                "0x8ad599c3A0ff1De082011EFDDc58f1908eb6e6D8",
                "slot0()(uint160,int24,uint16)",
                "[]",
            )
            assert "decoded" in result
            assert len(result["decoded"]) == 3
            assert result["decoded"][0] == str(2**96)
            assert result["decoded"][1] == str(-100)
            assert result["decoded"][2] == str(3000)

    @patch("minotaur_subnet.miner.agent.mcp_server._http_post")
    def test_block_parameter(self, mock_post):
        mock_post.return_value = {
            "result": "0x0000000000000000000000000000000000000000000000000000000000000064",
        }
        with patch.dict("os.environ", {"ANVIL_RPC_URL": "http://localhost:8545"}):
            mcp_server.read_contract(
                "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
                "balanceOf(address)(uint256)",
                '["0x0000000000000000000000000000000000000001"]',
                block="0x100",
            )
            call_payload = mock_post.call_args[0][1]
            assert call_payload["params"][1] == "0x100"


class TestGetTokenBalance:
    def test_no_rpc_url(self):
        with patch.dict("os.environ", {}, clear=False):
            with patch.object(mcp_server, "ANVIL_RPC_URL", ""):
                result = mcp_server.get_token_balance("0x1234", "0x5678")
                assert "error" in result
                assert "ANVIL_RPC_URL" in result["error"]

    @patch("minotaur_subnet.miner.agent.mcp_server._http_post")
    def test_returns_balance(self, mock_post):
        mock_post.return_value = {
            "result": "0x0000000000000000000000000000000000000000000000000000000000000064",
        }
        with patch.dict("os.environ", {"ANVIL_RPC_URL": "http://localhost:8545"}):
            result = mcp_server.get_token_balance(
                "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
                "0x0000000000000000000000000000000000000001",
            )
            assert result["balance"] == "100"

    @patch("minotaur_subnet.miner.agent.mcp_server._http_post")
    def test_zero_balance(self, mock_post):
        mock_post.return_value = {"result": "0x"}
        with patch.dict("os.environ", {"ANVIL_RPC_URL": "http://localhost:8545"}):
            result = mcp_server.get_token_balance(
                "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
                "0x0000000000000000000000000000000000000001",
            )
            assert result["balance"] == "0"


class TestGetTokenBalanceEnhanced:
    """Tests for enhanced get_token_balance with decimals/symbol/formatted."""

    @patch("minotaur_subnet.miner.agent.mcp_server._http_post")
    def test_includes_decimals_symbol_formatted(self, mock_post):
        from eth_abi.abi import encode as abi_encode

        # Responses: balanceOf, decimals, (symbol skipped — registry hit for USDC)
        balance_hex = "0x" + abi_encode(["uint256"], [100_500_000]).hex()
        decimals_hex = "0x" + abi_encode(["uint8"], [6]).hex()

        mock_post.side_effect = [
            {"result": balance_hex},     # balanceOf
            {"result": decimals_hex},    # decimals (via _execute_read)
        ]
        with patch.dict("os.environ", {"ANVIL_RPC_URL": "http://localhost:8545"}):
            result = mcp_server.get_token_balance(
                "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",  # USDC
                "0x0000000000000000000000000000000000000001",
            )
            assert result["balance"] == "100500000"
            assert result["symbol"] == "USDC"
            assert result["decimals"] == 6
            assert result["formatted"] == "100.500000"

    @patch("minotaur_subnet.miner.agent.mcp_server._http_post")
    def test_unknown_token_queries_symbol_on_chain(self, mock_post):
        from eth_abi.abi import encode as abi_encode

        balance_hex = "0x" + abi_encode(["uint256"], [1000]).hex()
        decimals_hex = "0x" + abi_encode(["uint8"], [18]).hex()
        # symbol returns ABI-encoded string
        symbol_hex = "0x" + abi_encode(["string"], ["FOO"]).hex()

        mock_post.side_effect = [
            {"result": balance_hex},     # balanceOf
            {"result": decimals_hex},    # decimals
            {"result": symbol_hex},      # symbol
        ]
        with patch.dict("os.environ", {"ANVIL_RPC_URL": "http://localhost:8545"}):
            result = mcp_server.get_token_balance(
                "0x1111111111111111111111111111111111111111",  # unknown token
                "0x0000000000000000000000000000000000000001",
            )
            assert result["balance"] == "1000"
            assert result["symbol"] == "FOO"
            assert result["decimals"] == 18


# ── Protocol-agnostic on-chain tools ─────────────────────────────────────


class TestResolveToken:
    def test_symbol_lookup(self):
        result = mcp_server.resolve_token("USDC", 1)
        assert result["address"] == "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
        assert result["symbol"] == "USDC"
        assert result["chain_id"] == 1

    def test_reverse_address_lookup(self):
        result = mcp_server.resolve_token(
            "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48", 1,
        )
        assert result["symbol"] == "USDC"
        assert result["address"] == "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"

    def test_different_chain(self):
        result = mcp_server.resolve_token("USDC", 8453)
        assert result["address"] == "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
        assert result["chain_id"] == 8453

    def test_native_to_wrapped(self):
        result = mcp_server.resolve_token("ETH", 1)
        assert result["address"] == "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"

    def test_unknown_token(self):
        result = mcp_server.resolve_token("FAKE_TOKEN_XYZ", 1)
        assert "error" in result
        assert "known_tokens" in result
        assert isinstance(result["known_tokens"], list)

    def test_known_chains_populated(self):
        result = mcp_server.resolve_token("USDC", 1)
        assert "known_chains" in result
        assert len(result["known_chains"]) >= 2  # At least ETH + Base


class TestGetTokenInfo:
    def test_no_rpc_url(self):
        with patch.dict("os.environ", {}, clear=False):
            with patch.object(mcp_server, "ANVIL_RPC_URL", ""):
                result = mcp_server.get_token_info("0x1234")
                assert "error" in result
                assert "ANVIL_RPC_URL" in result["error"]

    @patch("minotaur_subnet.miner.agent.mcp_server._http_post")
    def test_returns_metadata(self, mock_post):
        from eth_abi.abi import encode as abi_encode

        name_hex = "0x" + abi_encode(["string"], ["USD Coin"]).hex()
        symbol_hex = "0x" + abi_encode(["string"], ["USDC"]).hex()
        decimals_hex = "0x" + abi_encode(["uint8"], [6]).hex()
        supply_hex = "0x" + abi_encode(["uint256"], [1_000_000_000_000]).hex()

        mock_post.side_effect = [
            {"result": name_hex},
            {"result": symbol_hex},
            {"result": decimals_hex},
            {"result": supply_hex},
        ]
        with patch.dict("os.environ", {"ANVIL_RPC_URL": "http://localhost:8545"}):
            result = mcp_server.get_token_info(
                "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
            )
            assert result["name"] == "USD Coin"
            assert result["symbol"] == "USDC"
            assert result["decimals"] == 6
            assert result["total_supply"] == "1000000000000"
            assert result["registry_symbol"] == "USDC"


class TestMulticallRead:
    def test_no_rpc_url(self):
        with patch.dict("os.environ", {}, clear=False):
            with patch.object(mcp_server, "ANVIL_RPC_URL", ""):
                result = mcp_server.multicall_read("[]")
                assert "error" in result
                assert "ANVIL_RPC_URL" in result["error"]

    @patch("minotaur_subnet.miner.agent.mcp_server._http_post")
    def test_batch_two_calls_with_decode(self, mock_post):
        from eth_abi.abi import encode as abi_encode

        bal1 = "0x" + abi_encode(["uint256"], [100]).hex()
        bal2 = "0x" + abi_encode(["uint256"], [200]).hex()

        mock_post.side_effect = [
            {"result": bal1},
            {"result": bal2},
        ]
        calls = json.dumps([
            {
                "address": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
                "function_sig": "balanceOf(address)(uint256)",
                "args": ["0x0000000000000000000000000000000000000001"],
            },
            {
                "address": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
                "function_sig": "balanceOf(address)(uint256)",
                "args": ["0x0000000000000000000000000000000000000002"],
            },
        ])
        with patch.dict("os.environ", {"ANVIL_RPC_URL": "http://localhost:8545"}):
            result = mcp_server.multicall_read(calls)
            assert result["count"] == 2
            assert result["errors"] == 0
            assert result["results"][0]["decoded"] == ["100"]
            assert result["results"][1]["decoded"] == ["200"]

    def test_invalid_json(self):
        with patch.dict("os.environ", {"ANVIL_RPC_URL": "http://localhost:8545"}):
            result = mcp_server.multicall_read("not valid json")
            assert "error" in result
            assert "Invalid JSON" in result["error"]

    @patch("minotaur_subnet.miner.agent.mcp_server._http_post")
    def test_cap_at_20(self, mock_post):
        calls = json.dumps([
            {"address": "0x1234", "function_sig": "foo()"}
            for _ in range(25)
        ])
        with patch.dict("os.environ", {"ANVIL_RPC_URL": "http://localhost:8545"}):
            result = mcp_server.multicall_read(calls)
            assert "error" in result
            assert "25" in result["error"]


class TestGetLogs:
    def test_no_rpc_url(self):
        with patch.dict("os.environ", {}, clear=False):
            with patch.object(mcp_server, "ANVIL_RPC_URL", ""):
                result = mcp_server.get_logs("0x1234")
                assert "error" in result
                assert "ANVIL_RPC_URL" in result["error"]

    @patch("minotaur_subnet.miner.agent.mcp_server._http_post")
    def test_returns_logs(self, mock_post):
        mock_post.return_value = {
            "result": [
                {
                    "address": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
                    "topics": ["0xddf252ad"],
                    "data": "0x0064",
                    "blockNumber": "0xa",
                    "transactionHash": "0xabc",
                    "logIndex": "0x0",
                },
            ],
        }
        with patch.dict("os.environ", {"ANVIL_RPC_URL": "http://localhost:8545"}):
            result = mcp_server.get_logs(
                "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
                '["0xddf252ad"]',
            )
            assert result["count"] == 1
            assert result["logs"][0]["block_number"] == 10
            assert result["logs"][0]["tx_hash"] == "0xabc"

    @patch("minotaur_subnet.miner.agent.mcp_server._http_post")
    def test_empty_logs(self, mock_post):
        mock_post.return_value = {"result": []}
        with patch.dict("os.environ", {"ANVIL_RPC_URL": "http://localhost:8545"}):
            result = mcp_server.get_logs("0x1234")
            assert result["count"] == 0
            assert result["logs"] == []


class TestGetContractCode:
    def test_no_rpc_url(self):
        with patch.dict("os.environ", {}, clear=False):
            with patch.object(mcp_server, "ANVIL_RPC_URL", ""):
                result = mcp_server.get_contract_code("0x1234")
                assert "error" in result
                assert "ANVIL_RPC_URL" in result["error"]

    @patch("minotaur_subnet.miner.agent.mcp_server._http_post")
    def test_is_contract(self, mock_post):
        mock_post.return_value = {"result": "0x6080604052"}
        with patch.dict("os.environ", {"ANVIL_RPC_URL": "http://localhost:8545"}):
            result = mcp_server.get_contract_code(
                "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
            )
            assert result["is_contract"] is True
            assert result["code_size"] > 0

    @patch("minotaur_subnet.miner.agent.mcp_server._http_post")
    def test_is_eoa(self, mock_post):
        mock_post.return_value = {"result": "0x"}
        with patch.dict("os.environ", {"ANVIL_RPC_URL": "http://localhost:8545"}):
            result = mcp_server.get_contract_code(
                "0x0000000000000000000000000000000000000001",
            )
            assert result["is_contract"] is False
            assert result["code_size"] == 0


# ── Shared helpers ────────────────────────────────────────────────────────


class TestParseFunctionSig:
    def test_simple_sig(self):
        canonical, inputs, returns = mcp_server._parse_function_sig("balanceOf(address)")
        assert canonical == "balanceOf(address)"
        assert inputs == ["address"]
        assert returns == []

    def test_with_return_types(self):
        canonical, inputs, returns = mcp_server._parse_function_sig(
            "balanceOf(address)(uint256)",
        )
        assert canonical == "balanceOf(address)"
        assert inputs == ["address"]
        assert returns == ["uint256"]

    def test_no_args(self):
        canonical, inputs, returns = mcp_server._parse_function_sig("slot0()(uint160,int24)")
        assert canonical == "slot0()"
        assert inputs == []
        assert returns == ["uint160", "int24"]

    def test_invalid_sig(self):
        with pytest.raises(ValueError):
            mcp_server._parse_function_sig("not a function sig")


class TestToJsonSafe:
    def test_int_to_str(self):
        assert mcp_server._to_json_safe(123) == "123"

    def test_bytes_to_hex(self):
        assert mcp_server._to_json_safe(b"\xab\xcd") == "0xabcd"

    def test_nested(self):
        result = mcp_server._to_json_safe((42, b"\x01", [100]))
        assert result == ["42", "0x01", ["100"]]

    def test_string_passthrough(self):
        assert mcp_server._to_json_safe("hello") == "hello"
