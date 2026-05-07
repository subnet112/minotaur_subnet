"""Tests for StrategyGenerator (mocks ClaudeRunner.run subprocess)."""

import subprocess
import tempfile
import shutil
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

from minotaur_subnet.miner.agent.strategy_generator import StrategyGenerator
from minotaur_subnet.miner.agent.app_discovery import AppContext
from minotaur_subnet.miner.agent.score_tracker import ScoreFeedback


# ── Fake LLM response (valid strategy code) ────────────────────────────────

FAKE_STRATEGY_CODE = '''\
from minotaur_subnet.sdk.strategy import Strategy
from minotaur_subnet.shared.types import ExecutionPlan, Interaction
from minotaur_subnet.sdk.intent_solver import MarketSnapshot

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


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def tmp_dir():
    d = tempfile.mkdtemp(prefix="strat_gen_")
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def generator(tmp_dir):
    return StrategyGenerator(
        strategy_dir=tmp_dir,
        validator_url="http://localhost:9100",
        timeout=60.0,
        model="sonnet",
    )


@pytest.fixture
def app_context():
    return AppContext(
        app_id="app-test-001",
        name="Test App",
        description="A test app for unit testing",
        intent_type="swap",
        supported_chains=[1],
        solidity_code="// SPDX-License-Identifier: MIT\ncontract Test {}",
        manifest={"intent_functions": {"execute": {"params": {}}}},
    )


@pytest.fixture
def feedback():
    return ScoreFeedback(
        app_id="app-test-001",
        avg_score=0.4,
        best_score=0.6,
        recent_scores=[0.3, 0.4, 0.5, 0.4, 0.3],
        total_executions=10,
        trend="declining",
    )


# ── Helpers ─────────────────────────────────────────────────────────────────


def _setup_strategy_file(tmp_dir: str, app_id: str, code: str) -> None:
    """Pre-write a strategy file so ClaudeRunner finds it after 'subprocess'."""
    app_dir = Path(tmp_dir) / app_id
    app_dir.mkdir(parents=True, exist_ok=True)
    (app_dir / "strategy.py").write_text(code)


# ── Tests ───────────────────────────────────────────────────────────────────


class TestGenerate:
    @patch("minotaur_subnet.miner.agent.claude_runner.subprocess.run")
    def test_generates_code(self, mock_run, generator, tmp_dir, app_context):
        _setup_strategy_file(tmp_dir, "app-test-001", FAKE_STRATEGY_CODE)
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="Done", stderr="",
        )

        code = generator.generate(app_context)
        assert code is not None
        assert "STRATEGY_CLASS" in code
        assert "TestStrategy" in code
        mock_run.assert_called_once()

    @patch("minotaur_subnet.miner.agent.claude_runner.subprocess.run")
    def test_returns_none_on_failure(self, mock_run, generator, app_context):
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="Error",
        )

        code = generator.generate(app_context)
        assert code is None

    @patch("minotaur_subnet.miner.agent.claude_runner.subprocess.run")
    def test_returns_none_on_timeout(self, mock_run, generator, app_context):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="claude", timeout=60)

        code = generator.generate(app_context)
        assert code is None


class TestImprove:
    @patch("minotaur_subnet.miner.agent.claude_runner.subprocess.run")
    def test_improves_code(self, mock_run, generator, tmp_dir, app_context, feedback):
        _setup_strategy_file(tmp_dir, "app-test-001", FAKE_STRATEGY_CODE)
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="Done", stderr="",
        )

        code = generator.improve(app_context, "# old code", feedback)
        assert code is not None
        assert "STRATEGY_CLASS" in code
        mock_run.assert_called_once()

    @patch("minotaur_subnet.miner.agent.claude_runner.subprocess.run")
    def test_returns_none_on_failure(self, mock_run, generator, app_context, feedback):
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="Error",
        )

        code = generator.improve(app_context, "# old code", feedback)
        assert code is None


class TestClaudeInvocation:
    @patch("minotaur_subnet.miner.agent.claude_runner.subprocess.run")
    def test_uses_correct_model(self, mock_run, tmp_dir, app_context):
        gen = StrategyGenerator(
            strategy_dir=tmp_dir,
            validator_url="http://localhost:9100",
            model="haiku",
        )
        _setup_strategy_file(tmp_dir, "app-test-001", FAKE_STRATEGY_CODE)
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="Done", stderr="",
        )

        gen.generate(app_context)

        cmd = mock_run.call_args[0][0]
        assert "haiku" in cmd

    @patch("minotaur_subnet.miner.agent.claude_runner.subprocess.run")
    def test_workspace_setup(self, mock_run, tmp_dir, app_context):
        gen = StrategyGenerator(
            strategy_dir=tmp_dir,
            validator_url="http://localhost:9100",
        )
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="Done", stderr="",
        )

        gen.generate(app_context)

        # Workspace should have been set up
        assert (Path(tmp_dir) / ".mcp.json").exists()
        assert (Path(tmp_dir) / "CLAUDE.md").exists()
