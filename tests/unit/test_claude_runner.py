"""Tests for ClaudeRunner — Claude CLI subprocess invocation."""

import json
import pytest
import shutil
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

from minotaur_subnet.miner.agent.claude_runner import ClaudeRunner, ClaudeResult
from minotaur_subnet.miner.agent.app_discovery import AppContext
from minotaur_subnet.miner.agent.score_tracker import ScoreFeedback


# ── Valid strategy code ────────────────────────────────────────────────────

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


# ── Fixtures ───────────────────────────────────────────────────────────────


@pytest.fixture
def tmp_dir():
    d = tempfile.mkdtemp(prefix="claude_runner_")
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def runner(tmp_dir):
    return ClaudeRunner(
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
        description="A test app",
        intent_type="swap",
        supported_chains=[1],
    )


@pytest.fixture
def feedback():
    return ScoreFeedback(
        app_id="app-test-001",
        avg_score=0.4,
        best_score=0.6,
        recent_scores=[0.3, 0.4, 0.5],
        total_executions=10,
        trend="declining",
    )


# ── Workspace setup tests ─────────────────────────────────────────────────


class TestWorkspaceSetup:
    def test_creates_mcp_json(self, runner, tmp_dir):
        runner.setup_workspace()
        mcp_path = Path(tmp_dir) / ".mcp.json"
        assert mcp_path.exists()

        config = json.loads(mcp_path.read_text())
        assert "mcpServers" in config
        assert "minotaur-miner" in config["mcpServers"]

        srv = config["mcpServers"]["minotaur-miner"]
        assert srv["command"] == "python3"
        assert srv["env"]["VALIDATOR_URL"] == "http://localhost:9100"

    def test_creates_claude_md(self, runner, tmp_dir):
        runner.setup_workspace()
        claude_md = Path(tmp_dir) / "CLAUDE.md"
        assert claude_md.exists()
        content = claude_md.read_text()
        assert "Strategy" in content
        assert "test_strategy" in content
        assert "ExecutionPlan" in content

    def test_includes_anvil_rpc_url(self, tmp_dir):
        runner = ClaudeRunner(
            strategy_dir=tmp_dir,
            validator_url="http://localhost:9100",
            anvil_rpc_url="http://localhost:8545",
        )
        runner.setup_workspace()

        config = json.loads((Path(tmp_dir) / ".mcp.json").read_text())
        env = config["mcpServers"]["minotaur-miner"]["env"]
        assert env["ANVIL_RPC_URL"] == "http://localhost:8545"

    def test_no_anvil_rpc_url(self, runner, tmp_dir):
        runner.setup_workspace()
        config = json.loads((Path(tmp_dir) / ".mcp.json").read_text())
        env = config["mcpServers"]["minotaur-miner"]["env"]
        assert "ANVIL_RPC_URL" not in env

    def test_creates_strategy_dir(self, tmp_dir):
        nested = Path(tmp_dir) / "nested" / "strategies"
        runner = ClaudeRunner(strategy_dir=str(nested))
        runner.setup_workspace()
        assert nested.exists()


# ── Subprocess invocation tests ────────────────────────────────────────────


class TestRun:
    @patch("minotaur_subnet.miner.agent.claude_runner.subprocess.run")
    def test_success_with_strategy_file(self, mock_run, runner, tmp_dir):
        """Successful run reads strategy from disk."""
        # Pre-write the strategy file (Claude would write it)
        app_dir = Path(tmp_dir) / "app-test-001"
        app_dir.mkdir(parents=True)
        (app_dir / "strategy.py").write_text(VALID_STRATEGY)

        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout="Strategy written successfully.",
            stderr="",
        )

        result = runner.run("Write a strategy", "app-test-001")
        assert result.success is True
        assert result.strategy_code == VALID_STRATEGY
        assert result.error is None

    @patch("minotaur_subnet.miner.agent.claude_runner.subprocess.run")
    def test_failure_no_strategy_file(self, mock_run, runner, tmp_dir):
        """No strategy file written = failure."""
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout="I couldn't figure it out.",
            stderr="",
        )

        result = runner.run("Write a strategy", "app-test-001")
        assert result.success is False
        assert result.strategy_code is None

    @patch("minotaur_subnet.miner.agent.claude_runner.subprocess.run")
    def test_nonzero_exit_code(self, mock_run, runner, tmp_dir):
        """Non-zero exit code = failure."""
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=1,
            stdout="",
            stderr="Error: something went wrong",
        )

        result = runner.run("Write a strategy", "app-test-001")
        assert result.success is False
        assert "Error" in result.error

    @patch("minotaur_subnet.miner.agent.claude_runner.subprocess.run")
    def test_timeout(self, mock_run, runner):
        """Timeout is handled gracefully."""
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="claude", timeout=60)

        result = runner.run("Write a strategy", "app-test-001")
        assert result.success is False
        assert "Timed out" in result.error

    @patch("minotaur_subnet.miner.agent.claude_runner.subprocess.run")
    def test_claude_not_found(self, mock_run, runner):
        """Missing claude CLI is handled gracefully."""
        mock_run.side_effect = FileNotFoundError()

        result = runner.run("Write a strategy", "app-test-001")
        assert result.success is False
        assert "not found" in result.error

    @patch("minotaur_subnet.miner.agent.claude_runner.subprocess.run")
    def test_strips_claudecode_env(self, mock_run, runner, tmp_dir):
        """CLAUDECODE env var is stripped to prevent nested session error."""
        # Pre-write strategy so we get a successful result
        app_dir = Path(tmp_dir) / "app-test-001"
        app_dir.mkdir(parents=True)
        (app_dir / "strategy.py").write_text(VALID_STRATEGY)

        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="Done", stderr="",
        )

        # Set CLAUDECODE in environment
        with patch.dict("os.environ", {"CLAUDECODE": "1", "CLAUDE_CODE_ENTRYPOINT": "cli"}):
            runner.run("Write a strategy", "app-test-001")

        # Check the env passed to subprocess
        call_kwargs = mock_run.call_args[1]
        env = call_kwargs["env"]
        assert "CLAUDECODE" not in env
        assert "CLAUDE_CODE_ENTRYPOINT" not in env

    @patch("minotaur_subnet.miner.agent.claude_runner.subprocess.run")
    def test_passes_correct_args(self, mock_run, runner, tmp_dir):
        """Check subprocess args include model, output format, allowed tools."""
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr="",
        )

        runner.run("Do something", "app-test-001")

        call_args = mock_run.call_args[0][0]
        assert call_args[0] == "claude"
        assert "-p" in call_args
        assert "--model" in call_args
        assert "sonnet" in call_args
        assert "--allowedTools" in call_args
        assert "--output-format" in call_args

    @patch("minotaur_subnet.miner.agent.claude_runner.subprocess.run")
    def test_cwd_is_strategy_dir(self, mock_run, runner, tmp_dir):
        """Subprocess cwd is the strategy directory."""
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr="",
        )

        runner.run("Do something", "app-test-001")

        call_kwargs = mock_run.call_args[1]
        assert call_kwargs["cwd"] == tmp_dir

    @patch("minotaur_subnet.miner.agent.claude_runner.subprocess.run")
    def test_auto_setups_workspace(self, mock_run, runner, tmp_dir):
        """First run auto-creates workspace files."""
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr="",
        )

        assert not (Path(tmp_dir) / ".mcp.json").exists()
        runner.run("Do something", "app-test-001")
        assert (Path(tmp_dir) / ".mcp.json").exists()
        assert (Path(tmp_dir) / "CLAUDE.md").exists()


# ── High-level methods ─────────────────────────────────────────────────────


class TestGenerateStrategy:
    @patch("minotaur_subnet.miner.agent.claude_runner.subprocess.run")
    def test_returns_code_on_success(self, mock_run, runner, tmp_dir, app_context):
        app_dir = Path(tmp_dir) / "app-test-001"
        app_dir.mkdir(parents=True)
        (app_dir / "strategy.py").write_text(VALID_STRATEGY)

        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="Done", stderr="",
        )

        code = runner.generate_strategy(app_context)
        assert code is not None
        assert "STRATEGY_CLASS" in code

    @patch("minotaur_subnet.miner.agent.claude_runner.subprocess.run")
    def test_returns_none_on_failure(self, mock_run, runner, app_context):
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="Error",
        )

        code = runner.generate_strategy(app_context)
        assert code is None


class TestImproveStrategy:
    @patch("minotaur_subnet.miner.agent.claude_runner.subprocess.run")
    def test_returns_code_on_success(self, mock_run, runner, tmp_dir, app_context, feedback):
        app_dir = Path(tmp_dir) / "app-test-001"
        app_dir.mkdir(parents=True)
        (app_dir / "strategy.py").write_text(VALID_STRATEGY)

        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="Done", stderr="",
        )

        code = runner.improve_strategy(app_context, feedback)
        assert code is not None

    @patch("minotaur_subnet.miner.agent.claude_runner.subprocess.run")
    def test_returns_none_on_failure(self, mock_run, runner, app_context, feedback):
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="Error",
        )

        code = runner.improve_strategy(app_context, feedback)
        assert code is None
