"""Tests for API routes: health, orders, apps, intents, wallets, monitoring.

Uses FastAPI TestClient with disabled background workers.
Follows the same test patterns as test_submissions.py.
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# Ensure repo root is importable
_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# Disable background workers before importing the app
os.environ["DISABLE_BENCHMARK_WORKER"] = "1"
os.environ["DISABLE_BLOCK_LOOP"] = "1"

from fastapi.testclient import TestClient

from minotaur_subnet.api.server import app
from minotaur_subnet.api.routes import orders as orders_module
from minotaur_subnet.api.routes import apps as apps_module
from minotaur_subnet.orderbook.orderbook import IntentOrderBook


# ═══════════════════════════════════════════════════════════════════════════════
#                            HEALTH ENDPOINT
# ═══════════════════════════════════════════════════════════════════════════════


class TestHealthEndpoint(unittest.TestCase):
    """Tests for GET /health."""

    def setUp(self):
        self.client = TestClient(app, raise_server_exceptions=False)

    def test_health_returns_ok(self):
        resp = self.client.get("/health")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["status"], "ok")
        self.assertEqual(data["service"], "app-intents-api")
        self.assertIn("provenance_policy", data)
        policy = data["provenance_policy"]
        self.assertIsInstance(policy["startup_validated"], bool)
        self.assertIn("valid", policy)
        self.assertIn(
            policy["mode"],
            ["unknown", "optional", "signed_required", "asymmetric_only"],
        )
        self.assertIsInstance(policy["allowed_signers_count"], int)
        self.assertIn("runtime_security_policy", data)
        runtime = data["runtime_security_policy"]
        self.assertIsInstance(runtime["startup_validated"], bool)
        self.assertIn("enforced", runtime)
        self.assertIn("valid", runtime)
        self.assertIsInstance(runtime["violations"], list)

    def test_health_shows_workers_disabled(self):
        resp = self.client.get("/health")
        data = resp.json()
        self.assertEqual(data["benchmark_worker"], "disabled")
        self.assertEqual(data["block_loop"], "disabled")
        self.assertIn("provenance_policy", data)
        self.assertIn("runtime_security_policy", data)


# ═══════════════════════════════════════════════════════════════════════════════
#                            ORDER ROUTES
# ═══════════════════════════════════════════════════════════════════════════════


class TestOrderRoutes(unittest.TestCase):
    """Tests for order CRUD endpoints."""

    def setUp(self):
        self.ob = IntentOrderBook()
        orders_module.set_orderbook(self.ob)
        self.client = TestClient(app, raise_server_exceptions=False)
        # M3 (PR #26) added an EIP-191 owner-signature requirement on
        # DELETE /orders/{id}. The route tests below don't reproduce the
        # signing flow — that's covered by tests/unit/test_order_owner_sig.py.
        # Disable the gate here so these tests stay focused on the routing
        # + error-handling surface they were originally written for.
        os.environ["REQUIRE_ORDER_OWNER_SIG"] = "0"

    def tearDown(self):
        orders_module.set_orderbook(None)
        os.environ.pop("REQUIRE_ORDER_OWNER_SIG", None)

    def test_submit_order(self):
        resp = self.client.post("/v1/apps/test-app/orders", json={
            "submitted_by": "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045",
            "intent_function": "swap",
            "params": {"token_in": "WETH"},
            "chain_id": 1,
        })
        self.assertEqual(resp.status_code, 201)
        data = resp.json()
        self.assertIn("order_id", data)
        self.assertEqual(data["app_id"], "test-app")
        self.assertEqual(data["status"], "open")

    def _submit(self, app_id="app-1", submitted_by="0xUser"):
        """Helper to submit an order with required fields."""
        return self.ob.submit(
            app_id=app_id,
            intent_function="swap",
            params={"token_in": "WETH"},
            submitted_by=submitted_by,
        )

    def test_get_order(self):
        order = self._submit(app_id="app-1", submitted_by="0xUser")
        resp = self.client.get(f"/v1/orders/{order.order_id}")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["order_id"], order.order_id)

    def test_get_order_not_found(self):
        resp = self.client.get("/v1/orders/nonexistent")
        self.assertEqual(resp.status_code, 404)

    def test_list_orders(self):
        self._submit(app_id="app-1", submitted_by="0xA")
        self._submit(app_id="app-2", submitted_by="0xB")
        resp = self.client.get("/v1/orders")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["count"], 2)
        self.assertEqual(len(data["orders"]), 2)

    def test_public_orders_serves_failed_and_strips_sig(self):
        # The order book is PUBLIC (no auth) — followers pull /v1/orders to build
        # their benchmark corpus, so it must include FAILED orders (rejected/expired,
        # which never reach the chain) and strip user_signature.
        from unittest.mock import MagicMock
        store = MagicMock()
        store.list_orders.return_value = [
            {"order_id": "a", "status": "filled", "user_signature": "0xsecret"},
            {"order_id": "b", "status": "rejected", "user_signature": "0xalso"},
        ]
        orders_module.set_app_store(store)
        try:
            # No auth required, returns both orders (incl. the rejected one), sig stripped
            resp = self.client.get("/v1/orders")
            self.assertEqual(resp.status_code, 200)
            orders = resp.json()["orders"]
            self.assertEqual({o["order_id"] for o in orders}, {"a", "b"})
            # signature blanked (not leaked) for every entry
            self.assertTrue(all(not o.get("user_signature") for o in orders))
        finally:
            orders_module.set_app_store(None)

    def test_list_orders_filter_by_app(self):
        self._submit(app_id="app-1", submitted_by="0xA")
        self._submit(app_id="app-2", submitted_by="0xB")
        resp = self.client.get("/v1/orders?app_id=app-1")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["count"], 1)

    def test_list_orders_filter_by_status(self):
        o1 = self._submit(app_id="app-1", submitted_by="0xA")
        self._submit(app_id="app-1", submitted_by="0xB")
        # IntentOrderBook.cancel requires submitted_by (owner check at the
        # orderbook layer) — pass the original submitter.
        self.ob.cancel(o1.order_id, submitted_by="0xA")
        resp = self.client.get("/v1/orders?status=open")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["count"], 1)

    def test_cancel_order(self):
        order = self._submit(app_id="app-1", submitted_by="0xA")
        # M3 (PR #26): submitted_by is a required query param on the
        # cancel route. The EIP-191 owner-signature requirement is
        # disabled in setUp via REQUIRE_ORDER_OWNER_SIG=0.
        resp = self.client.delete(
            f"/v1/orders/{order.order_id}?submitted_by=0xA",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "cancelled")

    def test_cancel_nonexistent_order(self):
        resp = self.client.delete(
            "/v1/orders/nonexistent?submitted_by=0xA",
        )
        # The route now 404s on unknown order before reaching the
        # orderbook (M3 added that check).
        self.assertEqual(resp.status_code, 404)

    def test_orderbook_not_initialized_503(self):
        orders_module.set_orderbook(None)
        resp = self.client.get("/v1/orders")
        self.assertEqual(resp.status_code, 503)
        self.assertIn("not initialized", resp.json()["detail"])


# ═══════════════════════════════════════════════════════════════════════════════
#                            APP ROUTES
# ═══════════════════════════════════════════════════════════════════════════════


class TestAppRoutes(unittest.TestCase):
    """Tests for app CRUD endpoints."""

    def setUp(self):
        self.client = TestClient(app, raise_server_exceptions=False)

    def _create_app_payload(self, name="Test Swap"):
        """Build a create-app payload with pre-supplied code (no API key needed)."""
        return {
            "name": name,
            "supported_chains": [1, 8453],
            "js_code": "module.exports = { config: {name: 'swap'}, score: () => ({score: 0.8, valid: true}) }",
            "solidity_code": "// SPDX-License-Identifier: MIT\npragma solidity ^0.8.24;\ncontract TestApp {}",
        }

    def test_create_app(self):
        resp = self.client.post("/v1/apps/", json=self._create_app_payload())
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("app_id", data)
        self.assertEqual(data["name"], "Test Swap")

    def test_list_apps(self):
        resp = self.client.get("/v1/apps/")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("apps", data)

    def test_deploy_app_not_found(self):
        resp = self.client.post("/v1/apps/nonexistent/deploy")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        # The tools function returns an error dict rather than raising
        self.assertIn("error", data)

    def test_get_app_status(self):
        # Create an app first
        create_resp = self.client.post("/v1/apps/", json=self._create_app_payload("Status Test"))
        app_id = create_resp.json()["app_id"]
        resp = self.client.get(f"/v1/apps/{app_id}/status")
        self.assertEqual(resp.status_code, 200)


# ═══════════════════════════════════════════════════════════════════════════════
#                     DEPLOYER AUTHORIZATION
# ═══════════════════════════════════════════════════════════════════════════════


class TestDeployerAuthorization(unittest.TestCase):
    """Tests for deployer-based authorization on update_scoring."""

    def setUp(self):
        self.client = TestClient(app, raise_server_exceptions=False)

    def _create_app_payload(self, deployer=""):
        payload = {
            "name": "Auth Test",
            "supported_chains": [1],
            "js_code": "module.exports = { config: {name: 'test'}, score: () => ({score: 0.8, valid: true}) }",
            "solidity_code": "// SPDX-License-Identifier: MIT\npragma solidity ^0.8.24;\ncontract T {}",
        }
        if deployer:
            payload["deployer"] = deployer
        return payload

    def test_create_app_stores_deployer(self):
        """Creating an app with deployer sets it on the definition."""
        resp = self.client.post("/v1/apps/", json=self._create_app_payload(
            deployer="0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045",
        ))
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["deployer"], "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045")

    def test_create_app_no_deployer(self):
        """Creating an app without deployer stores empty string."""
        resp = self.client.post("/v1/apps/", json=self._create_app_payload())
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["deployer"], "")

    def test_update_scoring_authorized(self):
        """Deployer can update their own app's JS."""
        deployer = "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045"
        resp = self.client.post("/v1/apps/", json=self._create_app_payload(deployer=deployer))
        app_id = resp.json()["app_id"]

        resp = self.client.put(f"/v1/apps/{app_id}/scoring", json={
            "new_js_code": "module.exports = { score: () => ({score: 0.9, valid: true}) }",
            "caller": deployer,
        })
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["status"], "updated")

    def test_update_scoring_unauthorized(self):
        """Non-deployer cannot update the app's JS."""
        deployer = "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045"
        resp = self.client.post("/v1/apps/", json=self._create_app_payload(deployer=deployer))
        app_id = resp.json()["app_id"]

        resp = self.client.put(f"/v1/apps/{app_id}/scoring", json={
            "new_js_code": "module.exports = { score: () => ({score: 0.9, valid: true}) }",
            "caller": "0x1234567890123456789012345678901234567890",
        })
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("error", data)
        self.assertIn("Unauthorized", data["error"])

    def test_update_scoring_no_caller_with_deployer_set(self):
        """Missing caller when deployer is set should be rejected."""
        deployer = "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045"
        resp = self.client.post("/v1/apps/", json=self._create_app_payload(deployer=deployer))
        app_id = resp.json()["app_id"]

        resp = self.client.put(f"/v1/apps/{app_id}/scoring", json={
            "new_js_code": "module.exports = { score: () => ({score: 0.9, valid: true}) }",
        })
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("error", data)
        self.assertIn("Unauthorized", data["error"])

    def test_update_scoring_no_deployer_allows_anyone(self):
        """Apps created without deployer allow anyone to update (backward compat)."""
        resp = self.client.post("/v1/apps/", json=self._create_app_payload())
        app_id = resp.json()["app_id"]

        resp = self.client.put(f"/v1/apps/{app_id}/scoring", json={
            "new_js_code": "module.exports = { score: () => ({score: 0.9, valid: true}) }",
        })
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["status"], "updated")

    def test_update_scoring_case_insensitive(self):
        """Deployer check should be case-insensitive (EIP-55 mixed case)."""
        deployer_lower = "0xd8da6bf26964af9d7eed9e03e53415d37aa96045"
        deployer_mixed = "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045"

        resp = self.client.post("/v1/apps/", json=self._create_app_payload(deployer=deployer_mixed))
        app_id = resp.json()["app_id"]

        resp = self.client.put(f"/v1/apps/{app_id}/scoring", json={
            "new_js_code": "module.exports = { score: () => ({score: 0.9, valid: true}) }",
            "caller": deployer_lower,
        })
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["status"], "updated")


# ═══════════════════════════════════════════════════════════════════════════════
#                          WALLET ROUTES
# ═══════════════════════════════════════════════════════════════════════════════


class TestWalletRoutes(unittest.TestCase):
    """Tests for wallet management endpoints."""

    def setUp(self):
        self.client = TestClient(app, raise_server_exceptions=False)

    def test_create_wallet(self):
        resp = self.client.post("/v1/wallets/", json={
            "chain_ids": [1, 8453],
        })
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("address", data)

    def test_get_wallet_not_found(self):
        resp = self.client.get("/v1/wallets/0x" + "0" * 40)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("error", data)


# ═══════════════════════════════════════════════════════════════════════════════
#                        MONITORING ROUTES
# ═══════════════════════════════════════════════════════════════════════════════


class TestManifestRoutes(unittest.TestCase):
    """Tests for manifest extraction endpoints."""

    def setUp(self):
        self.client = TestClient(app, raise_server_exceptions=False)

    def _create_app_with_manifest(self):
        """Create an app with a manifest-bearing JS module."""
        js_code = (
            'module.exports = { config: {name: "swap"}, '
            'manifest: { intent_functions: [{name: "swap", params: {}, example_params: {}}], '
            'supported_chains: [1] }, '
            'score() { return {score: 0.8, valid: true}; } };'
        )
        resp = self.client.post("/v1/apps/", json={
            "name": "Manifest Test",
            "supported_chains": [1],
            "js_code": js_code,
            "solidity_code": "// SPDX-License-Identifier: MIT\npragma solidity ^0.8.24;\ncontract T {}",
        })
        return resp.json()["app_id"]

    def test_get_manifest(self):
        app_id = self._create_app_with_manifest()
        resp = self.client.get(f"/v1/apps/{app_id}/manifest")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["app_id"], app_id)
        self.assertIsNotNone(data["manifest"])

    def test_get_manifest_not_found(self):
        resp = self.client.get("/v1/apps/nonexistent/manifest")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("error", data)

    def test_list_manifests(self):
        self._create_app_with_manifest()
        resp = self.client.get("/v1/apps/manifests")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("manifests", data)
        self.assertIn("total", data)


class TestScorePlanRoutes(unittest.TestCase):
    """Tests for POST /v1/apps/{app_id}/score."""

    def setUp(self):
        self.client = TestClient(app, raise_server_exceptions=False)

    def tearDown(self):
        apps_module.set_js_engine(None)
        apps_module.set_simulator(None)

    def test_score_app_not_found(self):
        resp = self.client.post("/v1/apps/nonexistent/score", json={
            "plan": {
                "intent_id": "nonexistent",
                "interactions": [{"target": "0xabc", "value": "0", "call_data": "0x"}],
                "deadline": 0,
                "nonce": 0,
            },
            "params": {},
        })
        self.assertEqual(resp.status_code, 404)

    def test_score_no_js_engine(self):
        # Create an app first
        create_resp = self.client.post("/v1/apps/", json={
            "name": "Score Test App",
            "supported_chains": [1],
            "js_code": "module.exports = { score: () => ({score: 0.8, valid: true}) }",
            "solidity_code": "contract ScoreTestApp {}",
        })
        app_id = create_resp.json()["app_id"]
        apps_module.set_js_engine(None)

        resp = self.client.post(f"/v1/apps/{app_id}/score", json={
            "plan": {
                "intent_id": app_id,
                "interactions": [{"target": "0xabc", "value": "0", "call_data": "0x"}],
            },
            "params": {},
        })
        self.assertEqual(resp.status_code, 503)
        self.assertIn("not available", resp.json()["detail"])

    def test_score_with_mock_engine(self):
        from unittest.mock import AsyncMock
        from minotaur_subnet.shared.types import ScoreResult

        # Create an app
        create_resp = self.client.post("/v1/apps/", json={
            "name": "Scorable App",
            "supported_chains": [1],
            "js_code": "module.exports = { score: () => ({score: 0.85}) }",
            "solidity_code": "contract ScorableApp {}",
        })
        app_id = create_resp.json()["app_id"]

        # Mock JS engine
        mock_engine = MagicMock()
        mock_engine._intents = {app_id: "// js"}
        mock_engine.score = AsyncMock(return_value=ScoreResult(
            score=0.85,
            valid=True,
            reason="Good plan",
            breakdown={"efficiency": 0.9, "gas": 0.8},
        ))
        apps_module.set_js_engine(mock_engine)

        resp = self.client.post(f"/v1/apps/{app_id}/score", json={
            "plan": {
                "intent_id": app_id,
                "interactions": [
                    {
                        "target": "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",
                        "value": "1000000000000000",
                        "call_data": "0xd0e30db0",
                        "chain_id": 1,
                    },
                ],
                "deadline": 9999999999,
                "nonce": 1,
            },
            "params": {},
        })
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["app_id"], app_id)
        self.assertEqual(data["score"], 0.85)
        self.assertTrue(data["valid"])
        self.assertEqual(data["breakdown"]["efficiency"], 0.9)
        self.assertEqual(data["simulation_mode"], "mock")
        self.assertIn("simulation", data)

    def test_score_engine_loads_intent(self):
        """Engine loads JS code if not already loaded."""
        from unittest.mock import AsyncMock
        from minotaur_subnet.shared.types import ScoreResult

        create_resp = self.client.post("/v1/apps/", json={
            "name": "Lazy Load App",
            "supported_chains": [1],
            "js_code": "module.exports = { score: () => ({score: 0.5}) }",
            "solidity_code": "contract LazyLoadApp {}",
        })
        app_id = create_resp.json()["app_id"]

        mock_engine = MagicMock()
        mock_engine._intents = {}  # Not loaded yet
        mock_engine.load_intent = AsyncMock()
        mock_engine.score = AsyncMock(return_value=ScoreResult(score=0.5, valid=True))
        apps_module.set_js_engine(mock_engine)

        resp = self.client.post(f"/v1/apps/{app_id}/score", json={
            "plan": {
                "interactions": [{"target": "0x00", "value": "0", "call_data": "0x"}],
            },
            "params": {},
        })
        self.assertEqual(resp.status_code, 200)
        mock_engine.load_intent.assert_called_once()

    def test_score_params_in_body_go_to_structured_state(self):
        """body.params should populate state.raw_params/control, not plan metadata."""
        from unittest.mock import AsyncMock
        from minotaur_subnet.shared.types import ScoreResult

        create_resp = self.client.post("/v1/apps/", json={
            "name": "Params Test App",
            "supported_chains": [1],
            "js_code": "module.exports = { score: () => ({score: 0.7}) }",
            "solidity_code": "contract ParamsTestApp {}",
        })
        app_id = create_resp.json()["app_id"]

        mock_engine = MagicMock()
        mock_engine._intents = {app_id: "// js"}
        mock_engine.score = AsyncMock(return_value=ScoreResult(score=0.7, valid=True))
        apps_module.set_js_engine(mock_engine)

        resp = self.client.post(f"/v1/apps/{app_id}/score", json={
            "plan": {
                "interactions": [{"target": "0x00", "value": "0", "call_data": "0x"}],
                "metadata": {"should_not_be_used": True},
            },
            "params": {"input_token": "WETH", "output_token": "USDC"},
            "intent_function": "swap",
        })
        self.assertEqual(resp.status_code, 200)

        # Verify structured state was built from body.params
        call_args = mock_engine.score.call_args
        state_arg = call_args[0][3]  # 4th positional: state
        self.assertEqual(state_arg.raw_params["input_token"], "WETH")
        self.assertEqual(state_arg.raw_params["output_token"], "USDC")
        self.assertEqual(state_arg.control["_intent_function"], "swap")
        self.assertNotIn("should_not_be_used", state_arg.raw_params)

    def test_score_with_simulator(self):
        """When simulator is available, simulation_mode should be 'anvil'."""
        from unittest.mock import AsyncMock
        from minotaur_subnet.shared.types import ScoreResult, SimulationResult

        create_resp = self.client.post("/v1/apps/", json={
            "name": "Sim Test App",
            "supported_chains": [1],
            "js_code": "module.exports = { score: () => ({score: 0.9}) }",
            "solidity_code": "contract SimTestApp {}",
        })
        app_id = create_resp.json()["app_id"]

        mock_engine = MagicMock()
        mock_engine._intents = {app_id: "// js"}
        mock_engine.score = AsyncMock(return_value=ScoreResult(score=0.9, valid=True))
        apps_module.set_js_engine(mock_engine)

        mock_sim = MagicMock()
        mock_sim.simulate = AsyncMock(return_value=SimulationResult(
            success=True, gas_used=150000, on_chain_score=8500,
        ))
        apps_module.set_simulator(mock_sim)

        try:
            resp = self.client.post(f"/v1/apps/{app_id}/score", json={
                "plan": {
                    "interactions": [{"target": "0x00", "value": "0", "call_data": "0x"}],
                },
                "params": {},
            })
            self.assertEqual(resp.status_code, 200)
            data = resp.json()
            self.assertEqual(data["simulation_mode"], "anvil")
            self.assertEqual(data["simulation"]["gas_used"], 150000)
            self.assertEqual(data["simulation"]["on_chain_score"], 8500)
            mock_sim.simulate.assert_called_once()
        finally:
            apps_module.set_simulator(None)

    def test_score_surfaces_revert_reason(self):
        """A real-sim revert exposes the decoded reason in simulation.revert_reason
        so a miner can debug WHY their plan failed without their own node."""
        from unittest.mock import AsyncMock
        from minotaur_subnet.shared.types import ScoreResult, SimulationResult

        create_resp = self.client.post("/v1/apps/", json={
            "name": "Revert Reason App",
            "supported_chains": [1],
            "js_code": "module.exports = { score: () => ({score: 0.0}) }",
            "solidity_code": "contract RevertReasonApp {}",
        })
        app_id = create_resp.json()["app_id"]

        mock_engine = MagicMock()
        mock_engine._intents = {app_id: "// js"}
        mock_engine.score = AsyncMock(return_value=ScoreResult(score=0.0, valid=False))
        apps_module.set_js_engine(mock_engine)

        mock_sim = MagicMock()
        mock_sim.simulate = AsyncMock(return_value=SimulationResult(
            success=False,
            error='scoreIntent reverted: Error("Too little received")',
            revert_reason='Error("Too little received")',
        ))
        apps_module.set_simulator(mock_sim)

        try:
            resp = self.client.post(f"/v1/apps/{app_id}/score", json={
                "plan": {"interactions": [{"target": "0x00", "value": "0", "call_data": "0x"}]},
                "params": {},
            })
            self.assertEqual(resp.status_code, 200)
            sim = resp.json()["simulation"]
            self.assertFalse(sim["success"])
            self.assertEqual(sim["revert_reason"], 'Error("Too little received")')
        finally:
            apps_module.set_simulator(None)

    def test_score_simulator_fallback(self):
        """When simulator raises, should fall back to mock simulation."""
        from unittest.mock import AsyncMock
        from minotaur_subnet.shared.types import ScoreResult

        create_resp = self.client.post("/v1/apps/", json={
            "name": "Fallback Test App",
            "supported_chains": [1],
            "js_code": "module.exports = { score: () => ({score: 0.6}) }",
            "solidity_code": "contract FallbackTestApp {}",
        })
        app_id = create_resp.json()["app_id"]

        mock_engine = MagicMock()
        mock_engine._intents = {app_id: "// js"}
        mock_engine.score = AsyncMock(return_value=ScoreResult(score=0.6, valid=True))
        apps_module.set_js_engine(mock_engine)

        mock_sim = MagicMock()
        mock_sim.simulate = AsyncMock(side_effect=RuntimeError("Anvil unreachable"))
        apps_module.set_simulator(mock_sim)

        try:
            resp = self.client.post(f"/v1/apps/{app_id}/score", json={
                "plan": {
                    "interactions": [{"target": "0x00", "value": "0", "call_data": "0x"}],
                },
                "params": {},
            })
            self.assertEqual(resp.status_code, 200)
            data = resp.json()
            self.assertEqual(data["simulation_mode"], "mock")
        finally:
            apps_module.set_simulator(None)


class TestDryRunRoutes(unittest.TestCase):
    """Tests for dry-run scoring endpoint."""

    def setUp(self):
        self.ob = IntentOrderBook()
        orders_module.set_orderbook(self.ob)
        self.client = TestClient(app, raise_server_exceptions=False)

    def tearDown(self):
        orders_module.set_orderbook(None)

    def test_dry_run_order_not_found(self):
        resp = self.client.post("/v1/orders/nonexistent/dry-run", json={
            "interactions": [{"target": "0x", "value": "0", "call_data": "0x"}],
        })
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("error", data)

    def test_dry_run_no_orderbook(self):
        orders_module.set_orderbook(None)
        resp = self.client.post("/v1/orders/test/dry-run", json={
            "interactions": [{"target": "0x", "value": "0", "call_data": "0x"}],
        })
        self.assertEqual(resp.status_code, 503)


class TestMonitoringRoutes(unittest.TestCase):
    """Tests for monitoring endpoint."""

    def setUp(self):
        self.client = TestClient(app, raise_server_exceptions=False)

    def test_monitor_nonexistent_app(self):
        resp = self.client.get("/v1/apps/nonexistent/monitor")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("error", data)


# ═══════════════════════════════════════════════════════════════════════════════
#                        BLOCKLOOP STATUS
# ═══════════════════════════════════════════════════════════════════════════════


class TestBlockLoopStatus(unittest.TestCase):
    """Tests for blockloop status endpoint."""

    def setUp(self):
        self.client = TestClient(app, raise_server_exceptions=False)

    def test_blockloop_status_not_initialized(self):
        orders_module.set_block_loop(None)
        resp = self.client.get("/v1/blockloop/status")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertFalse(data["running"])

    def test_blockloop_status_with_mock(self):
        mock_loop = MagicMock()
        mock_loop.status.return_value = {
            "running": True,
            "total_ticks": 42,
            "orders_processed": 100,
        }
        orders_module.set_block_loop(mock_loop)
        resp = self.client.get("/v1/blockloop/status")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["running"])
        self.assertEqual(data["total_ticks"], 42)
        orders_module.set_block_loop(None)


# ═══════════════════════════════════════════════════════════════════════════════
#                    APP STATUS CHECK (API-7)
# ═══════════════════════════════════════════════════════════════════════════════


class TestAppStatusOnOrderSubmit(unittest.TestCase):
    """Tests that order submit rejects non-active apps (API-7)."""

    def setUp(self):
        self.ob = IntentOrderBook()
        orders_module.set_orderbook(self.ob)
        # Import and set up app_store
        from minotaur_subnet.store import AppIntentStore
        import tempfile
        self._tmp = tempfile.mkdtemp()
        self._store = AppIntentStore(store_path=Path(self._tmp) / "test.json")
        orders_module.set_app_store(self._store)
        self.client = TestClient(app, raise_server_exceptions=False)

    def tearDown(self):
        orders_module.set_orderbook(None)
        orders_module.set_app_store(None)
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_submit_order_rejects_nonexistent_app(self):
        resp = self.client.post("/v1/apps/nonexistent-app/orders", json={
            "submitted_by": "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045",
            "params": {},
        })
        self.assertEqual(resp.status_code, 404)
        self.assertIn("not found", resp.json()["detail"])

    def test_submit_order_rejects_draft_app(self):
        from minotaur_subnet.shared.types import AppIntentDefinition
        self._store.save_app(AppIntentDefinition(
            app_id="draft-app", name="Draft", version="1.0.0",
            intent_type="swap", js_code="// js",
        ))
        # No deployment = not active
        resp = self.client.post("/v1/apps/draft-app/orders", json={
            "submitted_by": "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045",
            "params": {},
        })
        self.assertEqual(resp.status_code, 400)
        self.assertIn("not ready for orders", resp.json()["detail"])

    def test_submit_order_accepts_active_app(self):
        from minotaur_subnet.shared.types import (
            AppIntentDefinition, DeploymentResult, AppStatus,
        )
        self._store.save_app(AppIntentDefinition(
            app_id="active-app", name="Active", version="1.0.0",
            intent_type="swap", js_code="// js",
        ))
        self._store.save_deployment(DeploymentResult(
            app_id="active-app", status=AppStatus.ACTIVE,
            contract_address="0x" + "ab" * 20,
        ))
        resp = self.client.post("/v1/apps/active-app/orders", json={
            "submitted_by": "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045",
            "params": {},
        })
        self.assertEqual(resp.status_code, 201)


# ═══════════════════════════════════════════════════════════════════════════════
#                    USER SIGNATURE (WAL-1, WAL-6)
# ═══════════════════════════════════════════════════════════════════════════════


class TestUserSignatureValidation(unittest.TestCase):
    """Tests for EIP-712 user signature validation on order submit."""

    def setUp(self):
        self.ob = IntentOrderBook()
        orders_module.set_orderbook(self.ob)
        from minotaur_subnet.store import AppIntentStore
        from minotaur_subnet.shared.types import (
            AppIntentDefinition, DeploymentResult, AppStatus,
        )
        import tempfile
        self._tmp = tempfile.mkdtemp()
        self._store = AppIntentStore(store_path=Path(self._tmp) / "test.json")
        self._store.save_app(AppIntentDefinition(
            app_id="sig-app", name="Sig Test", version="1.0.0",
            intent_type="swap", js_code="// js",
        ))
        self._store.save_deployment(DeploymentResult(
            app_id="sig-app", status=AppStatus.ACTIVE,
            contract_address="0x" + "ab" * 20,
        ))
        orders_module.set_app_store(self._store)
        self.client = TestClient(app, raise_server_exceptions=False)

    def tearDown(self):
        orders_module.set_orderbook(None)
        orders_module.set_app_store(None)
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_submit_without_signature(self):
        """No signature = backward compat, should succeed."""
        resp = self.client.post("/v1/apps/sig-app/orders", json={
            "submitted_by": "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045",
            "params": {},
        })
        self.assertEqual(resp.status_code, 201)

    def test_submit_with_invalid_signature(self):
        """Invalid signature hex should be rejected."""
        resp = self.client.post("/v1/apps/sig-app/orders", json={
            "submitted_by": "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045",
            "params": {},
            "user_signature": "0x" + "ab" * 65,  # Wrong signature
        })
        self.assertEqual(resp.status_code, 400)
        self.assertIn("Invalid user signature", resp.json()["detail"])


if __name__ == "__main__":
    unittest.main()
