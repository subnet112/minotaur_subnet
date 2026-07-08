"""Tests for scoring integration in run_benchmark() and BenchmarkWorker."""

from __future__ import annotations

import asyncio
import os
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from minotaur_subnet.shared.types import (
    AppIntentConfig,
    AppIntentDefinition,
    ExecutionPlan,
    Interaction,
    IntentState,
    ScoreResult,
    SimulationResult,
    TriggerType,
)
from minotaur_subnet.sdk.intent_solver import MarketSnapshot
from minotaur_subnet.harness.orchestrator import (
    BenchmarkConfig,
    BenchmarkResult,
    _build_benchmark_simulation,
    run_benchmark,
)
from minotaur_subnet.harness.submission_store import (
    SubmissionStatus,
    SubmissionStore,
)
from minotaur_subnet.harness.round_store import RoundStore
from minotaur_subnet.harness.benchmark_worker import BenchmarkWorker, GENESIS_HOTKEY

os.environ.setdefault("ALLOW_SUBPROCESS_BENCHMARK", "1")
# These are worker-LOGIC unit tests (routing / genesis / ranking) that mock the
# actual benchmark call; they do not exercise the fork-pin consensus path. Disable
# the round-anchored fork pin (default ON in code) so run_once doesn't DEFER on the
# missing round pin resolver before reaching the logic under test.
os.environ.setdefault("ROUND_ANCHORED_PIN", "0")


# ═══════════════════════════════════════════════════════════════════════════════
#                    SCORING INTEGRATION TESTS
# ═══════════════════════════════════════════════════════════════════════════════


def _make_intent(app_id: str = "test-swap", trigger: str = "user_triggered"):
    return AppIntentDefinition(
        app_id=app_id,
        name="Test Swap",
        version="1.0.0",
        intent_type="swap",
        js_code="",
        config=AppIntentConfig(
            supported_chains=[1],
            trigger_type=TriggerType(trigger),
        ),
    )


def _make_state():
    return IntentState(
        contract_address="0x" + "11" * 20,
        chain_id=1,
        nonce=0,
        owner="",
        raw_params={
            "input_token": "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",
            "output_token": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
            "input_amount": "1000000000000000000",
        },
    )


def _make_snapshot():
    return MarketSnapshot(
        chain_id=1,
        block_number=18000000,
        timestamp=int(time.time()),
        prices={"ETH/USD": 2500.0, "USDC/USD": 1.0},
        dex_config={"router_address": "0xE592427A0AEce92De3Edee1F18E0157C05861564"},
    )


def _make_plan(intent_id: str = "test-swap"):
    return ExecutionPlan(
        intent_id=intent_id,
        interactions=[
            Interaction(
                target="0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",
                value="0",
                call_data="0x095ea7b3" + "00" * 60,
                chain_id=1,
            ),
            Interaction(
                target="0xE592427A0AEce92De3Edee1F18E0157C05861564",
                value="0",
                call_data="0x414bf389" + "00" * 60,
                chain_id=1,
            ),
        ],
        deadline=int(time.time()) + 300,
        nonce=0,
    )

class _RecordingEngine:
    """Minimal JsExecutionEngine stand-in that records load_intent calls."""

    def __init__(self):
        self.loaded: dict[str, str] = {}
        self.load_calls: list[tuple[str, str]] = []

    async def load_intent(self, app_id: str, js_code: str) -> None:
        self.loaded[app_id] = js_code
        self.load_calls.append((app_id, js_code))

    def list_loaded_intents(self):
        return list(self.loaded.keys())

    def get_manifest(self, app_id: str):
        return {}

    async def score(self, app_id, plan, simulation, state):
        return ScoreResult(score=0.5)


def _intent_with_js(app_id: str, js_code: str):
    return AppIntentDefinition(
        app_id=app_id, name="Hot", version="1.0.0", intent_type="swap",
        js_code=js_code,
        config=AppIntentConfig(supported_chains=[1],
                               trigger_type=TriggerType.USER_TRIGGERED),
    )


class TestBenchmarkEngineHotReload(unittest.TestCase):
    """The benchmark worker keeps its OWN engine, so it must hot-reload on a
    js_code change (like the BlockLoop) — otherwise a developer's PUT /scoring
    is ignored until an api restart."""

    def test_build_score_fn_hot_reloads_on_js_change(self):
        eng = _RecordingEngine()
        worker = BenchmarkWorker(SubmissionStore(), js_engine=eng)
        snap, state = _make_snapshot(), _make_state()
        v1 = "function score(p,s,c){return {score:0.1};} // version one padding"
        v2 = "function score(p,s,c){return {score:0.9};} // version two padding"

        asyncio.run(worker._build_score_fn([(_intent_with_js("hot", v1), state, snap)]))
        self.assertEqual(eng.loaded["hot"], v1)
        self.assertEqual(len(eng.load_calls), 1)

        # Same JS → must NOT reload.
        asyncio.run(worker._build_score_fn([(_intent_with_js("hot", v1), state, snap)]))
        self.assertEqual(len(eng.load_calls), 1, "unchanged js must not reload")

        # Changed JS → must hot-reload to the new version.
        asyncio.run(worker._build_score_fn([(_intent_with_js("hot", v2), state, snap)]))
        self.assertEqual(len(eng.load_calls), 2, "changed js must hot-reload")
        self.assertEqual(eng.loaded["hot"], v2)


class TestBuildBenchmarkSimulation(unittest.TestCase):
    """Tests for _build_benchmark_simulation helper."""

    def test_builds_successful_simulation(self):
        plan = _make_plan()
        sim = _build_benchmark_simulation(plan)
        self.assertTrue(sim.success)
        self.assertGreater(sim.gas_used, 0)

    def test_gas_scales_with_interactions(self):
        plan1 = ExecutionPlan(
            intent_id="test",
            interactions=[
                Interaction(target="0x" + "aa" * 20, value="0", call_data="0x00"),
            ],
            deadline=int(time.time()) + 300,
            nonce=0,
        )
        plan2 = ExecutionPlan(
            intent_id="test",
            interactions=[
                Interaction(target="0x" + "aa" * 20, value="0", call_data="0x00"),
                Interaction(target="0x" + "bb" * 20, value="0", call_data="0x00"),
                Interaction(target="0x" + "cc" * 20, value="0", call_data="0x00"),
            ],
            deadline=int(time.time()) + 300,
            nonce=0,
        )
        sim1 = _build_benchmark_simulation(plan1)
        sim2 = _build_benchmark_simulation(plan2)
        self.assertGreater(sim2.gas_used, sim1.gas_used)

    def test_synthesizes_token_transfers_from_state(self):
        """When plan metadata and state provide output token info, transfers are synthesized."""
        plan = _make_plan()
        plan.metadata = {
            "output_token": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
            "min_output_amount": "1000000",
        }
        state = _make_state()
        state.raw_params["receiver"] = "0x" + "22" * 20
        state.sync_extra()
        sim = _build_benchmark_simulation(plan, state)
        self.assertTrue(sim.success)
        self.assertEqual(len(sim.token_transfers), 1)
        self.assertEqual(sim.token_transfers[0].token, "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48")
        self.assertEqual(sim.token_transfers[0].to_addr, "0x" + "22" * 20)
        # Amount should be ~5% above minimum
        self.assertEqual(sim.token_transfers[0].amount, str(int(1000000 * 1.05)))

    def test_no_transfers_without_output_token(self):
        """Without output token metadata, no transfers are synthesized."""
        plan = _make_plan()
        sim = _build_benchmark_simulation(plan, _make_state())
        self.assertEqual(len(sim.token_transfers), 0)


class TestRunBenchmarkWithScoring(unittest.TestCase):
    """Tests for run_benchmark() with score_fn integration."""

    def test_scoring_callback_is_called(self):
        """Verify that score_fn is called for each generated plan."""
        async def _run():
            mock_session = AsyncMock()
            mock_session._closed = False
            mock_session._start_time = time.monotonic()
            mock_session._proc = MagicMock()
            mock_session.elapsed_total = 0

            mock_session.initialize = AsyncMock()
            mock_session.metadata = AsyncMock(return_value=MagicMock(
                name="test", version="1.0", author="test",
            ))
            mock_session.on_benchmark_start = AsyncMock()
            mock_session.on_benchmark_end = AsyncMock()
            mock_session.generate_plan = AsyncMock(return_value=_make_plan())

            score_call_count = 0

            async def mock_score_fn(app_id, plan, sim, state):
                nonlocal score_call_count
                score_call_count += 1
                return ScoreResult(score=0.85, valid=True, breakdown={"base": 0.85})

            intents = [(_make_intent(), _make_state(), _make_snapshot())]
            results = await run_benchmark(
                mock_session, intents, score_fn=mock_score_fn,
            )

            self.assertEqual(score_call_count, 1)
            self.assertEqual(len(results), 1)
            self.assertAlmostEqual(results[0].score, 0.85, places=2)
            self.assertAlmostEqual(results[0].plan_score, 0.85, places=2)
            self.assertIn("base", results[0].score_breakdown)

        asyncio.run(_run())

    def test_no_score_fn_leaves_zero(self):
        """Without score_fn, scores remain at 0.0."""
        async def _run():
            mock_session = AsyncMock()
            mock_session.elapsed_total = 0
            mock_session.initialize = AsyncMock()
            mock_session.metadata = AsyncMock(return_value=MagicMock(
                name="test", version="1.0", author="test",
            ))
            mock_session.on_benchmark_start = AsyncMock()
            mock_session.on_benchmark_end = AsyncMock()
            mock_session.generate_plan = AsyncMock(return_value=_make_plan())

            intents = [(_make_intent(), _make_state(), _make_snapshot())]
            results = await run_benchmark(mock_session, intents)

            self.assertEqual(results[0].score, 0.0)
            self.assertIsNone(results[0].plan_score)

        asyncio.run(_run())

    def test_auto_trigger_composite_scoring(self):
        """Auto-triggered intents compute composite score from trigger + plan."""
        async def _run():
            mock_session = AsyncMock()
            mock_session.elapsed_total = 0
            mock_session.initialize = AsyncMock()
            mock_session.metadata = AsyncMock(return_value=MagicMock(
                name="test", version="1.0", author="test",
            ))
            mock_session.on_benchmark_start = AsyncMock()
            mock_session.on_benchmark_end = AsyncMock()
            mock_session.generate_plan = AsyncMock(return_value=_make_plan("auto-app"))
            mock_session.check_trigger = AsyncMock(return_value=True)

            async def mock_score_fn(app_id, plan, sim, state):
                return ScoreResult(score=0.80, valid=True)

            intent = _make_intent(app_id="auto-app", trigger="auto_triggered")
            intents = [(intent, _make_state(), _make_snapshot())]
            config = BenchmarkConfig(auto_trigger_weight=0.4, plan_quality_weight=0.6)

            results = await run_benchmark(
                mock_session, intents,
                config=config,
                trigger_ground_truth={"auto-app": True},
                score_fn=mock_score_fn,
            )

            r = results[0]
            self.assertTrue(r.trigger_decision)
            self.assertAlmostEqual(r.trigger_score, 1.0)
            self.assertAlmostEqual(r.plan_score, 0.80)
            # composite = 0.4 * 1.0 + 0.6 * 0.8 = 0.88
            self.assertAlmostEqual(r.score, 0.88, places=2)

        asyncio.run(_run())

    def test_wrong_trigger_penalizes_score(self):
        """Wrong trigger decision reduces composite score."""
        async def _run():
            mock_session = AsyncMock()
            mock_session.elapsed_total = 0
            mock_session.initialize = AsyncMock()
            mock_session.metadata = AsyncMock(return_value=MagicMock(
                name="test", version="1.0", author="test",
            ))
            mock_session.on_benchmark_start = AsyncMock()
            mock_session.on_benchmark_end = AsyncMock()
            mock_session.generate_plan = AsyncMock(return_value=_make_plan("auto-app"))
            mock_session.check_trigger = AsyncMock(return_value=False)

            async def mock_score_fn(app_id, plan, sim, state):
                return ScoreResult(score=0.80, valid=True)

            intent = _make_intent(app_id="auto-app", trigger="auto_triggered")
            intents = [(intent, _make_state(), _make_snapshot())]
            config = BenchmarkConfig(auto_trigger_weight=0.4, plan_quality_weight=0.6)

            results = await run_benchmark(
                mock_session, intents,
                config=config,
                trigger_ground_truth={"auto-app": True},  # GT=True, solver said False
                score_fn=mock_score_fn,
            )

            r = results[0]
            self.assertFalse(r.trigger_decision)
            self.assertAlmostEqual(r.trigger_score, 0.0)
            # composite = 0.4 * 0.0 + 0.6 * 0.8 = 0.48
            self.assertAlmostEqual(r.score, 0.48, places=2)

        asyncio.run(_run())

    def test_scoring_error_is_captured(self):
        """Scoring errors are captured in BenchmarkResult.error."""
        async def _run():
            mock_session = AsyncMock()
            mock_session.elapsed_total = 0
            mock_session.initialize = AsyncMock()
            mock_session.metadata = AsyncMock(return_value=MagicMock(
                name="test", version="1.0", author="test",
            ))
            mock_session.on_benchmark_start = AsyncMock()
            mock_session.on_benchmark_end = AsyncMock()
            mock_session.generate_plan = AsyncMock(return_value=_make_plan())

            async def bad_score_fn(app_id, plan, sim, state):
                raise RuntimeError("JS engine exploded")

            intents = [(_make_intent(), _make_state(), _make_snapshot())]
            results = await run_benchmark(
                mock_session, intents, score_fn=bad_score_fn,
            )

            self.assertIn("scoring_error", results[0].error)

        asyncio.run(_run())


# ═══════════════════════════════════════════════════════════════════════════════
#                    BENCHMARK WORKER TESTS
# ═══════════════════════════════════════════════════════════════════════════════


class TestBenchmarkWorkerLogic(unittest.TestCase):
    """Tests for BenchmarkWorker logic without Docker."""

    def test_run_once_no_work(self):
        """run_once returns 0 when there are no BENCHMARKING submissions."""
        store = SubmissionStore()
        worker = BenchmarkWorker(store)
        result = asyncio.run(worker.run_once())
        self.assertEqual(result, 0)

    def test_run_once_processes_benchmarking(self):
        """run_once processes submissions in BENCHMARKING status."""
        store = SubmissionStore()
        sub = store.create(
            repo_url="https://github.com/test/solver",
            commit_hash="abc123def456",
            epoch=42,
            hotkey="5GrwvaEF_test_hotkey",
        )
        store.update_status(sub.submission_id, SubmissionStatus.BENCHMARKING)
        store.set_image_tag(sub.submission_id, "solver-abc123:screening")

        # use_docker=False bypasses the "real simulator not wired" defer guard;
        # _benchmark_submission is mocked so no container actually runs.
        worker = BenchmarkWorker(store, use_docker=False)

        # Mock the actual benchmarking to avoid Docker. raw_output>0 makes the
        # order deliver value, so the submission passes the validity gate -> SCORED.
        async def mock_benchmark_submission(image_tag, intents, score_fn):
            return [BenchmarkResult(
                intent_id="test",
                plan=_make_plan("test"),
                score=0.75,
                plan_score=0.75,
                raw_output="1000",
            )]

        worker._benchmark_submission = mock_benchmark_submission

        result = asyncio.run(worker.run_once())
        self.assertEqual(result, 1)

        updated = store.get(sub.submission_id)
        self.assertEqual(updated.status, SubmissionStatus.SCORED)
        self.assertIsNotNone(updated.benchmark_details)

    def test_open_round_blocks_benchmarking_when_round_store_enabled(self):
        """Miner benchmarking waits until the current round is explicitly closed."""
        store = SubmissionStore()
        round_store = RoundStore()
        current_round = round_store.ensure_open_round(opened_epoch=42)
        sub = store.create(
            repo_url="https://github.com/test/solver",
            commit_hash="abc123def456",
            epoch=42,
            hotkey="5GrwvaEF_test_hotkey",
            round_id=current_round.round_id,
        )
        store.update_status(sub.submission_id, SubmissionStatus.BENCHMARKING)
        store.set_image_tag(sub.submission_id, "solver-abc123:screening")
        store.set_image_id(sub.submission_id, "sha256:" + "a" * 64)

        worker = BenchmarkWorker(store, use_docker=True, round_store=round_store)

        async def mock_benchmark_submission(image_tag, intents, score_fn):
            raise AssertionError("benchmark should not run while round is open")

        worker._benchmark_submission = mock_benchmark_submission

        result = asyncio.run(worker.run_once())
        self.assertEqual(result, 0)
        updated = store.get(sub.submission_id)
        self.assertEqual(updated.status, SubmissionStatus.BENCHMARKING)

    def test_closed_round_with_existing_submissions_does_not_spawn_late_genesis(self):
        """Replay rounds with miner submissions should not invent a late genesis entry."""
        store = SubmissionStore()
        round_store = RoundStore()
        current_round = round_store.ensure_open_round(opened_epoch=42)
        round_store.close_current_round(close_epoch=42)
        sub = store.create(
            repo_url="https://github.com/test/solver",
            commit_hash="abc123def456",
            epoch=42,
            hotkey="5GrwvaEF_test_hotkey",
            round_id=current_round.round_id,
        )
        store.set_benchmark_result(
            sub.submission_id,
            valid=True,
            details={"per_intent": [{"intent_id": "dex:s1", "raw_output": "1000"}]},
        )

        # use_docker=False so the round-gating path is actually exercised (rather
        # than short-circuiting on the "real simulator not wired" defer guard).
        worker = BenchmarkWorker(store, round_store=round_store, use_docker=False)

        result = asyncio.run(worker.run_once())
        self.assertEqual(result, 0)
        self.assertEqual(
            len([s for s in store.list_by_round(current_round.round_id) if s.hotkey == GENESIS_HOTKEY]),
            0,
        )

    def test_no_image_tag_or_solver_path_rejects(self):
        """Submissions without an image_tag or solver_path are rejected."""
        store = SubmissionStore()
        sub = store.create(
            repo_url="https://github.com/test/solver",
            commit_hash="abc123def456",
            epoch=42,
            hotkey="5GrwvaEF_test_hotkey",
        )
        store.update_status(sub.submission_id, SubmissionStatus.BENCHMARKING)
        # No image_tag or solver_path set

        worker = BenchmarkWorker(store, use_docker=False)
        result = asyncio.run(worker.run_once())
        self.assertEqual(result, 1)

        updated = store.get(sub.submission_id)
        self.assertEqual(updated.status, SubmissionStatus.REJECTED)

    def test_run_once_ranks_best_without_adoption(self):
        """Replay-only worker assigns ranks but does not adopt a champion.

        Ranking is now RELATIVE NET-BETTER (delivered-value breadth) vs the
        champion, not a scalar score. With no champion every delivered order is a
        blind-spot cover, so the broader solver (more orders delivering value)
        ranks first.
        """
        store = SubmissionStore()
        sub1 = store.create(
            repo_url="https://github.com/a/s", commit_hash="aaa1234",
            epoch=42, hotkey="miner_aaa_key",
        )
        sub2 = store.create(
            repo_url="https://github.com/b/s", commit_hash="bbb1234",
            epoch=42, hotkey="miner_bbb_key",
        )

        for s in [sub1, sub2]:
            store.update_status(s.submission_id, SubmissionStatus.BENCHMARKING)
            store.set_image_tag(s.submission_id, f"solver-{s.commit_hash}:screening")
            store.set_image_id(
                s.submission_id,
                "sha256:" + s.commit_hash.ljust(64, "0")[:64],
            )

        worker = BenchmarkWorker(store, use_docker=False)
        # sub2 delivers value on TWO orders, sub1 on ONE — sub2 is net-broader.
        value_orders = {
            sub1.submission_id: [("test_a", "1000")],
            sub2.submission_id: [("test_a", "1000"), ("test_b", "1000")],
        }

        async def mock_benchmark_submission(image_tag, intents, score_fn):
            for sid, orders in value_orders.items():
                sub = store.get(sid)
                if sub and sub.image_tag == image_tag:
                    return [
                        BenchmarkResult(
                            intent_id=iid, plan=_make_plan(), score=0.5, raw_output=raw,
                        )
                        for iid, raw in orders
                    ]
            return []

        worker._benchmark_submission = mock_benchmark_submission

        asyncio.run(worker.run_once())

        s1 = store.get(sub1.submission_id)
        s2 = store.get(sub2.submission_id)

        # sub2 (broader delivery) ranks first, but both remain replay-scored only
        self.assertEqual(s2.status, SubmissionStatus.SCORED)
        self.assertEqual(s1.status, SubmissionStatus.SCORED)
        self.assertEqual(s2.benchmark_rank, 1)
        self.assertEqual(s1.benchmark_rank, 2)
        self.assertIsNone(store.get_champion())

    def test_results_to_details(self):
        worker = BenchmarkWorker(SubmissionStore())
        results = [
            BenchmarkResult(intent_id="a", score=0.8, plan=_make_plan(), elapsed_ms=100),
            BenchmarkResult(intent_id="b", error="timeout", elapsed_ms=30000),
        ]
        details = worker._results_to_details(results)
        self.assertEqual(details["total_intents"], 2)
        self.assertEqual(details["plans_generated"], 1)
        self.assertEqual(details["errors"], 1)
        self.assertEqual(len(details["per_intent"]), 2)

    def test_load_synthetic_intents_without_app_store(self):
        """Without an app store, synthetic intents are used."""
        worker = BenchmarkWorker(SubmissionStore(), app_store=None)
        intents = worker._load_benchmark_intents()
        self.assertEqual(len(intents), 3)  # build_synthetic_intents returns 3

    def test_stop(self):
        worker = BenchmarkWorker(SubmissionStore())
        worker._running = True
        worker.stop()
        self.assertFalse(worker._running)

    def test_run_once_does_not_replace_existing_champion(self):
        """Replay scoring must not mutate the currently adopted champion."""
        store = SubmissionStore()

        champion = store.create(
            repo_url="https://github.com/a/s", commit_hash="champ123",
            epoch=42, hotkey="champ_key",
        )
        store.set_benchmark_result(
            champion.submission_id,
            valid=True,
            details={"per_intent": [{"intent_id": "test", "raw_output": "1000"}]},
        )
        store.adopt(champion.submission_id)

        challenger = store.create(
            repo_url="https://github.com/b/s", commit_hash="chal123",
            epoch=42, hotkey="chal_key",
        )
        store.update_status(challenger.submission_id, SubmissionStatus.BENCHMARKING)
        store.set_image_tag(challenger.submission_id, "solver-chal:screening")
        store.set_image_id(challenger.submission_id, "sha256:" + "b" * 64)

        worker = BenchmarkWorker(store, use_docker=False)

        # Challenger beats the champion on the shared order (2000 > 1000).
        async def mock_benchmark(image_tag, intents, score_fn):
            return [BenchmarkResult(
                intent_id="test", plan=_make_plan(), score=0.99, raw_output="2000",
            )]

        worker._benchmark_submission = mock_benchmark
        asyncio.run(worker.run_once())

        current_champion = store.get_champion()
        self.assertEqual(current_champion.submission_id, champion.submission_id)
        updated = store.get(challenger.submission_id)
        self.assertEqual(updated.status, SubmissionStatus.SCORED)
        self.assertEqual(updated.benchmark_rank, 1)

    def test_replay_only_worker_does_not_invoke_champion_callback(self):
        """Champion callbacks are deferred to the later orchestration layer."""
        store = SubmissionStore()
        sub = store.create(
            repo_url="https://github.com/a/s", commit_hash="abc123",
            epoch=42, hotkey="miner_key",
        )
        store.update_status(sub.submission_id, SubmissionStatus.BENCHMARKING)
        store.set_image_tag(sub.submission_id, "solver-abc:screening")
        store.set_image_id(sub.submission_id, "sha256:" + "e" * 64)

        callback_called = []

        def on_adopted(submission):
            callback_called.append(submission.submission_id)

        worker = BenchmarkWorker(store, on_champion_adopted=on_adopted)

        async def mock_benchmark(image_tag, intents, score_fn):
            return [BenchmarkResult(intent_id="test", plan=_make_plan(), score=0.85)]

        worker._benchmark_submission = mock_benchmark
        asyncio.run(worker.run_once())

        self.assertEqual(callback_called, [])

    def test_get_champion(self):
        """SubmissionStore.get_champion returns the adopted submission."""
        store = SubmissionStore()
        sub = store.create(
            repo_url="https://github.com/a/s", commit_hash="abc",
            epoch=1, hotkey="key",
        )
        self.assertIsNone(store.get_champion())
        store.update_status(sub.submission_id, SubmissionStatus.BENCHMARKING)
        store.set_benchmark_result(
            sub.submission_id,
            valid=True,
            details={"per_intent": [{"intent_id": "a", "raw_output": "1000"}]},
        )
        store.adopt(sub.submission_id)
        champion = store.get_champion()
        self.assertIsNotNone(champion)
        self.assertEqual(champion.submission_id, sub.submission_id)


# ═══════════════════════════════════════════════════════════════════════════════
#                    MANIFEST ENRICHMENT TESTS
# ═══════════════════════════════════════════════════════════════════════════════


class TestEnrichIntentsWithManifests(unittest.TestCase):
    """Tests for BenchmarkWorker._enrich_intents_with_manifests()."""

    def _make_worker_with_engine(self, manifests: dict):
        """Create a worker with a mock JS engine that returns given manifests."""
        mock_engine = MagicMock()
        mock_engine.get_manifest = MagicMock(side_effect=lambda app_id: manifests.get(app_id))
        store = SubmissionStore()
        worker = BenchmarkWorker(store, js_engine=mock_engine)
        return worker

    def test_enrichment_with_no_manifest_returns_unchanged(self):
        """Intents with no manifest are returned unchanged."""
        worker = self._make_worker_with_engine({})
        intent = _make_intent("app-1")
        state = _make_state()
        snapshot = _make_snapshot()

        result = worker._enrich_intents_with_manifests([(intent, state, snapshot)])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0][1].raw_params, state.raw_params)
        self.assertEqual(result[0][1].control, state.control)

    def test_enrichment_injects_example_params(self):
        """Manifest example_params are injected into state.raw_params/control."""
        manifest = {
            "intent_functions": [{
                "name": "swap",
                "example_params": {
                    "input_token": "0xWETH",
                    "output_token": "0xUSDC",
                    "input_amount": "1000000000000000000",
                },
            }],
        }
        worker = self._make_worker_with_engine({"app-1": manifest})
        intent = _make_intent("app-1")
        state = IntentState(
            contract_address="0x" + "11" * 20,
            chain_id=1, nonce=0, owner="",
        )
        snapshot = _make_snapshot()

        result = worker._enrich_intents_with_manifests([(intent, state, snapshot)])
        self.assertEqual(len(result), 1)
        enriched_state = result[0][1]
        self.assertEqual(enriched_state.control["_intent_function"], "swap")
        self.assertEqual(enriched_state.raw_params["input_token"], "0xWETH")
        self.assertEqual(enriched_state.raw_params["output_token"], "0xUSDC")

    def test_enrichment_expands_multi_function_apps(self):
        """Apps with multiple intent functions are expanded into separate test intents."""
        manifest = {
            "intent_functions": [
                {
                    "name": "buyDip",
                    "example_params": {"vault": "0xVault", "threshold": 500},
                },
                {
                    "name": "withdraw",
                    "example_params": {"amount": "1000000"},
                },
            ],
        }
        worker = self._make_worker_with_engine({"app-1": manifest})
        intent = _make_intent("app-1")
        state = IntentState(
            contract_address="0x" + "11" * 20,
            chain_id=1, nonce=0, owner="",
        )
        snapshot = _make_snapshot()

        result = worker._enrich_intents_with_manifests([(intent, state, snapshot)])
        self.assertEqual(len(result), 2)

        # First: buyDip
        self.assertEqual(result[0][1].control["_intent_function"], "buyDip")
        self.assertEqual(result[0][1].raw_params["vault"], "0xVault")

        # Second: withdraw
        self.assertEqual(result[1][1].control["_intent_function"], "withdraw")
        self.assertEqual(result[1][1].raw_params["amount"], "1000000")

    def test_enrichment_without_engine_returns_unchanged(self):
        """Without a JS engine, intents are returned unchanged."""
        store = SubmissionStore()
        worker = BenchmarkWorker(store, js_engine=None)
        intent = _make_intent("app-1")
        state = _make_state()
        snapshot = _make_snapshot()

        result = worker._enrich_intents_with_manifests([(intent, state, snapshot)])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0][1], state)

    def test_enrichment_empty_intent_functions(self):
        """Manifest with empty intent_functions returns intent unchanged."""
        manifest = {"intent_functions": []}
        worker = self._make_worker_with_engine({"app-1": manifest})
        intent = _make_intent("app-1")
        state = _make_state()
        snapshot = _make_snapshot()

        result = worker._enrich_intents_with_manifests([(intent, state, snapshot)])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0][1].raw_params, state.raw_params)
        self.assertEqual(result[0][1].control, state.control)


class TestBenchmarkWorkerSolverPath(unittest.TestCase):
    """Tests for solver_path-based benchmarking (source submissions)."""

    def test_solver_path_submission_is_rejected(self):
        """Source solver_path submissions can no longer be benchmarked.

        Subprocess mode is permanently disabled, so a solver_path submission is
        rejected by policy instead of being scored.
        """
        store = SubmissionStore()
        sub = store.create(
            repo_url="source://inline",
            commit_hash="abc123",
            epoch=0,
            hotkey="miner_test",
        )
        store.update_status(sub.submission_id, SubmissionStatus.BENCHMARKING)
        store.set_solver_path(sub.submission_id, "/tmp/fake_solver.py")

        worker = BenchmarkWorker(store, use_docker=False)

        result = asyncio.run(worker.run_once())
        self.assertEqual(result, 1)

        updated = store.get(sub.submission_id)
        self.assertEqual(updated.status, SubmissionStatus.REJECTED)
        self.assertIn("disabled by policy", updated.rejection_reason or "")

    def test_solver_path_preferred_over_image_tag(self):
        """When both solver_path and image_tag are set, the solver_path branch is
        evaluated first. Subprocess mode is disabled, so the submission is rejected
        by policy and the Docker path is never taken."""
        store = SubmissionStore()
        sub = store.create(
            repo_url="source://inline",
            commit_hash="abc123",
            epoch=0,
            hotkey="miner_test",
        )
        store.update_status(sub.submission_id, SubmissionStatus.BENCHMARKING)
        store.set_solver_path(sub.submission_id, "/tmp/solver.py")
        store.set_image_tag(sub.submission_id, "solver-abc:screening")

        worker = BenchmarkWorker(store, use_docker=False)

        async def mock_benchmark_submission(image_tag, intents, score_fn):
            self.fail("Docker benchmark should not be called when solver_path exists")

        worker._benchmark_submission = mock_benchmark_submission

        asyncio.run(worker.run_once())
        updated = store.get(sub.submission_id)
        self.assertEqual(updated.status, SubmissionStatus.REJECTED)
        self.assertIn("disabled by policy", updated.rejection_reason or "")

    def test_solver_path_rejected_when_policy_disabled(self):
        """Source solver_path submissions are rejected when subprocess mode is disabled."""
        store = SubmissionStore()
        sub = store.create(
            repo_url="source://inline",
            commit_hash="abc123",
            epoch=0,
            hotkey="miner_test",
        )
        store.update_status(sub.submission_id, SubmissionStatus.BENCHMARKING)
        store.set_solver_path(sub.submission_id, "/tmp/solver.py")

        worker = BenchmarkWorker(store, use_docker=False)
        with patch.dict(os.environ, {"ALLOW_SUBPROCESS_BENCHMARK": "0"}, clear=False):
            processed = asyncio.run(worker.run_once())
        self.assertEqual(processed, 1)

        updated = store.get(sub.submission_id)
        self.assertEqual(updated.status, SubmissionStatus.REJECTED)
        self.assertIn("disabled by policy", (updated.rejection_reason or ""))


class TestGenesisBootstrap(unittest.TestCase):
    """Tests for the genesis auto-bootstrap mechanism."""

    def test_genesis_runs_when_no_champion_and_solving_apps(self):
        """Genesis creates a baseline submission when conditions are met."""
        store = SubmissionStore()
        app_store = MagicMock()

        # Mock: one app in SOLVING status
        from minotaur_subnet.shared.types import AppStatus
        mock_app = _make_intent("dex-app")
        app_store.list_apps.return_value = [mock_app]
        mock_deployment = MagicMock()
        mock_deployment.status = AppStatus.SOLVING
        mock_deployment.chain_id = 1
        app_store.get_deployment.return_value = mock_deployment

        # Genesis now benchmarks a Docker image (genesis_solver_image), not a
        # subprocess baseline path. use_docker=False bypasses the simulator guard.
        worker = BenchmarkWorker(
            store,
            app_store=app_store,
            genesis_solver_image="genesis:latest",
            use_docker=False,
        )

        # Mock the actual (Docker) benchmarking; raw_output>0 => delivered value.
        async def mock_benchmark_submission(image_tag, intents, score_fn):
            return [BenchmarkResult(
                intent_id="dex-app", plan=_make_plan("dex-app"), score=0.6,
                raw_output="1000",
            )]

        worker._benchmark_submission = mock_benchmark_submission

        result = asyncio.run(worker.run_once())
        self.assertEqual(result, 1)

        # Genesis submission should exist
        genesis = store.get_by_hotkey_epoch(GENESIS_HOTKEY, 0)
        self.assertIsNotNone(genesis)
        self.assertIn(genesis.status, [SubmissionStatus.SCORED, SubmissionStatus.ADOPTED])

    def test_genesis_uses_current_round_when_round_store_present(self):
        """Genesis bootstrap should inherit the active solver round ID."""
        from minotaur_subnet.shared.types import AppStatus

        store = SubmissionStore()
        round_store = RoundStore()
        current_round = round_store.ensure_open_round(opened_epoch=7)
        app_store = MagicMock()
        mock_app = _make_intent("dex-app")
        app_store.list_apps.return_value = [mock_app]
        mock_deployment = MagicMock()
        mock_deployment.status = AppStatus.SOLVING
        mock_deployment.chain_id = 1
        app_store.get_deployment.return_value = mock_deployment

        worker = BenchmarkWorker(
            store,
            app_store=app_store,
            round_store=round_store,
            genesis_solver_image="genesis:latest",
            use_docker=False,
        )

        async def mock_benchmark_submission(image_tag, intents, score_fn):
            return [BenchmarkResult(
                intent_id="dex-app", plan=_make_plan("dex-app"), score=0.6,
                raw_output="1000",
            )]

        worker._benchmark_submission = mock_benchmark_submission

        asyncio.run(worker.run_once())

        genesis = store.get_by_hotkey_epoch(GENESIS_HOTKEY, 0)
        self.assertIsNotNone(genesis)
        self.assertEqual(genesis.round_id, current_round.round_id)

    def test_genesis_skipped_when_champion_exists(self):
        """Genesis does not run if a champion already exists."""
        store = SubmissionStore()

        # Create and adopt an existing champion
        sub = store.create(
            repo_url="https://github.com/a/s", commit_hash="champ1",
            epoch=1, hotkey="existing_miner",
        )
        store.update_status(sub.submission_id, SubmissionStatus.BENCHMARKING)
        store.set_benchmark_result(
            sub.submission_id,
            valid=True,
            details={"per_intent": [{"intent_id": "a", "raw_output": "1000"}]},
        )
        store.adopt(sub.submission_id)

        worker = BenchmarkWorker(
            store,
            genesis_solver_image="genesis:latest",
            use_docker=False,
        )

        result = asyncio.run(worker.run_once())
        self.assertEqual(result, 0)  # No work done

    def test_genesis_skipped_without_genesis_image(self):
        """Genesis does not run if no genesis solver image is configured."""
        store = SubmissionStore()
        worker = BenchmarkWorker(store, genesis_solver_image=None, use_docker=False)

        result = asyncio.run(worker.run_once())
        self.assertEqual(result, 0)

    def test_genesis_idempotent(self):
        """Running genesis twice does not create duplicate submissions."""
        from minotaur_subnet.harness.benchmark_worker import GENESIS_EPOCH

        store = SubmissionStore()
        app_store = MagicMock()

        from minotaur_subnet.shared.types import AppStatus
        mock_app = _make_intent("dex-app")
        app_store.list_apps.return_value = [mock_app]
        mock_deployment = MagicMock()
        mock_deployment.status = AppStatus.SOLVING
        mock_deployment.chain_id = 1
        app_store.get_deployment.return_value = mock_deployment

        worker = BenchmarkWorker(
            store,
            app_store=app_store,
            genesis_solver_image="genesis:latest",
            use_docker=False,
        )

        async def mock_benchmark_submission(image_tag, intents, score_fn):
            return [BenchmarkResult(
                intent_id="dex-app", plan=_make_plan("dex-app"), score=0.6,
                raw_output="1000",
            )]

        worker._benchmark_submission = mock_benchmark_submission

        # Run twice
        asyncio.run(worker.run_once())
        asyncio.run(worker.run_once())

        # Should only have one genesis submission
        genesis = store.get_by_hotkey_epoch(GENESIS_HOTKEY, GENESIS_EPOCH)
        self.assertIsNotNone(genesis)
        all_subs = store.list_by_epoch(GENESIS_EPOCH)
        genesis_subs = [s for s in all_subs if s.hotkey == GENESIS_HOTKEY]
        self.assertEqual(len(genesis_subs), 1)

    def test_genesis_skipped_without_solving_apps(self):
        """Genesis does not run if no apps are in SOLVING status."""
        store = SubmissionStore()
        app_store = MagicMock()
        app_store.list_apps.return_value = []

        worker = BenchmarkWorker(
            store,
            app_store=app_store,
            genesis_solver_image="genesis:latest",
            use_docker=False,
        )

        result = asyncio.run(worker.run_once())
        self.assertEqual(result, 0)


class TestChampionBootstrap(unittest.TestCase):
    """Tests for champion-driven SOLVING -> SOLVED bootstrap."""

    def test_champion_bootstraps_new_solving_apps(self):
        """An existing champion should validate new solving apps automatically."""
        from minotaur_subnet.shared.types import AppStatus

        store = SubmissionStore()
        champion = store.create(
            repo_url="https://github.com/a/s",
            commit_hash="champ1",
            epoch=1,
            hotkey="champion_miner",
        )
        store.set_image_tag(champion.submission_id, "solver-champ:screening")
        store.set_benchmark_result(
            champion.submission_id,
            valid=True,
            details={"per_intent": [{"intent_id": "x", "raw_output": "1000"}]},
        )
        store.adopt(champion.submission_id)

        solving_app = _make_intent("solving-app")
        solved_app = _make_intent("solved-app")
        app_store = MagicMock()
        app_store.list_apps.return_value = [solving_app, solved_app]

        def _deployment_for(app_id: str, chain_id: int | None = None):
            dep = MagicMock()
            dep.chain_id = 1
            dep.contract_address = "0x" + ("11" if app_id == "solving-app" else "22") * 20
            dep.status = AppStatus.SOLVING if app_id == "solving-app" else AppStatus.SOLVED
            return dep

        app_store.get_deployment.side_effect = _deployment_for

        worker = BenchmarkWorker(store, app_store=app_store, use_docker=False)

        async def mock_build_score_fn(intents):
            return lambda _plan, _state, _snapshot: 0.0

        async def mock_benchmark_submission(image_tag, intents, score_fn):
            self.assertEqual(image_tag, "solver-champ:screening")
            self.assertEqual([app.app_id for app, _, _ in intents], ["solving-app"])
            return [BenchmarkResult(
                intent_id="solving-app",
                plan=_make_plan("solving-app"),
                score=0.6,
                raw_output="1000",
            )]

        worker._build_score_fn = mock_build_score_fn
        worker._benchmark_submission = mock_benchmark_submission

        result = asyncio.run(worker.run_once())
        self.assertEqual(result, 1)
        self.assertEqual(store.get_champion().submission_id, champion.submission_id)
        app_store.update_deployment_status.assert_called_once_with(
            "solving-app", 1, AppStatus.SOLVED,
        )

    def test_genesis_champion_uses_genesis_image_for_bootstrap(self):
        """Builtin genesis champion (no image_tag) bootstraps solving apps via the
        configured genesis solver image."""
        from minotaur_subnet.shared.types import AppStatus

        store = SubmissionStore()
        champion = store.create(
            repo_url="builtin://baseline-swap-solver",
            commit_hash="builtin",
            epoch=0,
            hotkey=GENESIS_HOTKEY,
        )
        store.set_benchmark_result(
            champion.submission_id,
            valid=True,
            details={"per_intent": [{"intent_id": "x", "raw_output": "1000"}]},
        )
        store.adopt(champion.submission_id)

        solving_app = _make_intent("solving-app")
        app_store = MagicMock()
        app_store.list_apps.return_value = [solving_app]

        dep = MagicMock()
        dep.chain_id = 1
        dep.contract_address = "0x" + "11" * 20
        dep.status = AppStatus.SOLVING
        app_store.get_deployment.return_value = dep

        worker = BenchmarkWorker(
            store,
            app_store=app_store,
            genesis_solver_image="genesis:latest",
            use_docker=False,
        )

        async def mock_build_score_fn(intents):
            return lambda _plan, _state, _snapshot: 0.0

        async def mock_benchmark_submission(image_tag, intents, score_fn):
            self.assertEqual(image_tag, "genesis:latest")
            self.assertEqual([app.app_id for app, _, _ in intents], ["solving-app"])
            return [BenchmarkResult(
                intent_id="solving-app",
                plan=_make_plan("solving-app"),
                score=0.6,
                raw_output="1000",
            )]

        worker._build_score_fn = mock_build_score_fn
        worker._benchmark_submission = mock_benchmark_submission

        result = asyncio.run(worker.run_once())
        self.assertEqual(result, 1)
        app_store.update_deployment_status.assert_called_once_with(
            "solving-app", 1, AppStatus.SOLVED,
        )

    def test_scored_genesis_bootstraps_new_solving_apps_without_adoption(self):
        """A scored genesis baseline can bootstrap later apps before activation."""
        from minotaur_subnet.shared.types import AppStatus

        store = SubmissionStore()
        genesis = store.create(
            repo_url="builtin://baseline-swap-solver",
            commit_hash="builtin",
            epoch=0,
            hotkey=GENESIS_HOTKEY,
        )
        store.set_benchmark_result(
            genesis.submission_id,
            valid=True,
            details={"per_intent": [{"intent_id": "x", "raw_output": "1000"}]},
        )

        solving_app = _make_intent("solving-app")
        app_store = MagicMock()
        app_store.list_apps.return_value = [solving_app]

        dep = MagicMock()
        dep.chain_id = 1
        dep.contract_address = "0x" + "11" * 20
        dep.status = AppStatus.SOLVING
        app_store.get_deployment.return_value = dep

        worker = BenchmarkWorker(
            store,
            app_store=app_store,
            genesis_solver_image="genesis:latest",
            use_docker=False,
        )

        async def mock_build_score_fn(intents):
            return lambda _plan, _state, _snapshot: 0.0

        async def mock_benchmark_submission(image_tag, intents, score_fn):
            self.assertEqual(image_tag, "genesis:latest")
            self.assertEqual([app.app_id for app, _, _ in intents], ["solving-app"])
            return [BenchmarkResult(
                intent_id="solving-app",
                plan=_make_plan("solving-app"),
                score=0.6,
                raw_output="1000",
            )]

        worker._build_score_fn = mock_build_score_fn
        worker._benchmark_submission = mock_benchmark_submission

        result = asyncio.run(worker.run_once())
        self.assertEqual(result, 1)
        app_store.update_deployment_status.assert_called_once_with(
            "solving-app", 1, AppStatus.SOLVED,
        )


# ═══════════════════════════════════════════════════════════════════════════════
#                       BENCHMARK SCENARIO EXPANSION TESTS
# ═══════════════════════════════════════════════════════════════════════════════


class TestEnrichIntentsWithManifests(unittest.TestCase):
    """Tests for _enrich_intents_with_manifests() benchmark_scenarios support."""

    def _make_worker_with_engine(self, manifests: dict[str, dict]) -> BenchmarkWorker:
        """Create a BenchmarkWorker with a mock JS engine returning given manifests."""
        store = SubmissionStore()
        worker = BenchmarkWorker(store)
        engine = MagicMock()
        engine.get_manifest = lambda app_id: manifests.get(app_id)
        worker._js_engine = engine
        return worker

    def _make_intent_tuple(self, app_id: str = "app_test"):
        app_def = AppIntentDefinition(
            app_id=app_id, name="Test", version="1.0.0",
            intent_type="test", js_code="", description="test",
        )
        state = IntentState(contract_address="0xabc", chain_id=1, nonce=0, owner="")
        snapshot = MarketSnapshot(chain_id=1, block_number=100, timestamp=1000)
        return (app_def, state, snapshot)

    def test_benchmark_scenarios_expand(self):
        """When manifest has benchmark_scenarios, expand one intent per scenario."""
        manifest = {
            "intent_functions": [{"name": "swap", "example_params": {"foo": "1"}}],
            "benchmark_scenarios": [
                {"name": "scenario_A", "intent_function": "swap", "params": {"bar": "2"}},
                {"name": "scenario_B", "intent_function": "swap", "params": {"bar": "3"}},
                {"name": "scenario_C", "intent_function": "swap", "params": {"bar": "4"}},
            ],
        }
        worker = self._make_worker_with_engine({"app_test": manifest})
        intents = [self._make_intent_tuple()]

        enriched = worker._enrich_intents_with_manifests(intents)

        self.assertEqual(len(enriched), 3)
        names = [e[1].control.get("_scenario_name") for e in enriched]
        self.assertEqual(names, ["scenario_A", "scenario_B", "scenario_C"])
        # All should have the scenario params merged
        self.assertEqual(enriched[0][1].raw_params["bar"], "2")
        self.assertEqual(enriched[1][1].raw_params["bar"], "3")
        self.assertEqual(enriched[2][1].raw_params["bar"], "4")

    def test_fallback_to_example_params_without_scenarios(self):
        """Without benchmark_scenarios, falls back to example_params per function."""
        manifest = {
            "intent_functions": [
                {"name": "swap", "example_params": {"input_token": "0xaaa"}},
            ],
        }
        worker = self._make_worker_with_engine({"app_test": manifest})
        intents = [self._make_intent_tuple()]

        enriched = worker._enrich_intents_with_manifests(intents)

        self.assertEqual(len(enriched), 1)
        self.assertEqual(enriched[0][1].control["_intent_function"], "swap")
        self.assertEqual(enriched[0][1].raw_params["input_token"], "0xaaa")
        self.assertNotIn("_scenario_name", enriched[0][1].control)

    def test_empty_scenarios_falls_back(self):
        """Empty benchmark_scenarios array falls back to example_params."""
        manifest = {
            "intent_functions": [{"name": "execute", "example_params": {"x": "1"}}],
            "benchmark_scenarios": [],
        }
        worker = self._make_worker_with_engine({"app_test": manifest})
        intents = [self._make_intent_tuple()]

        enriched = worker._enrich_intents_with_manifests(intents)

        self.assertEqual(len(enriched), 1)
        self.assertEqual(enriched[0][1].raw_params["x"], "1")

    def test_no_manifest_passes_through(self):
        """Intent without manifest is returned unchanged."""
        worker = self._make_worker_with_engine({})
        intents = [self._make_intent_tuple()]

        enriched = worker._enrich_intents_with_manifests(intents)

        self.assertEqual(len(enriched), 1)
        self.assertEqual(enriched[0][1].raw_params, {})
        self.assertEqual(enriched[0][1].control, {})


if __name__ == "__main__":
    unittest.main()
