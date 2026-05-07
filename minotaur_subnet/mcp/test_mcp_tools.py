"""Tests for MCP server: tool registration and HTTP client helpers."""

from __future__ import annotations

import sys
import time
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from minotaur_subnet.store import AppIntentStore
from minotaur_subnet.api import services as _tools
from minotaur_subnet.shared.types import WalletInfo


class TestListWallets(unittest.TestCase):
    """Tests for list_wallets service function (used by both API and MCP)."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.store = AppIntentStore(store_path=Path(self.tmpdir) / "store.json")

    def test_list_empty(self):
        result = _tools.list_wallets(self.store)
        self.assertEqual(result["total"], 0)
        self.assertEqual(result["wallets"], [])

    def test_list_wallets(self):
        w1 = WalletInfo(address="0xaaa", chain_ids=[1], wallet_type="local", created_at=time.time())
        w2 = WalletInfo(address="0xbbb", chain_ids=[1, 8453], wallet_type="local", created_at=time.time())
        self.store.save_wallet(w1)
        self.store.save_wallet(w2)

        result = _tools.list_wallets(self.store)
        self.assertEqual(result["total"], 2)
        addresses = [w["address"] for w in result["wallets"]]
        self.assertIn("0xaaa", addresses)
        self.assertIn("0xbbb", addresses)


class TestMCPServerToolRegistration(unittest.TestCase):
    """Test that tools are properly registered on the MCP server."""

    def test_core_tools_registered(self):
        """Key tools exist on the server."""
        from minotaur_subnet.mcp.server import server
        tool_names = [t.name for t in server._tool_manager.list_tools()]
        expected = [
            "create_wallet", "get_wallet", "fund_wallet", "list_wallets",
            "list_chains",
            "create_app_intent", "validate_app_intent", "deploy_app_intent", "list_app_intents",
            "get_app_status", "monitor_app", "update_scoring",
            "submit_order", "get_quote", "cancel_order", "get_order_status", "list_orders",
            "get_app_manifest",
            "testnet_faucet_eth", "testnet_faucet_erc20",
        ]
        for name in expected:
            self.assertIn(name, tool_names, f"Missing tool: {name}")

    def test_total_tool_count(self):
        """Verify expected total number of MCP tools (23)."""
        from minotaur_subnet.mcp.server import server
        tools = server._tool_manager.list_tools()
        # 5 wallet + 1 chain + 5 app lifecycle (incl validate) + 2 monitoring + 7 order + 1 manifest + 2 testnet faucet = 23
        self.assertEqual(len(tools), 23)


class TestMCPHttpHelpers(unittest.TestCase):
    """Test the HTTP helper functions used by MCP tools."""

    @patch("minotaur_subnet.mcp.server._client")
    def test_get_passes_params(self, mock_client):
        """_get filters out None and empty string params."""
        from minotaur_subnet.mcp.server import _get
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": True}
        mock_client.get.return_value = mock_resp

        result = _get("/test", foo="bar", empty="", none_val=None)
        mock_client.get.assert_called_once_with("/test", params={"foo": "bar"})
        self.assertEqual(result, {"ok": True})

    @patch("minotaur_subnet.mcp.server._client")
    def test_post_sends_json(self, mock_client):
        """_post sends JSON body."""
        from minotaur_subnet.mcp.server import _post
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"created": True}
        mock_client.post.return_value = mock_resp

        result = _post("/test", {"key": "value"})
        mock_client.post.assert_called_once_with("/test", json={"key": "value"})
        self.assertEqual(result, {"created": True})


if __name__ == "__main__":
    unittest.main()
