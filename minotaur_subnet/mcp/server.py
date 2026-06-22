"""
MCP server for the Minotaur App Intents platform.

Thin HTTP client that proxies all tool calls to the REST API server.
No internal imports of business logic — only httpx + mcp.

Start the server:
    python -m minotaur_subnet.mcp.server

Requires the API server to be running (default: http://localhost:8080).
Set MINOTAUR_API_URL to override.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Ensure the repo root is importable
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import httpx
from mcp.server.fastmcp import FastMCP

# ─── configuration ───────────────────────────────────────────────────────────

_API_BASE = os.environ.get("MINOTAUR_API_URL", "http://localhost:8080")
_API_V1 = f"{_API_BASE}/v1"

# Operator round-control routes are fail-closed (require the internal key); send
# it as a default header when configured so the MCP operator tools keep working.
_internal_round_key = (
    os.environ.get("SOLVER_ROUND_INTERNAL_API_KEY", "").strip()
    or os.environ.get("SUBMISSIONS_API_KEY", "").strip()
)
_client = httpx.Client(
    base_url=_API_V1,
    timeout=30.0,
    headers={"x-solver-round-internal-key": _internal_round_key} if _internal_round_key else None,
)

server = FastMCP(
    name="minotaur-app-intents",
    instructions=(
        "Minotaur App Intents MCP server. "
        "Use these tools to create, deploy, and monitor App Intents -- "
        "outcome-based dApps where solvers compete to find optimal execution."
    ),
)


def _safe_response(resp) -> dict:
    """Parse API response, returning structured error on non-2xx status."""
    try:
        data = resp.json()
    except Exception:
        data = {}
    if resp.status_code >= 400:
        error_msg = data.get("detail") or data.get("error") or resp.text[:200]
        return {"error": f"API error {resp.status_code}: {error_msg}", "_status": resp.status_code}
    return data


def _get(path: str, **params) -> dict:
    """GET request to API, return JSON response."""
    try:
        resp = _client.get(path, params={k: v for k, v in params.items() if v is not None and v != ""})
        return _safe_response(resp)
    except Exception as exc:
        return {"error": f"Request failed: {exc}"}


def _post(path: str, json_body: dict | None = None) -> dict:
    """POST request to API, return JSON response."""
    try:
        resp = _client.post(path, json=json_body or {})
        return _safe_response(resp)
    except Exception as exc:
        return {"error": f"Request failed: {exc}"}


def _put(path: str, json_body: dict | None = None) -> dict:
    """PUT request to API, return JSON response."""
    try:
        resp = _client.put(path, json=json_body or {})
        return _safe_response(resp)
    except Exception as exc:
        return {"error": f"Request failed: {exc}"}


def _delete(path: str) -> dict:
    """DELETE request to API, return JSON response."""
    try:
        resp = _client.delete(path)
        return _safe_response(resp)
    except Exception as exc:
        return {"error": f"Request failed: {exc}"}


def _patch(path: str, json_body: dict | None = None) -> dict:
    """PATCH request to API, return JSON response."""
    try:
        resp = _client.patch(path, json=json_body or {})
        return _safe_response(resp)
    except Exception as exc:
        return {"error": f"Request failed: {exc}"}


# ═══════════════════════════════════════════════════════════════════════════════
#                          WALLET MANAGEMENT
# ═══════════════════════════════════════════════════════════════════════════════


@server.tool(
    name="create_wallet",
    description=(
        "Create a new managed wallet for the user. "
        "For the MVP this generates a local dev wallet. "
        "In production it provisions an MPC wallet via Lit Protocol. "
        "Returns the wallet address, type, supported chains, and creation time."
    ),
)
def create_wallet(chain_ids: list[int]) -> dict:
    """Create a new managed wallet.

    Args:
        chain_ids: Which blockchain networks to support (e.g. [1, 8453]).
    """
    return _post("/wallets/", {"chain_ids": chain_ids})


@server.tool(
    name="get_wallet",
    description=(
        "Look up wallet information by its 0x address. "
        "Returns the wallet type, supported chains, and creation time."
    ),
)
def get_wallet(address: str) -> dict:
    """Get wallet info by address.

    Args:
        address: The 0x-prefixed wallet address.
    """
    return _get(f"/wallets/{address}")


@server.tool(
    name="fund_wallet",
    description=(
        "Deposit tokens into an App Intent's on-chain contract. "
        "Requires the app to be deployed and active. "
        "For the MVP this returns a stub transaction hash."
    ),
)
def fund_wallet(app_id: str, token: str, amount: str, chain_id: int) -> dict:
    """Fund an app's contract with tokens.

    Args:
        app_id:   ID of the deployed app intent.
        token:    Token contract address (0x-prefixed).
        amount:   Amount in wei as a decimal string.
        chain_id: Target chain ID for the deposit.
    """
    return _post(f"/apps/{app_id}/fund", {
        "token": token,
        "amount": amount,
        "chain_id": chain_id,
    })


@server.tool(
    name="get_wallet_balances",
    description=(
        "Query ETH and ERC-20 token balances for any address. "
        "Scans all known tokens (USDC, WETH, USDT, WBTC, DAI, wTAO) on the "
        "specified chain and returns non-zero balances with human-readable amounts. "
        "Works for any address, not just managed wallets."
    ),
)
def get_wallet_balances(address: str, chain_id: int = 31337) -> dict:
    """Get ETH + token balances for an address.

    Args:
        address:  The 0x-prefixed Ethereum address to query.
        chain_id: Chain to query balances on (default: 31337 for local Anvil fork).
    """
    return _get(f"/wallets/{address}/balances", chain_id=chain_id)


@server.tool(
    name="list_wallets",
    description=(
        "List all managed wallets. Returns each wallet's address, "
        "supported chains, type, and creation time."
    ),
)
def list_wallets() -> dict:
    """List all managed wallets."""
    return _get("/wallets/")


# ═══════════════════════════════════════════════════════════════════════════════
#                         CHAIN DISCOVERY
# ═══════════════════════════════════════════════════════════════════════════════


@server.tool(
    name="list_chains",
    description=(
        "List all blockchain networks the platform supports. "
        "Returns each chain's ID, name, whether an RPC endpoint is available, "
        "and the validator registry contract address (if deployed). "
        "Use this to discover which chain_ids to pass to create_app_intent "
        "and submit_order."
    ),
)
def list_chains() -> dict:
    """List supported blockchain networks."""
    return _get("/chains")


# ═══════════════════════════════════════════════════════════════════════════════
#                       APP INTENT LIFECYCLE
# ═══════════════════════════════════════════════════════════════════════════════


@server.tool(
    name="create_app_intent",
    description=(
        "Define a new App Intent. Both js_code and solidity_code are required -- "
        "apps must provide their own scoring JS and Solidity contract. "
        "Returns the full definition including code and hashes."
    ),
)
def create_app_intent(
    name: str,
    description: str,
    supported_chains: list[int],
    js_code: str,
    solidity_code: str,
    deployer: str = "",
    fee_mode: str = "",
) -> dict:
    """Create a new App Intent definition.

    Args:
        name:             Human-readable name (e.g. "ETH-USDC Swap").
        description:      What this app does, in natural language.
        supported_chains: Chain IDs to deploy on (e.g. [1, 8453]).
        js_code:          JS scoring code (required).
        solidity_code:    Solidity contract code (required).
        deployer:         Deployer wallet address. Only this address can update JS later.
        fee_mode:         Per-App on-chain fee mode: "USER" (users pay the fee) or
                          "APP" (the App's paymaster pays). Empty = operator default.
    """
    body: dict = {
        "name": name,
        "description": description,
        "supported_chains": supported_chains,
        "js_code": js_code,
        "solidity_code": solidity_code,
    }
    if deployer:
        body["deployer"] = deployer
    if fee_mode:
        body["fee_mode"] = fee_mode
    return _post("/apps/", body)


@server.tool(
    name="validate_app_intent",
    description=(
        "Pre-flight validation for App Intent JS and Solidity code. "
        "Validates JS by loading it in a sandbox (checks syntax and required "
        "exports like score()). Optionally validates Solidity by compiling "
        "with Forge. Returns structured errors and warnings with extracted "
        "metadata (config, manifest, ABI). Use this before create_app_intent "
        "to catch errors early."
    ),
)
def validate_app_intent(
    js_code: str,
    solidity_code: str = "",
    skip_solidity: bool = False,
) -> dict:
    """Validate App Intent code without creating an app.

    Args:
        js_code:        JavaScript scoring code to validate (required).
        solidity_code:  Solidity contract code to validate (optional).
        skip_solidity:  If True, skip Solidity compilation check.
    """
    return _post("/apps/validate", {
        "js_code": js_code,
        "solidity_code": solidity_code,
        "skip_solidity": skip_solidity,
    })


@server.tool(
    name="deploy_app_intent",
    description=(
        "Deploy an App Intent to the Minotaur network. "
        "Pushes the JS scoring code to validators and the Solidity contract "
        "to the target chain. Requires the app to have been created first "
        "via create_app_intent. Returns a deployment result with contract "
        "address and JS code hash."
    ),
)
def deploy_app_intent(app_id: str, chain_id: int = 0) -> dict:
    """Deploy a previously created App Intent.

    Args:
        app_id:   The app_id returned by create_app_intent.
        chain_id: Target chain ID to deploy on. 0 = first supported chain.
    """
    path = f"/apps/{app_id}/deploy"
    if chain_id:
        path += f"?chain_id={chain_id}"
    return _post(path)


@server.tool(
    name="list_app_intents",
    description=(
        "List all App Intents, optionally filtered by deployer address. "
        "Returns a summary of each app: app_id, name, description, "
        "supported_chains, intent functions, and deployment status. "
        "Source code is excluded for readability — use get_app_manifest "
        "for the full manifest if needed."
    ),
)
def list_app_intents(deployer: str = "") -> dict:
    """List all App Intents (summary view, no source code).

    Args:
        deployer: Optional deployer address to filter by. Pass empty string for all.
    """
    result = _get("/apps/", deployer=deployer if deployer else None)
    if "error" in result:
        return result
    # Strip source code to keep response concise for agents
    apps = result.get("apps", [])
    for app in apps:
        app.pop("js_code", None)
        app.pop("solidity_code", None)
    return result


@server.tool(
    name="get_app_status",
    description=(
        "Check an App Intent's health and execution statistics. "
        "Returns status (draft/active/partial/paused/retired), execution count, "
        "average score, best score, and when it was last triggered."
    ),
)
def get_app_status(app_id: str) -> dict:
    """Get status and stats for an App Intent.

    Args:
        app_id: The app to check.
    """
    return _get(f"/apps/{app_id}/status")


# ═══════════════════════════════════════════════════════════════════════════════
#                            MONITORING
# ═══════════════════════════════════════════════════════════════════════════════


@server.tool(
    name="monitor_app",
    description=(
        "Get real-time execution monitoring data for an App Intent. "
        "Returns the best recent scores, execution history summary, "
        "and per-solver performance statistics."
    ),
)
def monitor_app(app_id: str) -> dict:
    """Monitor an App Intent's execution performance.

    Args:
        app_id: The app to monitor.
    """
    return _get(f"/apps/{app_id}/monitor")


@server.tool(
    name="update_scoring",
    description=(
        "Update the JS scoring code for an existing App Intent. "
        "The new code is distributed to validators on the next sync cycle. "
        "Automatically bumps the app version. Returns the new code hash."
    ),
)
def update_scoring(app_id: str, new_js_code: str, caller: str = "") -> dict:
    """Update JS scoring code for an app.

    Args:
        app_id:      The app whose scoring to update.
        new_js_code: The new JavaScript scoring source code.
        caller:      Caller wallet address (must match the app's deployer, if one was set).
    """
    body: dict = {"new_js_code": new_js_code}
    if caller:
        body["caller"] = caller
    return _put(f"/apps/{app_id}/scoring", body)


# ═══════════════════════════════════════════════════════════════════════════════
#                         ORDER MANAGEMENT
# ═══════════════════════════════════════════════════════════════════════════════


@server.tool(
    name="prepare_order",
    description=(
        "Resolve all parameters for an order before quoting or submitting. "
        "Auto-resolves token symbols (USDC, WETH) to addresses, detects the "
        "correct chain_id from the app's deployment, detects intent_function "
        "from the manifest, and fetches the on-chain nonce for managed wallets. "
        "Returns resolved_params, chain_id, intent_function, and next_steps. "
        "This is step 1 of the recommended flow: prepare → quote → submit."
    ),
)
def prepare_order(
    app_id: str,
    params: dict,
    submitted_by: str = "",
    intent_function: str = "execute",
    chain_id: int = 0,
) -> dict:
    """Prepare and resolve all parameters for an order.

    Args:
        app_id:           ID of the App Intent.
        params:           Order parameters — token symbols like "USDC" are auto-resolved.
        submitted_by:     User wallet address (0x-prefixed). Needed for nonce lookup.
        intent_function:  Intent function name (default: "execute", auto-detected if only one exists).
        chain_id:         Target chain ID (default: 0 = auto-detect from deployment).
    """
    body: dict = {
        "params": params,
        "intent_function": intent_function,
        "chain_id": chain_id,
    }
    if submitted_by:
        body["submitted_by"] = submitted_by
    return _post(f"/apps/{app_id}/prepare", body)


@server.tool(
    name="submit_order",
    description=(
        "Submit an order to the Intent OrderBook. Orders are queued for "
        "processing by the block loop, which generates plans, scores them, "
        "and routes approved plans to the relayer. Returns the order with "
        "its assigned order_id and initial status. "
        "Token symbols (USDC, WETH, WBTC) are auto-resolved to addresses. "
        "chain_id, intent_function, and nonce are auto-detected from the app's "
        "deployment and manifest. Recommended flow: prepare_order → get_quote → submit_order."
    ),
)
def submit_order(
    app_id: str,
    params: dict,
    submitted_by: str,
    intent_function: str = "execute",
    chain_id: int = 0,
    deadline: float = 0.0,
    perpetual: bool = False,
    max_executions: int = 1,
    cooldown: float = 0.0,
) -> dict:
    """Submit an order to the OrderBook.

    Args:
        app_id:           ID of the App Intent.
        params:           Order parameters (type-dependent).
        submitted_by:     User wallet address (0x-prefixed).
        intent_function:  Intent function name (default: "execute").
        chain_id:         Target chain ID.
        deadline:         Unix timestamp deadline (0 = no deadline).
        perpetual:        Whether the order re-executes.
        max_executions:   Max fills for perpetual orders.
        cooldown:         Seconds between perpetual fills.
    """
    return _post(f"/apps/{app_id}/orders", {
        "params": params,
        "submitted_by": submitted_by,
        "intent_function": intent_function,
        "chain_id": chain_id,
        "deadline": deadline,
        "perpetual": perpetual,
        "max_executions": max_executions,
        "cooldown": cooldown,
    })


@server.tool(
    name="get_quote",
    description=(
        "Get a quote for an intent without creating an order. "
        "Dry-runs the Solving Engine against the current market state: "
        "generates a plan, simulates it, and scores it. Returns estimated "
        "output, suggested min output (with slippage protection), JS score "
        "with breakdown, gas estimate, and route summary. "
        "No signature required, no side effects. "
        "Token symbols (USDC, WETH, WBTC) are auto-resolved to addresses. "
        "chain_id and intent_function are auto-detected from the app's deployment "
        "and manifest. Response includes ready_params — pass these directly to submit_order."
    ),
)
def get_quote(
    app_id: str,
    params: dict,
    intent_function: str = "execute",
    chain_id: int = 0,
    slippage_bps: int = 50,
) -> dict:
    """Get a quote for an intent execution.

    Args:
        app_id:           ID of the App Intent to quote.
        params:           Intent parameters (e.g. {"input_token": "0x...", "output_token": "0x...", "input_amount": "1000000"}).
        intent_function:  Intent function name (default: "execute").
        chain_id:         Target chain ID (default: 0 = auto-detect from deployment).
        slippage_bps:     Slippage tolerance in basis points for suggested_min_output (default: 50 = 0.5%).
    """
    return _post(f"/apps/{app_id}/quote", {
        "intent_function": intent_function,
        "params": params,
        "chain_id": chain_id,
        "slippage_bps": slippage_bps,
    })


@server.tool(
    name="cancel_order",
    description="Cancel an open order in the Intent OrderBook.",
)
def cancel_order(order_id: str) -> dict:
    """Cancel an order.

    Args:
        order_id: The order ID to cancel.
    """
    return _delete(f"/orders/{order_id}")


@server.tool(
    name="get_order_status",
    description=(
        "Check the current status of an order in the OrderBook. "
        "Returns a summary: status, score, tx_hash, error, and consensus "
        "result (quorum reached, number of approvals). "
        "Statuses: open → filled (success) or rejected (failure)."
    ),
)
def get_order_status(order_id: str) -> dict:
    """Get order status (summary view).

    Args:
        order_id: The order ID to check.
    """
    result = _get(f"/orders/{order_id}")
    if "error" in result:
        return result
    # Return a concise summary instead of the full order blob
    cr = result.get("consensus_result") or {}
    summary = {
        "order_id": result.get("order_id"),
        "status": result.get("status"),
        "score": result.get("score"),
        "tx_hash": result.get("tx_hash"),
        "block_number": result.get("block_number"),
        "error": result.get("error"),
        "consensus_reached": cr.get("reached"),
        "consensus_approvals": cr.get("collected"),
        "consensus_quorum": cr.get("quorum"),
        "app_id": result.get("app_id"),
        "chain_id": result.get("chain_id"),
        "submitted_by": result.get("submitted_by"),
        "created_at": result.get("created_at"),
    }
    # Include route info from plan metadata if available
    plan = result.get("plan")
    if plan and isinstance(plan, dict):
        meta = plan.get("metadata", {})
        summary["route"] = meta.get("route")
        summary["fee_tier"] = meta.get("fee_tier")
    return summary


@server.tool(
    name="list_orders",
    description="List orders in the OrderBook, optionally filtered by app_id and status.",
)
def list_orders_tool(app_id: str = "", status: str = "") -> dict:
    """List orders.

    Args:
        app_id: Filter by app ID. Empty for all.
        status: Filter by status. Empty for all.
    """
    return _get("/orders", app_id=app_id if app_id else None, status=status if status else None)


# ═══════════════════════════════════════════════════════════════════════════════
#                      MANIFEST & TESTNET TOOLS
# ═══════════════════════════════════════════════════════════════════════════════


@server.tool(
    name="get_bridge_status",
    description=(
        "Get bridge transfer status for a cross-chain order. "
        "Returns bridge protocol, source/destination chains, tracking "
        "poll count, and current order status. Only meaningful for "
        "orders that involve a bridge (status=bridging)."
    ),
)
def get_bridge_status(order_id: str) -> dict:
    """Get bridge status for a cross-chain order.

    Args:
        order_id: The order ID to check bridge status for.
    """
    return _get(f"/orders/{order_id}/bridge")


@server.tool(
    name="get_app_manifest",
    description=(
        "Extract and return the JS manifest for an app. "
        "The manifest contains intent function definitions, parameter schemas, "
        "example params, and scoring hints for miner plan generation."
    ),
)
def get_app_manifest(app_id: str) -> dict:
    """Get the manifest for an App Intent.

    Args:
        app_id: The app whose manifest to extract.
    """
    return _get(f"/apps/{app_id}/manifest")


@server.tool(
    name="testnet_faucet_eth",
    description=(
        "Fund an address with ETH on a local Anvil testnet fork. "
        "Uses anvil_setBalance to instantly set the balance. "
        "Supports multiple Anvil forks (ETH mainnet, Base, etc.). "
        "Only works when the local testnet is running (ANVIL_RPC_URL set)."
    ),
)
def faucet_eth(address: str, amount_eth: float = 10.0, chain_id: int = 0) -> dict:
    """Fund an address with ETH on Anvil.

    Args:
        address:    The 0x-prefixed Ethereum address to fund.
        amount_eth: Amount of ETH (default 10.0).
        chain_id:   Target chain (0=first available, 31337=ETH fork, 8453=Base fork).
    """
    body: dict = {"address": address, "amount_eth": amount_eth}
    if chain_id:
        body["chain_id"] = chain_id
    return _post("/testnet/faucet", body)


@server.tool(
    name="testnet_faucet_erc20",
    description=(
        "Fund an address with ERC-20 tokens on a local Anvil testnet fork. "
        "Uses anvil_setStorageAt to directly write token balances. "
        "Accepts token symbols (USDC), addresses (0x...), or chain-qualified (USDC@8453). "
        "Only works when the local testnet is running."
    ),
)
def faucet_erc20(
    token: str,
    address: str,
    amount: str,
    chain_id: int = 0,
) -> dict:
    """Fund an address with ERC-20 tokens on Anvil.

    Args:
        token:    Token identifier — symbol (USDC), 0x address, or chain-qualified (USDC@8453).
        address:  The 0x-prefixed recipient address.
        amount:   Amount in token's smallest unit as decimal string (e.g. "10000000000" for 10k USDC).
        chain_id: Target chain (0=first available, 31337=ETH fork, 8453=Base fork).
    """
    body: dict = {"token": token, "address": address, "amount": amount}
    if chain_id:
        body["chain_id"] = chain_id
    return _post("/testnet/faucet_erc20", body)


# ═══════════════════════════════════════════════════════════════════════════════
#                      SOLVER ROUND MANAGEMENT
# ═══════════════════════════════════════════════════════════════════════════════


@server.tool(
    name="get_solver_round",
    description=(
        "Get current solver round metadata including status, epoch, "
        "and submission counts."
    ),
)
def get_solver_round() -> dict:
    """Get current solver round metadata."""
    return _get("/solver/round")


@server.tool(
    name="get_champion",
    description=(
        "Get the current champion solver info including image_id, score, "
        "and adoption time."
    ),
)
def get_champion() -> dict:
    """Get the current champion solver info."""
    return _get("/solver/champion")


@server.tool(
    name="list_submissions",
    description="List solver submissions, optionally filtered by status.",
)
def list_submissions(status: str = "") -> dict:
    """List solver submissions.

    Args:
        status: Filter by submission status. Empty for all.
    """
    return _get("/submissions", status=status if status else None)


@server.tool(
    name="get_submission_status",
    description=(
        "Check the screening and benchmark status of a solver submission."
    ),
)
def get_submission_status(submission_id: str) -> dict:
    """Check a solver submission's screening and benchmark status.

    Args:
        submission_id: The submission ID to check.
    """
    return _get(f"/submissions/{submission_id}/status")


@server.tool(
    name="close_solver_round",
    description=(
        "Close the current solver round, triggering benchmark evaluation."
    ),
)
def close_solver_round() -> dict:
    """Close the current solver round."""
    return _post("/solver/round/close")


@server.tool(
    name="certify_solver_round",
    description=(
        "Certify the current round results, adopting the winning solver "
        "as champion."
    ),
)
def certify_solver_round() -> dict:
    """Certify the current round results."""
    return _post("/solver/round/certify")


@server.tool(
    name="abort_solver_round",
    description=(
        "Abort the current solver round without adopting a new champion."
    ),
)
def abort_solver_round() -> dict:
    """Abort the current solver round."""
    return _post("/solver/round/abort")


# ═══════════════════════════════════════════════════════════════════════════════
#                        NATIVE BITTENSOR
# ═══════════════════════════════════════════════════════════════════════════════


@server.tool(
    name="list_bittensor_permissions",
    description=(
        "List all Bittensor staking permissions (delegated proxy authorizations)."
    ),
)
def list_bittensor_permissions() -> dict:
    """List all Bittensor staking permissions."""
    return _get("/native-bittensor/permissions")


@server.tool(
    name="create_bittensor_permission",
    description=(
        "Create a permission for delegated Bittensor staking operations."
    ),
)
def create_bittensor_permission(
    wallet_address: str,
    netuid: int,
    action_type: str = "add_stake",
    hotkey: str = "",
) -> dict:
    """Create a Bittensor staking permission.

    Args:
        wallet_address: The wallet address to grant permission for.
        netuid:         Bittensor subnet UID.
        action_type:    Permission type (default: "add_stake").
        hotkey:         Optional hotkey to scope the permission to.
    """
    body: dict = {
        "wallet_address": wallet_address,
        "netuid": netuid,
        "action_type": action_type,
    }
    if hotkey:
        body["hotkey"] = hotkey
    return _post("/native-bittensor/permissions", body)


@server.tool(
    name="simulate_bittensor_swap",
    description=(
        "Simulate a TAO/Alpha swap on Bittensor to estimate output "
        "without executing."
    ),
)
def simulate_bittensor_swap(
    netuid: int,
    direction: str = "tao_to_alpha",
    amount: str = "1000000000",
) -> dict:
    """Simulate a TAO/Alpha swap on Bittensor.

    Args:
        netuid:    Bittensor subnet UID.
        direction: Swap direction ("tao_to_alpha" or "alpha_to_tao").
        amount:    Amount in smallest unit as decimal string.
    """
    return _post("/native-bittensor/sim-swap", {
        "netuid": netuid,
        "direction": direction,
        "amount": amount,
    })


@server.tool(
    name="execute_bittensor_stake",
    description=(
        "Execute a staking action on Bittensor (add_stake, remove_stake, "
        "move_stake)."
    ),
)
def execute_bittensor_stake(
    netuid: int,
    action: str,
    amount: str,
    hotkey: str = "",
) -> dict:
    """Execute a Bittensor staking action.

    Args:
        netuid: Bittensor subnet UID.
        action: Staking action ("add_stake", "remove_stake", "move_stake").
        amount: Amount in smallest unit as decimal string.
        hotkey: Optional hotkey for the staking operation.
    """
    body: dict = {
        "netuid": netuid,
        "action": action,
        "amount": amount,
    }
    if hotkey:
        body["hotkey"] = hotkey
    return _post("/native-bittensor/stake", body)



# ═══════════════════════════════════════════════════════════════════════════════
#                             ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════════════


def main() -> None:
    """Run the MCP server over stdio or SSE transport.

    Set MCP_TRANSPORT=sse and MCP_PORT=8090 for SSE mode.
    Default is stdio transport.
    """
    import asyncio

    transport = os.environ.get("MCP_TRANSPORT", "stdio").lower()

    if transport == "sse":
        host = os.environ.get("MCP_HOST", "0.0.0.0")
        port = int(os.environ.get("MCP_PORT", "8090"))
        asyncio.run(server.run_sse_async(host=host, port=port))
    else:
        asyncio.run(server.run_stdio_async())


if __name__ == "__main__":
    main()
