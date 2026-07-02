"""MCP server for the miner agent.

Exposes tools that let Claude Code autonomously develop strategies:
- Validator proxy: fetch app details, scores, orders
- Strategy development: test, score, list strategies
- Contract state: read on-chain view functions, token balances, logs

Start the server (stdio transport):
    python -m minotaur_subnet.miner.agent.mcp_server
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from pathlib import Path

# Ensure repo root is importable
_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from mcp.server.fastmcp import FastMCP

logger = logging.getLogger(__name__)

server = FastMCP(
    name="minotaur-miner",
    instructions=(
        "Minotaur Miner Agent MCP server. "
        "Use these tools to research apps, develop strategies, "
        "test them, and check scores."
    ),
)

# ── Configuration ────────────────────────────────────────────────────────────

VALIDATOR_URL = os.environ.get("VALIDATOR_URL", "http://localhost:8080")
ANVIL_RPC_URL = os.environ.get("ANVIL_RPC_URL", "")
STRATEGY_DIR = Path(os.environ.get("STRATEGY_DIR", "strategies"))


def _validator_url() -> str:
    return os.environ.get("VALIDATOR_URL", VALIDATOR_URL).rstrip("/")


def _strategy_dir() -> Path:
    return Path(os.environ.get("STRATEGY_DIR", str(STRATEGY_DIR)))


def _state_params(state) -> dict:
    typed = getattr(state, "typed_context", None)
    if typed is not None:
        raw = getattr(typed, "raw_params", None)
        if isinstance(raw, dict):
            return raw
    if hasattr(state, "raw_params_view"):
        return state.raw_params_view()
    return getattr(state, "raw_params", {}) or {}


def _intent_function_from_state(state) -> str:
    control = (
        state.control_view()
        if hasattr(state, "control_view")
        else getattr(state, "control", {}) or {}
    )
    return (
        getattr(getattr(state, "typed_context", None), "intent_function", "")
        or control.get("_intent_function")
        or _state_params(state).get("intent_function")
        or "execute"
    )


# ═════════════════════════════════════════════════════════════════════════════
#                        SHARED HELPERS
# ═════════════════════════════════════════════════════════════════════════════


def _parse_function_sig(sig: str) -> tuple[str, list[str], list[str]]:
    """Parse ``"balanceOf(address)(uint256)"`` into components.

    Returns (canonical_sig, input_types, return_types).
    The second parenthesized group (return types) is optional.
    """
    # Match: name(types)(return_types) or name(types)
    m = re.match(r'^([^(]+\([^)]*\))(?:\(([^)]*)\))?$', sig.strip())
    if not m:
        raise ValueError(f"Invalid function signature: {sig!r}")

    canonical = m.group(1)  # e.g. "balanceOf(address)"
    params_str = canonical[canonical.index("(") + 1:-1]
    input_types = [p.strip() for p in params_str.split(",") if p.strip()]

    ret_str = m.group(2)  # e.g. "uint256" or None
    return_types = []
    if ret_str is not None:
        return_types = [r.strip() for r in ret_str.split(",") if r.strip()]

    return canonical, input_types, return_types


def _to_json_safe(value):
    """Convert eth_abi decoded values to JSON-safe types.

    - int → str (avoids JS precision loss for uint256)
    - bytes → "0x" + hex
    - recursive for tuples/lists
    """
    if isinstance(value, int):
        return str(value)
    if isinstance(value, bytes):
        return "0x" + value.hex()
    if isinstance(value, (list, tuple)):
        return [_to_json_safe(v) for v in value]
    return value


def _execute_read(
    rpc_url: str,
    address: str,
    function_sig: str,
    args: list,
    block: str = "latest",
) -> dict:
    """Shared logic: encode + eth_call + optional decode for a single call.

    Returns dict with ``raw`` and optionally ``decoded`` keys.
    """
    from eth_abi.abi import encode as abi_encode, decode as abi_decode
    try:
        from eth_hash.auto import keccak
    except ImportError:
        import hashlib
        keccak = lambda data: hashlib.sha3_256(data).digest()

    canonical, input_types, return_types = _parse_function_sig(function_sig)

    # Compute selector from canonical sig (without return types)
    selector = keccak(canonical.encode())[:4]

    # Encode args
    if input_types:
        encoded_args = abi_encode(input_types, args)
    else:
        encoded_args = b""

    calldata = "0x" + (selector + encoded_args).hex()

    payload = {
        "jsonrpc": "2.0",
        "method": "eth_call",
        "params": [{"to": address, "data": calldata}, block],
        "id": 1,
    }
    result = _http_post(rpc_url, payload)

    if "error" in result and "result" not in result:
        return result

    raw_hex = result.get("result", "0x")
    out = {
        "raw": raw_hex,
        "function": function_sig,
        "address": address,
    }

    # Auto-decode if return types specified
    if return_types and raw_hex and raw_hex != "0x":
        try:
            raw_bytes = bytes.fromhex(raw_hex[2:])
            decoded = abi_decode(return_types, raw_bytes)
            out["decoded"] = [_to_json_safe(v) for v in decoded]
        except Exception:
            pass  # Return raw only if decode fails

    return out


# ═════════════════════════════════════════════════════════════════════════════
#                     VALIDATOR PROXY TOOLS
# ═════════════════════════════════════════════════════════════════════════════


def _http_get(url: str, timeout: float = 30.0) -> dict:
    """Synchronous HTTP GET returning JSON dict."""
    import urllib.request
    import urllib.error

    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return {"error": f"HTTP {exc.code}: {exc.reason}", "url": url}
    except urllib.error.URLError as exc:
        return {"error": f"Connection failed: {exc.reason}", "url": url}
    except Exception as exc:
        return {"error": str(exc), "url": url}


def _http_post(url: str, payload: dict, timeout: float = 60.0) -> dict:
    """Synchronous HTTP POST with JSON body returning JSON dict."""
    import urllib.request
    import urllib.error

    try:
        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            url, data=data,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode()
        except Exception:
            pass
        return {"error": f"HTTP {exc.code}: {exc.reason}", "body": body, "url": url}
    except urllib.error.URLError as exc:
        return {"error": f"Connection failed: {exc.reason}", "url": url}
    except Exception as exc:
        return {"error": str(exc), "url": url}


@server.tool(
    name="get_app_details",
    description=(
        "Get metadata + manifest for an app: name, description, supported "
        "chains, contract address, ABI, intent functions, benchmark scenarios. "
        "Does NOT include Solidity source or JS scoring code. Call "
        "get_app_solidity(app_id) separately if you need the contract source."
    ),
)
def get_app_details(app_id: str) -> dict:
    """Fetch app details from the API.

    Combines status (app definition + deployment) and manifest. The full
    Solidity source is intentionally excluded — use get_app_solidity() when
    you actually need it. Including it by default pushed responses past 27
    KB, which caused Claude to enter an extended thinking phase (silent
    for minutes) before being able to act on any of the data.
    """
    base = _validator_url()
    status = _http_get(f"{base}/v1/apps/{app_id}/status")
    manifest_resp = _http_get(f"{base}/v1/apps/{app_id}/manifest")

    if "error" in status:
        return status

    app_def = status.get("app", status)
    deployment = status.get("deployment", {})
    manifest = manifest_resp.get("manifest") if "error" not in manifest_resp else None

    sol = app_def.get("solidity_code") or ""
    return {
        "app_id": app_def.get("app_id", app_id),
        "name": app_def.get("name", ""),
        "description": app_def.get("description", ""),
        "intent_type": app_def.get("intent_type", ""),
        "supported_chains": app_def.get("config", {}).get("supported_chains", []),
        "solidity_size_bytes": len(sol),
        "abi": deployment.get("abi") if isinstance(deployment, dict) else None,
        "manifest": manifest,
        "config": app_def.get("config", {}),
        "contract_address": deployment.get("contract_address") if isinstance(deployment, dict) else None,
    }


@server.tool(
    name="get_app_solidity",
    description=(
        "Fetch the Solidity source for an app. Returns {source, size_bytes}. "
        "Separate from get_app_details because the source is usually 10-20 KB "
        "and most reasoning only needs the ABI + manifest to write a strategy."
    ),
)
def get_app_solidity(app_id: str) -> dict:
    """Return just the Solidity source for an app.

    Called explicitly by the agent when the ABI/manifest is insufficient
    and it needs to read the contract (e.g. to understand a custom
    invariant or to decode a non-standard event).
    """
    base = _validator_url()
    status = _http_get(f"{base}/v1/apps/{app_id}/status")
    if "error" in status:
        return status
    app_def = status.get("app", status)
    source = app_def.get("solidity_code") or ""
    return {
        "app_id": app_def.get("app_id", app_id),
        "source": source,
        "size_bytes": len(source),
    }


@server.tool(
    name="get_app_scores",
    description=(
        "Get execution statistics for an app: total_executions, avg_score, "
        "best_score, recent_scores. NOTE: post relative-scoring cutover these "
        "are on-chain delivered-quality BPS (0..10000), and the JS 0..1 score is "
        "a validity sentinel — the head-to-head 'am I beating the champion' signal "
        "lives in the per-submission relative COUNTS (better/worse/matched/new) on "
        "GET /v1/submissions/{id}/status, not here."
    ),
)
def get_app_scores(app_id: str) -> dict:
    """Fetch score stats from the API.

    Args:
        app_id: The app identifier.
    """
    url = f"{_validator_url()}/v1/apps/{app_id}/status"
    data = _http_get(url)
    if "error" in data:
        return data
    return {
        "total_executions": data.get("execution_count", 0),
        "avg_score": data.get("avg_score", 0.0),
        "best_score": data.get("best_score", 0.0),
        "recent_scores": data.get("recent_scores", []),
        "scoring_mode": data.get("scoring_mode", ""),
    }


@server.tool(
    name="list_available_apps",
    description=(
        "List all active apps on the network. Returns app_id, name, "
        "intent_type, description for each app."
    ),
)
def list_available_apps() -> dict:
    """List available apps from the API."""
    url = f"{_validator_url()}/v1/apps/"
    data = _http_get(url)
    if "error" in data:
        return data
    return {"apps": data.get("apps", []), "count": len(data.get("apps", []))}


@server.tool(
    name="list_orders",
    description=(
        "List orders in the OrderBook, optionally filtered by app_id and status. "
        "Use this to understand what orders exist for an app. Returns newest-first "
        "SUMMARIES (token/amount params, status, scores — no execution plan or "
        "consensus detail; fetch a single order for those), paginated: the "
        "response carries total/limit/offset."
    ),
)
def list_orders(app_id: str = "", status: str = "", limit: int = 100, offset: int = 0) -> dict:
    """List orders from the validator (paginated summary view, newest first).

    Args:
        app_id: Filter by app ID. Empty for all.
        status: Filter by status (open, filled, cancelled). Empty for all.
        limit: Page size (server clamps to 1..500).
        offset: Page start; the response's ``total`` says how many match.
    """
    params = [f"limit={int(limit)}", f"offset={int(offset)}"]
    if app_id:
        params.append(f"app_id={app_id}")
    if status:
        params.append(f"status={status}")
    url = f"{_validator_url()}/v1/orders?{'&'.join(params)}"
    return _http_get(url)


# ═════════════════════════════════════════════════════════════════════════════
#                     STRATEGY DEVELOPMENT TOOLS
# ═════════════════════════════════════════════════════════════════════════════


@server.tool(
    name="test_strategy",
    description=(
        "Test a strategy by running structural validation: imports, APP_ID check, "
        "generate_plan against synthetic intents, plan structure validation. "
        "Returns {passed: bool, message: str, details: str}. "
        "Pass the FULL Python source code as the 'code' parameter."
    ),
)
def test_strategy(app_id: str, code: str) -> dict:
    """Test strategy code structurally.

    Args:
        app_id: The app_id this strategy should handle.
        code: Complete Python source code for the strategy.
    """
    try:
        from minotaur_subnet.miner.agent.strategy_tester import StrategyTester
        from minotaur_subnet.miner.agent.app_discovery import AppContext
        # Build an AppContext from live app details so the synthetic state
        # uses the correct chain_id, contract, and token addresses — a
        # structural test on Ethereum-default state would mislead Claude.
        details = get_app_details(app_id)
        app_context: AppContext | None = None
        if isinstance(details, dict) and "error" not in details:
            app_context = AppContext(
                app_id=details.get("app_id", app_id),
                name=details.get("name", ""),
                description=details.get("description", ""),
                intent_type=details.get("intent_type", ""),
                supported_chains=details.get("supported_chains", []) or [],
                solidity_code=details.get("solidity_code"),
                abi=details.get("abi"),
                manifest=details.get("manifest"),
                config=details.get("config", {}) or {},
                contract_address=details.get("contract_address"),
            )
        tester = StrategyTester()
        passed, message = tester.test_strategy_with_context(
            code, app_id, app_context,
        )
        return {"passed": passed, "message": message}
    except Exception as exc:
        return {"passed": False, "message": f"Test error: {exc}"}


def _build_app_context(app_id: str):
    """Fetch live app details and build an AppContext, or None on error."""
    from minotaur_subnet.miner.agent.app_discovery import AppContext
    details = get_app_details(app_id)
    if not isinstance(details, dict) or "error" in details:
        return None
    return AppContext(
        app_id=details.get("app_id", app_id),
        name=details.get("name", ""),
        description=details.get("description", ""),
        intent_type=details.get("intent_type", ""),
        supported_chains=details.get("supported_chains", []) or [],
        solidity_code=details.get("solidity_code"),
        abi=details.get("abi"),
        manifest=details.get("manifest"),
        config=details.get("config", {}) or {},
        contract_address=details.get("contract_address"),
    )


def _initialize_with_rpc(strategy, chain_id: int) -> None:
    """Mirror what the validator's RoutingSolver.initialize does for live
    runs: hand the strategy the per-chain RPC URLs so its dynamic probing
    actually works at score-time. Without this, every strategy falls
    through its hardcoded ``_RPC_URL = "localhost:18545"`` path and
    every miner's plan is structurally identical.

    The miner's MCP server runs INSIDE the miner container; the
    validator's anvil endpoints aren't reachable as ``localhost`` — but
    they ARE reachable via the docker-compose service names exposed in
    the env (``BASE_SIM_RPC_URL``, ``ANVIL_RPC_URL``, etc.). We surface
    those as the same env-var names ``RoutingSolver.initialize`` sets.
    """
    import os
    env_for_chain = {
        1: ("ANVIL_RPC_URL",),
        31337: ("ANVIL_RPC_URL",),
        8453: ("BASE_SIM_RPC_URL", "BASE_RPC_URL"),
        964: ("BITTENSOR_EVM_SIM_RPC_URL", "BITTENSOR_EVM_RPC_URL"),
    }.get(int(chain_id), ())
    rpc_url = ""
    for var in env_for_chain:
        url = os.environ.get(var, "").strip()
        if url:
            rpc_url = url
            break
    if not rpc_url:
        return
    # Plumb to the convention RoutingSolver uses (env vars), so any
    # strategy that reads os.environ.get("BASE_RPC_URL") at runtime
    # picks it up. Also call initialize directly when the strategy
    # supports it.
    if int(chain_id) in (1, 31337):
        os.environ.setdefault("ANVIL_RPC_URL", rpc_url)
    elif int(chain_id) == 8453:
        os.environ.setdefault("BASE_RPC_URL", rpc_url)
    elif int(chain_id) == 964:
        os.environ.setdefault("BITTENSOR_EVM_RPC_URL", rpc_url)
    init = getattr(strategy, "initialize", None)
    if callable(init):
        try:
            init({
                "chain_ids": [int(chain_id)],
                "rpc_urls": {str(int(chain_id)): rpc_url},
            })
        except Exception:
            pass


def _load_strategy_from_code(code: str):
    """Write code to a temp file and load the Strategy subclass."""
    from minotaur_subnet.miner.agent.strategy_tester import load_strategy
    import tempfile
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".py", delete=False, prefix="strategy_",
    ) as f:
        f.write(code)
        tmp_path = f.name
    try:
        return load_strategy(tmp_path)
    finally:
        try:
            Path(tmp_path).unlink()
        except OSError:
            pass


def _score_one_fixture(
    strategy, app_id: str, fixture: tuple,
) -> dict:
    """Run one fixture through the validator /score endpoint.

    Fixture shape: (chain_id, contract, params, intent_fn, scenario_name,
    fork_block). fork_block is None for manifest scenarios and an int
    for historical replays — the server-side simulator will rewind the
    anvil fork to that block before simulating so pool prices match the
    state when the original order was filled.

    Annotates the response with the scenario name so the caller can tell
    per-scenario results apart.
    """
    from minotaur_subnet.harness.snapshot import build_synthetic_snapshot
    from minotaur_subnet.shared.types import (
        AppIntentConfig, AppIntentDefinition, IntentState, TriggerType,
    )
    chain_id, contract_address, params, intent_fn, scen_name, fork_block = fixture
    snapshot = build_synthetic_snapshot(chain_id=chain_id)
    intent = AppIntentDefinition(
        app_id=app_id,
        name="Score Test",
        version="1.0.0",
        intent_type="swap",
        js_code="// test",
        config=AppIntentConfig(
            supported_chains=[chain_id],
            trigger_type=TriggerType.USER_TRIGGERED,
        ),
    )
    state = IntentState(
        contract_address=contract_address,
        chain_id=chain_id,
        nonce=1,
        owner="0x0000000000000000000000000000000000000001",
        raw_params=params,
        control={"_intent_function": intent_fn},
    )

    # Give the strategy live RPC access — same convention the validator's
    # RoutingSolver uses on real runs. Without this, dynamic quoter
    # probes always fail and every strategy falls through its hardcoded
    # fallback path, masking real performance differences between miners.
    _initialize_with_rpc(strategy, chain_id)
    try:
        plan = strategy.generate_plan(intent, state, snapshot)
    except Exception as exc:
        return {
            "scenario": scen_name,
            "error": f"generate_plan raised: {exc}",
            "score": 0.0,
        }

    plan_dict = {
        "intent_id": plan.intent_id,
        "interactions": [
            {
                "target": ix.target,
                "value": ix.value,
                "call_data": ix.call_data,
                "chain_id": ix.chain_id,
            }
            for ix in plan.interactions
        ],
        "deadline": plan.deadline,
        "nonce": plan.nonce,
        "metadata": plan.metadata or {},
    }

    url = f"{_validator_url()}/v1/apps/{app_id}/score"
    body = {
        "plan": plan_dict,
        "params": _state_params(state),
        "intent_function": _intent_function_from_state(state),
    }
    if fork_block is not None:
        body["fork_block"] = int(fork_block)
    result = _http_post(url, body)
    # Prepend scenario name for per-scenario traceability.
    if isinstance(result, dict):
        result = {"scenario": scen_name, **result}
        if fork_block is not None:
            result["fork_block"] = int(fork_block)
    return result


@server.tool(
    name="get_champion_strategy",
    description=(
        "Read the CURRENT CHAMPION's strategy.py for an app. The champion is "
        "the highest-scoring submission the network has accepted; its code "
        "lives on the solver repo's ``main`` branch. Study it before "
        "rewriting yours — copy the parts that work, change only what's "
        "needed to outscore it. Returns {code: str, source: str} or "
        "{error: str} if the champion isn't accessible from this miner."
    ),
)
def get_champion_strategy(app_id: str) -> dict:
    """Read the champion's code from solver_repo/main:strategies/{app_id}/strategy.py."""
    import subprocess
    repo_dir = os.environ.get(
        "SOLVER_REPO_PATH", os.path.expanduser("~/git/minotaur-solver"),
    )
    if not Path(repo_dir, ".git").exists():
        return {"error": f"Solver repo not found at {repo_dir}"}
    try:
        result = subprocess.run(
            ["git", "show", f"main:strategies/{app_id}/strategy.py"],
            cwd=repo_dir, capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            return {
                "error": (
                    f"git show failed: {result.stderr.strip()[:200] or 'no champion on main yet'}"
                ),
            }
        return {
            "code": result.stdout,
            "source": f"{repo_dir}#main:strategies/{app_id}/strategy.py",
            "bytes": len(result.stdout),
        }
    except Exception as exc:
        return {"error": f"Read failed: {exc}"}


@server.tool(
    name="replay_failed_swap",
    description=(
        "DEEP DEBUG TOOL — when a strategy reverts and the basic revert "
        "decoder ('Error(STF)' etc.) doesn't tell you WHY, call this. Runs "
        "ONE scenario through the simulator and returns per-interaction "
        "state snapshots: pre/post balances of input+output tokens for the "
        "executor and recipient, allowance to the swap router, gas used, "
        "and the decoded revert reason. The most actionable debug data "
        "available — use it sparingly, it costs ~1 score-call each."
    ),
)
def replay_failed_swap(app_id: str, code: str, scenario_name: str = "") -> dict:
    """Replay a strategy on one scenario with full per-step trace.

    Returns:
        {
          "scenario": str,
          "interactions": [
            {
              "index": int,
              "target": str, "fn": str, "calldata": str, "value": str,
              "status": "ok" | "reverted",
              "revert_reason": str,           # decoded if reverted
              "gas_used": int,
              "executor_token_in_balance_before": str,
              "executor_token_in_balance_after": str,
              "executor_token_out_balance_before": str,
              "executor_token_out_balance_after": str,
              "executor_allowance_to_target_before": str,
              "executor_allowance_to_target_after": str,
            },
            ...
          ],
          "final_score": float,
          "summary": str,
        }
    """
    try:
        from minotaur_subnet.miner.agent.strategy_tester import _pick_test_fixture
        strategy = _load_strategy_from_code(code)
        app_context = _build_app_context(app_id)
        fixture = _pick_test_fixture(
            app_context, scenario_name=(scenario_name or None),
        )
        chain_id, contract_address, params, intent_fn, scen_name, fork_block = fixture
        # Hit the validator's debug-replay endpoint — it has the full
        # simulator + state snapshot infrastructure. The miner running
        # this MCP tool doesn't have direct Anvil access in --network=none.
        url = f"{_validator_url()}/v1/apps/{app_id}/replay-debug"
        body = {
            "code": code,
            "params": params,
            "intent_function": intent_fn,
            "scenario_name": scen_name,
        }
        if fork_block is not None:
            body["fork_block"] = int(fork_block)
        return _http_post(url, body)
    except Exception as exc:
        return {"error": f"Replay failed: {exc}"}


@server.tool(
    name="inspect_strategy_plan",
    description=(
        "DEBUG TOOL — when a strategy reverts, call this to see exactly what "
        "calldata it generates and which functions it calls before sending "
        "anything to a chain. Runs ``generate_plan`` against ONE fixture "
        "(manifest scenario or historical replay) and returns the plan "
        "structured with each interaction's: target address, decoded function "
        "name (e.g. ``exactInputSingle(...)``), full calldata hex, value, "
        "chain_id. Then tries to score it and includes the simulator's revert "
        "trace if it fails. Pass ``scenario_name`` to target a specific "
        "scenario; empty string picks the first chain match."
    ),
)
def inspect_strategy_plan(
    app_id: str, code: str, scenario_name: str = "",
) -> dict:
    """Decode a strategy's plan and (if it fails) the revert reason.

    The cheap question to ask before another expensive Claude iteration:
    *what calldata is my strategy about to send, and where does it
    revert?*. ``generate_plan`` is local and free; we run it, decode each
    interaction's calldata, then submit to ``/score`` for the simulator's
    revert trace.

    Args:
        app_id: The app_id this strategy handles.
        code: Complete Python source code for the strategy.
        scenario_name: Optional benchmark scenario name; empty = first
            chain-matching scenario.

    Returns:
        {
          "scenario": str,
          "plan": {
            "intent_id": str,
            "deadline": int,
            "nonce": int,
            "interactions": [
              {"index": 0, "chain_id": 1, "target": "0x...",
               "fn": "exactInputSingle(...)", "calldata": "0x...",
               "value": "0"},
              ...
            ],
            "metadata": {...},
          },
          "score": float,        # 0.0 if simulation failed
          "score_message": str,  # rich revert reason if any
        }
    """
    try:
        from minotaur_subnet.miner.agent.strategy_tester import _pick_test_fixture
        from minotaur_subnet.simulator.revert_decoder import decode_call

        strategy = _load_strategy_from_code(code)
        app_context = _build_app_context(app_id)
        fixture = _pick_test_fixture(
            app_context, scenario_name=(scenario_name or None),
        )
        score_result = _score_one_fixture(strategy, app_id, fixture)

        # Re-run generate_plan locally to capture the plan structure for
        # display. _score_one_fixture already did this once but doesn't
        # return the plan; doing it again is cheap (no chain calls).
        from minotaur_subnet.harness.snapshot import build_synthetic_snapshot
        from minotaur_subnet.shared.types import (
            AppIntentConfig, AppIntentDefinition, IntentState, TriggerType,
        )
        chain_id, contract_address, params, intent_fn, scen_name, _fb = fixture
        snapshot = build_synthetic_snapshot(chain_id=chain_id)
        intent = AppIntentDefinition(
            app_id=app_id, name="Inspect", version="1.0.0",
            intent_type="swap", js_code="// test",
            config=AppIntentConfig(
                supported_chains=[chain_id],
                trigger_type=TriggerType.USER_TRIGGERED,
            ),
        )
        state = IntentState(
            contract_address=contract_address, chain_id=chain_id,
            nonce=1, owner="0x0000000000000000000000000000000000000001",
            raw_params=params,
            control={"_intent_function": intent_fn},
        )
        plan = strategy.generate_plan(intent, state, snapshot)

        decoded_interactions = []
        for i, ix in enumerate(plan.interactions):
            decoded_interactions.append({
                "index": i,
                "chain_id": ix.chain_id,
                "target": ix.target,
                "fn": decode_call(ix.call_data),
                "calldata": ix.call_data,
                "value": str(ix.value or 0),
            })

        return {
            "scenario": scen_name,
            "plan": {
                "intent_id": plan.intent_id,
                "deadline": plan.deadline,
                "nonce": plan.nonce,
                "interactions": decoded_interactions,
                "metadata": plan.metadata or {},
            },
            "score": float(score_result.get("score", 0.0)) if isinstance(score_result, dict) else 0.0,
            "score_message": (score_result.get("reason") or score_result.get("message") or "")
                if isinstance(score_result, dict) else str(score_result),
        }
    except Exception as exc:
        return {"error": f"Inspect failed: {exc}"}


@server.tool(
    name="score_strategy",
    description=(
        "Score a strategy against ONE manifest scenario on the validator's "
        "anvil fork (scoreIntent path — matches production). Quick iteration. "
        "Pass scenario_name to target a specific scenario; otherwise the first "
        "chain-matching one is used. For full benchmark fidelity before "
        "accepting a strategy, prefer score_strategy_all."
    ),
)
def score_strategy(app_id: str, code: str, scenario_name: str = "") -> dict:
    """Load strategy, generate plan for one scenario, submit to validator.

    Args:
        app_id: The app_id this strategy handles.
        code: Complete Python source code for the strategy.
        scenario_name: Optional manifest benchmark_scenarios[].name to target.
            Empty string (default) picks the first chain-matching scenario.
    """
    try:
        from minotaur_subnet.miner.agent.strategy_tester import _pick_test_fixture
        strategy = _load_strategy_from_code(code)
        app_context = _build_app_context(app_id)
        fixture = _pick_test_fixture(
            app_context, scenario_name=(scenario_name or None),
        )
        return _score_one_fixture(strategy, app_id, fixture)
    except Exception as exc:
        return {"error": f"Score failed: {exc}"}


@server.tool(
    name="score_strategy_all",
    description=(
        "Run the strategy against EVERY manifest benchmark_scenarios entry "
        "matching the app's chain, the same way the live validator's "
        "benchmark worker does. Returns per-scenario scores + an aggregate "
        "mean. Slower than score_strategy (~3-5s per scenario) but matches "
        "the score the validator will compute at submission time. Use this "
        "as the final check before accepting a strategy."
    ),
)
def score_strategy_all(app_id: str, code: str) -> dict:
    """Benchmark a strategy across all chain-matching scenarios AND
    replayed historical orders — matches what the validator's
    benchmark_worker runs at submission time.

    The live benchmark has two stages: (1) manifest benchmark_scenarios,
    (2) replay of sampled historical filled orders. Without Stage-2
    coverage, the miner-side score can report "aggregate 0.84" while
    the live benchmark scores 0.25 because all historical replays fail.
    This function pulls both and reports them separately so regressions
    in historical replays are visible to the agent.

    Args:
        app_id: The app_id this strategy handles.
        code: Complete Python source code for the strategy.

    Returns:
        {
            "app_id": ...,
            "aggregate_score": float (mean over all scenarios, incl. historical),
            "manifest_score": float (mean over Stage-1 only),
            "historical_score": float (mean over Stage-2 only; 0.0 if no history),
            "scenarios_run": int,
            "scenarios_passed": int (score >= 0.5),
            "per_scenario": [{"scenario": name, "score": 0.9, ...}, ...],
        }
    """
    try:
        from minotaur_subnet.miner.agent.strategy_tester import _all_test_fixtures
        strategy = _load_strategy_from_code(code)
        app_context = _build_app_context(app_id)

        # Fetch historical scenarios from the validator API so we replay
        # the same orders the live benchmark will. Failures here are
        # soft: if history isn't available, we still run Stage-1.
        historical: list[dict] = []
        try:
            base = _validator_url().rstrip("/")
            resp = _http_get(f"{base}/v1/apps/{app_id}/historical-scenarios")
            if isinstance(resp, dict) and "error" not in resp:
                historical = resp.get("scenarios") or []
        except Exception:
            historical = []

        fixtures = _all_test_fixtures(app_context, historical_scenarios=historical)

        per_scenario: list[dict] = []
        scores: list[float] = []
        manifest_scores: list[float] = []
        historical_scores: list[float] = []
        for fixture in fixtures:
            result = _score_one_fixture(strategy, app_id, fixture)
            per_scenario.append(result)
            try:
                s = float(result.get("score", 0.0) or 0.0)
            except (ValueError, TypeError):
                s = 0.0
            scores.append(s)
            scen_name = result.get("scenario", "")
            if scen_name.startswith("hist:"):
                historical_scores.append(s)
            else:
                manifest_scores.append(s)

        aggregate = (sum(scores) / len(scores)) if scores else 0.0
        manifest_mean = (
            sum(manifest_scores) / len(manifest_scores) if manifest_scores else 0.0
        )
        historical_mean = (
            sum(historical_scores) / len(historical_scores)
            if historical_scores else 0.0
        )
        passed = sum(1 for s in scores if s >= 0.5)
        return {
            "app_id": app_id,
            "aggregate_score": aggregate,
            "manifest_score": manifest_mean,
            "historical_score": historical_mean,
            "scenarios_run": len(per_scenario),
            "manifest_scenarios": len(manifest_scores),
            "historical_scenarios": len(historical_scores),
            "scenarios_passed": passed,
            "per_scenario": per_scenario,
        }
    except Exception as exc:
        return {"error": f"Score-all failed: {exc}"}


@server.tool(
    name="list_strategies",
    description=(
        "List all strategies saved on disk. Returns a list of "
        "{app_id, path, size_bytes} for each strategy.py found."
    ),
)
def list_strategies() -> dict:
    """List strategy files in the strategy directory."""
    sdir = _strategy_dir()
    strategies = []
    if sdir.exists():
        for app_dir in sorted(sdir.iterdir()):
            if not app_dir.is_dir():
                continue
            strategy_file = app_dir / "strategy.py"
            if strategy_file.exists():
                strategies.append({
                    "app_id": app_dir.name,
                    "path": str(strategy_file),
                    "size_bytes": strategy_file.stat().st_size,
                })
    return {"strategies": strategies, "count": len(strategies)}


@server.tool(
    name="get_score_feedback",
    description=(
        "Get score feedback for an app: avg_score, best_score, recent_scores, "
        "trend (improving/declining/stable), total_executions. "
        "Use this to understand how the current strategy is performing."
    ),
)
def get_score_feedback(app_id: str) -> dict:
    """Fetch scores from API and compute feedback.

    Args:
        app_id: The app identifier.
    """
    url = f"{_validator_url()}/v1/apps/{app_id}/status"
    data = _http_get(url)
    if "error" in data:
        return data

    stats = {
        "total_executions": data.get("execution_count", 0),
        "avg_score": data.get("avg_score", 0.0),
        "best_score": data.get("best_score", 0.0),
        "recent_scores": data.get("recent_scores", []),
    }

    # Trend over the recent series. Threshold is proportional with a 0.05 floor
    # so it works whether recent_scores are legacy 0..1 values or post-cutover
    # on-chain BPS (0..10000) — a few-BPS wobble must not read as a swing.
    recent = stats.get("recent_scores", [])
    trend = "stable"
    if len(recent) >= 4:
        mid = len(recent) // 2
        first_half = sum(recent[:mid]) / mid
        second_half = sum(recent[mid:]) / (len(recent) - mid)
        diff = second_half - first_half
        threshold = max(0.05, abs(first_half) * 0.05)
        if diff > threshold:
            trend = "improving"
        elif diff < -threshold:
            trend = "declining"

    return {
        "app_id": app_id,
        "avg_score": stats.get("avg_score", 0.0),
        "best_score": stats.get("best_score", 0.0),
        "recent_scores": recent[-10:],
        "total_executions": stats.get("total_executions", 0),
        "trend": trend,
        "scoring_mode": data.get("scoring_mode", ""),
    }


# ═════════════════════════════════════════════════════════════════════════════
#                     CONTRACT STATE TOOLS
# ═════════════════════════════════════════════════════════════════════════════


@server.tool(
    name="read_contract",
    description=(
        "Call a view/pure function on a smart contract. Requires ANVIL_RPC_URL. "
        "function_sig: 'balanceOf(address)' returns raw hex. "
        "Add return types for auto-decode: 'balanceOf(address)(uint256)' returns decoded value. "
        "args is a JSON array of arguments. block defaults to 'latest'."
    ),
)
def read_contract(
    address: str,
    function_sig: str,
    args: str = "[]",
    block: str = "latest",
) -> dict:
    """Call a view function on-chain with optional auto-decode.

    Args:
        address: Contract address (0x-prefixed).
        function_sig: Signature with optional return types, e.g. 'balanceOf(address)(uint256)'.
        args: JSON array of arguments, e.g. '["0x1234..."]'.
        block: Block number or tag (default "latest").
    """
    rpc_url = os.environ.get("ANVIL_RPC_URL", ANVIL_RPC_URL)
    if not rpc_url:
        return {"error": "ANVIL_RPC_URL not set. Cannot read on-chain state."}

    try:
        parsed_args = json.loads(args)
        return _execute_read(rpc_url, address, function_sig, parsed_args, block)
    except Exception as exc:
        return {"error": f"read_contract failed: {exc}"}


@server.tool(
    name="get_token_balance",
    description=(
        "Get the ERC-20 token balance for an address with metadata. "
        "Returns balance, symbol, decimals, and formatted amount. "
        "Checks token registry first for known tokens. Requires ANVIL_RPC_URL."
    ),
)
def get_token_balance(token_address: str, holder_address: str) -> dict:
    """Query ERC-20 balanceOf with decimals/symbol metadata.

    Args:
        token_address: The ERC-20 token contract address.
        holder_address: The address to check balance for.
    """
    rpc_url = os.environ.get("ANVIL_RPC_URL", ANVIL_RPC_URL)
    if not rpc_url:
        return {"error": "ANVIL_RPC_URL not set. Cannot read on-chain state."}

    try:
        from eth_abi.abi import encode as abi_encode

        # balanceOf(address) selector = 0x70a08231
        selector = bytes.fromhex("70a08231")
        encoded_args = abi_encode(["address"], [holder_address])
        calldata = "0x" + (selector + encoded_args).hex()

        payload = {
            "jsonrpc": "2.0",
            "method": "eth_call",
            "params": [{"to": token_address, "data": calldata}, "latest"],
            "id": 1,
        }
        result = _http_post(rpc_url, payload)

        if "result" not in result:
            return result

        raw_hex = result["result"]
        if raw_hex and raw_hex != "0x":
            balance = int(raw_hex, 16)
        else:
            balance = 0

        out: dict = {
            "token": token_address,
            "holder": holder_address,
            "balance": str(balance),
            "balance_raw": raw_hex,
        }

        # Try to get symbol from registry first (zero RPC)
        try:
            from minotaur_subnet.blockchain.tokens import get_token_symbol
            known_symbol = get_token_symbol(token_address)
        except Exception:
            known_symbol = None

        # Query decimals and symbol on-chain
        decimals = None
        symbol = known_symbol

        try:
            dec_result = _execute_read(
                rpc_url, token_address, "decimals()(uint8)", [], "latest",
            )
            if "decoded" in dec_result:
                decimals = int(dec_result["decoded"][0])
        except Exception:
            pass

        if symbol is None:
            try:
                sym_result = _execute_read(
                    rpc_url, token_address, "symbol()(string)", [], "latest",
                )
                if "decoded" in sym_result:
                    symbol = sym_result["decoded"][0]
            except Exception:
                pass

        if decimals is not None:
            out["decimals"] = decimals
            # Format with proper decimal places
            if balance > 0:
                formatted = f"{balance / (10 ** decimals):.{decimals}f}"
            else:
                formatted = f"0.{'0' * decimals}"
            out["formatted"] = formatted

        if symbol is not None:
            out["symbol"] = symbol

        return out

    except Exception as exc:
        return {"error": f"get_token_balance failed: {exc}"}


# ═════════════════════════════════════════════════════════════════════════════
#                    PROTOCOL-AGNOSTIC ON-CHAIN TOOLS
# ═════════════════════════════════════════════════════════════════════════════


@server.tool(
    name="resolve_token",
    description=(
        "Resolve a token symbol or address using the built-in registry. "
        "No RPC needed — instant lookup. Supports USDC, WETH, WBTC, DAI, USDT, wTAO "
        "across Ethereum, Base, Arbitrum, Optimism. ETH resolves to WETH. "
        "Returns address, symbol, chain_id, and known_chains."
    ),
)
def resolve_token(token: str, chain_id: int = 1) -> dict:
    """Look up a token in the built-in registry.

    Args:
        token: Symbol ("USDC") or address ("0xA0b...").
        chain_id: Chain to resolve on (default 1 = Ethereum).
    """
    try:
        from minotaur_subnet.blockchain.tokens import (
            TOKENS, NATIVE_TO_WRAPPED,
            get_token_address, get_token_symbol,
        )

        address = None
        symbol = None

        # If it looks like an address, do reverse lookup
        if token.startswith("0x") and len(token) == 42:
            symbol = get_token_symbol(token, chain_id)
            if symbol is None:
                # Try all chains
                symbol = get_token_symbol(token)
            address = token
        else:
            # Symbol lookup — handle native → wrapped
            lookup_symbol = token.upper()
            wrapped = NATIVE_TO_WRAPPED.get(chain_id, {}).get(lookup_symbol)
            if wrapped:
                lookup_symbol = wrapped
                symbol = token.upper()  # Keep original for display
            try:
                address = get_token_address(token, chain_id)
                if symbol is None:
                    symbol = token
            except ValueError:
                # Token not found
                known = []
                for cid, toks in TOKENS.items():
                    known.extend(toks.keys())
                return {
                    "error": f"Token {token!r} not found on chain {chain_id}",
                    "known_tokens": sorted(set(known)),
                }

        # Build known_chains map
        known_chains = {}
        if address:
            addr_lower = address.lower()
            for cid, toks in TOKENS.items():
                for sym, addr in toks.items():
                    if addr.lower() == addr_lower or (symbol and sym == symbol):
                        known_chains[str(cid)] = addr

        return {
            "address": address,
            "symbol": symbol,
            "chain_id": chain_id,
            "known_chains": known_chains,
        }

    except Exception as exc:
        return {"error": f"resolve_token failed: {exc}"}


@server.tool(
    name="get_token_info",
    description=(
        "Get full ERC-20 metadata in one call: name, symbol, decimals, totalSupply. "
        "Handles both string and bytes32 return types. "
        "Cross-references with token registry for known tokens. Requires ANVIL_RPC_URL."
    ),
)
def get_token_info(token_address: str) -> dict:
    """Query on-chain ERC-20 metadata.

    Args:
        token_address: The ERC-20 token contract address.
    """
    rpc_url = os.environ.get("ANVIL_RPC_URL", ANVIL_RPC_URL)
    if not rpc_url:
        return {"error": "ANVIL_RPC_URL not set. Cannot read on-chain state."}

    out: dict = {"address": token_address}

    # Try registry first
    try:
        from minotaur_subnet.blockchain.tokens import get_token_symbol
        known = get_token_symbol(token_address)
        if known:
            out["registry_symbol"] = known
    except Exception:
        pass

    # Query name()
    try:
        r = _execute_read(rpc_url, token_address, "name()(string)", [], "latest")
        if "decoded" in r:
            out["name"] = r["decoded"][0]
    except Exception:
        pass
    if "name" not in out:
        try:
            r = _execute_read(rpc_url, token_address, "name()(bytes32)", [], "latest")
            if "decoded" in r:
                raw = r["decoded"][0]
                if isinstance(raw, str) and raw.startswith("0x"):
                    out["name"] = bytes.fromhex(raw[2:]).rstrip(b"\x00").decode("utf-8", errors="replace")
        except Exception:
            pass

    # Query symbol()
    try:
        r = _execute_read(rpc_url, token_address, "symbol()(string)", [], "latest")
        if "decoded" in r:
            out["symbol"] = r["decoded"][0]
    except Exception:
        pass
    if "symbol" not in out:
        try:
            r = _execute_read(rpc_url, token_address, "symbol()(bytes32)", [], "latest")
            if "decoded" in r:
                raw = r["decoded"][0]
                if isinstance(raw, str) and raw.startswith("0x"):
                    out["symbol"] = bytes.fromhex(raw[2:]).rstrip(b"\x00").decode("utf-8", errors="replace")
        except Exception:
            pass

    # Query decimals()
    try:
        r = _execute_read(rpc_url, token_address, "decimals()(uint8)", [], "latest")
        if "decoded" in r:
            out["decimals"] = int(r["decoded"][0])
    except Exception:
        pass

    # Query totalSupply()
    try:
        r = _execute_read(rpc_url, token_address, "totalSupply()(uint256)", [], "latest")
        if "decoded" in r:
            out["total_supply"] = r["decoded"][0]
    except Exception:
        pass

    return out


@server.tool(
    name="multicall_read",
    description=(
        "Batch up to 20 view calls in one MCP tool invocation. Each call is an "
        "independent eth_call (no on-chain Multicall3 dependency). "
        "Input: JSON array of {address, function_sig, args} objects. "
        "Per-call errors don't fail the batch. Requires ANVIL_RPC_URL."
    ),
)
def multicall_read(calls: str) -> dict:
    """Batch multiple read_contract calls.

    Args:
        calls: JSON array of {address, function_sig, args?} objects. Max 20.
    """
    rpc_url = os.environ.get("ANVIL_RPC_URL", ANVIL_RPC_URL)
    if not rpc_url:
        return {"error": "ANVIL_RPC_URL not set. Cannot read on-chain state."}

    try:
        parsed = json.loads(calls)
    except (json.JSONDecodeError, TypeError) as exc:
        return {"error": f"Invalid JSON: {exc}"}

    if not isinstance(parsed, list):
        return {"error": "calls must be a JSON array"}

    if len(parsed) > 20:
        return {"error": f"Too many calls ({len(parsed)}). Maximum is 20."}

    results = []
    errors = 0

    for i, call in enumerate(parsed):
        if not isinstance(call, dict):
            results.append({"index": i, "error": "call must be an object"})
            errors += 1
            continue

        addr = call.get("address", "")
        sig = call.get("function_sig", "")
        args = call.get("args", [])

        if not addr or not sig:
            results.append({"index": i, "error": "missing address or function_sig"})
            errors += 1
            continue

        try:
            r = _execute_read(rpc_url, addr, sig, args, "latest")
            r["index"] = i
            results.append(r)
            if "error" in r:
                errors += 1
        except Exception as exc:
            results.append({"index": i, "error": str(exc)})
            errors += 1

    return {"results": results, "count": len(results), "errors": errors}


@server.tool(
    name="get_logs",
    description=(
        "Scan event logs via eth_getLogs. Protocol-agnostic event scanning. "
        "Provide address and topics array (topic0 = event signature hash). "
        "Returns logs with block_number, tx_hash, data, topics. "
        "Cap at 1000 logs. Requires ANVIL_RPC_URL."
    ),
)
def get_logs(
    address: str,
    topics: str = "[]",
    from_block: str = "latest",
    to_block: str = "latest",
    max_logs: int = 100,
) -> dict:
    """Query event logs via eth_getLogs.

    Args:
        address: Contract address to filter logs from.
        topics: JSON array of topic filters (null for wildcard).
        from_block: Start block (hex, number, or "latest").
        to_block: End block (hex, number, or "latest").
        max_logs: Maximum logs to return (cap 1000).
    """
    rpc_url = os.environ.get("ANVIL_RPC_URL", ANVIL_RPC_URL)
    if not rpc_url:
        return {"error": "ANVIL_RPC_URL not set. Cannot read on-chain state."}

    try:
        parsed_topics = json.loads(topics)
    except (json.JSONDecodeError, TypeError):
        parsed_topics = []

    max_logs = min(max_logs, 1000)

    try:
        filter_obj: dict = {"address": address}
        if parsed_topics:
            filter_obj["topics"] = parsed_topics
        if from_block != "latest":
            filter_obj["fromBlock"] = from_block
        else:
            filter_obj["fromBlock"] = "latest"
        if to_block != "latest":
            filter_obj["toBlock"] = to_block
        else:
            filter_obj["toBlock"] = "latest"

        payload = {
            "jsonrpc": "2.0",
            "method": "eth_getLogs",
            "params": [filter_obj],
            "id": 1,
        }
        result = _http_post(rpc_url, payload)

        if "error" in result and "result" not in result:
            return result

        raw_logs = result.get("result", [])
        if not isinstance(raw_logs, list):
            return {"logs": [], "count": 0}

        logs = []
        for log in raw_logs[:max_logs]:
            block_num = log.get("blockNumber", "0x0")
            if isinstance(block_num, str) and block_num.startswith("0x"):
                block_num = int(block_num, 16)
            log_idx = log.get("logIndex", "0x0")
            if isinstance(log_idx, str) and log_idx.startswith("0x"):
                log_idx = int(log_idx, 16)

            logs.append({
                "address": log.get("address", ""),
                "topics": log.get("topics", []),
                "data": log.get("data", "0x"),
                "block_number": block_num,
                "tx_hash": log.get("transactionHash", ""),
                "log_index": log_idx,
            })

        return {"logs": logs, "count": len(logs)}

    except Exception as exc:
        return {"error": f"get_logs failed: {exc}"}


@server.tool(
    name="get_contract_code",
    description=(
        "Check if an address has deployed contract code. "
        "Returns is_contract bool and code_size (bytes). "
        "Does NOT return full bytecode (saves tokens). Requires ANVIL_RPC_URL."
    ),
)
def get_contract_code(address: str) -> dict:
    """Check if address is a contract via eth_getCode.

    Args:
        address: The address to check (0x-prefixed).
    """
    rpc_url = os.environ.get("ANVIL_RPC_URL", ANVIL_RPC_URL)
    if not rpc_url:
        return {"error": "ANVIL_RPC_URL not set. Cannot read on-chain state."}

    try:
        payload = {
            "jsonrpc": "2.0",
            "method": "eth_getCode",
            "params": [address, "latest"],
            "id": 1,
        }
        result = _http_post(rpc_url, payload)

        if "error" in result and "result" not in result:
            return result

        code_hex = result.get("result", "0x")
        code_bytes = bytes.fromhex(code_hex[2:]) if code_hex and code_hex != "0x" else b""

        return {
            "address": address,
            "is_contract": len(code_bytes) > 0,
            "code_size": len(code_bytes),
        }

    except Exception as exc:
        return {"error": f"get_contract_code failed: {exc}"}


# ═════════════════════════════════════════════════════════════════════════════
#                          ENTRY POINT
# ═════════════════════════════════════════════════════════════════════════════


def main() -> None:
    """Run the MCP server over stdio."""
    import asyncio

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        stream=sys.stderr,
    )

    asyncio.run(server.run_stdio_async())


if __name__ == "__main__":
    main()
