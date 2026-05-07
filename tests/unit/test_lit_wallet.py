"""Tests for LitMpcWallet — Lit Protocol MPC wallet integration."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import asyncio
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from minotaur_subnet.wallet.lit_wallet import (
    LitMpcWallet,
    BridgeUnavailableError,
    BridgeError,
    SignResult,
)
from minotaur_subnet.shared.types import WalletInfo


# ── Mock Bridge Helpers ──────────────────────────────────────────────────────


class MockResponse:
    """Mock aiohttp response."""

    def __init__(self, data: dict, status: int = 200):
        self._data = data
        self.status = status

    async def json(self):
        return self._data

    async def text(self):
        return json.dumps(self._data)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


class MockSession:
    """Mock aiohttp.ClientSession."""

    def __init__(self, responses: dict[str, MockResponse] | None = None):
        self._responses = responses or {}
        self.requests: list[tuple[str, str, dict | None]] = []

    def request(self, method: str, url: str, **kwargs):
        self.requests.append((method, url, kwargs.get("json")))
        # Match by path
        for pattern, response in self._responses.items():
            if pattern in url:
                return response
        return MockResponse({"error": "not found"}, status=404)

    async def close(self):
        pass


# ── Tests ────────────────────────────────────────────────────────────────────


class TestLitMpcWalletWithBridge:

    @pytest.mark.asyncio
    async def test_create_wallet_via_bridge(self):
        """Create wallet calls the bridge POST /wallets."""
        session = MockSession({
            "/wallets": MockResponse({
                "address": "0x" + "ab" * 20,
                "pkp_token_id": "123",
                "public_key": "0x04" + "cd" * 32,
            }),
        })

        wallet = LitMpcWallet(
            bridge_url="http://localhost:3100",
            session=session,
        )

        info = await wallet.create_wallet(chain_ids=[1, 8453])

        assert info.address == "0x" + "ab" * 20
        assert info.wallet_type == "lit_mpc"
        assert info.chain_ids == [1, 8453]
        assert len(session.requests) == 1
        method, url, body = session.requests[0]
        assert method == "POST"
        assert "/wallets" in url
        assert body["chain_ids"] == [1, 8453]

    @pytest.mark.asyncio
    async def test_sign_transaction_via_bridge(self):
        """Sign transaction calls the bridge POST /sign/transaction."""
        session = MockSession({
            "/sign/transaction": MockResponse({
                "signed_tx": "0xf8" + "aa" * 100,
            }),
        })

        wallet = LitMpcWallet(session=session)
        signed = await wallet.sign_transaction(
            "0x" + "ab" * 20,
            {"to": "0x" + "cd" * 20, "value": 1000},
            chain_id=1,
        )

        assert signed.startswith("0xf8")
        assert len(session.requests) == 1

    @pytest.mark.asyncio
    async def test_sign_message_via_bridge(self):
        """Sign message calls the bridge POST /sign/message."""
        session = MockSession({
            "/sign/message": MockResponse({
                "signature": "0x" + "ee" * 65,
            }),
        })

        wallet = LitMpcWallet(session=session)
        sig = await wallet.sign_message("0x" + "ab" * 20, "Hello Minotaur")

        assert sig == "0x" + "ee" * 65

    @pytest.mark.asyncio
    async def test_sign_message_bytes(self):
        """Sign message works with bytes input."""
        session = MockSession({
            "/sign/message": MockResponse({
                "signature": "0x" + "ff" * 65,
            }),
        })

        wallet = LitMpcWallet(session=session)
        sig = await wallet.sign_message("0x" + "ab" * 20, b"\x01\x02\x03")

        assert sig == "0x" + "ff" * 65
        # Verify message_hex was sent
        _, _, body = session.requests[0]
        assert body["message_hex"] == "010203"

    @pytest.mark.asyncio
    async def test_get_wallet_from_cache(self):
        """get_wallet returns cached PKP info without calling bridge."""
        session = MockSession({
            "/wallets": MockResponse({
                "address": "0x" + "ab" * 20,
                "pkp_token_id": "123",
                "public_key": "0x04" + "cd" * 32,
            }),
        })

        wallet = LitMpcWallet(session=session)
        await wallet.create_wallet(chain_ids=[1])

        # Should come from cache, no extra request
        info = await wallet.get_wallet("0x" + "ab" * 20)
        assert info is not None
        assert info.wallet_type == "lit_mpc"
        assert len(session.requests) == 1  # Only the create request

    @pytest.mark.asyncio
    async def test_list_wallets_via_bridge(self):
        """list_wallets calls GET /wallets on the bridge."""
        session = MockSession({
            "/wallets": MockResponse({
                "wallets": [
                    {"address": "0x" + "aa" * 20, "chain_ids": [1]},
                    {"address": "0x" + "bb" * 20, "chain_ids": [8453]},
                ],
            }),
        })

        wallet = LitMpcWallet(session=session)
        wallets = await wallet.list_wallets()

        assert len(wallets) == 2
        assert wallets[0].address == "0x" + "aa" * 20
        assert wallets[1].chain_ids == [8453]

    @pytest.mark.asyncio
    async def test_health_check(self):
        """Health endpoint reports bridge status."""
        session = MockSession({
            "/health": MockResponse({"lit_network": "datil-dev", "connected": True}),
        })

        wallet = LitMpcWallet(session=session)
        health = await wallet.health()

        assert health["status"] == "ok"
        assert health["bridge"] == "connected"


class TestLitMpcWalletFallback:

    @pytest.mark.asyncio
    async def test_fallback_on_bridge_unavailable(self):
        """Falls back to LocalWalletManager when bridge is unreachable."""
        wallet = LitMpcWallet(
            bridge_url="http://localhost:19999",  # No bridge
            allow_fallback=True,
        )

        # Mock the local fallback to avoid needing cryptography
        mock_local = AsyncMock()
        mock_local.create_wallet = AsyncMock(return_value=WalletInfo(
            address="0x" + "cc" * 20,
            chain_ids=[1],
            wallet_type="local",
            created_at=1000.0,
        ))
        wallet._local_fallback = mock_local

        info = await wallet.create_wallet(chain_ids=[1])

        assert info.address == "0x" + "cc" * 20
        assert info.wallet_type == "local"

    @pytest.mark.asyncio
    async def test_no_fallback_raises(self):
        """When fallback disabled, bridge unavailable raises error."""
        wallet = LitMpcWallet(
            bridge_url="http://localhost:19999",
            allow_fallback=False,
        )

        with pytest.raises(BridgeUnavailableError):
            await wallet.create_wallet(chain_ids=[1])

    @pytest.mark.asyncio
    async def test_health_degraded_without_bridge(self):
        """Health reports degraded when bridge is unreachable."""
        wallet = LitMpcWallet(
            bridge_url="http://localhost:19999",
            allow_fallback=True,
        )

        health = await wallet.health()

        assert health["status"] == "degraded"
        assert health["bridge"] == "unavailable"
        assert health["fallback"] is True


class TestLitMpcWalletBridgeErrors:

    @pytest.mark.asyncio
    async def test_bridge_400_error(self):
        """Bridge 400 error raises BridgeError, falls back."""
        session = MockSession({
            "/wallets": MockResponse({"error": "bad request"}, status=400),
        })

        wallet = LitMpcWallet(session=session, allow_fallback=True)

        # Mock local fallback
        mock_local = AsyncMock()
        mock_local.create_wallet = AsyncMock(return_value=WalletInfo(
            address="0x" + "dd" * 20,
            chain_ids=[1],
            wallet_type="local",
        ))
        wallet._local_fallback = mock_local

        # BridgeError from 400 should propagate (not caught by fallback)
        with pytest.raises(BridgeError):
            await wallet.create_wallet(chain_ids=[1])

    @pytest.mark.asyncio
    async def test_get_wallet_not_found(self):
        """get_wallet returns None for unknown address."""
        session = MockSession({})  # No matching routes

        wallet = LitMpcWallet(session=session)
        result = await wallet.get_wallet("0x" + "ab" * 20)

        assert result is None
