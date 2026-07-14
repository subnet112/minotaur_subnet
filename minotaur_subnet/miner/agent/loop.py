"""AgentLoop — main orchestrator for the agentic solver development loop.

Periodically:
1. Discovers active apps from the validator
2. Fetches per-app scores
3. Identifies underperformers
4. Generates/improves strategies via Claude CLI
5. Re-validates strategies locally (belt and suspenders)
6. Bundles into RoutingSolver and submits to validator
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import time
from pathlib import Path
from typing import Any

from minotaur_subnet.miner.agent.app_discovery import AppDiscovery, AppContext
from minotaur_subnet.miner.agent.cost_gate import CostGate
from minotaur_subnet.miner.agent.score_tracker import ScoreTracker
from minotaur_subnet.miner.agent.strategy_generator import StrategyGenerator
from minotaur_subnet.miner.agent.strategy_tester import StrategyTester, load_strategy
from minotaur_subnet.miner.metrics import MinerCounters
from minotaur_subnet.sdk.routing_solver import RoutingSolver
from minotaur_subnet.sdk.strategy import Strategy

logger = logging.getLogger(__name__)


def _open_or_get_pr(
    upstream: str, head_owner: str, branch: str, token: str, *, head_sha: str = "",
) -> int | None:
    """Open (or find the existing open) PR for ``head_owner:branch`` against
    ``upstream``'s default branch, returning the PR number.

    The PR-based submission fold: the miner forks the canonical solver repo, pushes
    a branch, and opens a PR; the PR number + head SHA are what it signs and submits.
    Idempotent — GitHub returns 422 if a PR already exists for the head, in which
    case we look it up. Returns None on failure (no token / API error).
    """
    import json as _json
    import urllib.error
    import urllib.request

    if not token or not upstream or not head_owner or not branch:
        return None
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "Authorization": f"Bearer {token}",
    }
    head = f"{head_owner}:{branch}"
    base_api = f"https://api.github.com/repos/{upstream}"

    def _req(method: str, path: str, payload: dict | None = None):
        data = _json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(base_api + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:  # noqa: S310 fixed host
                body = resp.read().decode("utf-8")
                return resp.status, (_json.loads(body) if body else None)
        except urllib.error.HTTPError as exc:
            return exc.code, None
        except Exception:
            return 0, None

    title = f"Champion submission: {head_owner}/{branch}"
    body = f"Automated Minotaur solver submission.\nhead_sha: `{head_sha}`"
    status, data = _req("POST", "/pulls", {"title": title, "head": head, "base": "main", "body": body})
    if status == 201 and data:
        return int(data["number"])
    # 422 = a PR already exists for this head -> find it.
    status, data = _req("GET", f"/pulls?head={head}&state=open")
    if status == 200 and isinstance(data, list) and data:
        return int(data[0]["number"])
    logger.warning("Could not open/find PR for %s on %s (status=%s)", head, upstream, status)
    return None


async def submit_solver_via_git(
    source: str,
    validator_url: str,
    miner_id: str,
    output_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Commit strategy updates to the solver repo and submit via git path.

    Uses the persistent solver repo (SOLVER_REPO_PATH env var, defaults to
    ``~/git/minotaur-solver``). The repo already has the proper structure:
    solver.py (PrivateRoutingSolver), Dockerfile, strategies/ directory.

    The agent writes per-app strategies to ``strategies/{app_id}/strategy.py``
    inside the repo, commits, pushes to the remote, and submits the repo URL
    + commit hash to the validator via ``/v1/submissions``.

    Args:
        source: Bundled solver source (written to output_dir for reference).
        validator_url: Base URL of the validator.
        miner_id: Miner identifier.
        output_dir: Local workspace directory for reference copy.
    """
    import aiohttp
    import base64
    import subprocess

    # Save bundled source locally for reference
    out = Path(output_dir) if output_dir else Path(".")
    out.mkdir(parents=True, exist_ok=True)
    (out / "solver.py").write_text(source)

    # Find the solver repo
    repo_dir = Path(os.environ.get(
        "SOLVER_REPO_PATH",
        os.path.expanduser("~/git/minotaur-solver"),
    ))
    if not (repo_dir / ".git").exists():
        logger.error("Solver repo not found at %s — set SOLVER_REPO_PATH", repo_dir)
        return {"accepted": False, "error": "Solver repo not found"}

    # Copy per-app strategies from the workspace into the repo
    strategies_src = out if out.name != "." else Path("strategies")
    copied = 0
    for app_dir in strategies_src.iterdir():
        if not app_dir.is_dir() or not app_dir.name.startswith("app_"):
            continue
        strategy_file = app_dir / "strategy.py"
        if not strategy_file.exists():
            continue
        dest_dir = repo_dir / "strategies" / app_dir.name
        dest_dir.mkdir(parents=True, exist_ok=True)
        (dest_dir / "strategy.py").write_text(strategy_file.read_text())
        # Ensure __init__.py for importability
        init_file = dest_dir / "__init__.py"
        if not init_file.exists():
            init_file.write_text("")
        copied += 1

    if copied == 0:
        logger.warning("No strategies to commit to solver repo")
        return {"accepted": False, "error": "No strategies found"}

    # Git add, commit, push
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": f"Minotaur Miner ({miner_id})",
        "GIT_AUTHOR_EMAIL": f"{miner_id}@minotaur.miner",
        "GIT_COMMITTER_NAME": f"Minotaur Miner ({miner_id})",
        "GIT_COMMITTER_EMAIL": f"{miner_id}@minotaur.miner",
    }
    try:
        subprocess.run(["git", "add", "-A"], cwd=repo_dir, check=True, env=env, capture_output=True)

        # Check if there are changes to commit
        diff_result = subprocess.run(
            ["git", "diff", "--cached", "--quiet"],
            cwd=repo_dir, env=env, capture_output=True,
        )
        if diff_result.returncode == 0:
            logger.info("No changes in solver repo — strategies already up to date")
            # Still get the current commit for submission
        else:
            subprocess.run(
                ["git", "commit", "-m", f"Update strategies ({copied} apps) by {miner_id}"],
                cwd=repo_dir, check=True, env=env, capture_output=True,
            )
            logger.info("Committed %d strategy updates to solver repo", copied)

        # Push to a miner-specific branch (not main).
        # Main is reserved for the current champion; miners submit on
        # their own branches. The validator clones the branch for screening.
        branch_name = os.environ.get(
            "SOLVER_BRANCH",
            f"miner/{miner_id}",
        )
        # Create or switch to the miner branch
        subprocess.run(
            ["git", "checkout", "-B", branch_name],
            cwd=repo_dir, env=env, capture_output=True,
        )
        push_result = subprocess.run(
            ["git", "push", "origin", branch_name, "--force"],
            cwd=repo_dir, env=env, capture_output=True, text=True, timeout=30,
        )
        if push_result.returncode != 0:
            logger.warning("Git push failed: %s", push_result.stderr[:200])
            # Continue — local commit still has the hash for file:// fallback

        commit_hash = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_dir, check=True, env=env, capture_output=True, text=True,
        ).stdout.strip()
    except Exception as exc:
        logger.error("Git operations failed: %s", exc)
        return {"accepted": False, "error": f"Git error: {exc}"}

    # Determine repo URL for submission
    # Use SOLVER_REPO_URL if set, otherwise read from git remote
    repo_url = os.environ.get("SOLVER_REPO_URL", "").strip()
    if not repo_url:
        try:
            remote_result = subprocess.run(
                ["git", "remote", "get-url", "origin"],
                cwd=repo_dir, capture_output=True, text=True,
            )
            repo_url = remote_result.stdout.strip()
        except Exception:
            pass
    if not repo_url:
        repo_url = f"file://{repo_dir}"

    logger.info(
        "Solver repo updated: %s commit=%s (%d strategies)",
        repo_url, commit_hash[:12], copied,
    )

    # Get current round info
    round_id = ""
    epoch = 0
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{validator_url.rstrip('/')}/v1/solver/round",
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                round_info = await resp.json()
                round_id = round_info.get("round_id", "")
                epoch = round_info.get("opened_epoch", 0)
    except Exception:
        pass

    # PR-based submission (the fold): open/find the PR for this branch on the
    # canonical solver repo; the PR number + head SHA are the submission identity.
    upstream = os.environ.get("SOLVER_UPSTREAM_REPO", "subnet112/minotaur-solver").strip()
    head_owner = os.environ.get("MINER_GITHUB_USER", "").strip()
    gh_token = (os.environ.get("MINER_GITHUB_TOKEN") or os.environ.get("GITHUB_TOKEN") or "").strip()
    head_sha = commit_hash  # the pushed HEAD — the exact commit we sign + that gets built
    pr_number = None
    _env_pr = os.environ.get("MINER_PR_NUMBER", "").strip()
    if _env_pr.isdigit():
        pr_number = int(_env_pr)  # operator supplied a pre-opened PR
    elif gh_token and head_owner:
        pr_number = _open_or_get_pr(upstream, head_owner, branch_name, gh_token, head_sha=head_sha)
    if not pr_number:
        return {
            "accepted": False,
            "error": (
                "Could not resolve a PR for this submission. Set MINER_PR_NUMBER, or "
                "MINER_GITHUB_USER + MINER_GITHUB_TOKEN so the miner can open one."
            ),
        }

    # Sign the submission: {pr_number}:{head_sha}:{round_id}
    hotkey = miner_id
    signature = ""
    try:
        from bittensor_wallet import Keypair
        keypair = Keypair.create_from_mnemonic(Keypair.generate_mnemonic())
        message = f"{pr_number}:{head_sha}:{round_id}"
        signature = base64.b64encode(keypair.sign(message.encode())).decode("ascii")
        hotkey = keypair.ss58_address
    except ImportError:
        pass

    # Submit via the PR path
    try:
        url = f"{validator_url.rstrip('/')}/v1/submissions"
        payload = {
            "pr_number": pr_number,
            "head_sha": head_sha,
            "epoch": epoch,
            "round_id": round_id,
            "hotkey": hotkey,
            "signature": signature,
        }
        headers: dict[str, str] = {}
        api_key = os.environ.get("SUBMISSIONS_API_KEY", "").strip()
        if api_key:
            headers["x-submission-api-key"] = api_key

        async with aiohttp.ClientSession() as session:
            async with session.post(
                url, json=payload, headers=headers or None,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                result = await resp.json()
                if resp.status == 201:
                    logger.info(
                        "Solver submitted: %s (status=%s, repo=%s, commit=%s)",
                        result.get("submission_id"),
                        result.get("status"),
                        repo_url[:40],
                        commit_hash[:12],
                    )
                    return {**result, "repo_url": repo_url, "commit_hash": commit_hash}
                else:
                    logger.warning("Submission rejected: %s", result)
                    return {"accepted": False, "error": result}
    except Exception as exc:
        logger.warning("Failed to submit to validator: %s", exc)
        return {"accepted": False, "path": str(solver_path), "error": str(exc)}


class AgentLoop:
    """Main orchestrator for the agentic solver development loop.

    Args:
        validator_url: Base URL of the validator.
        strategy_dir: Directory to store strategy files.
        miner_id: Miner identifier for solver submission.
        loop_interval: Seconds between cycles.
        max_llm_calls_per_cycle: Max LLM calls per cycle to control costs.
        cooldown: Seconds after improving before re-targeting the same app.
    """

    def __init__(
        self,
        validator_url: str,
        strategy_dir: str = "strategies",
        miner_id: str = "miner-agent-001",
        loop_interval: float = 60.0,
        max_llm_calls_per_cycle: int = 3,
        cooldown: float = 120.0,
        anvil_rpc_url: str | None = None,
        model: str = "sonnet",
        claude_timeout: float = 300.0,
        # Legacy params (ignored)
        improvement_threshold: float = 0.7,
        stale_after: float = 600.0,
    ) -> None:
        self.validator_url = validator_url
        self.strategy_dir = Path(strategy_dir)
        self.miner_id = miner_id
        self.loop_interval = loop_interval
        self.max_llm_calls = max_llm_calls_per_cycle

        self.discovery = AppDiscovery(validator_url)
        self.score_tracker = ScoreTracker(
            min_executions=3,
            cooldown=cooldown,
        )
        self.generator = StrategyGenerator(
            strategy_dir=strategy_dir,
            validator_url=validator_url,
            anvil_rpc_url=anvil_rpc_url,
            timeout=claude_timeout,
            model=model,
        )
        self.tester = StrategyTester()
        self.router = RoutingSolver()
        self.router.initialize({"chain_ids": [1]})

        # CloudWatch metrics counters — shared with miner.metrics.publish_loop
        self.metrics = MinerCounters()

        # Cost-awareness gate: reads env for K / cooldown / token budget,
        # persists plateau + budget counters under strategy_dir/cost_gate_state.json.
        self.cost_gate = CostGate(
            miner_id=miner_id,
            state_dir=strategy_dir,
            plateau_k=int(os.environ.get("MINER_PLATEAU_K", "5")),
            plateau_min_delta=float(os.environ.get("MINER_PLATEAU_MIN_DELTA", "0.005")),
            plateau_cooldown_seconds=float(
                os.environ.get("MINER_PLATEAU_COOLDOWN_SECONDS", str(4 * 3600)),
            ),
            token_budget_per_day=int(
                os.environ.get("MINER_TOKEN_BUDGET_PER_DAY", "100000"),
            ),
            stagnation_window=int(os.environ.get("MINER_STAGNATION_WINDOW", "5")),
            stagnation_delta=float(os.environ.get("MINER_STAGNATION_DELTA", "0.01")),
            stagnation_cooldown_seconds=float(
                os.environ.get("MINER_STAGNATION_COOLDOWN_SECONDS", str(12 * 3600)),
            ),
        )

        self._running = False
        self._cycle_count = 0
        self._strategies_changed = False

        # Submission gate: only submit a new WIP if its pre-sim score is at
        # least the last submitted pre-sim score minus this margin. Keeps
        # the validator queue clean of regressions while still tolerating
        # tiny noise. Configurable via MINER_SUBMIT_MARGIN.
        self.submit_margin = float(os.environ.get("MINER_SUBMIT_MARGIN", "0.02"))
        # If the gate blocks N consecutive WIP attempts for an app, restore
        # the last-submitted snapshot so Claude doesn't iterate forever on a
        # dead-end hypothesis. 0 disables the escape hatch.
        self.wip_max_failures = int(os.environ.get("MINER_WIP_MAX_FAILURES", "5"))
        # Per-app submission state: {app_id: {submitted_score, submitted_at,
        # wip_failures}}. Loaded eagerly so cycle #1 can gate correctly.
        self._submission_state_path = self.strategy_dir / "submission_state.json"
        self._submission_state: dict[str, dict[str, Any]] = self._load_submission_state()
        # Apps whose new WIP this cycle cleared the gate. Populated during
        # the per-target loop, consumed by _bundle_solver_source to decide
        # which version of each app's strategy to ship.
        self._cycle_cleared_apps: set[str] = set()

    async def run(self) -> None:
        """Run the agent loop indefinitely."""
        self._running = True
        logger.info(
            "AgentLoop started (interval=%.0fs, cooldown=%.0fs, max_llm=%d)",
            self.loop_interval,
            self.score_tracker.cooldown,
            self.max_llm_calls,
        )

        # Ensure the solver repo is present before the first cycle. Without
        # it, submit_git_strategy bails every cycle with "Solver repo not
        # found" and successful strategies can never reach the validator.
        self._ensure_solver_repo()

        # Load existing strategies from disk
        self._load_existing_strategies()

        while self._running:
            try:
                await self._cycle()
            except Exception as exc:
                logger.error("Agent cycle error: %s", exc)

            self._cycle_count += 1
            await asyncio.sleep(self.loop_interval)

    def stop(self) -> None:
        """Signal the loop to stop."""
        self._running = False

    def _ensure_solver_repo(self) -> None:
        """Clone the miner's solver-repo fork if it isn't on disk yet.

        The miner writes strategies into this repo, commits to a per-miner
        branch, and submits the commit hash to the validator. If the repo
        isn't present, every submission silently fails with "Solver repo
        not found". Requires SOLVER_REPO_URL (the fork's HTTPS URL) and
        SOLVER_REPO_TOKEN (a fine-grained PAT with contents:write +
        pull-requests:write). Falls through quietly if anything's missing
        so that dev rigs without a configured fork can still run the
        agent loop for pre-submission testing.
        """
        import subprocess

        repo_dir = Path(os.environ.get(
            "SOLVER_REPO_PATH",
            os.path.expanduser("~/git/minotaur-solver"),
        ))
        if (repo_dir / ".git").exists():
            return  # Already cloned.

        repo_url = os.environ.get("SOLVER_REPO_URL", "").strip()
        token = os.environ.get("SOLVER_REPO_TOKEN", "").strip()
        if not repo_url:
            logger.warning(
                "SOLVER_REPO_URL not set — skipping clone. PR submissions "
                "will fail until a solver-repo fork is configured.",
            )
            return
        if not token:
            logger.warning(
                "SOLVER_REPO_TOKEN not set — skipping clone. The fork may "
                "require auth for private repos and for push access.",
            )
            # Still attempt unauthenticated clone; it may work for public
            # repos at read-only, which is enough for the sync_champion path.

        # Inject the PAT into the URL for auth.
        auth_url = repo_url
        if token and repo_url.startswith("https://"):
            # https://github.com/owner/repo.git →
            # https://x-access-token:TOKEN@github.com/owner/repo.git
            auth_url = repo_url.replace(
                "https://", f"https://x-access-token:{token}@", 1,
            )

        repo_dir.parent.mkdir(parents=True, exist_ok=True)
        logger.info(
            "Cloning solver-repo fork from %s into %s",
            repo_url, repo_dir,
        )
        try:
            # --depth 1 keeps clone fast; we rebase onto main as needed.
            proc = subprocess.run(
                ["git", "clone", "--depth", "1", auth_url, str(repo_dir)],
                capture_output=True, text=True, timeout=120,
            )
            if proc.returncode != 0:
                # Don't leak the token in logs — strip it from stderr.
                stderr_clean = proc.stderr.replace(token, "<TOKEN>") if token else proc.stderr
                logger.error(
                    "solver-repo clone failed (rc=%d): %s",
                    proc.returncode, stderr_clean[:300],
                )
                return
            # Persist the authenticated URL for subsequent push/pull
            # operations — `git clone` already does this, but we rewrite
            # it to ensure the token is present if it wasn't on clone.
            if token:
                subprocess.run(
                    ["git", "-C", str(repo_dir), "remote", "set-url",
                     "origin", auth_url],
                    capture_output=True,
                )
            # Set a sensible author identity so downstream commits don't
            # fail with "please tell me who you are".
            subprocess.run(
                ["git", "-C", str(repo_dir), "config", "user.name",
                 f"Minotaur Miner ({self.miner_id})"],
                capture_output=True,
            )
            subprocess.run(
                ["git", "-C", str(repo_dir), "config", "user.email",
                 f"{self.miner_id}@minotaur.miner"],
                capture_output=True,
            )
            logger.info("solver-repo cloned to %s", repo_dir)
        except subprocess.TimeoutExpired:
            logger.error("solver-repo clone timed out after 120s")
        except Exception as exc:
            logger.error("solver-repo clone raised: %s", exc)

    async def _sync_champion_code(self) -> None:
        """Pull the latest champion code from main in the solver repo.

        If a different miner became champion, their code is on main
        (merged by the relayer). Every miner should start from the
        champion's code — not keep iterating on their own inferior version.
        """
        repo_dir = Path(os.environ.get(
            "SOLVER_REPO_PATH",
            os.path.expanduser("~/git/minotaur-solver"),
        ))
        if not (repo_dir / ".git").exists():
            return

        try:
            import subprocess
            env = {
                **os.environ,
                "GIT_AUTHOR_NAME": f"Minotaur Miner ({self.miner_id})",
                "GIT_AUTHOR_EMAIL": f"{self.miner_id}@minotaur.miner",
                "GIT_COMMITTER_NAME": f"Minotaur Miner ({self.miner_id})",
                "GIT_COMMITTER_EMAIL": f"{self.miner_id}@minotaur.miner",
            }
            branch = os.environ.get("SOLVER_BRANCH", f"miner/{self.miner_id}")

            # Fetch latest from remote
            subprocess.run(
                ["git", "fetch", "origin"],
                cwd=repo_dir, env=env, capture_output=True, timeout=30,
            )

            # Check if main has new champion code ahead of our branch
            result = subprocess.run(
                ["git", "log", f"{branch}..origin/main", "--oneline"],
                cwd=repo_dir, env=env, capture_output=True, text=True, timeout=10,
            )
            new_commits = result.stdout.strip()
            if not new_commits:
                return  # Our branch is up to date with main

            logger.info(
                "Champion code updated on main — rebasing miner branch:\n%s",
                new_commits,
            )

            # Rebase our branch on top of main to get champion's strategies
            subprocess.run(
                ["git", "checkout", branch],
                cwd=repo_dir, env=env, capture_output=True, timeout=10,
            )
            rebase = subprocess.run(
                ["git", "rebase", "origin/main"],
                cwd=repo_dir, env=env, capture_output=True, text=True, timeout=30,
            )
            if rebase.returncode != 0:
                # Conflict — abort and reset to main (champion wins)
                subprocess.run(
                    ["git", "rebase", "--abort"],
                    cwd=repo_dir, env=env, capture_output=True,
                )
                subprocess.run(
                    ["git", "reset", "--hard", "origin/main"],
                    cwd=repo_dir, env=env, capture_output=True,
                )
                logger.info("Rebase conflict — reset to champion code on main")

            # Copy champion's strategies to our workspace
            strategies_dir = repo_dir / "strategies"
            for app_dir in strategies_dir.iterdir():
                if not app_dir.is_dir() or not app_dir.name.startswith("app_"):
                    continue
                strategy_file = app_dir / "strategy.py"
                if not strategy_file.exists():
                    continue
                local_dir = self.strategy_dir / app_dir.name
                local_dir.mkdir(parents=True, exist_ok=True)
                local_strategy = local_dir / "strategy.py"
                if not local_strategy.exists() or strategy_file.read_text() != local_strategy.read_text():
                    local_strategy.write_text(strategy_file.read_text())
                    logger.info("Updated local strategy from champion: %s", app_dir.name)

        except Exception as exc:
            logger.warning("Failed to sync champion code: %s", exc)

    def _current_progress_signal(self) -> float:
        """Counts-based progress signal for the cost gate (0..1).

        Post relative-cutover the validator's 0..1 ``best_score`` is a saturated
        validity sentinel, useless as a progress measure. The real signal is the
        best ``better/compared`` ratio across this miner's apps — the fraction of
        orders our latest submission beats the champion on. Rises as we improve,
        so the gate's plateau/stagnation deltas (on a 0..1 ratio) stay meaningful.
        Falls back to 0.0 when no relative counts are available yet (fresh miner
        or benched before the cutover) — which keeps the TOP_RANKED rule's
        ``my_best > 0`` guard from wrongly skipping a miner with no signal.
        """
        best = 0.0
        for stats in self.score_tracker._stats.values():
            rel = stats.get("relative")
            if rel and int(rel.get("compared", 0) or 0) > 0:
                ratio = int(rel.get("better", 0) or 0) / int(rel["compared"])
                if ratio > best:
                    best = ratio
        return best

    async def _evaluate_cost_gate(self, app_ids: list[str]):
        """Ask the cost gate whether this cycle should run the LLM.

        Feeds the gate current champion identity + rank info pulled from
        the validator. Returns a GateDecision. Failures here fail OPEN
        (let the cycle run) so a transient API outage doesn't silently
        stop mining.
        """
        from minotaur_subnet.miner.agent.cost_gate import GateDecision
        try:
            champion = await self.discovery.fetch_current_champion()
        except Exception as exc:
            logger.debug("cost-gate: champion fetch failed (%s); failing open", exc)
            return GateDecision(should_run=True)

        my_best = self._current_progress_signal()

        # "top rival" had no relative analog after the cutover: the champion has
        # no absolute score (champion_score is null/0), and the API serves no
        # per-rival relative ratio. So the score-comparison half of the rank
        # rules is inert (top_rival stays 0) and "a rival is ahead" is detected
        # purely via new_submissions_since_ours below — which still works.
        top_rival = 0.0
        for app_id in app_ids:
            stats = self.score_tracker._stats.get(app_id, {})
            champ_score = float(stats.get("champion_score") or 0.0)
            if champ_score > top_rival:
                top_rival = champ_score

        try:
            new_since = await self.discovery.fetch_submissions_since(
                after=self.cost_gate.state.last_submission_at,
                exclude_miner=self.miner_id,
            )
        except Exception as exc:
            logger.debug("cost-gate: submissions-since fetch failed (%s); failing open", exc)
            return GateDecision(should_run=True)

        return self.cost_gate.should_run_this_cycle(
            champion=champion,
            my_best_score=my_best,
            top_rival_score=top_rival,
            new_submissions_since_ours=int(new_since),
        )

    async def _cycle(self) -> None:
        """Run one agent cycle."""
        logger.info("Agent cycle #%d starting", self._cycle_count + 1)
        self._strategies_changed = False
        self._cycle_cleared_apps = set()

        # 0. Sync champion code from main (if a new champion was adopted)
        await self._sync_champion_code()

        # 1. Discover apps
        apps = await self.discovery.fetch_available_apps()
        if not apps:
            logger.debug("No apps available")
            return

        app_ids = [a["app_id"] for a in apps]
        logger.info("Discovered %d apps: %s", len(apps), app_ids)

        # 2. Fetch per-app scores
        for app in apps:
            app_id = app["app_id"]
            scores = await self.discovery.fetch_app_scores(app_id)
            if scores:
                self.score_tracker.update(app_id, scores)

        # 2b. Cost gate — may skip the expensive LLM work this cycle if
        # we're an unchallenged champion, top-ranked with no new rivals,
        # on a plateau, or out of daily token budget.
        gate_decision = await self._evaluate_cost_gate(app_ids)
        if not gate_decision.should_run:
            logger.info(
                "[cycle-skip miner=%s] %s: %s",
                self.miner_id, gate_decision.reason, gate_decision.detail,
            )
            self.metrics.record_skip(gate_decision.reason)
            # Still update plateau bookkeeping with our current progress signal
            # (counts-based: fraction of orders beating the champion).
            best = self._current_progress_signal()
            self.metrics.record_score(best)
            self.cost_gate.record_cycle(
                best_score=best,
                submitted=False,
            )
            return

        # 3. Identify underperformers
        targets = self.score_tracker.get_underperformers(app_ids)
        if not targets:
            logger.debug("All apps performing well")
            return

        logger.info(
            "Improvement targets: %s",
            [(t.app_id, t.reason, f"{t.avg_score:.2f}") for t in targets],
        )

        # 4. Generate/improve strategies (limited per cycle)
        llm_calls = 0
        for target in targets:
            if llm_calls >= self.max_llm_calls:
                break

            context = await self.discovery.fetch_app_details(target.app_id)
            if context is None:
                logger.warning("Could not fetch details for %s", target.app_id)
                continue

            try:
                if target.reason in ("improve", "coverage_gap"):
                    code = await self._improve_strategy(target.app_id, context)
                else:
                    code = await self._generate_strategy(context)
                llm_calls += 1
            except Exception as exc:
                logger.error(
                    "LLM generation failed for %s: %s", target.app_id, exc,
                )
                continue

            if code is None:
                continue

            # 5. Test locally (structural)
            passed, msg = self.tester.test_strategy_with_context(
                code, target.app_id, context,
            )
            if not passed:
                logger.warning(
                    "Strategy test failed for %s: %s", target.app_id, msg,
                )
                continue

            # 5b. Score against the validator's FULL fixture set —
            # every manifest scenario + historical replays. This is the
            # exact same set the validator's benchmark_worker grades
            # the submission against, so the gate signal can no longer
            # drift from the true verdict. Adds ~60-90s vs the 3-scenario
            # sample but eliminates false PASSes where Claude optimised
            # for manifest scenarios at the cost of historical replays.
            # MINER_PRESIM_FULL=0 reverts to the cheap sample for debug.
            if os.environ.get("MINER_PRESIM_FULL", "1").strip() != "0":
                score, score_msg, per_scenario = await self.tester.score_strategy_full(
                    code, target.app_id, self.validator_url,
                    app_context=context, include_historical=True,
                )
            else:
                score, score_msg, per_scenario = await self.tester.score_strategy_sampled(
                    code, target.app_id, self.validator_url, app_context=context,
                    sample_count=int(os.environ.get("MINER_PRESIM_SAMPLES", "3")),
                )
            logger.info(
                "Pre-submission score for %s: %s", target.app_id, score_msg,
            )
            # Detect transient infra failures (Anvil unavailable, network
            # blips). If EVERY sample was transient, skip the gate
            # entirely — the strategy probably works, we just can't tell.
            transient_count = sum(1 for r in per_scenario if r.get("transient"))
            real_samples = len(per_scenario) - transient_count
            all_transient = real_samples == 0 and transient_count > 0
            # Surface the worst real failure to Claude for next iteration
            # (transient samples are useless feedback). Falls back to the
            # full score_msg if no real samples ran.
            real_failures = [r for r in per_scenario if not r.get("transient")]
            if real_failures:
                worst = min(real_failures, key=lambda r: r["score"])
                feedback_msg = (
                    f"score={worst['score']:.4f} ({worst['scenario']}): "
                    f"{worst['reason']}"
                )
            else:
                feedback_msg = score_msg
            self.score_tracker.set_last_score_feedback(
                target.app_id, score, feedback_msg,
            )
            # Local-best ratchet: any pre-sim mean above the prior local
            # best gets snapshotted regardless of the submission gate.
            # Lets Claude roll back to its highest WIP later.
            #
            # NOTE: the pre-sim mean ``score`` is now a saturated validity
            # sentinel (the live JS scorer clamps to [0,1] as valid/invalid), so
            # it is NO LONGER fed to the cost gate's stagnation window — that
            # would false-trip on a constant ≈1.0. Stagnation is now counts-based
            # and recorded once per benchmarked submission in
            # _poll_benchmark_feedback. The ratchet still uses the sentinel only
            # as a "did this WIP produce a valid plan" snapshot heuristic.
            if not all_transient:
                self._maybe_record_local_best(target.app_id, score, code)

            # Always save WIP — keeps Claude's hypothesis on disk so the
            # next improve cycle iterates on the same attempt instead of
            # regenerating from the last submitted code. The router runs
            # WIP locally; this only affects miner-internal routing, not
            # what the validator sees (that's gated below).
            # RPC-pattern guard: Claude has repeatedly reverted the
            # env-var RPC lookup back to a hardcoded localhost URL,
            # despite the writer prompt explicitly forbidding it. The
            # hardcoded URL means the strategy's dynamic pool probing
            # always fails inside the validator's containers, so every
            # plan falls into the static fee-tier table — identical to
            # every other miner and incapable of meaningful improvement.
            #
            # Two failure modes seen:
            #   1. Plain ``_RPC_URL = "http://localhost:18545"`` constant
            #   2. Subverted helper: ``def _rpc_url_for(...): return _RPC_URL``
            #      where _RPC_URL is hardcoded — passes "function exists"
            #      check but is functionally identical to (1).
            #
            # The guard checks BOTH: hardcoded URL string at module
            # level AND structural presence of an os.environ.get call
            # for an *_RPC_URL env var. If hardcoded URL exists without
            # a real env-var lookup, reject the WIP.
            import re as _re_guard
            _has_hardcoded_localhost = bool(
                _re_guard.search(
                    r"_RPC_URL\s*=\s*['\"]https?://localhost", code,
                )
            )
            _has_env_lookup = bool(
                _re_guard.search(
                    r"os\.environ(?:\.get)?\s*[\(\[]\s*['\"][A-Z_]*RPC_URL", code,
                ) or _re_guard.search(
                    r"os\.getenv\s*\(\s*['\"][A-Z_]*RPC_URL", code,
                )
            )
            if _has_hardcoded_localhost and not _has_env_lookup:
                logger.warning(
                    "[rpc-guard] Rejecting WIP for %s: hardcoded _RPC_URL "
                    "to localhost present AND no os.environ.get(*RPC_URL) "
                    "lookup found. Strategy must resolve RPC URL from env "
                    "vars (ANVIL_RPC_URL / BASE_RPC_URL / "
                    "BITTENSOR_EVM_RPC_URL) at runtime — see writer prompt.",
                    target.app_id,
                )
                fails = self._record_wip_fail(target.app_id)
                if self.wip_max_failures > 0 and fails >= self.wip_max_failures:
                    if self._restore_submitted_strategy(target.app_id):
                        logger.warning(
                            "[rpc-guard] WIP escape hatch tripped for %s "
                            "(%d failures) — restored last-good snapshot",
                            target.app_id, fails,
                        )
                continue
            self._save_strategy(target.app_id, code)
            strategy = self._load_strategy_from_disk(target.app_id)
            if strategy:
                self.router.register_strategy(strategy)
                self.score_tracker.mark_has_strategy(target.app_id)

            # Submission gate: only ship if pre-sim score is at least
            # last-submitted minus a small noise margin. Below the bar,
            # the WIP stays on disk for the next iteration but is not
            # bundled. After ``wip_max_failures`` consecutive misses,
            # restore the submitted snapshot so Claude doesn't spiral on
            # a dead-end hypothesis.
            submitted_score = self._get_submitted_score(target.app_id)
            gate_floor = max(submitted_score - self.submit_margin, 0.0)
            if all_transient:
                # Infra is flapping — the strategy may be fine but we
                # can't tell. Skip the gate entirely so the WIP failure
                # counter doesn't tick on infrastructure noise.
                logger.warning(
                    "Submission gate SKIPPED for %s: all %d samples were "
                    "transient (Anvil/RPC unavailable). Not penalizing WIP; "
                    "will retry next cycle.",
                    target.app_id, transient_count,
                )
            elif score >= gate_floor and score > 0.0:
                self._record_wip_pass(target.app_id, score, code)
                self._cycle_cleared_apps.add(target.app_id)
                self._strategies_changed = True
                logger.info(
                    "Submission gate PASSED for %s: js_score=%.4f >= floor=%.4f "
                    "(submitted=%.4f, margin=%.3f)",
                    target.app_id, score, gate_floor,
                    submitted_score, self.submit_margin,
                )
            else:
                fails = self._record_wip_fail(target.app_id)
                logger.info(
                    "Submission gate BLOCKED for %s: js_score=%.4f < floor=%.4f "
                    "(submitted=%.4f, consecutive WIP fails: %d/%d) — keeping WIP "
                    "on disk for next iteration",
                    target.app_id, score, gate_floor,
                    submitted_score, fails, self.wip_max_failures,
                )
                if self.wip_max_failures > 0 and fails >= self.wip_max_failures:
                    if self._restore_submitted_strategy(target.app_id):
                        logger.warning(
                            "WIP escape hatch tripped for %s (%d consecutive "
                            "failures) — restored last-submitted snapshot",
                            target.app_id, fails,
                        )

        # 6. Bundle, submit, and wait for Docker benchmark feedback
        if self._strategies_changed:
            source = self._bundle_solver_source()
            result = await submit_solver_via_git(
                source, self.validator_url, self.miner_id,
                output_dir=self.strategy_dir,
            )
            # 7. Poll for benchmark results so feedback flows to next cycle
            sub_id = (result or {}).get("submission_id", "")
            if sub_id:
                self.metrics.record_submission()
                await self._poll_benchmark_feedback(
                    sub_id, submitted_apps=set(self._cycle_cleared_apps),
                )

        # 8. Record this cycle's result for cost-gate + metrics. The progress
        # signal is counts-based (better/compared) — see _current_progress_signal.
        best = self._current_progress_signal()
        self.metrics.record_score(best)
        self.cost_gate.record_cycle(
            best_score=best,
            submitted=self._strategies_changed,
        )

    async def _poll_benchmark_feedback(
        self, submission_id: str, submitted_apps: set[str] | None = None,
    ) -> None:
        """Poll the validator for Docker benchmark results.

        Waits up to 5 minutes for the submission to be scored, then
        stores the per-app scorecard as feedback for the next improvement
        cycle. This is what makes iteration work: the agent sees which
        app/scenario failed in the Docker benchmark and tells Claude to fix it.
        """
        import aiohttp

        url = f"{self.validator_url.rstrip('/')}/v1/submissions/{submission_id}/status"
        deadline = time.time() + 300  # 5 min max wait

        logger.info("Waiting for Docker benchmark results: %s", submission_id)

        while time.time() < deadline:
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                        if resp.status != 200:
                            await asyncio.sleep(5)
                            continue
                        data = await resp.json()
            except Exception:
                await asyncio.sleep(5)
                continue

            status = data.get("status", "")
            if status in ("scored", "adopted"):
                score = data.get("benchmark_score", 0)
                details = data.get("benchmark_details") or {}
                scorecard = details.get("scorecard", {})
                per_intent = details.get("per_intent", [])

                # Authoritative post-cutover signal: the per-submission RELATIVE
                # COUNTS vs the champion ({better, worse, matched, new, compared,
                # verdict}), served on the submission-status ``report.relative``.
                # The 0..1 benchmark_score / app_scores are now saturated validity
                # sentinels, so they only tell us valid vs failed — the counts tell
                # us whether we actually beat the champion.
                report = data.get("report") or {}
                relative = report.get("relative")
                scoring_mode = report.get("scoring_mode", "")
                reason_relative = report.get("reason_relative")

                # Build a feedback message. app_score is a 0/≈1 validity sentinel
                # now, so label it valid vs failed (not a quality grade).
                lines = [f"Docker benchmark score: {score:.4f}"]
                if relative:
                    lines.append(
                        "Relative vs champion: "
                        f"better={relative.get('better', 0)} "
                        f"worse={relative.get('worse', 0)} "
                        f"matched={relative.get('matched', 0)} "
                        f"new={relative.get('new', 0)} "
                        f"compared={relative.get('compared', 0)} "
                        f"verdict={relative.get('verdict', '?')}"
                    )
                    if reason_relative:
                        lines.append(f"  {reason_relative}")
                app_scores = scorecard.get("app_scores", {})
                for app_key, app_score in app_scores.items():
                    status_str = "VALID" if app_score > 0 else "INVALID"
                    lines.append(f"  {app_key}: {app_score:.3f} ({status_str})")

                # Find invalid (no-value) scenarios
                failed = [pi for pi in per_intent if pi.get("score", 0) == 0]
                if failed:
                    lines.append("Invalid scenarios:")
                    for pi in failed:
                        lines.append(f"  {pi.get('intent_id', '?')}: score=0 error={pi.get('error', 'none')}")

                feedback_msg = "\n".join(lines)
                logger.info("Docker benchmark feedback:\n%s", feedback_msg)

                # Store benchmark feedback for the next improvement cycle.
                # Iterate ALL apps in the scorecard — the original `break`
                # after the first one dropped per-app feedback for any
                # submission spanning multiple apps. Per-app messages are
                # the same global feedback today, but the bug would silently
                # bite once we ship a multi-app solver.
                seen_app_ids: set[str] = set()
                for app_key in app_scores:
                    app_id = app_key.split(":")[0] if ":" in app_key else app_key
                    if app_id in seen_app_ids:
                        continue
                    seen_app_ids.add(app_id)
                    self.score_tracker.set_last_score_feedback(
                        app_id, score, feedback_msg,
                    )
                    # Feed the authoritative relative counts to the tracker so
                    # priority/trend/feedback are counts-based, not score-based.
                    # The counts are submission-wide (champion-vs-challenger over
                    # all orders), so the same block is attached to each app.
                    if scoring_mode or relative is not None:
                        self.score_tracker.set_relative_counts(
                            app_id, relative, scoring_mode or "relative",
                        )

                # Stagnation tracking is counts-based now: record the
                # better/compared ratio (fraction of orders beating the champion)
                # once per benchmarked submission. A flat ratio across K
                # submissions ⇒ Claude is churning, not improving.
                if relative and int(relative.get("compared", 0) or 0) > 0:
                    _ratio = int(relative.get("better", 0) or 0) / int(relative["compared"])
                    self.cost_gate.record_pre_sim_score(_ratio)

                # Ratchet the gate floor on validator-confirmed score.
                # We use the validator's benchmark_score (not pre-sim)
                # so the floor only moves up on submissions that actually
                # passed Stage 1+2 + scoring. Use the submitted_apps set
                # (passed in from the caller — known at submit time) as
                # the source of truth for which apps to confirm. The
                # validator response's per-app scorecard may be empty
                # even on success, so seen_app_ids alone misses cases.
                confirm_apps = submitted_apps or seen_app_ids
                for app_id in confirm_apps:
                    self._confirm_submission(
                        app_id, validator_score=float(score), accepted=True,
                    )

                # CRITICAL: refresh validator stats synchronously now that
                # the Docker benchmark has landed. Without this, the next
                # cycle's score_tracker still reflects pre-submission state
                # — score_tracker.get_feedback would return stale avg_score
                # / champion_score / scenario_scores, and Claude's improve
                # prompt would read as if nothing happened. fetch_app_scores
                # populates all three (avg/recent/champion/scenarios) per app.
                for app_id in seen_app_ids:
                    try:
                        scores = await self.discovery.fetch_app_scores(app_id)
                        if scores:
                            self.score_tracker.update(app_id, scores)
                            logger.info(
                                "[score-tracker] refreshed %s after benchmark "
                                "(champion=%.3f, avg=%.3f)",
                                app_id,
                                scores.get("champion_score", 0.0),
                                scores.get("avg_score", 0.0),
                            )
                    except Exception as exc:
                        logger.debug(
                            "Post-benchmark stats refresh failed for %s: %s",
                            app_id, exc,
                        )
                return

            if status == "rejected":
                logger.warning(
                    "Submission %s rejected: %s",
                    submission_id, data.get("rejection_reason", "?"),
                )
                # Do not ratchet submitted_score; clear the pending fields.
                # Prefer the submitted_apps set (known at submit time) over
                # scanning state for stale pending entries.
                clear_apps = submitted_apps or {
                    aid for aid, st in self._submission_state.items()
                    if "pending_submitted_score" in (st or {})
                }
                for app_id in clear_apps:
                    self._confirm_submission(
                        app_id, validator_score=0.0, accepted=False,
                    )
                return

            await asyncio.sleep(10)

        logger.warning("Timed out waiting for benchmark results: %s", submission_id)

    async def _generate_strategy(self, context: AppContext) -> str | None:
        """Generate a new strategy via Claude CLI.

        Records token usage in the cost-gate so the daily budget gate
        actually enforces. Returns just the code for caller convenience.
        """
        result = await asyncio.to_thread(self.generator.generate, context)
        self._record_claude_usage(result, context.app_id, "generate")
        return result.strategy_code

    async def _improve_strategy(
        self, app_id: str, context: AppContext,
    ) -> str | None:
        """Improve an existing strategy via Claude CLI."""
        existing_code = self._read_strategy_code(app_id)
        if not existing_code:
            result = await asyncio.to_thread(self.generator.generate, context)
            self._record_claude_usage(result, app_id, "generate")
            return result.strategy_code

        feedback = self.score_tracker.get_feedback(app_id)
        result = await asyncio.to_thread(
            self.generator.improve, context, existing_code, feedback,
        )
        self._record_claude_usage(result, app_id, "improve")
        return result.strategy_code

    def _record_claude_usage(self, result, app_id: str, kind: str) -> None:
        """Push Claude's billed token count into the cost-gate.

        Without this call, the cost-gate's daily token budget gate is a
        no-op — the counter never increments and the gate never fires.
        Charges happen even for failed/timed-out runs because Anthropic
        bills for partial work.
        """
        tokens = int(getattr(result, "tokens_used", 0) or 0)
        cost = float(getattr(result, "cost_usd", 0.0) or 0.0)
        if tokens > 0:
            self.cost_gate.record_token_usage(tokens)
            logger.info(
                "[cost-gate] %s/%s billed: %d tokens / $%.4f (daily total: %d/%d)",
                app_id, kind, tokens, cost,
                self.cost_gate.state.token_budget_used,
                self.cost_gate.token_budget_per_day,
            )
            try:
                self.metrics.record_claude_cost_usd(cost)
            except (AttributeError, Exception):
                # metrics doesn't have to expose this method yet; fail soft.
                pass

    def _save_strategy(self, app_id: str, code: str) -> None:
        """Save strategy code to disk, keeping previous versions."""
        app_dir = self.strategy_dir / app_id
        app_dir.mkdir(parents=True, exist_ok=True)

        target = app_dir / "strategy.py"

        # Archive previous version
        if target.exists():
            version = 1
            while (app_dir / f"strategy_v{version}.py").exists():
                version += 1
            shutil.copy2(target, app_dir / f"strategy_v{version}.py")

        target.write_text(code)
        logger.info("Strategy saved: %s", target)

    def _read_strategy_code(self, app_id: str) -> str | None:
        """Read current strategy code from disk."""
        path = self.strategy_dir / app_id / "strategy.py"
        if path.exists():
            return path.read_text()
        return None

    # ── Submission gate state ──────────────────────────────────────────

    def _load_submission_state(self) -> dict[str, dict[str, Any]]:
        try:
            return json.loads(self._submission_state_path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _save_submission_state(self) -> None:
        self._submission_state_path.parent.mkdir(parents=True, exist_ok=True)
        self._submission_state_path.write_text(
            json.dumps(self._submission_state, indent=2),
        )

    def _get_submitted_score(self, app_id: str) -> float:
        """Pre-sim score of the last successful submission for ``app_id``.

        Returns 0.0 if we've never submitted this app — the genesis case
        where any non-reverting WIP clears the gate.
        """
        return float(self._submission_state.get(app_id, {}).get("submitted_score", 0.0))

    def _record_wip_pass(self, app_id: str, score: float, code: str) -> None:
        """A new WIP cleared the pre-sim gate. Snapshot it as the *pending*
        submission and reset the failure counter.

        Note: this does NOT update ``submitted_score`` — that's the
        validator-confirmed floor used by the gate. We only ratchet
        ``submitted_score`` after the validator's Docker benchmark
        actually scores the submission. Otherwise a rejected/build-
        failed submission would bump our gate floor based on pre-sim
        alone, blocking future legitimate submissions whose pre-sim
        comes in below the unconfirmed value.
        """
        app_dir = self.strategy_dir / app_id
        app_dir.mkdir(parents=True, exist_ok=True)
        (app_dir / "strategy_submitted.py").write_text(code)
        st = self._submission_state.setdefault(app_id, {
            "submitted_score": 0.0, "submitted_at": 0.0, "wip_failures": 0,
        })
        st["pending_submitted_score"] = float(score)
        st["pending_submitted_at"] = time.time()
        st["wip_failures"] = 0
        self._save_submission_state()

    def _confirm_submission(
        self, app_id: str, validator_score: float, accepted: bool,
    ) -> None:
        """Called from ``_poll_benchmark_feedback`` once the validator
        scored (or rejected) a submission.

        On accepted: ratchet ``submitted_score`` up to the validator's
        actual score (NOT the pre-sim score). The gate floor will use
        this value going forward.

        On rejected: clear the pending fields. The validator's verdict
        on this submission is "this is not us" — we don't change the
        baseline.
        """
        st = self._submission_state.setdefault(app_id, {
            "submitted_score": 0.0, "submitted_at": 0.0, "wip_failures": 0,
        })
        if accepted:
            st["submitted_score"] = float(validator_score)
            st["submitted_at"] = time.time()
            logger.info(
                "[gate] submitted_score ratcheted: %s = %.4f (validator-confirmed)",
                app_id, validator_score,
            )
        else:
            logger.info(
                "[gate] submission rejected; submitted_score unchanged at %.4f",
                float(st.get("submitted_score", 0.0)),
            )
        st.pop("pending_submitted_score", None)
        st.pop("pending_submitted_at", None)
        self._save_submission_state()

    def _record_wip_fail(self, app_id: str) -> int:
        """A WIP failed the submission gate. Increment the failure counter
        and return the new count. WIP file stays on disk so Claude can
        iterate on its own attempt next cycle.
        """
        st = self._submission_state.setdefault(app_id, {
            "submitted_score": 0.0,
            "submitted_at": 0.0,
            "wip_failures": 0,
        })
        st["wip_failures"] = int(st.get("wip_failures", 0)) + 1
        self._save_submission_state()
        return int(st["wip_failures"])

    def _restore_submitted_strategy(self, app_id: str) -> bool:
        """Restore the best snapshot we have when WIP regresses.

        Preference order (highest known score first):
          1. ``strategy_local_best.py`` — best WIP we've ever seen, even
             if it never made it through submission. Higher mean than
             whatever we last submitted in many cases (because the gate
             only sees the floor, not the ceiling).
          2. ``strategy_submitted.py`` — last gate-cleared snapshot.
        """
        app_dir = self.strategy_dir / app_id
        local_best = app_dir / "strategy_local_best.py"
        submitted = app_dir / "strategy_submitted.py"
        target = app_dir / "strategy.py"

        chosen: Path | None = None
        if local_best.exists():
            chosen = local_best
        elif submitted.exists():
            chosen = submitted
        if chosen is None:
            return False
        shutil.copy2(chosen, target)
        logger.info("Restored strategy.py from %s", chosen.name)
        st = self._submission_state.setdefault(app_id, {})
        st["wip_failures"] = 0
        self._save_submission_state()
        strategy = self._load_strategy_from_disk(app_id)
        if strategy:
            self.router.register_strategy(strategy)
        return True

    # ── Local-best ratchet ─────────────────────────────────────────────

    def _get_local_best_score(self, app_id: str) -> float:
        """Best pre-sim mean score we've ever recorded for this app's WIP."""
        return float(self._submission_state.get(app_id, {}).get("local_best_score", 0.0))

    def _maybe_record_local_best(self, app_id: str, score: float, code: str) -> bool:
        """Snapshot WIP as ``strategy_local_best.py`` if score beats prior.

        Independent of submission gate — even regressions vs submitted
        get the local-best ratchet so Claude can return to its highest
        ground when an aggressive rewrite breaks things.
        """
        prev = self._get_local_best_score(app_id)
        if score <= prev:
            return False
        app_dir = self.strategy_dir / app_id
        app_dir.mkdir(parents=True, exist_ok=True)
        (app_dir / "strategy_local_best.py").write_text(code)
        st = self._submission_state.setdefault(app_id, {
            "submitted_score": 0.0, "submitted_at": 0.0, "wip_failures": 0,
        })
        st["local_best_score"] = float(score)
        st["local_best_at"] = time.time()
        self._save_submission_state()
        logger.info(
            "[local-best] %s: %.4f -> %.4f (snapshotted strategy_local_best.py)",
            app_id, prev, score,
        )
        return True

    # ── Champion strategy fetch ─────────────────────────────────────────

    def _read_champion_strategy(self, app_id: str) -> str | None:
        """Read the current champion's strategy.py from solver_repo/main.

        The champion lives on the ``main`` branch of the solver repo; we
        cloned/tracked it during ``_ensure_solver_repo`` and re-sync at
        the top of each cycle via ``_sync_champion_code``. So the file
        we want is just ``<repo>/strategies/<app_id>/strategy.py``
        but read FROM the main branch ref, not the worktree (the
        worktree may be on a miner branch with our WIP).
        """
        repo_dir = Path(os.environ.get(
            "SOLVER_REPO_PATH", os.path.expanduser("~/git/minotaur-solver"),
        ))
        if not (repo_dir / ".git").exists():
            return None
        try:
            import subprocess
            result = subprocess.run(
                ["git", "show", f"main:strategies/{app_id}/strategy.py"],
                cwd=repo_dir, capture_output=True, text=True, timeout=10,
            )
            if result.returncode != 0:
                return None
            return result.stdout
        except Exception:
            return None

    def _load_strategy_from_disk(self, app_id: str) -> Strategy | None:
        """Load a strategy from the on-disk file."""
        path = self.strategy_dir / app_id / "strategy.py"
        if not path.exists():
            return None
        try:
            return load_strategy(str(path))
        except Exception as exc:
            logger.error("Failed to load strategy %s: %s", app_id, exc)
            return None

    def _load_existing_strategies(self) -> None:
        """Load all strategies from the strategy directory on startup."""
        if not self.strategy_dir.exists():
            return

        for app_dir in self.strategy_dir.iterdir():
            if not app_dir.is_dir():
                continue
            strategy_file = app_dir / "strategy.py"
            if not strategy_file.exists():
                continue
            try:
                strategy = load_strategy(str(strategy_file))
                self.router.register_strategy(strategy)
                self.score_tracker.mark_has_strategy(strategy.APP_ID)
                logger.info("Loaded existing strategy: %s", strategy.APP_ID)
            except Exception as exc:
                logger.warning(
                    "Failed to load strategy from %s: %s", strategy_file, exc,
                )
            # Seed the submitted-snapshot from the on-disk strategy if we
            # don't have one yet. This makes existing miners' first restart
            # treat the current strategy.py as their baseline so the
            # submission gate uses a real floor, not 0.0.
            snapshot = app_dir / "strategy_submitted.py"
            if not snapshot.exists():
                snapshot.write_text(strategy_file.read_text())
                logger.info(
                    "Seeded submitted snapshot for %s from existing strategy.py",
                    app_dir.name,
                )

    @staticmethod
    def _strip_strategy_for_bundle(code: str) -> str:
        """Strip lines from a strategy file that break when concatenated.

        Removes:
        - ``from __future__ import ...`` (must be at file top; header has it)
        - ``STRATEGY_CLASS = ...`` (registration done separately)
        - Duplicate module-level imports already in the header
        """
        skip_prefixes = (
            "from __future__ import",
            "STRATEGY_CLASS",
        )
        lines = []
        for line in code.splitlines():
            stripped = line.strip()
            if any(stripped.startswith(p) for p in skip_prefixes):
                continue
            lines.append(line)
        return "\n".join(lines)

    def _bundle_solver_source(self) -> str:
        """Bundle the RoutingSolver + all strategies into a single source string.

        Concatenates all strategy files with unique class names, then appends
        the RoutingSolver template that registers them all.

        Per-app version selection:
          - If the WIP cleared the gate this cycle: use ``strategy.py``.
          - Else if a submitted snapshot exists: use ``strategy_submitted.py``
            so the bundle ships the last known-good code, not a regressed WIP.
          - Else fall back to ``strategy.py`` (genesis case — first submit).
        """
        strategy_sections: list[str] = []
        register_lines: list[str] = []

        for app_id, strategy in self.router._strategies.items():
            code = None
            if app_id in self._cycle_cleared_apps:
                code = self._read_strategy_code(app_id)
            else:
                snapshot = self.strategy_dir / app_id / "strategy_submitted.py"
                if snapshot.exists():
                    code = snapshot.read_text()
                # No snapshot AND not cleared this cycle = no known-good
                # version to ship. Skip this app rather than shipping an
                # ungated WIP. Edge case: only matters before the first
                # successful submission for this app.
            if not code:
                continue

            # Strip problematic lines before bundling
            clean_code = self._strip_strategy_for_bundle(code)

            strategy_sections.append(
                f"# === Strategy for {app_id} ===\n{clean_code}\n"
            )

            # Find the STRATEGY_CLASS line to get the class name
            for line in code.splitlines():
                if line.strip().startswith("STRATEGY_CLASS"):
                    parts = line.split("=", 1)
                    if len(parts) == 2:
                        class_name = parts[1].strip()
                        register_lines.append(
                            f"_router.register_strategy({class_name}())"
                        )
                    break

        # Build the complete solver source
        header = '''\
"""Auto-bundled RoutingSolver with per-app strategies.

Generated by the AgentLoop. Do not edit manually.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from minotaur_subnet.shared.types import (
    AppIntentDefinition,
    ExecutionPlan,
    Interaction,
    IntentState,
)
from minotaur_subnet.sdk.intent_solver import IntentSolver, MarketSnapshot, SolverMetadata
from minotaur_subnet.sdk.strategy import Strategy
from minotaur_subnet.chains import registry

logger = logging.getLogger(__name__)
'''

        # Include strategy sections (each has their own imports, but that's fine)
        strategies_block = "\n\n".join(strategy_sections)

        # RoutingSolver + registration
        router_block = f'''

# === RoutingSolver (dispatcher) ===

_WETH = "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"
_DEPOSIT_SELECTOR = "0xd0e30db0"


def _intent_function_from_state(state):
    typed = getattr(state, "typed_context", None)
    return (
        getattr(typed, "intent_function", "")
        or state.control_view().get("_intent_function", "")
        or state.raw_params_view().get("intent_function", "")
    )


class BundledRoutingSolver(IntentSolver):
    def __init__(self):
        self._strategies = {{}}
        self._chain_ids = []

    def register_strategy(self, strategy):
        if strategy.APP_ID:
            self._strategies[strategy.APP_ID] = strategy

    def initialize(self, config):
        self._chain_ids = config.get("chain_ids", [1])
        # Set legacy env vars for old-style strategies that read
        # os.environ directly. New strategies should use the
        # Strategy.rpc_for(chain_id) accessor.
        import os as _os
        raw_urls = config.get("rpc_urls", {{}}) or {{}}
        if isinstance(raw_urls, dict):
            for chain_str, url in raw_urls.items():
                if not url:
                    continue
                try:
                    cid = int(chain_str)
                except (ValueError, TypeError):
                    continue
                _spec = registry.spec(cid)
                if _spec is not None and _spec.boot_rpc_envs:
                    _os.environ.setdefault(_spec.boot_rpc_envs[0], url)
        # Propagate the full config to every registered strategy so
        # Strategy.rpc_for(chain_id) works inside generate_plan. This is
        # the canonical contract — strategies should NEVER hardcode RPC
        # URLs, the validator's URL is the source of truth.
        for _strat in self._strategies.values():
            _init = getattr(_strat, "initialize", None)
            if callable(_init):
                try:
                    _init(config)
                except Exception as _exc:
                    logger.warning(
                        "Strategy %s.initialize raised: %s",
                        getattr(_strat, "APP_ID", "?"), _exc,
                    )

    def generate_plan(self, intent, state, snapshot):
        intent_function = _intent_function_from_state(state)
        strategy = self._strategies.get(intent.app_id)
        if strategy and strategy.accepts(intent.app_id, intent_function):
            try:
                return strategy.generate_plan(intent, state, snapshot)
            except Exception as exc:
                logger.error("Strategy for %s failed: %s", intent.app_id, exc)
        chain_id = state.chain_id or snapshot.chain_id or 1
        deadline = max(snapshot.timestamp + 300, int(time.time()) + 300)
        return ExecutionPlan(
            intent_id=intent.app_id,
            interactions=[
                Interaction(target=_WETH, value="1000000000000000",
                            call_data=_DEPOSIT_SELECTOR, chain_id=chain_id),
            ],
            deadline=deadline, nonce=state.nonce,
            metadata={{"plan_type": "fallback"}},
        )

    def check_trigger(self, intent, state, snapshot):
        strategy = self._strategies.get(intent.app_id)
        if strategy:
            try:
                return strategy.check_trigger(intent, state, snapshot)
            except Exception:
                pass
        return False

    def metadata(self):
        n = len(self._strategies)
        return SolverMetadata(
            name="routing-solver",
            version="1.0.0",
            author="minotaur-agent",
            description=f"Routes to {{n}} app strategies",
            supported_chains=self._chain_ids or [1],
            supported_intent_types=["swap", "vault", "limit_order"],
        )


# Register all strategies
_router = BundledRoutingSolver()
{chr(10).join(register_lines)}

class ConfiguredSolver(BundledRoutingSolver):
    \"\"\"Pre-configured solver with all strategies registered.\"\"\"
    def __init__(self):
        super().__init__()
        for s in _router._strategies.values():
            self.register_strategy(s)

SOLVER_CLASS = ConfiguredSolver
'''

        return header + "\n" + strategies_block + "\n" + router_block

    def status(self) -> dict[str, Any]:
        """Return current agent status."""
        return {
            "running": self._running,
            "cycle_count": self._cycle_count,
            "strategy_count": len(self.router._strategies),
            "strategies": list(self.router._strategies.keys()),
        }
