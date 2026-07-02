"""E2E Miner Lifecycle: Manifest Discovery → Dry-Run → Submit → Benchmark → Fill.

Tests the full miner experience:
  1. App deployed with rich JS manifest
  2. Miner discovers the app via manifest extraction
  3. Miner builds a plan using manifest data
  4. Miner dry-runs the plan → gets score back (no side effects)
  5. Miner iterates to improve score
  6. Miner submits solver code → screening → benchmarking
  7. Solver adopted as champion → hot-swapped into BlockLoop
  8. User submits order → BlockLoop processes it with adopted solver
  9. Order reaches FILLED status

No external requirements (no Docker, no Anvil) — pure in-process.
"""

import asyncio
import sys
import time
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from minotaur_subnet.blockloop.loop import BlockLoop
from minotaur_subnet.shared.simulation import build_mock_simulation
from minotaur_subnet.engine import JsExecutionEngine
from minotaur_subnet.epoch.manager import EpochManager
from minotaur_subnet.harness.submission_store import SubmissionStatus, SubmissionStore
from minotaur_subnet.api import services as _tools
from minotaur_subnet.store import AppIntentStore
from minotaur_subnet.orderbook.orderbook import IntentOrderBook, OrderStatus
from minotaur_subnet.relayer.base import MockRelayer
from minotaur_subnet.sdk.intent_solver import IntentSolver, MarketSnapshot, SolverMetadata
from minotaur_subnet.shared.types import (
    AppIntentConfig,
    AppIntentDefinition,
    ExecutionPlan,
    Interaction,
    IntentState,
    ScoreResult,
    SimulationResult,
)

# Import discovery solver
from minotaur_subnet.docker.discovery_solver import DiscoverySolver

# Reuse test helpers from epoch lifecycle test. NOTE: the shared
# ``TestBenchmarkWorker`` there still uses the retired scalar API
# (``set_benchmark_result(score=...)``), so this file defines its own
# ``LocalBenchmarkWorker`` below against the post-scalar (delivered-value)
# contract instead of importing it.
from tests.e2e.test_epoch_lifecycle import (
    MockOrchestrator,
    MockSolverSession,
    _build_test_intents,
)
from minotaur_subnet.epoch.relative_scoring import has_delivered_value_rows


# ── JS fixture with manifest ──────────────────────────────────────────────

MANIFEST_JS = """\
module.exports = {
  config: { name: "test-swap", version: "1.0.0" },
  manifest: {
    intent_functions: [{
      name: "swap",
      description: "Swap ERC-20 tokens at best rate",
      params: {
        input_token: { type: "address", required: true },
        output_token: { type: "address", required: true },
        input_amount: { type: "uint256", required: true },
      },
      example_params: {
        input_token: "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",
        output_token: "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
        input_amount: "1000000000000000000",
      },
    }],
    supported_chains: [1],
    scoring_hints: "Multi-step plans with valid token targets score highest.",
  },
  score(plan, state, context) {
    let s = 0.3;
    if (plan.interactions && plan.interactions.length >= 2) s += 0.3;
    if (plan.metadata && plan.metadata.manifest_aware) s += 0.2;
    if (plan.deadline > 0) s += 0.1;
    return { score: Math.min(s, 1.0), valid: true, reason: "scored" };
  },
};
"""

SOLIDITY_STUB = """\
// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;
contract TestSwap {}
"""


# ── In-process benchmark worker (post-scalar / delivered-value API) ────────


class LocalBenchmarkWorker:
    """In-process benchmark worker on the NEW (post-scalar) contract.

    Mirrors the real worker: run each solver against the test intents, record
    per-order ``raw_output`` rows in ``benchmark_details``, and let the
    delivered-value gate (``has_delivered_value_rows``) decide SCORED vs
    REJECTED. Ranking + adoption use that same delivered-value signal — there
    is no scalar ``benchmark_score`` anymore.
    """
    __test__ = False

    def __init__(
        self,
        submission_store: SubmissionStore,
        orchestrator: MockOrchestrator,
        test_intents=None,
    ):
        self._sub_store = submission_store
        self._orchestrator = orchestrator
        self._test_intents = test_intents or _build_test_intents()

    async def run_once(self) -> int:
        """Benchmark all BENCHMARKING submissions, then rank + adopt."""
        pending = self._sub_store.list_by_status(SubmissionStatus.BENCHMARKING)
        if not pending:
            return 0

        for sub in pending:
            if sub.image_tag is None:
                self._sub_store.reject(sub.submission_id, "No image tag")
                continue

            try:
                session = await self._orchestrator.start_docker(sub.image_tag)
                await session.initialize({"epoch": sub.epoch})
                rows = self._benchmark_session(session)
                details = {"per_intent": rows, "method": "in-process"}
                # Validity gate: SCORED iff >= 1 order delivered value
                # (raw_output parses to > 0), else REJECTED.
                self._sub_store.set_benchmark_result(
                    sub.submission_id,
                    valid=has_delivered_value_rows(rows),
                    details=details,
                )
            except Exception as exc:
                self._sub_store.set_benchmark_result(
                    sub.submission_id,
                    valid=False,
                    details={"per_intent": [], "error": str(exc)},
                )

        # Rank scored submissions (display rank) + adopt the top delivered-value
        # solver. Ranking by total delivered value stands in for the real
        # worker's relative net-better ordering for this in-process mock.
        scored = self._sub_store.list_by_status(SubmissionStatus.SCORED)
        if scored:
            scored.sort(key=self._delivered_value, reverse=True)
            for i, s in enumerate(scored):
                self._sub_store.set_benchmark_rank(s.submission_id, i + 1)
            best = scored[0]
            if has_delivered_value_rows(
                (best.benchmark_details or {}).get("per_intent")
            ):
                self._sub_store.adopt(best.submission_id)

        return len(pending)

    def _benchmark_session(self, session: MockSolverSession) -> list[dict]:
        """Run solver against test intents → per-order raw_output rows.

        A plan with interactions "delivers value" (positive wei raw_output that
        scales with plan richness, mirroring the old 0.5 + 0.15*n heuristic);
        an empty/failed plan delivers "0".
        """
        rows = []
        for idx, (intent_def, state, snapshot) in enumerate(self._test_intents):
            try:
                plan = session.generate_plan(intent_def, state, snapshot)
                if plan and len(plan.interactions) > 0:
                    wei = 10**18 * (1 + min(len(plan.interactions), 4))
                    raw_output = str(wei)
                else:
                    raw_output = "0"
            except Exception:
                raw_output = "0"
            rows.append({"intent_id": f"app:scn{idx}", "raw_output": raw_output})
        return rows

    @staticmethod
    def _delivered_value(sub) -> int:
        total = 0
        for r in (sub.benchmark_details or {}).get("per_intent") or []:
            try:
                total += int(r.get("raw_output") or "0")
            except (TypeError, ValueError):
                continue
        return total


# ── Fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture
def app_store(tmp_path):
    return AppIntentStore(store_path=tmp_path / "store.json")


@pytest.fixture
def test_app(app_store):
    """Create and deploy a test app with a manifest-bearing JS module."""
    result = _tools.create_app_intent(
        app_store,
        name="Test Swap",
        description="Test swap app with manifest",
        supported_chains=[1],
        js_code=MANIFEST_JS,
        solidity_code=SOLIDITY_STUB,
    )
    app_id = result["app_id"]
    _tools.deploy_app_intent(app_store, app_id)
    return app_id


@pytest.fixture
def js_engine():
    return JsExecutionEngine(timeout_ms=5000, max_memory_mb=128)


@pytest.fixture
def orderbook():
    return IntentOrderBook()


@pytest.fixture
def sub_store():
    return SubmissionStore()


@pytest.fixture
def orchestrator():
    orch = MockOrchestrator()
    orch.register("discovery-solver:latest", DiscoverySolver())
    return orch


@pytest.fixture
def benchmark_worker(sub_store, orchestrator):
    return LocalBenchmarkWorker(sub_store, orchestrator)


# ── Test: Manifest Discovery ──────────────────────────────────────────────


class TestManifestDiscovery:
    """Extract and verify JS manifests."""

    def test_extract_manifest_from_js(self, js_engine, test_app, app_store):
        """Load JS with manifest → engine caches manifest → verify structure."""
        app = app_store.get_app(test_app)

        async def _run():
            await js_engine.load_intent(test_app, app.js_code)
            return js_engine.get_manifest(test_app)

        manifest = asyncio.get_event_loop().run_until_complete(_run())

        assert manifest is not None
        assert "intent_functions" in manifest
        funcs = manifest["intent_functions"]
        assert len(funcs) == 1
        assert funcs[0]["name"] == "swap"
        assert "params" in funcs[0]
        assert "example_params" in funcs[0]
        assert funcs[0]["example_params"]["input_token"] == "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"
        assert manifest["supported_chains"] == [1]
        assert "scoring_hints" in manifest

    def test_list_manifests(self, js_engine, test_app, app_store):
        """list_manifests returns all loaded app manifests."""
        app = app_store.get_app(test_app)

        async def _run():
            await js_engine.load_intent(test_app, app.js_code)
            return js_engine.list_manifests()

        manifests = asyncio.get_event_loop().run_until_complete(_run())
        assert test_app in manifests
        assert "intent_functions" in manifests[test_app]

    def test_no_manifest_returns_empty(self, js_engine):
        """JS without manifest → get_manifest returns None."""
        js_no_manifest = "module.exports = { config: {name: 'bare'}, score() { return {score: 0.5, valid: true}; } };"

        async def _run():
            await js_engine.load_intent("bare-app", js_no_manifest)
            return js_engine.get_manifest("bare-app")

        result = asyncio.get_event_loop().run_until_complete(_run())
        assert result is None

    def test_manifest_via_tools(self, test_app, app_store):
        """get_app_manifest tool extracts manifest from store."""
        result = asyncio.get_event_loop().run_until_complete(
            _tools.get_app_manifest(app_store, test_app)
        )
        assert "error" not in result
        assert result["app_id"] == test_app
        manifest = result["manifest"]
        assert manifest is not None
        assert manifest["intent_functions"][0]["name"] == "swap"

    def test_manifest_app_not_found(self, app_store):
        """get_app_manifest returns error for nonexistent app."""
        result = asyncio.get_event_loop().run_until_complete(
            _tools.get_app_manifest(app_store, "nonexistent")
        )
        assert "error" in result


# ── Test: Dry-Run Scoring ─────────────────────────────────────────────────


class TestDryRunScoring:
    """Submit a plan, get a score, verify no side effects."""

    def test_dry_run_returns_score(self, test_app, app_store, js_engine, orderbook):
        """Dry-run a plan → get score back without order mutation."""
        # Submit an order
        order = orderbook.submit(
            app_id=test_app,
            intent_function="swap",
            params={
                "input_token": "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",
                "output_token": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
                "input_amount": "1000000000000000000",
            },
            submitted_by="0x" + "01" * 20,
        )

        interactions = [
            {"target": "0x" + "aa" * 20, "value": "0", "call_data": "0x095ea7b3", "chain_id": 1},
            {"target": "0x" + "bb" * 20, "value": "0", "call_data": "0x38ed1739", "chain_id": 1},
        ]

        async def _run():
            return await _tools.dry_run_order(
                app_store, orderbook, js_engine,
                order.order_id, interactions,
                deadline=int(time.time()) + 300,
                metadata={"manifest_aware": True},
            )

        result = asyncio.get_event_loop().run_until_complete(_run())

        assert "error" not in result
        assert result["order_id"] == order.order_id
        assert result["score"] > 0
        assert result["valid"] is True

        # Order should still be OPEN (no mutation)
        refreshed = orderbook.get(order.order_id)
        assert refreshed.status == OrderStatus.OPEN

    def test_dry_run_improves_with_iteration(self, test_app, app_store, js_engine, orderbook):
        """Miner iterates: first plan scores lower, improved plan scores higher."""
        order = orderbook.submit(
            app_id=test_app,
            intent_function="swap",
            params={
                "input_token": "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",
                "output_token": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
                "input_amount": "1000000000000000000",
            },
            submitted_by="0x" + "01" * 20,
        )

        # Plan 1: single interaction, no metadata, no deadline
        plan1_interactions = [
            {"target": "0x" + "aa" * 20, "value": "0", "call_data": "0x", "chain_id": 1},
        ]

        # Plan 2: multi-step, manifest_aware, with deadline
        plan2_interactions = [
            {"target": "0x" + "aa" * 20, "value": "0", "call_data": "0x095ea7b3", "chain_id": 1},
            {"target": "0x" + "bb" * 20, "value": "0", "call_data": "0x38ed1739", "chain_id": 1},
        ]

        async def _run():
            score1 = await _tools.dry_run_order(
                app_store, orderbook, js_engine,
                order.order_id, plan1_interactions,
            )
            score2 = await _tools.dry_run_order(
                app_store, orderbook, js_engine,
                order.order_id, plan2_interactions,
                deadline=int(time.time()) + 300,
                metadata={"manifest_aware": True},
            )
            return score1, score2

        r1, r2 = asyncio.get_event_loop().run_until_complete(_run())

        assert r1["score"] < r2["score"], (
            f"Improved plan should score higher: {r1['score']} vs {r2['score']}"
        )

    def test_dry_run_order_not_found(self, app_store, orderbook, js_engine):
        """Dry-run with bad order_id returns error."""
        result = asyncio.get_event_loop().run_until_complete(
            _tools.dry_run_order(
                app_store, orderbook, js_engine,
                "nonexistent", [{"target": "0x", "value": "0", "call_data": "0x"}],
            )
        )
        assert "error" in result

    def test_dry_run_without_js_engine(self, test_app, app_store, orderbook):
        """Dry-run falls back to mock scoring when JS engine is None."""
        order = orderbook.submit(
            app_id=test_app,
            intent_function="swap",
            params={"input_token": "0x" + "aa" * 20, "output_token": "0x" + "bb" * 20},
            submitted_by="0x" + "01" * 20,
        )

        result = asyncio.get_event_loop().run_until_complete(
            _tools.dry_run_order(
                app_store, orderbook, None,
                order.order_id,
                [{"target": "0x" + "cc" * 20, "value": "0", "call_data": "0x"}],
            )
        )
        assert "error" not in result
        assert result["score"] > 0
        assert "mock" in result["reason"]


# ── Test: Full Miner Lifecycle ────────────────────────────────────────────


class TestFullMinerLifecycle:
    """End-to-end: discovery → dry-run → submit → benchmark → adopt → fill."""

    def test_full_lifecycle(
        self, test_app, app_store, js_engine, orderbook,
        sub_store, orchestrator, benchmark_worker,
    ):
        """Complete miner journey from discovery to order fill."""

        async def _run():
            # ── Step 1: Miner discovers app manifest ──────────────────
            manifest_result = await _tools.get_app_manifest(app_store, test_app)
            assert "error" not in manifest_result
            manifest = manifest_result["manifest"]
            assert manifest is not None
            assert manifest["intent_functions"][0]["name"] == "swap"

            # ── Step 2: User submits an order ─────────────────────────
            order = orderbook.submit(
                app_id=test_app,
                intent_function="swap",
                params={
                    "input_token": "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",
                    "output_token": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
                    "input_amount": "1000000000000000000",
                },
                submitted_by="0x" + "01" * 20,
            )
            assert order.status == OrderStatus.OPEN

            # ── Step 3: Miner builds plan from manifest ───────────────
            fn = manifest["intent_functions"][0]
            interactions = [
                {
                    "target": "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",
                    "value": "0",
                    "call_data": "0x095ea7b3",
                    "chain_id": 1,
                },
                {
                    "target": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
                    "value": "0",
                    "call_data": "0x38ed1739",
                    "chain_id": 1,
                },
            ]

            # ── Step 4: Miner dry-runs the plan ──────────────────────
            dry_run_result = await _tools.dry_run_order(
                app_store, orderbook, js_engine,
                order.order_id, interactions,
                deadline=int(time.time()) + 300,
                metadata={"manifest_aware": True},
            )
            assert "error" not in dry_run_result
            assert dry_run_result["score"] >= 0.5, (
                f"Plan should pass threshold: {dry_run_result['score']}"
            )

            # Order still OPEN after dry-run
            assert orderbook.get(order.order_id).status == OrderStatus.OPEN

            # ── Step 5: Miner submits solver code ────────────────────
            sub = sub_store.create(
                repo_url="https://github.com/miner/discovery-solver",
                commit_hash="disc123",
                epoch=1,
                hotkey="5Gdisc...",
            )
            sub_store.update_status(sub.submission_id, SubmissionStatus.SCREENING_STAGE_1)
            sub_store.set_screening_result(sub.submission_id, stage=1, passed=True, duration_ms=100)
            sub_store.update_status(sub.submission_id, SubmissionStatus.SCREENING_STAGE_2)
            sub_store.set_screening_result(sub.submission_id, stage=2, passed=True, duration_ms=200)
            sub_store.set_solver_info(sub.submission_id, name="discovery-solver", version="1.0.0")
            sub_store.set_image_tag(sub.submission_id, "discovery-solver:latest")
            sub_store.update_status(sub.submission_id, SubmissionStatus.SCREENING_STAGE_3)
            sub_store.set_screening_result(sub.submission_id, stage=3, passed=True, duration_ms=300)
            sub_store.update_status(sub.submission_id, SubmissionStatus.BENCHMARKING)

            # ── Step 6: Benchmark + adopt ────────────────────────────
            processed = await benchmark_worker.run_once()
            assert processed == 1

            refreshed_sub = sub_store.get(sub.submission_id)
            # Post-scalar: "scored with value" == the submission delivered value
            # on >= 1 order (raw_output > 0 in the per_intent rows), which is the
            # validity gate that replaced ``benchmark_score > 0``.
            assert refreshed_sub.benchmark_details is not None
            assert has_delivered_value_rows(
                refreshed_sub.benchmark_details.get("per_intent")
            )
            assert refreshed_sub.status in (SubmissionStatus.SCORED, SubmissionStatus.ADOPTED)

            # ── Step 7: Hot-swap solver into BlockLoop ───────────────
            # Create a discovery solver initialized with manifest data
            solver = DiscoverySolver()
            solver.initialize({"manifests": {test_app: manifest}})

            block_loop = BlockLoop(
                orderbook=orderbook,
                app_store=app_store,
                js_engine=js_engine,
                solver=solver,
                relayer=MockRelayer(),
                tick_interval=0.1,
                score_threshold=0.3,
            )
            block_loop.set_solver(solver)

            # ── Step 8: BlockLoop processes the order ────────────────
            tick_result = await block_loop.tick()

            assert tick_result.orders_processed >= 1
            assert tick_result.orders_approved >= 1
            assert tick_result.errors == []

            # ── Step 9: Order should be FILLED ───────────────────────
            final_order = orderbook.get(order.order_id)
            assert final_order.status == OrderStatus.FILLED
            assert final_order.tx_hash is not None

        asyncio.get_event_loop().run_until_complete(_run())

    def test_discovery_solver_uses_manifest(self, test_app, app_store, js_engine):
        """DiscoverySolver with manifest produces manifest-aware plans."""
        manifest_result = asyncio.get_event_loop().run_until_complete(
            _tools.get_app_manifest(app_store, test_app)
        )
        manifest = manifest_result["manifest"]

        solver = DiscoverySolver()
        solver.initialize({"manifests": {test_app: manifest}})

        app = app_store.get_app(test_app)
        state = IntentState(
            contract_address="0x" + "00" * 20,
            chain_id=1,
            nonce=0,
            owner="0x" + "01" * 20,
            raw_params={
                "input_token": "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",
                "output_token": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
                "input_amount": "1000000000000000000",
            },
            control={"_intent_function": "swap"},
        )
        snapshot = MarketSnapshot(
            chain_id=1, block_number=18500000, timestamp=1700000000,
        )

        plan = solver.generate_plan(app, state, snapshot)

        assert plan is not None
        assert len(plan.interactions) == 2
        assert plan.metadata.get("manifest_aware") is True
        assert plan.metadata.get("intent_function") == "swap"
        assert plan.deadline > 0

    def test_discovery_solver_without_manifest_falls_back(self, test_app, app_store):
        """DiscoverySolver without manifest produces fallback plan."""
        solver = DiscoverySolver()
        solver.initialize({"manifests": {}})  # No manifests

        app = app_store.get_app(test_app)
        state = IntentState(
            contract_address="0x" + "00" * 20,
            chain_id=1,
            nonce=0,
            owner="0x" + "01" * 20,
            raw_params={},
        )
        snapshot = MarketSnapshot(
            chain_id=1, block_number=18500000, timestamp=1700000000,
        )

        plan = solver.generate_plan(app, state, snapshot)

        assert plan is not None
        assert plan.metadata.get("fallback") is True
        assert len(plan.interactions) == 1
