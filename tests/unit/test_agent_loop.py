"""Tests for AgentLoop — the agentic solver development loop."""

import asyncio
import pytest
import tempfile
import shutil
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from minotaur_subnet.miner.agent.loop import AgentLoop, submit_solver_source
from minotaur_subnet.miner.agent.app_discovery import AppContext
from minotaur_subnet.miner.agent.score_tracker import ScoreFeedback


# ── Fake strategy code ──────────────────────────────────────────────────────

VALID_STRATEGY_CODE = '''\
from minotaur_subnet.sdk.strategy import Strategy
from minotaur_subnet.shared.types import ExecutionPlan, Interaction
from minotaur_subnet.sdk.intent_solver import MarketSnapshot

class TestStrategy(Strategy):
    APP_ID = "{app_id}"

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


MOCK_AVAILABLE_APPS = [
    {
        "app_id": "app-test-001",
        "name": "Test App",
        "intent_type": "swap",
        "description": "A test app",
        "config": {"supported_chains": [1]},
    },
]

MOCK_APP_CONTEXT = AppContext(
    app_id="app-test-001",
    name="Test App",
    description="A test app",
    intent_type="swap",
    supported_chains=[1],
    solidity_code="// test contract",
)

MOCK_SCORES_NO_DATA = {
    "total_executions": 0,
    "avg_score": 0.0,
    "best_score": 0.0,
    "recent_scores": [],
}

MOCK_SCORES_LOW = {
    "total_executions": 10,
    "avg_score": 0.3,
    "best_score": 0.5,
    "recent_scores": [0.2, 0.3, 0.4, 0.3, 0.2],
}


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def tmp_strategy_dir():
    d = tempfile.mkdtemp(prefix="strategies_")
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def agent(tmp_strategy_dir):
    return AgentLoop(
        validator_url="http://localhost:9100",
        strategy_dir=tmp_strategy_dir,
        miner_id="test-miner",
        loop_interval=1.0,
        improvement_threshold=0.7,
        max_llm_calls_per_cycle=3,
    )


# ── Tests ───────────────────────────────────────────────────────────────────


class TestAgentInit:
    def test_creates_with_defaults(self, agent):
        assert agent.miner_id == "test-miner"
        assert agent.loop_interval == 1.0
        assert agent.max_llm_calls == 3

    def test_status_initial(self, agent):
        status = agent.status()
        assert status["running"] is False
        assert status["cycle_count"] == 0
        assert status["strategy_count"] == 0


class TestCycle:
    @pytest.mark.asyncio
    async def test_cycle_no_apps(self, agent):
        """Cycle with no apps available does nothing."""
        agent.discovery.fetch_available_apps = AsyncMock(return_value=[])
        await agent._cycle()
        agent.discovery.fetch_available_apps.assert_called_once()

    @pytest.mark.asyncio
    async def test_cycle_discovers_apps_and_generates(self, agent):
        """Cycle discovers apps, generates strategies for new ones."""
        code = VALID_STRATEGY_CODE.format(app_id="app-test-001")

        agent.discovery.fetch_available_apps = AsyncMock(return_value=MOCK_AVAILABLE_APPS)
        agent.discovery.fetch_app_scores = AsyncMock(return_value=MOCK_SCORES_NO_DATA)
        agent.discovery.fetch_app_details = AsyncMock(return_value=MOCK_APP_CONTEXT)
        agent.generator.generate = MagicMock(return_value=code)

        with patch.object(agent, '_bundle_solver_source', return_value="# bundled"):
            with patch("minotaur_subnet.miner.agent.loop.submit_solver_source", new_callable=AsyncMock) as mock_submit:
                mock_submit.return_value = {"accepted": True}
                await agent._cycle()

        # Should have generated a strategy
        agent.generator.generate.assert_called_once()
        # Should have submitted
        mock_submit.assert_called_once()

    @pytest.mark.asyncio
    async def test_cycle_improves_low_score(self, agent, tmp_strategy_dir):
        """Cycle improves strategy when score is low."""
        # Pre-register a strategy
        app_dir = Path(tmp_strategy_dir) / "app-test-001"
        app_dir.mkdir(parents=True)
        existing_code = VALID_STRATEGY_CODE.format(app_id="app-test-001")
        (app_dir / "strategy.py").write_text(existing_code)

        improved_code = VALID_STRATEGY_CODE.format(app_id="app-test-001")

        agent.score_tracker.mark_has_strategy("app-test-001")
        agent.score_tracker._last_improved["app-test-001"] = 0
        agent.discovery.fetch_available_apps = AsyncMock(return_value=MOCK_AVAILABLE_APPS)
        agent.discovery.fetch_app_scores = AsyncMock(return_value=MOCK_SCORES_LOW)
        agent.discovery.fetch_app_details = AsyncMock(return_value=MOCK_APP_CONTEXT)
        agent.generator.improve = MagicMock(return_value=improved_code)

        with patch.object(agent, '_bundle_solver_source', return_value="# bundled"):
            with patch("minotaur_subnet.miner.agent.loop.submit_solver_source", new_callable=AsyncMock) as mock_submit:
                mock_submit.return_value = {"accepted": True}
                await agent._cycle()

        agent.generator.improve.assert_called_once()

    @pytest.mark.asyncio
    async def test_cycle_respects_max_llm_calls(self, agent):
        """Cycle limits LLM calls per cycle."""
        agent.max_llm_calls = 1

        apps = [
            {"app_id": "app-a", "name": "A", "intent_type": "swap", "description": "a", "config": {}},
            {"app_id": "app-b", "name": "B", "intent_type": "swap", "description": "b", "config": {}},
        ]

        agent.discovery.fetch_available_apps = AsyncMock(return_value=apps)
        agent.discovery.fetch_app_scores = AsyncMock(return_value=MOCK_SCORES_NO_DATA)
        agent.discovery.fetch_app_details = AsyncMock(return_value=MOCK_APP_CONTEXT)

        call_count = 0

        def mock_generate(ctx):
            nonlocal call_count
            call_count += 1
            return VALID_STRATEGY_CODE.format(app_id=ctx.app_id)

        agent.generator.generate = mock_generate

        with patch.object(agent, '_bundle_solver_source', return_value="# bundled"):
            with patch("minotaur_subnet.miner.agent.loop.submit_solver_source", new_callable=AsyncMock) as mock_submit:
                mock_submit.return_value = {"accepted": True}
                await agent._cycle()

        assert call_count == 1  # Only 1 call despite 2 apps

    @pytest.mark.asyncio
    async def test_cycle_skips_on_test_failure(self, agent):
        """Failed strategy test doesn't get submitted."""
        bad_code = "# not a valid strategy"

        agent.discovery.fetch_available_apps = AsyncMock(return_value=MOCK_AVAILABLE_APPS)
        agent.discovery.fetch_app_scores = AsyncMock(return_value=MOCK_SCORES_NO_DATA)
        agent.discovery.fetch_app_details = AsyncMock(return_value=MOCK_APP_CONTEXT)
        agent.generator.generate = MagicMock(return_value=bad_code)

        with patch("minotaur_subnet.miner.agent.loop.submit_solver_source", new_callable=AsyncMock) as mock_submit:
            await agent._cycle()

        # Should NOT submit because test failed
        mock_submit.assert_not_called()

    @pytest.mark.asyncio
    async def test_cycle_skips_on_none_result(self, agent):
        """None result from generator doesn't crash and doesn't submit."""
        agent.discovery.fetch_available_apps = AsyncMock(return_value=MOCK_AVAILABLE_APPS)
        agent.discovery.fetch_app_scores = AsyncMock(return_value=MOCK_SCORES_NO_DATA)
        agent.discovery.fetch_app_details = AsyncMock(return_value=MOCK_APP_CONTEXT)
        agent.generator.generate = MagicMock(return_value=None)

        with patch("minotaur_subnet.miner.agent.loop.submit_solver_source", new_callable=AsyncMock) as mock_submit:
            await agent._cycle()

        mock_submit.assert_not_called()

    @pytest.mark.asyncio
    async def test_cycle_all_performing_well(self, agent):
        """Cycle with all apps above threshold does nothing."""
        agent.score_tracker.mark_has_strategy("app-test-001")
        agent.score_tracker._last_improved["app-test-001"] = 0
        agent.score_tracker.update("app-test-001", {
            "total_executions": 10,
            "avg_score": 0.9,
        })

        agent.discovery.fetch_available_apps = AsyncMock(return_value=MOCK_AVAILABLE_APPS)
        agent.discovery.fetch_app_scores = AsyncMock(return_value={
            "total_executions": 10,
            "avg_score": 0.9,
        })

        agent.generator.generate = MagicMock()

        await agent._cycle()

        # Should NOT call generator
        agent.generator.generate.assert_not_called()


class TestStrategyPersistence:
    def test_save_and_load(self, agent, tmp_strategy_dir):
        code = VALID_STRATEGY_CODE.format(app_id="app-test-001")
        agent._save_strategy("app-test-001", code)

        loaded = agent._read_strategy_code("app-test-001")
        assert loaded == code

    def test_save_archives_previous(self, agent, tmp_strategy_dir):
        code_v1 = VALID_STRATEGY_CODE.format(app_id="app-test-001")
        code_v2 = code_v1 + "\n# v2"

        agent._save_strategy("app-test-001", code_v1)
        agent._save_strategy("app-test-001", code_v2)

        # v1 should be archived
        v1_path = Path(tmp_strategy_dir) / "app-test-001" / "strategy_v1.py"
        assert v1_path.exists()
        assert v1_path.read_text() == code_v1

        # Current should be v2
        current = agent._read_strategy_code("app-test-001")
        assert "# v2" in current

    def test_load_existing_strategies(self, tmp_strategy_dir):
        # Write a strategy to disk
        app_dir = Path(tmp_strategy_dir) / "app-test-001"
        app_dir.mkdir(parents=True)
        code = VALID_STRATEGY_CODE.format(app_id="app-test-001")
        (app_dir / "strategy.py").write_text(code)

        # Create agent — should load on init
        agent = AgentLoop(
            validator_url="http://localhost:9100",
            strategy_dir=tmp_strategy_dir,
        )
        agent._load_existing_strategies()

        assert agent.router.get_strategy("app-test-001") is not None

    def test_read_nonexistent(self, agent):
        assert agent._read_strategy_code("nonexistent") is None


class TestBundleSolver:
    def test_bundle_empty(self, agent):
        source = agent._bundle_solver_source()
        assert "SOLVER_CLASS" in source
        assert "BundledRoutingSolver" in source

    def test_bundle_with_strategies(self, agent, tmp_strategy_dir):
        code = VALID_STRATEGY_CODE.format(app_id="app-test-001")
        agent._save_strategy("app-test-001", code)

        # Register the strategy so it appears in router
        strategy = agent._load_strategy_from_disk("app-test-001")
        agent.router.register_strategy(strategy)

        source = agent._bundle_solver_source()
        assert "SOLVER_CLASS" in source
        assert "app-test-001" in source
        assert "TestStrategy" in source


class TestStatus:
    def test_status_with_strategies(self, agent, tmp_strategy_dir):
        code = VALID_STRATEGY_CODE.format(app_id="app-test-001")
        agent._save_strategy("app-test-001", code)
        strategy = agent._load_strategy_from_disk("app-test-001")
        agent.router.register_strategy(strategy)

        status = agent.status()
        assert status["strategy_count"] == 1
        assert "app-test-001" in status["strategies"]
