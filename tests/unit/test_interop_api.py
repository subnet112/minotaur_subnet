"""Integration tests for InteropAddress at API boundaries.

Tests the actual FastAPI routes with plain 0x, CAIP-10, and invalid inputs.
"""

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pytest
from fastapi.testclient import TestClient

from minotaur_subnet.orderbook.orderbook import IntentOrderBook


VITALIK = "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045"


@pytest.fixture
def client():
    """Create a TestClient with a fresh OrderBook."""
    from minotaur_subnet.api.routes import orders as order_routes

    ob = IntentOrderBook()
    order_routes.set_orderbook(ob)

    # Build minimal FastAPI app with order routes
    from fastapi import FastAPI
    app = FastAPI()
    app.include_router(order_routes.router, prefix="/v1")

    return TestClient(app)


# ═══════════════════════════════════════════════════════════════════════════
#  Submit order tests
# ═══════════════════════════════════════════════════════════════════════════


class TestSubmitOrderInterop:
    def test_plain_address(self, client):
        """Plain 0x address should work and response includes interop_address."""
        resp = client.post("/v1/apps/app_test/orders", json={
            "submitted_by": VITALIK,
            "params": {},
        })
        assert resp.status_code == 201
        data = resp.json()
        assert data["submitted_by"] == VITALIK
        assert data["interop_address"] == f"eip155:1:{VITALIK}"

    def test_caip10_address(self, client):
        """CAIP-10 address should be parsed and chain_id extracted."""
        resp = client.post("/v1/apps/app_test/orders", json={
            "submitted_by": f"eip155:8453:{VITALIK}",
            "params": {},
            "chain_id": 8453,
        })
        assert resp.status_code == 201
        data = resp.json()
        assert data["submitted_by"] == VITALIK
        assert data["chain_id"] == 8453
        assert data["interop_address"] == f"eip155:8453:{VITALIK}"

    def test_caip10_chain_mismatch(self, client):
        """CAIP-10 chain_id must match request chain_id."""
        resp = client.post("/v1/apps/app_test/orders", json={
            "submitted_by": f"eip155:8453:{VITALIK}",
            "params": {},
            "chain_id": 1,  # Conflicts with eip155:8453
        })
        assert resp.status_code == 400
        assert "chain_id" in resp.json()["detail"].lower()

    def test_lowercase_address_gets_checksummed(self, client):
        """Lowercase addresses should be EIP-55 checksummed."""
        resp = client.post("/v1/apps/app_test/orders", json={
            "submitted_by": VITALIK.lower(),
            "params": {},
        })
        assert resp.status_code == 201
        assert resp.json()["submitted_by"] == VITALIK

    def test_invalid_address_format(self, client):
        """Invalid address should return 400."""
        resp = client.post("/v1/apps/app_test/orders", json={
            "submitted_by": "not-an-address",
            "params": {},
        })
        assert resp.status_code == 400


# ═══════════════════════════════════════════════════════════════════════════
#  Service-level tests (get_wallet, fund_wallet, faucet_eth)
# ═══════════════════════════════════════════════════════════════════════════


class TestServicesInterop:
    def test_get_wallet_caip10(self):
        """get_wallet should accept CAIP-10 and look up by plain address."""
        from minotaur_subnet.api import services
        from unittest.mock import MagicMock

        store = MagicMock()
        store.get_wallet.return_value = None

        result = services.get_wallet(store, f"eip155:1:{VITALIK}")
        # Should call store with checksummed plain address
        store.get_wallet.assert_called_once_with(VITALIK)

    def test_get_wallet_invalid(self):
        """get_wallet should return error for invalid address."""
        from minotaur_subnet.api import services
        from unittest.mock import MagicMock

        store = MagicMock()
        result = services.get_wallet(store, "not-valid")
        assert "error" in result

    def test_fund_wallet_caip10_token(self):
        """fund_wallet should accept CAIP-10 token address."""
        from minotaur_subnet.api import services
        from minotaur_subnet.shared.types import AppStatus, DeploymentResult
        from unittest.mock import MagicMock
        from dataclasses import dataclass

        store = MagicMock()
        store.get_app.return_value = MagicMock()
        store.get_deployment.return_value = MagicMock(status=AppStatus.ACTIVE)

        result = services.fund_wallet(
            store,
            app_id="app_test",
            token=f"eip155:1:{VITALIK}",
            amount="1000",
            chain_id=1,
        )
        assert "error" not in result
        assert result["token"] == VITALIK

    def test_fund_wallet_token_chain_mismatch(self):
        """fund_wallet should reject CAIP-10 token with wrong chain_id."""
        from minotaur_subnet.api import services
        from unittest.mock import MagicMock

        store = MagicMock()
        result = services.fund_wallet(
            store,
            app_id="app_test",
            token=f"eip155:8453:{VITALIK}",
            amount="1000",
            chain_id=1,
        )
        assert "error" in result
        assert "chain_id" in result["error"]

    def test_faucet_caip10(self):
        """faucet_eth should accept CAIP-10 address."""
        from minotaur_subnet.api import services
        import os

        # Without ANVIL_RPC_URL, it returns error about testnet
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ANVIL_RPC_URL", None)
            # Clear any pre-configured faucet URLs
            old_urls = services._faucet_rpc_urls.copy()
            services._faucet_rpc_urls.clear()
            try:
                result = services.faucet_eth(f"eip155:1:{VITALIK}")
                # Error about Anvil RPC, not about address format
                assert "Anvil RPC URL" in result.get("error", "")
            finally:
                services._faucet_rpc_urls.update(old_urls)

    def test_faucet_invalid(self):
        """faucet_eth should return error for invalid address."""
        from minotaur_subnet.api import services

        result = services.faucet_eth("bad-address")
        assert "error" in result


# ═══════════════════════════════════════════════════════════════════════════
#  OrderBook to_dict interop_address field
# ═══════════════════════════════════════════════════════════════════════════


# ═══════════════════════════════════════════════════════════════════════════
#  Token resolution
# ═══════════════════════════════════════════════════════════════════════════


class TestResolveToken:
    def test_plain_address(self):
        from minotaur_subnet.blockchain.tokens import resolve_token
        addr, cid = resolve_token(VITALIK, fallback_chain_id=1)
        assert addr == VITALIK
        assert cid == 1

    def test_caip10_address(self):
        from minotaur_subnet.blockchain.tokens import resolve_token
        addr, cid = resolve_token(f"eip155:8453:{VITALIK}")
        assert addr == VITALIK
        assert cid == 8453

    def test_symbol(self):
        from minotaur_subnet.blockchain.tokens import resolve_token
        addr, cid = resolve_token("USDC", fallback_chain_id=1)
        assert addr == "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
        assert cid == 1

    def test_chain_qualified_symbol(self):
        from minotaur_subnet.blockchain.tokens import resolve_token
        addr, cid = resolve_token("USDC@8453")
        assert addr == "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
        assert cid == 8453

    def test_unknown_symbol_raises(self):
        from minotaur_subnet.blockchain.tokens import resolve_token
        with pytest.raises(ValueError):
            resolve_token("NOTAREAL", fallback_chain_id=1)

    def test_empty_raises(self):
        from minotaur_subnet.blockchain.tokens import resolve_token
        with pytest.raises(ValueError, match="empty"):
            resolve_token("")


# ═══════════════════════════════════════════════════════════════════════════
#  OrderBook to_dict interop_address field
# ═══════════════════════════════════════════════════════════════════════════


class TestOrderToDict:
    def test_interop_address_in_to_dict(self):
        ob = IntentOrderBook()
        order = ob.submit(
            app_id="app_test",
            intent_function="execute",
            params={},
            submitted_by=VITALIK,
            chain_id=1,
        )
        d = order.to_dict()
        assert d["interop_address"] == f"eip155:1:{VITALIK}"

    def test_interop_address_different_chain(self):
        ob = IntentOrderBook()
        order = ob.submit(
            app_id="app_test",
            intent_function="execute",
            params={},
            submitted_by=VITALIK,
            chain_id=8453,
        )
        d = order.to_dict()
        assert d["interop_address"] == f"eip155:8453:{VITALIK}"

    def test_no_interop_address_without_chain(self):
        """If chain_id is 0 (falsy), no interop_address field."""
        from minotaur_subnet.orderbook.orderbook import Order, OrderStatus

        order = Order(
            order_id="ord_test",
            app_id="app_test",
            intent_function="execute",
            params={},
            submitted_by=VITALIK,
            chain_id=0,
        )
        d = order.to_dict()
        assert "interop_address" not in d
