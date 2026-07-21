"""
App Intents Miner — agentic solver development.

Subcommands:

    # Run the agent loop (discovers apps, generates strategies via Claude CLI)
    python -m minotaur_subnet.miner.main agent \\
        --validator-url http://localhost:8080 \\
        --anvil-rpc-url http://localhost:8545

    # Submit a solver PR (open a PR on subnet112/minotaur-solver first)
    python -m minotaur_subnet.miner.main submit \\
        --pr-number 123 \\
        --head-sha <40-char-sha> --hotkey my-wallet

    # Check submission status (rendered per-order verdicts; --json for raw)
    python -m minotaur_subnet.miner.main status --submission-id sub_xxx

    # Dry-run a plan without submitting (gated, signed with your hotkey)
    python -m minotaur_subnet.miner.main dry-run \\
        --app-id app_123 --plan plan.json --params params.json \\
        --wallet-name my-wallet --hotkey-name my-hotkey
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

import aiohttp

# Ensure repo root is importable
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

logger = logging.getLogger("minotaur_subnet.miner")


# ═══════════════════════════════════════════════════════════════════════════════
#                           SUBMIT (git-based)
# ═══════════════════════════════════════════════════════════════════════════════


async def submit_solver_git(
    pr_number: int,
    head_sha: str,
    hotkey: str,
    wallet_path: str | None = None,
    validator_url: str = "http://localhost:9100",
    round_id: str | None = None,
    epoch: int | None = None,
    private_repo: str | None = None,
    repo_token: str | None = None,
) -> dict[str, Any]:
    """Submit a solver PR to the v1 submissions API (the PR-based fold).

    Discovers the active round via GET {validator_url}/v1/solver/round and
    signs "{pr_number}:{head_sha}:{round_id}". The leader resolves the PR number
    to the fork clone_url + head SHA and rejects if the live head != head_sha.

    Returns the API response dict (submission_id, status, status_url, epoch).
    """
    base = validator_url.rstrip("/")

    resolved_round_id = round_id
    resolved_epoch = epoch

    # Discover the active round. Round-based submission is required — the
    # /v1/status epoch fallback was removed (no legacy clients to support).
    async with aiohttp.ClientSession() as session:
        async with session.get(
            f"{base}/v1/solver/round",
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status != 200:
                raise RuntimeError(
                    f"Cannot submit: solver round unavailable at "
                    f"{base}/v1/solver/round (HTTP {resp.status})"
                )
            round_data = await resp.json()

    discovered_round_id = round_data.get("round_id")
    if not discovered_round_id:
        raise RuntimeError(
            f"Cannot submit: no open solver round at {base} "
            f"(status={round_data.get('status')})"
        )
    if not round_data.get("accepting_submissions", False):
        raise RuntimeError(
            "Solver submissions are currently closed "
            f"(round={discovered_round_id}, status={round_data.get('status')})"
        )
    resolved_round_id = resolved_round_id or discovered_round_id
    if resolved_epoch is None:
        resolved_epoch = int(round_data.get("opened_epoch", 0))
    logger.info(
        "Submitting to solver round %s (epoch=%d)",
        resolved_round_id,
        resolved_epoch,
    )

    # Sign the message with bittensor wallet
    import base64
    from bittensor_wallet import Wallet as BtWallet

    wallet_dir = (
        wallet_path
        or os.environ.get("BT_WALLET_PATH")
        or os.path.join(os.path.expanduser("~"), ".bittensor", "wallets")
    )
    wallet = BtWallet(name=hotkey, path=wallet_dir)
    keypair = wallet.get_hotkey()

    message = _build_submission_message(
        pr_number,
        head_sha,
        round_id=resolved_round_id,
    )
    signature_bytes = keypair.sign(message.encode("utf-8"))
    signature_b64 = base64.b64encode(signature_bytes).decode("ascii")

    logger.info(
        "Signed submission for round=%s epoch=%d (hotkey=%s)",
        resolved_round_id,
        resolved_epoch,
        hotkey,
    )

    # POST to v1/submissions
    payload = {
        "pr_number": pr_number,
        "head_sha": head_sha,
        "round_id": resolved_round_id,
        "epoch": resolved_epoch,
        "hotkey": keypair.ss58_address,
        "signature": signature_b64,
    }
    # Private-repo path: the PR lives in the miner's own private repo and the
    # validator clones it + comments with this per-submission token. Sent over
    # HTTPS only (the token is transport, not part of the signed message above).
    if private_repo and repo_token:
        payload["private_repo"] = private_repo
        payload["repo_token"] = repo_token
    headers: dict[str, str] = {}
    submissions_api_key = os.environ.get("SUBMISSIONS_API_KEY", "").strip()
    if submissions_api_key:
        headers["x-submission-api-key"] = submissions_api_key

    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{base}/v1/submissions",
            json=payload,
            headers=headers or None,
            timeout=aiohttp.ClientTimeout(total=60),
        ) as resp:
            result = await resp.json()
            if resp.status == 201:
                logger.info(
                    "Submission accepted: %s (status=%s)",
                    result.get("submission_id"),
                    result.get("status"),
                )
            else:
                logger.error(
                    "Submission failed (HTTP %d): %s",
                    resp.status,
                    result.get("detail", result),
                )
            return result


async def get_solver_round(
    validator_url: str = "http://localhost:9100",
) -> dict[str, Any]:
    """Fetch the current solver submission round from the API server."""
    base = validator_url.rstrip("/")
    async with aiohttp.ClientSession() as session:
        async with session.get(
            f"{base}/v1/solver/round",
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status != 200:
                raise RuntimeError(
                    f"Failed to fetch solver round from {base}/v1/solver/round "
                    f"(HTTP {resp.status})"
                )
            return await resp.json()


def _build_submission_message(
    pr_number: int,
    head_sha: str,
    *,
    round_id: str,
) -> str:
    """Build the signed submission payload: {pr_number}:{head_sha}:{round_id}."""
    if not round_id:
        raise ValueError("round_id is required")
    return f"{pr_number}:{head_sha}:{round_id}"


async def poll_submission_status(
    submission_id: str,
    validator_url: str = "http://localhost:9100",
    interval: float = 5.0,
    timeout: float = 600.0,
) -> dict[str, Any]:
    """Poll a submission's status until it reaches a terminal state.

    Terminal states: scored, adopted, rejected.
    Returns the final status response dict.
    """
    base = validator_url.rstrip("/")
    url = f"{base}/v1/submissions/{submission_id}/status"
    terminal_states = {"scored", "adopted", "rejected"}
    start = time.time()

    async with aiohttp.ClientSession() as session:
        while True:
            elapsed = time.time() - start
            if elapsed > timeout:
                logger.warning("Polling timed out after %.0fs", timeout)
                return {"submission_id": submission_id, "status": "timeout"}

            async with session.get(
                url,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    logger.warning("Status poll returned HTTP %d", resp.status)
                    await asyncio.sleep(interval)
                    continue

                data = await resp.json()
                status = data.get("status", "")
                logger.info(
                    "Submission %s: %s (%.0fs elapsed)",
                    submission_id, status, elapsed,
                )

                if status in terminal_states:
                    return data

            await asyncio.sleep(interval)


# ═══════════════════════════════════════════════════════════════════════════════
#                            DRY-RUN (score a plan without submitting)
# ═══════════════════════════════════════════════════════════════════════════════


async def run_dry_run(
    *,
    validator_url: str,
    plan: dict[str, Any],
    wallet_name: str,
    hotkey_name: str | None,
    wallet_path: str | None,
    app_id: str | None = None,
    order_id: str | None = None,
    params: dict[str, Any] | None = None,
    chain_id: int = 0,
    intent_function: str = "execute",
    fork_block: int | None = None,
) -> dict[str, Any]:
    """Score a single execution plan against the validator without submitting.

    Two gated endpoints, chosen by which id is given:

    * ``--app-id`` → ``POST /v1/apps/{app_id}/score`` — the validator's REAL
      fork simulation (on-chain score, gas, transfers, decoded revert reason).
      Needs ``params`` (the order params → ``state.raw_params``).
    * ``--order-id`` → ``POST /v1/orders/{order_id}/dry-run`` — fast MOCK
      simulation, JS score only. Params come from the stored order; the body
      is just the plan's interactions/deadline/nonce/metadata.

    Both are authenticated with ``X-Bittensor-*`` headers signed by the
    miner's registered hotkey (see ``minotaur_subnet.miner.signing``).
    """
    from minotaur_subnet.miner.signing import signed_headers

    base = validator_url.rstrip("/")
    if app_id:
        path = f"/v1/apps/{app_id}/score"
        body: dict[str, Any] = {
            "plan": plan,
            "params": params or {},
            "chain_id": chain_id,
            "intent_function": intent_function,
        }
        if fork_block is not None:
            body["fork_block"] = fork_block
    else:
        path = f"/v1/orders/{order_id}/dry-run"
        # /dry-run takes the plan's fields directly (DryRunRequest).
        body = {
            "interactions": plan.get("interactions", []),
            "deadline": plan.get("deadline", 0),
            "nonce": plan.get("nonce", 0),
            "metadata": plan.get("metadata", {}) or {},
        }

    # required=True: a miner who invoked `dry-run` wants a signed call — fail
    # loudly if the wallet can't be loaded rather than silently 401.
    headers = signed_headers(
        "POST", path,
        wallet_name=wallet_name, hotkey_name=hotkey_name, wallet_path=wallet_path,
        required=True,
    )
    headers["Content-Type"] = "application/json"

    url = base + path
    logger.info("Dry-run: POST %s (hotkey=%s)", url, headers["X-Bittensor-Hotkey"][:16])
    async with aiohttp.ClientSession() as session:
        async with session.post(
            url, json=body, headers=headers,
            timeout=aiohttp.ClientTimeout(total=60),
        ) as resp:
            text = await resp.text()
            if resp.status != 200:
                logger.error("Dry-run failed (HTTP %d): %s", resp.status, text[:400])
                return {"error": text, "status_code": resp.status}
            return json.loads(text)


# ═══════════════════════════════════════════════════════════════════════════════
#                            STATUS RENDERING (per-order blind spots)
# ═══════════════════════════════════════════════════════════════════════════════


def render_status(data: dict[str, Any]) -> str:
    """Human-readable submission status incl. the per-order blind-spot table.

    Surfaces ``report.relative.per_order`` — the row-per-order verdicts
    (``win`` / ``regression`` / ``matched`` / ``blind_spot_cover`` /
    ``dropped``) that tell a miner exactly where the champion is weak
    (``blind_spot_cover``) and where they must not regress (``dropped`` is a
    hard veto). Falls back to a compact summary when no report is attached.
    """
    lines: list[str] = []
    sid = data.get("submission_id", "?")
    status = data.get("status", "?")
    lines.append(f"Submission {sid}: {status}")

    report = data.get("report") or {}
    outcome = report.get("outcome")
    if outcome:
        lines.append(f"Outcome: {outcome}")
    if data.get("benchmark_rank") is not None:
        lines.append(f"Benchmark rank: {data['benchmark_rank']}")
    if data.get("rejection_reason"):
        lines.append(f"Rejection: {data['rejection_reason']}")

    relative = report.get("relative") or {}
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
        if report.get("reason_relative"):
            lines.append(f"  {report['reason_relative']}")

    per_order = relative.get("per_order") or []
    if per_order:
        # Lead with the actionable rows: blind spots to keep, regressions to
        # fix, drops (hard vetoes) first.
        priority = {
            "dropped": 0, "regression": 1, "blind_spot_cover": 2,
            "win": 3, "matched": 4, "skip": 5,
        }
        rows = sorted(per_order, key=lambda r: priority.get(r.get("verdict", ""), 9))
        lines.append("")
        lines.append(f"Per-order verdicts ({len(rows)}):")
        lines.append(f"  {'verdict':<18} {'intent_id':<24} {'champ':>14} {'chal':>14}  ratio")
        for r in rows:
            iid = str(r.get("intent_id", "?"))
            iid_disp = iid if len(iid) <= 24 else iid[:21] + "…"
            ratio = r.get("ratio")
            ratio_s = f"{ratio:.4f}" if isinstance(ratio, (int, float)) else "—"
            flag = " ⚠ HARD-LOSS" if r.get("catastrophic") else ""
            lines.append(
                f"  {str(r.get('verdict', '?')):<18} {iid_disp:<24} "
                f"{str(r.get('champ', '—')):>14} {str(r.get('chal', '—')):>14}  {ratio_s}{flag}"
            )
        lines.append("")
        lines.append(
            "Legend: blind_spot_cover = champion delivered nothing, you did "
            "(pure win) · regression = you delivered less (stay within the 1% "
            "floor) · dropped = you produced nothing the champion serves (HARD "
            "VETO — fix first)."
        )
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════════════════
#                            ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════════════


def main() -> None:
    parser = argparse.ArgumentParser(
        description="App Intents Miner — agentic solver development",
    )
    subparsers = parser.add_subparsers(dest="command", help="Subcommand")

    # agent (default workflow)
    agent_parser = subparsers.add_parser(
        "agent", help="Run agentic solver development loop (LLM-driven)",
    )
    agent_parser.add_argument(
        "--validator-url", default="http://localhost:8080",
        help="API server URL (for app discovery and scoring)",
    )
    agent_parser.add_argument(
        "--strategy-dir", default="strategies",
        help="Directory to store strategy files",
    )
    agent_parser.add_argument(
        "--miner-id", default="miner-agent-001",
        help="Unique miner identifier",
    )
    agent_parser.add_argument(
        "--loop-interval", type=float, default=60.0,
        help="Seconds between agent cycles",
    )
    agent_parser.add_argument(
        "--improvement-threshold", type=float, default=0.7,
        help="Score below which improvement is triggered",
    )
    agent_parser.add_argument(
        "--max-llm-calls", type=int, default=3,
        help="Max LLM calls per cycle",
    )
    agent_parser.add_argument(
        "--stale-after", type=float, default=600.0,
        help="Seconds before re-evaluating a strategy even if scores are fine",
    )
    agent_parser.add_argument(
        "--model", default="sonnet",
        help="Claude model for strategy generation (sonnet, haiku, opus)",
    )
    agent_parser.add_argument(
        "--claude-timeout", type=float, default=300.0,
        help="Timeout in seconds per Claude invocation",
    )
    agent_parser.add_argument(
        "--anvil-rpc-url", default=None,
        help="Anvil RPC URL for on-chain queries (optional)",
    )

    # submit (PR-based)
    submit_parser = subparsers.add_parser("submit", help="Submit a solver PR")
    submit_parser.add_argument(
        "--pr-number", required=True, type=int,
        help="PR number on the canonical solver repo (subnet112/minotaur-solver)",
    )
    submit_parser.add_argument(
        "--head-sha", required=True, help="Full 40-char PR head commit SHA",
    )
    submit_parser.add_argument(
        "--hotkey", required=True, help="Bittensor wallet name (hotkey)",
    )
    submit_parser.add_argument(
        "--wallet-path", default=None, help="Path to bittensor wallets directory",
    )
    submit_parser.add_argument(
        "--validator-url", default="http://localhost:9100",
        help="Validator base URL",
    )
    submit_parser.add_argument(
        "--round-id", default=None,
        help="Solver round ID (auto-detected when omitted)",
    )
    submit_parser.add_argument(
        "--epoch", type=int, default=None,
        help="Epoch number (auto-detected if omitted)",
    )
    submit_parser.add_argument(
        "--poll", action="store_true",
        help="Poll submission status until terminal state",
    )
    submit_parser.add_argument(
        "--private-repo", default=None,
        help=(
            "owner/repo of your PRIVATE solver repo (opt-in private submission). "
            "pr-number/head-sha then refer to a PR in THIS repo, not the canonical "
            "solver repo. Requires a fine-grained PAT (see --repo-token)."
        ),
    )
    submit_parser.add_argument(
        "--repo-token", default=None,
        help=(
            "Fine-grained GitHub PAT for --private-repo, valid for this submission "
            "only (Metadata:Read, Contents:Read, Pull requests:Read+Write). Prefer "
            "the MINER_REPO_TOKEN env var to keep it out of shell history."
        ),
    )

    # status
    status_parser = subparsers.add_parser("status", help="Check submission status")
    status_parser.add_argument(
        "--submission-id", required=True, help="Submission ID to check",
    )
    status_parser.add_argument(
        "--validator-url", default="http://localhost:9100",
        help="Validator base URL",
    )
    status_parser.add_argument(
        "--json", action="store_true",
        help="Print the raw status JSON instead of the rendered per-order view",
    )

    # dry-run (score a plan without submitting)
    dryrun_parser = subparsers.add_parser(
        "dry-run",
        help="Score a plan against the validator without submitting (gated, signed)",
    )
    dryrun_parser.add_argument(
        "--validator-url", default="http://localhost:8080",
        help="Validator base URL",
    )
    dryrun_parser.add_argument(
        "--plan", required=True, type=Path,
        help="Path to plan.json ({interactions, deadline, nonce, metadata})",
    )
    dryrun_target = dryrun_parser.add_mutually_exclusive_group(required=True)
    dryrun_target.add_argument(
        "--app-id",
        help="Score via the REAL fork sim: POST /v1/apps/{app_id}/score "
             "(needs --params)",
    )
    dryrun_target.add_argument(
        "--order-id",
        help="Score via the fast MOCK sim: POST /v1/orders/{order_id}/dry-run",
    )
    dryrun_parser.add_argument(
        "--params", type=Path, default=None,
        help="Path to params.json (order params → state.raw_params). "
             "Required with --app-id.",
    )
    dryrun_parser.add_argument(
        "--chain-id", type=int, default=0,
        help="Chain ID (0 = auto-detect from deployment)",
    )
    dryrun_parser.add_argument(
        "--intent-function", default="execute", help="Intent function name",
    )
    dryrun_parser.add_argument(
        "--fork-block", type=int, default=None,
        help="Historical block to rewind the fork to (archive RPC; ±100 of head)",
    )
    dryrun_parser.add_argument(
        "--wallet-name", default=None,
        help="Bittensor wallet name (or MINER_WALLET_NAME)",
    )
    dryrun_parser.add_argument(
        "--hotkey-name", default=None,
        help="Bittensor hotkey name (or MINER_HOTKEY_NAME)",
    )
    dryrun_parser.add_argument(
        "--wallet-path", default=None, help="Path to bittensor wallets directory",
    )

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    # Hydrate signing keys from AWS Secrets Manager before starting any
    # subcommand. No-op without boto3 / SM access — env-set values win.
    # Same pattern as the API startup (Phase 5.1).
    try:
        from minotaur_subnet.api.secrets_loader import hydrate_env_from_secrets_manager
        _outcome = hydrate_env_from_secrets_manager()
        if _outcome.env_vars_set:
            logger.info(
                "[secrets] hydrated %d env var(s) from Secrets Manager",
                _outcome.env_vars_set,
            )
    except Exception as exc:
        logger.debug("secrets hydration skipped: %s", exc)

    if args.command == "agent":
        from minotaur_subnet.miner.agent.loop import AgentLoop
        from minotaur_subnet.miner.metrics import publish_loop as _miner_metrics_loop

        agent = AgentLoop(
            validator_url=args.validator_url,
            strategy_dir=args.strategy_dir,
            miner_id=args.miner_id,
            loop_interval=args.loop_interval,
            improvement_threshold=args.improvement_threshold,
            max_llm_calls_per_cycle=args.max_llm_calls,
            stale_after=args.stale_after,
            model=args.model,
            claude_timeout=args.claude_timeout,
            anvil_rpc_url=args.anvil_rpc_url,
        )

        async def _run_both():
            # Run the agent loop and the metrics publisher concurrently.
            # If either crashes, the whole miner exits — systemd/compose
            # restarts it.
            await asyncio.gather(
                agent.run(),
                _miner_metrics_loop(
                    miner_id=args.miner_id,
                    counters=agent.metrics,
                    cost_gate=agent.cost_gate,
                ),
            )
        asyncio.run(_run_both())

    elif args.command == "submit":
        result = asyncio.run(submit_solver_git(
            pr_number=args.pr_number,
            head_sha=args.head_sha,
            hotkey=args.hotkey,
            wallet_path=args.wallet_path,
            validator_url=args.validator_url,
            round_id=args.round_id,
            epoch=args.epoch,
            private_repo=args.private_repo,
            # PAT: env wins (keeps it out of shell history), CLI flag as fallback.
            repo_token=os.environ.get("MINER_REPO_TOKEN", "").strip() or args.repo_token,
        ))
        if args.poll and result.get("submission_id"):
            asyncio.run(poll_submission_status(
                result["submission_id"], args.validator_url,
            ))
        sys.exit(0 if result.get("submission_id") else 1)

    elif args.command == "status":
        result = asyncio.run(poll_submission_status(
            args.submission_id, args.validator_url, timeout=30.0,
        ))
        if args.json:
            print(json.dumps(result, indent=2))
        else:
            print(render_status(result))
        sys.exit(0)

    elif args.command == "dry-run":
        if args.app_id and args.params is None:
            parser.error("--params is required with --app-id (POST /apps/{id}/score)")
        wallet_name = (
            args.wallet_name
            or os.environ.get("MINER_WALLET_NAME")
            or os.environ.get("MINER_HOTKEY")
        )
        if not wallet_name:
            parser.error("--wallet-name (or MINER_WALLET_NAME) is required to sign")
        if not args.plan.is_file():
            parser.error(f"plan file not found: {args.plan}")
        plan = json.loads(args.plan.read_text())
        params = (
            json.loads(args.params.read_text())
            if args.params and args.params.is_file()
            else None
        )
        try:
            result = asyncio.run(run_dry_run(
                validator_url=args.validator_url,
                plan=plan,
                params=params,
                app_id=args.app_id,
                order_id=args.order_id,
                chain_id=args.chain_id,
                intent_function=args.intent_function,
                fork_block=args.fork_block,
                wallet_name=wallet_name,
                hotkey_name=args.hotkey_name,
                wallet_path=args.wallet_path,
            ))
        except Exception as exc:
            logger.error(
                "dry-run could not sign the request (wallet=%s): %s. "
                "Check --wallet-name / --hotkey-name / --wallet-path.",
                wallet_name, exc,
            )
            sys.exit(1)
        print(json.dumps(result, indent=2))
        sys.exit(0 if "error" not in result else 1)

    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
