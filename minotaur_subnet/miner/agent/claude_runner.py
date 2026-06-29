"""Claude CLI runner — invokes `claude -p` with MCP tools for strategy generation.

Spawns a Claude Code subprocess with:
- Built-in tools: Read, Write, Edit, Bash, Glob, Grep, WebSearch, WebFetch
- MCP tools: minotaur-miner server (10 tools for app research + strategy dev)
- CLAUDE.md: Strategy development instructions in the workspace

The runner manages workspace setup (.mcp.json, CLAUDE.md), subprocess invocation,
and result extraction (strategy code read from disk after completion).
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from minotaur_subnet.miner.agent.app_discovery import AppContext
from minotaur_subnet.miner.agent.prompts import (
    build_claude_md,
    build_generate_task,
    build_improve_task,
)
from minotaur_subnet.miner.agent.score_tracker import ScoreFeedback
from minotaur_subnet.miner.agent.subagents import build_agents_json

logger = logging.getLogger(__name__)


@dataclass
class ClaudeResult:
    """Result from a Claude CLI invocation."""
    success: bool
    strategy_code: str | None  # Read from {app_id}/strategy.py after completion
    output_text: str            # Claude's stdout
    error: str | None
    duration_seconds: float
    # Token usage (parsed from the stream-json `result` event). Total
    # billable tokens across all sub-agents and turns; used by the
    # cost-gate's daily budget enforcement. 0 if the run didn't reach a
    # `result` event (timeout, crash, kill-switch).
    tokens_used: int = 0
    cost_usd: float = 0.0


# MCP tools to allow (no user confirmation needed)
_ALLOWED_TOOLS = ",".join([
    # Built-in tools — Bash unrestricted so the agent can run cast, curl,
    # python scripts, and any chain investigation commands.
    "Read", "Write", "Edit", "Bash", "Glob", "Grep",
    "WebSearch", "WebFetch",
    # Agent tool lets the root delegate to the custom sub-agents defined
    # in subagents.py (analyzer, strategy-writer, benchmark-runner).
    "Agent",
    "TodoWrite",
    # MCP tools
    "mcp__minotaur-miner__get_app_details",
    "mcp__minotaur-miner__get_app_solidity",
    "mcp__minotaur-miner__get_app_scores",
    "mcp__minotaur-miner__list_available_apps",
    "mcp__minotaur-miner__list_orders",
    "mcp__minotaur-miner__test_strategy",
    "mcp__minotaur-miner__score_strategy",
    "mcp__minotaur-miner__score_strategy_all",
    "mcp__minotaur-miner__inspect_strategy_plan",
    "mcp__minotaur-miner__get_champion_strategy",
    "mcp__minotaur-miner__replay_failed_swap",
    "mcp__minotaur-miner__list_strategies",
    "mcp__minotaur-miner__get_score_feedback",
    "mcp__minotaur-miner__read_contract",
    "mcp__minotaur-miner__get_token_balance",
    "mcp__minotaur-miner__resolve_token",
    "mcp__minotaur-miner__get_token_info",
    "mcp__minotaur-miner__multicall_read",
    "mcp__minotaur-miner__get_logs",
    "mcp__minotaur-miner__get_contract_code",
])


class ClaudeRunner:
    """Invokes Claude CLI for autonomous strategy development.

    Args:
        strategy_dir: Directory for strategy files and workspace config.
        validator_url: Validator URL passed to MCP server.
        anvil_rpc_url: Optional Anvil RPC URL for on-chain queries.
        timeout: Max seconds per Claude invocation.
        model: Claude model to use (sonnet, haiku, opus).
    """

    def __init__(
        self,
        strategy_dir: str = "strategies",
        validator_url: str = "http://localhost:8080",
        anvil_rpc_url: str | None = None,
        timeout: float = 300.0,
        model: str = "sonnet",
    ) -> None:
        self.strategy_dir = Path(strategy_dir)
        self.validator_url = validator_url
        self.anvil_rpc_url = anvil_rpc_url or ""
        self.timeout = timeout
        self.model = model
        self._workspace_ready = False

    @staticmethod
    def _parse_token_usage(trace_path: Path) -> tuple[int, float]:
        """Extract total billable tokens + cost from the stream-json trace.

        Claude Code emits a single ``{"type":"result", ..., "usage":{...},
        "total_cost_usd": X}`` event at the end of every successful run.
        That event aggregates usage across all sub-agents and turns —
        cleaner than summing per-message usage which double-counts due
        to streaming chunks sharing msg_ids.

        Returns (total_tokens, cost_usd). (0, 0.0) if the trace doesn't
        contain a result event (timeout, crash, kill-switch).
        """
        if not trace_path.exists():
            return 0, 0.0
        try:
            # Scan from the end — the result event is the last line.
            for line in reversed(trace_path.read_text().splitlines()):
                line = line.strip()
                if not line or '"type":"result"' not in line:
                    continue
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if e.get("type") != "result":
                    continue
                u = e.get("usage") or {}
                total = (
                    int(u.get("input_tokens", 0))
                    + int(u.get("cache_creation_input_tokens", 0))
                    + int(u.get("cache_read_input_tokens", 0))
                    + int(u.get("output_tokens", 0))
                )
                cost = float(e.get("total_cost_usd", 0.0) or 0.0)
                return total, cost
        except OSError:
            pass
        return 0, 0.0

    @staticmethod
    def _prune_old_traces(app_dir: Path, keep: int = 10) -> None:
        """Keep only the most recent ``keep`` trace files per app."""
        try:
            traces = sorted(app_dir.glob("claude_trace_*.log"))
            for old in traces[:-keep] if len(traces) > keep else []:
                try:
                    old.unlink()
                except OSError:
                    pass
        except OSError:
            pass

    def setup_workspace(self) -> None:
        """Create .mcp.json and CLAUDE.md in strategy_dir."""
        self.strategy_dir.mkdir(parents=True, exist_ok=True)

        # .mcp.json — points to the miner MCP server
        mcp_config = {
            "mcpServers": {
                "minotaur-miner": {
                    "type": "stdio",
                    "command": "python3",
                    "args": ["-m", "minotaur_subnet.miner.agent.mcp_server"],
                    "env": {
                        "PYTHONUNBUFFERED": "1",
                        "VALIDATOR_URL": self.validator_url,
                        "STRATEGY_DIR": str(self.strategy_dir.resolve()),
                    },
                },
            },
        }
        if self.anvil_rpc_url:
            mcp_config["mcpServers"]["minotaur-miner"]["env"]["ANVIL_RPC_URL"] = (
                self.anvil_rpc_url
            )

        mcp_path = self.strategy_dir / ".mcp.json"
        mcp_path.write_text(json.dumps(mcp_config, indent=2))

        # CLAUDE.md — strategy development instructions
        claude_md_path = self.strategy_dir / "CLAUDE.md"
        claude_md_path.write_text(build_claude_md())

        self._workspace_ready = True
        logger.info("Workspace configured: %s", self.strategy_dir)

    def run(self, task_prompt: str, app_id: str) -> ClaudeResult:
        """Invoke claude -p with MCP tools and return result.

        Args:
            task_prompt: The task for Claude to perform.
            app_id: App ID (used to locate strategy file after completion).

        Returns:
            ClaudeResult with success status and strategy code.
        """
        # Kill-switch: MINER_LLM_ENABLED=0 disables the Claude subprocess
        # entirely. Belt-and-suspenders so that an accidental
        # `docker compose up` can't burn Anthropic budget while the miner
        # is meant to be off (e.g. during debugging). Default is enabled.
        enabled_raw = os.environ.get("MINER_LLM_ENABLED", "1").strip().lower()
        if enabled_raw in ("0", "false", "no", "off", ""):
            logger.warning(
                "MINER_LLM_ENABLED=%r — skipping Claude subprocess for %s",
                enabled_raw, app_id,
            )
            return ClaudeResult(
                success=False,
                strategy_code=None,
                output_text="",
                error="MINER_LLM_ENABLED disabled",
                duration_seconds=0.0,
            )

        if not self._workspace_ready:
            self.setup_workspace()

        # Ensure app directory exists
        app_dir = self.strategy_dir / app_id
        app_dir.mkdir(parents=True, exist_ok=True)

        # Use stream-json output so Claude emits tool calls + partial text as
        # newline-separated JSON events, and redirect stdout/stderr to disk
        # BEFORE the process starts. This way any output Claude produces is
        # persisted even if we kill it on timeout — subprocess.run's
        # capture_output would lose the stream on SIGKILL.
        #
        # --include-partial-messages: flush token-level deltas (thinking,
        #   text, tool_use args) as they arrive, not only when a content
        #   block finishes. Without this, an extended thinking phase on a
        #   large tool response looks like 5 minutes of silence.
        # --effort medium: cap the reasoning budget so a single turn can't
        #   burn the full 300s wall clock on thinking alone. Prior runs
        #   went silent mid-turn for the full timeout after receiving a
        #   27 KB get_app_details response.
        # Sub-agents: analyzer, strategy-writer, benchmark-runner are
        # defined in subagents.py. The root session delegates to them via
        # the Agent tool. Each sub-agent has a focused tool allow-list,
        # its own model (benchmark-runner runs on Haiku for cost), and
        # a maxTurns cap. See subagents.py for design rationale.
        agents_json = build_agents_json()

        cmd = [
            "claude", "-p", task_prompt,
            "--output-format", "stream-json",
            "--verbose",
            "--include-partial-messages",
            "--effort", "medium",
            "--model", self.model,
            "--allowedTools", _ALLOWED_TOOLS,
            "--agents", agents_json,
        ]

        # Build env: strip CLAUDECODE to prevent nested session error
        env = os.environ.copy()
        env.pop("CLAUDECODE", None)
        env.pop("CLAUDE_CODE_ENTRYPOINT", None)

        trace_path = app_dir / f"claude_trace_{int(time.time())}.log"
        self._prune_old_traces(app_dir, keep=10)
        stderr_path = app_dir / f".claude_stderr_{int(time.time())}.log"

        start = time.monotonic()
        proc: subprocess.Popen | None = None
        try:
            with open(trace_path, "wb", buffering=0) as out_f, \
                 open(stderr_path, "wb", buffering=0) as err_f:
                proc = subprocess.Popen(
                    cmd,
                    stdout=out_f,
                    stderr=err_f,
                    cwd=str(self.strategy_dir),
                    env=env,
                )
                try:
                    rc = proc.wait(timeout=self.timeout)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        pass
                    rc = None  # signal timeout below

            duration = time.monotonic() - start
            output = trace_path.read_text(errors="replace") if trace_path.exists() else ""
            stderr = stderr_path.read_text(errors="replace") if stderr_path.exists() else ""
            # Collapse stderr into the trace file for single-file diagnostics.
            try:
                if stderr:
                    with open(trace_path, "a") as f:
                        f.write(f"\n===== STDERR =====\n{stderr}\n")
                stderr_path.unlink(missing_ok=True)
            except OSError:
                pass

            # Parse token usage + cost from the trace's `result` event.
            # Even on timeout/error, partial usage may be recoverable for
            # accurate budget tracking — if the trace had any complete
            # turns, we charge for them.
            tokens_used, cost_usd = self._parse_token_usage(trace_path)

            if rc is None:
                # Timed out
                strategy_path = app_dir / "strategy.py"
                leftover_bytes = (
                    strategy_path.stat().st_size if strategy_path.exists() else 0
                )
                logger.error(
                    "Claude timed out for %s after %.1fs "
                    "(leftover strategy on disk: %d bytes, stream captured: "
                    "%d B, billed: %d tokens / $%.4f — trace: %s) — not treated as success",
                    app_id, duration, leftover_bytes,
                    len(output), tokens_used, cost_usd, trace_path.name,
                )
                return ClaudeResult(
                    success=False,
                    strategy_code=None,
                    output_text=output,
                    error=f"Timed out after {self.timeout}s",
                    duration_seconds=duration,
                    tokens_used=tokens_used,
                    cost_usd=cost_usd,
                )

            if rc != 0:
                logger.warning(
                    "Claude exited with code %d for %s (%.1fs, billed: "
                    "%d tokens / $%.4f) — trace: %s",
                    rc, app_id, duration, tokens_used, cost_usd, trace_path.name,
                )
                return ClaudeResult(
                    success=False,
                    strategy_code=None,
                    output_text=output,
                    error=stderr or f"Exit code {rc}",
                    duration_seconds=duration,
                    tokens_used=tokens_used,
                    cost_usd=cost_usd,
                )

            # Read strategy from disk (Claude should have written it)
            strategy_path = app_dir / "strategy.py"
            strategy_code = None
            if strategy_path.exists():
                strategy_code = strategy_path.read_text()

            success = strategy_code is not None and len(strategy_code) > 0
            logger.info(
                "Claude completed for %s: success=%s, %.1fs, %d B stream, "
                "billed: %d tokens / $%.4f — trace: %s",
                app_id, success, duration, len(output),
                tokens_used, cost_usd, trace_path.name,
            )
            return ClaudeResult(
                success=success,
                strategy_code=strategy_code,
                output_text=output,
                error=None if success else "No strategy file written",
                duration_seconds=duration,
                tokens_used=tokens_used,
                cost_usd=cost_usd,
            )

        except (OSError, subprocess.SubprocessError) as exc:
            # Defensive catch so a disk-full or spawn failure doesn't crash
            # the agent loop. The trace file may still be useful for
            # inspection even if we got here.
            duration = time.monotonic() - start
            if proc is not None and proc.poll() is None:
                proc.kill()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
            logger.error(
                "Claude subprocess error for %s (%.1fs): %s",
                app_id, duration, exc,
            )
            return ClaudeResult(
                success=False,
                strategy_code=None,
                output_text="",
                error=f"Subprocess error: {exc}",
                duration_seconds=duration,
            )

        except FileNotFoundError:
            duration = time.monotonic() - start
            logger.error("claude CLI not found. Is Claude Code installed?")
            return ClaudeResult(
                success=False,
                strategy_code=None,
                output_text="",
                error="claude CLI not found. Install Claude Code first.",
                duration_seconds=duration,
            )

    def generate_strategy(self, app_context: AppContext) -> ClaudeResult:
        """Generate a new strategy for an app.

        Returns the full ClaudeResult so callers can inspect token usage
        and cost for budget accounting, not just the strategy code.
        """
        task = build_generate_task(
            app_id=app_context.app_id,
            name=app_context.name,
            description=app_context.description,
            intent_type=app_context.intent_type,
            supported_chains=app_context.supported_chains,
        )
        return self.run(task, app_context.app_id)

    def improve_strategy(
        self,
        app_context: AppContext,
        feedback: ScoreFeedback,
    ) -> ClaudeResult:
        """Improve an existing strategy based on score feedback.

        Returns the full ClaudeResult so callers can inspect token usage
        and cost for budget accounting, not just the strategy code.
        """
        task = build_improve_task(
            app_id=app_context.app_id,
            avg_score=feedback.avg_score,
            best_score=feedback.best_score,
            trend=feedback.trend,
            recent_scores=feedback.recent_scores,
            champion_score=feedback.champion_score,
            target_score=feedback.target_score,
            scenario_scores=feedback.scenario_scores,
            quote_failure_rate=feedback.quote_failure_rate,
            recent_quote_errors=feedback.recent_quote_errors,
            last_score=feedback.last_score,
            last_score_message=feedback.last_score_message,
            relative=feedback.relative,
            verdict=feedback.verdict,
            relative_headroom=feedback.relative_headroom,
        )
        return self.run(task, app_context.app_id)
