"""E2E tests for MVP spec gap closures.

Tests cover:
  - Perpetual cooldown enforcement (OB-6, VAL-8)
  - Order persistence across BlockLoop restart (OB-11, OB-12)
  - Dual scoring gate: JS + on-chain (SCR-4, SCR-5, SCR-6, VAL-10)
  - App status check on order submit (API-7)

These tests do NOT require Anvil — they use in-memory stores and mock
simulation to exercise the Python pipeline logic.
"""

import asyncio
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pytest

from minotaur_subnet.blockloop.loop import BlockLoop
from minotaur_subnet.orderbook.orderbook import IntentOrderBook, OrderStatus
from minotaur_subnet.relayer.base import MockRelayer
from minotaur_subnet.store import AppIntentStore
from minotaur_subnet.shared.types import (
    AppIntentConfig,
    AppIntentDefinition,
    AppStatus,
    DeploymentResult,
    SimulationResult,
)


# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_store(tmp_path, app_id="test_app", active=True):
    """Create a store with a test app, optionally deployed & active."""
    store = AppIntentStore(store_path=tmp_path / "store.json")
    app_def = AppIntentDefinition(
        app_id=app_id,
        name="Test Swap",
        version="1.0.0",
        intent_type="swap",
        js_code="module.exports = { config: {name:'test'}, score: () => ({score:0.8, valid:true}) }",
        config=AppIntentConfig(supported_chains=[1], score_threshold=0.3),
    )
    store.save_app(app_def)
    if active:
        store.save_deployment(DeploymentResult(
            app_id=app_id,
            status=AppStatus.ACTIVE,
            contract_address="0x" + "ab" * 20,
        ))
    return store


def _make_loop(ob, store, simulator=None, threshold=0.5):
    return BlockLoop(
        orderbook=ob,
        app_store=store,
        relayer=MockRelayer(),
        simulator=simulator,
        tick_interval=1.0,
        score_threshold=threshold,
    )


# ── Perpetual Cooldown ────────────────────────────────────────────────────────


class TestPerpetualCooldownEnforced:
    """Verify that perpetual orders respect cooldown between fills (OB-6)."""

    @pytest.mark.asyncio
    async def test_cooldown_skips_order_then_processes(self, tmp_path):
        store = _make_store(tmp_path)
        ob = IntentOrderBook()
        loop = _make_loop(ob, store, threshold=0.3)

        order = ob.submit(
            app_id="test_app",
            intent_function="execute",
            params={"input_token": "WETH", "output_token": "USDC", "input_amount": "1000"},
            submitted_by="0xuser",
            perpetual=True,
            max_executions=10,
            cooldown=2.0,
        )

        # First fill
        result = await loop.tick()
        assert result.orders_processed == 1
        filled = ob.get(order.order_id)
        assert filled.last_filled_at > 0

        # Immediate second tick: cooldown should skip this order
        result2 = await loop.tick()
        assert result2.orders_processed == 0  # Skipped due to cooldown

        # Wait for cooldown to expire
        ob.update_order(order.order_id, last_filled_at=time.time() - 3.0)

        # Third tick: cooldown expired, should process
        result3 = await loop.tick()
        assert result3.orders_processed == 1


# ── Order Persistence ─────────────────────────────────────────────────────────


class TestOrderSurvivesRestart:
    """Verify that order state is persisted and survives BlockLoop restart (OB-11)."""

    @pytest.mark.asyncio
    async def test_order_persisted_after_fill(self, tmp_path):
        store = _make_store(tmp_path)
        ob = IntentOrderBook()
        loop = _make_loop(ob, store, threshold=0.3)

        ob.submit(
            app_id="test_app",
            intent_function="execute",
            params={"input_token": "WETH", "output_token": "USDC", "input_amount": "1000"},
            submitted_by="0xuser",
        )

        await loop.tick()

        # Verify persisted in store
        stored = store.list_orders()
        assert len(stored) >= 1
        order_dict = stored[0]
        assert order_dict["app_id"] == "test_app"
        # Should be in a terminal or advanced state
        assert order_dict["status"] in (
            "filled", "rejected", "scored", "approved", "solved",
        )

    @pytest.mark.asyncio
    async def test_order_readable_from_fresh_store(self, tmp_path):
        store = _make_store(tmp_path)
        ob = IntentOrderBook()
        loop = _make_loop(ob, store, threshold=0.3)

        order = ob.submit(
            app_id="test_app",
            intent_function="execute",
            params={},
            submitted_by="0xuser",
        )
        await loop.tick()

        # Create a fresh store pointing to the same file
        store2 = AppIntentStore(store_path=tmp_path / "store.json")
        persisted = store2.get_order(order.order_id)
        assert persisted is not None
        assert persisted["app_id"] == "test_app"


# ── Dual Scoring ──────────────────────────────────────────────────────────────


class TestDualScoringRejectsBadOnchainScore:
    """On-chain score below threshold should reject even if JS passes (SCR-5)."""

    @pytest.mark.asyncio
    async def test_rejects_low_onchain_score(self, tmp_path):
        store = _make_store(tmp_path)
        ob = IntentOrderBook()

        mock_sim = AsyncMock()
        mock_sim.simulate = AsyncMock(return_value=SimulationResult(
            success=True,
            gas_used=100000,
            on_chain_score=3000,  # Below default 5000 BPS threshold
        ))

        loop = _make_loop(ob, store, simulator=mock_sim, threshold=0.3)

        ob.submit(
            app_id="test_app",
            intent_function="execute",
            params={"input_token": "WETH", "output_token": "USDC", "input_amount": "1000"},
            submitted_by="0xuser",
        )

        result = await loop.tick()
        assert result.orders_rejected == 1

        rejected = ob.list_orders(status="rejected")
        assert len(rejected) >= 1
        assert "On-chain score" in (rejected[0].error or "")

    @pytest.mark.asyncio
    async def test_passes_both_gates(self, tmp_path):
        store = _make_store(tmp_path)
        ob = IntentOrderBook()

        mock_sim = AsyncMock()
        mock_sim.simulate = AsyncMock(return_value=SimulationResult(
            success=True,
            gas_used=100000,
            on_chain_score=8000,  # Above 5000 threshold
        ))

        loop = _make_loop(ob, store, simulator=mock_sim, threshold=0.3)

        ob.submit(
            app_id="test_app",
            intent_function="execute",
            params={"input_token": "WETH", "output_token": "USDC", "input_amount": "1000"},
            submitted_by="0xuser",
        )

        result = await loop.tick()
        assert result.orders_approved == 1


# ── App Status on Order Submit ────────────────────────────────────────────────


class TestOrderRejectedForDraftApp:
    """Orders should be rejected for apps that are not deployed/active (API-7)."""

    def test_draft_app_rejected(self, tmp_path):
        """Submit order for a DRAFT app via the API → 400."""
        import os
        os.environ["DISABLE_BENCHMARK_WORKER"] = "1"
        os.environ["DISABLE_BLOCK_LOOP"] = "1"

        from fastapi.testclient import TestClient
        from minotaur_subnet.api.server import app
        from minotaur_subnet.api.routes import orders as orders_module

        store = _make_store(tmp_path, app_id="draft-app", active=False)
        ob = IntentOrderBook()
        orders_module.set_orderbook(ob)
        orders_module.set_app_store(store)

        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post("/v1/apps/draft-app/orders", json={
            "submitted_by": "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045",
            "params": {},
        })
        assert resp.status_code == 400
        detail = resp.json()["detail"]
        assert "not ready for orders" in detail
        assert "not deployed" in detail

        orders_module.set_orderbook(None)
        orders_module.set_app_store(None)


# ── Perpetual Max Executions ─────────────────────────────────────────────────


class TestPerpetualMaxExecutions:
    """Verify OB-7: perpetual orders that reach max_executions stay FILLED."""

    @pytest.mark.asyncio
    async def test_perpetual_stays_filled_at_max(self, tmp_path):
        store = _make_store(tmp_path)
        ob = IntentOrderBook()
        loop = _make_loop(ob, store, threshold=0.3)

        order = ob.submit(
            app_id="test_app",
            intent_function="execute",
            params={"input_token": "WETH", "output_token": "USDC", "input_amount": "1000"},
            submitted_by="0xuser",
            perpetual=True,
            max_executions=2,
            cooldown=0.0,
        )

        # First fill
        await loop.tick()
        o = ob.get(order.order_id)
        assert o.execution_count == 1
        assert o.status == OrderStatus.OPEN  # Re-opened (1 < 2)

        # Second fill
        await loop.tick()
        o = ob.get(order.order_id)
        assert o.execution_count == 2
        assert o.status == OrderStatus.FILLED  # max reached, stays FILLED

        # Third tick: should not process (order is FILLED, not OPEN)
        result = await loop.tick()
        assert result.orders_processed == 0


# ── Hot-Reload E2E ───────────────────────────────────────────────────────────


class TestHotReloadAppsE2E:
    """Verify VAL-17/18: new apps are loaded and JS updates are detected."""

    @pytest.mark.asyncio
    async def test_new_app_loaded_on_tick(self, tmp_path):
        """A newly deployed app's JS is loaded after the reload interval."""
        from unittest.mock import MagicMock, AsyncMock

        store = _make_store(tmp_path, app_id="app1")
        ob = IntentOrderBook()

        mock_engine = MagicMock()
        mock_engine.load_intent = AsyncMock()
        mock_engine._intents = {}

        loop = BlockLoop(
            orderbook=ob,
            app_store=store,
            relayer=MockRelayer(),
            js_engine=mock_engine,
            tick_interval=1.0,
        )
        loop._reload_interval = 1  # Reload every tick

        # First tick triggers reload — should load app1's JS
        await loop.tick()
        mock_engine.load_intent.assert_called_once()
        assert "app1" in loop._known_js_hashes


# ── Leader-Only Weights E2E ──────────────────────────────────────────────────


class TestLeaderOnlyWeightsE2E:
    """Verify BT-8: follower validators skip weight emission."""

    @pytest.mark.asyncio
    async def test_follower_skips_weights(self):
        from unittest.mock import MagicMock
        from minotaur_subnet.validator.weights_emitter import WeightsEmitter

        mock_sync = MagicMock()
        mock_sync.is_leader = False

        emitter = WeightsEmitter(
            wallet=MagicMock(),
            subtensor=MagicMock(),
            metagraph_sync=mock_sync,
        )

        result = await emitter.emit_async({"miner1": 0.8, "miner2": 0.2})
        assert result is False  # Skipped because not leader


# ── EIP-712 Wallet Signing E2E ───────────────────────────────────────────────


class TestLitWalletSigningE2E:
    """Verify WAL-6: embedded wallet can sign EIP-712 orders."""

    @pytest.mark.asyncio
    async def test_sign_and_verify_roundtrip(self):
        """Sign an order with Lit wallet (fallback) and verify the signature."""
        from minotaur_subnet.wallet.lit_wallet import LitMpcWallet
        from minotaur_subnet.consensus.eip712 import (
            hash_order_struct,
            build_domain_separator,
            _to_typed_data_hash,
        )
        from eth_account import Account

        wallet = LitMpcWallet(
            bridge_url="http://localhost:99999",  # unreachable → local fallback
            allow_fallback=True,
        )

        info = await wallet.create_wallet(chain_ids=[1])
        address = info.address

        contract = "0x5FbDB2315678afecb367f032d93F642f64180aa3"
        chain_id = 1
        order_id = b"\x01" * 32
        selector = bytes.fromhex("12345678")
        params = b'{"input_token":"WETH"}'

        sig_hex = await wallet.sign_eip712_order(
            address=address,
            order_id=order_id,
            app=contract,
            intent_selector=selector,
            intent_params=params,
            submitted_by=address,
            chain_id=chain_id,
            deadline=2000000000,
            nonce=0,
            perpetual=False,
            max_executions=1,
            cooldown=0,
            contract_address=contract,
        )

        # Verify: recover signer from signature
        sig_bytes = bytes.fromhex(sig_hex.replace("0x", ""))
        struct_hash = hash_order_struct(
            order_id, contract, selector, params, address,
            chain_id, 2000000000, 0, False, 1, 0,
        )
        domain_sep = build_domain_separator(chain_id, contract)
        digest = _to_typed_data_hash(domain_sep, struct_hash)
        recovered = Account._recover_hash(digest, signature=sig_bytes)
        assert recovered.lower() == address.lower()
