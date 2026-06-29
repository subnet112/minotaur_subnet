"""Prompt templates for LLM-driven strategy generation.

The system prompt provides only the mechanical interface — what a Strategy
class must look like, what types to use, what constraints to satisfy. No
reference implementations or worked examples. The miner LLM must figure
out the actual strategy logic from the app's Solidity code, ABI, and manifest.

Three prompt modes:
- System + generate/improve: Legacy stateless API prompts (backward compat)
- build_claude_md + build_*_task: Claude CLI prompts with MCP tool access
"""

from __future__ import annotations

import json
from pathlib import Path


_SDK_DIR = Path(__file__).resolve().parents[2] / "sdk"
_STRATEGY_SOURCE: str | None = None
_ABI_UTILS_SOURCE: str | None = None


def _get_strategy_source() -> str:
    global _STRATEGY_SOURCE
    if _STRATEGY_SOURCE is None:
        p = _SDK_DIR / "strategy.py"
        _STRATEGY_SOURCE = p.read_text() if p.exists() else ""
    return _STRATEGY_SOURCE


def _get_abi_utils_source() -> str:
    global _ABI_UTILS_SOURCE
    if _ABI_UTILS_SOURCE is None:
        p = _SDK_DIR / "abi_utils.py"
        _ABI_UTILS_SOURCE = p.read_text() if p.exists() else ""
    return _ABI_UTILS_SOURCE


# ── System prompt ───────────────────────────────────────────────────────────

def build_system_prompt() -> str:
    """Build the system prompt for strategy generation.

    Contains only the interface contract and type definitions. No examples,
    no hints about what a good strategy looks like. The LLM must derive
    that from the app's on-chain code and manifest.
    """
    strategy_abc = _get_strategy_source()
    abi_utils = _get_abi_utils_source()

    return f"""\
You are a DeFi strategy developer. You write Python Strategy classes that \
produce execution plans for on-chain intents.

You will receive an app's Solidity contract, ABI, and/or manifest. Your job \
is to read and understand the on-chain logic, then write a Strategy that \
generates execution plans the scoring function will rate highly.

## Strategy Interface

Your code must extend this base class:

```python
{strategy_abc}
```

## Output Types

```python
from minotaur_subnet.shared.types import ExecutionPlan, Interaction, AppIntentDefinition, IntentState

ExecutionPlan(
    intent_id=str,        # Must equal intent.app_id
    interactions=list,     # List of Interaction objects
    deadline=int,          # Unix timestamp, must be > snapshot.timestamp
    nonce=int,             # From state.nonce
    metadata=dict,         # Optional
)

Interaction(
    target=str,            # 0x-prefixed, 42 char hex address
    value=str,             # ETH value in wei as string
    call_data=str,         # 0x-prefixed ABI-encoded calldata
    chain_id=int,          # Target chain
)
```

## Available ABI Encoding Utilities

```python
{abi_utils}
```

You can also use `from eth_abi import encode` directly for custom encoding.

## Constraints

- intent_id MUST equal intent.app_id
- deadline MUST be > snapshot.timestamp
- All addresses: 0x-prefixed, 42 characters
- All calldata: 0x-prefixed hex
- File must end with: STRATEGY_CLASS = YourClassName
- Return ONLY Python code, no markdown fences or explanation"""


# ── User prompts ────────────────────────────────────────────────────────────

def build_generate_prompt(
    app_id: str,
    name: str,
    description: str,
    intent_type: str,
    supported_chains: list[int],
    solidity_code: str | None = None,
    manifest: dict | None = None,
    abi: list | None = None,
) -> str:
    """Build user prompt for generating a new strategy."""
    sections = [
        f"Write a Strategy for this app:\n",
        f"App ID: {app_id}",
        f"Name: {name}",
        f"Type: {intent_type}",
        f"Description: {description}",
        f"Chains: {supported_chains}",
    ]

    if solidity_code:
        sections.append(f"\nSolidity contract:\n```solidity\n{solidity_code}\n```")

    if manifest:
        sections.append(f"\nManifest:\n```json\n{json.dumps(manifest, indent=2)}\n```")

    if abi:
        sections.append(f"\nABI:\n```json\n{json.dumps(abi, indent=2)}\n```")

    return "\n".join(sections)


def build_improve_prompt(
    app_id: str,
    current_code: str,
    avg_score: float,
    best_score: float,
    recent_scores: list[float],
    trend: str,
    solidity_code: str | None = None,
    manifest: dict | None = None,
) -> str:
    """Build user prompt for improving an existing strategy."""
    sections = [
        f"Improve this strategy for app {app_id}.\n",
        f"Average score: {avg_score:.3f}",
        f"Best score: {best_score:.3f}",
        f"Recent scores: {recent_scores}",
        f"Trend: {trend}",
        f"\nCurrent code:\n```python\n{current_code}\n```",
    ]

    if solidity_code:
        sections.append(f"\nSolidity contract:\n```solidity\n{solidity_code}\n```")

    if manifest:
        sections.append(f"\nManifest:\n```json\n{json.dumps(manifest, indent=2)}\n```")

    return "\n".join(sections)


# ── Claude CLI prompts (MCP-aware) ─────────────────────────────────────────


def build_claude_md() -> str:
    """Build CLAUDE.md content for the strategies workspace.

    This file is placed in the strategies/ directory and read automatically
    by Claude Code when spawned via `claude -p`. It contains the Strategy
    interface, output types, ABI utilities, available MCP tools, and workflow.
    """
    strategy_abc = _get_strategy_source()
    abi_utils = _get_abi_utils_source()

    return f"""\
# Strategy Development Instructions

You are writing Python Strategy classes for the Minotaur solver network.
Each strategy generates execution plans for a specific on-chain app (DeFi protocol).

## Workflow

1. **Research the app**: Use `get_app_details` to read the Solidity contract, ABI, and manifest.
2. **Search for docs**: Use WebSearch to find documentation for the DeFi protocol.
3. **Write the strategy**: Create `{{app_id}}/strategy.py` with a Strategy subclass.
4. **Test it**: Use `test_strategy` to validate structure and plan generation.
5. **Fix failures**: Read error messages, fix the code, re-test.
6. **Save when passing**: The final strategy.py must pass `test_strategy`.

## Strategy Interface

```python
{strategy_abc}
```

## Output Types

```python
from minotaur_subnet.shared.types import ExecutionPlan, Interaction, AppIntentDefinition, IntentState

ExecutionPlan(
    intent_id=str,        # Must equal intent.app_id
    interactions=list,     # List of Interaction objects
    deadline=int,          # Unix timestamp, must be > snapshot.timestamp
    nonce=int,             # From state.nonce
    metadata=dict,         # Optional
)

Interaction(
    target=str,            # 0x-prefixed, 42 char hex address
    value=str,             # ETH value in wei as string
    call_data=str,         # 0x-prefixed ABI-encoded calldata
    chain_id=int,          # Target chain
)
```

## ABI Encoding Utilities

```python
{abi_utils}
```

You can also use `from eth_abi import encode` directly.

## Chain Investigation

Before writing strategy code, **investigate the actual chain state** to understand
what contracts and liquidity exist. Minotaur apps can involve any DeFi interaction:
swaps, lending, vaults, arbitrage, staking, options, etc. Your strategy must
generate plans that actually execute on-chain.

```bash
# Verify a contract exists
get_contract_code("0xAddress")

# Read view functions on any contract (auto-decode with return types)
read_contract("0xAddress", "balanceOf(address)(uint256)", ["0xHolder"])
read_contract("0xAddress", "getReserves()(uint112,uint112,uint32)")

# Batch multiple queries
multicall_read('[{{"address":"0xA","function_sig":"name()(string)"}},{{"address":"0xB","function_sig":"totalSupply()(uint256)"}}]')

# Scan events
get_logs("0xAddress", ["0xTransferEventTopic"], from_block="latest-1000")

# Token info
resolve_token("USDC")
get_token_info("0xTokenAddress")
```

Well-known contract addresses (Ethereum mainnet / Anvil fork):
- Uniswap V3 Factory: `0x1F98431c8aD98523631AE4a59f267346ea31F984`
- Uniswap V3 SwapRouter: `0xE592427A0AEce92De3Edee1F18E0157C05861564`
- Aave V3 Pool: `0x87870Bca3F3fD6335C3F4ce8392D69350B4fA4E2`
- Compound V3 (cUSDC): `0xc3d688B66703497DAA19211EEdff47f25384cdc3`
- WETH: `0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2`
- USDC: `0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48`

**ALWAYS verify on-chain state before hardcoding values.** Use `read_contract`
to check pool liquidity, oracle prices, protocol parameters, etc. Hardcoded
assumptions (like fee tiers) will fail when the actual chain state differs.

## Python Environment

You have access to `Bash` (unrestricted) with these packages pre-installed:
- **web3** 7.14.1 — `from web3 import Web3`
- **eth_abi** 5.2.0 — `from eth_abi import encode, decode`
- **eth_hash** — `from eth_hash.auto import keccak`

You can also use `cast` (Foundry CLI) for direct chain queries:
```bash
# Query a contract directly
cast call 0xFactoryAddr "getPool(address,address,uint24)(address)" 0xTokenA 0xTokenB 500 --rpc-url http://localhost:18545

# Get pool liquidity
cast call 0xPoolAddr "liquidity()(uint128)" --rpc-url http://localhost:18545
```

Use Python for ABI encoding and computation:
```bash
python3 -c "from eth_abi import encode; print('0x' + encode(['uint256'], [100]).hex())"
python3 -c "from eth_hash.auto import keccak; print('0x' + keccak(b'Transfer(address,address,uint256)').hex())"
```

## Available MCP Tools

### App Research
- `get_app_details(app_id)` -- metadata + ABI + manifest (no Solidity source — call get_app_solidity if needed)
- `get_app_solidity(app_id)` -- the full Solidity source (10-20 KB). Only call when the ABI + manifest isn't enough.
- `get_app_scores(app_id)` -- execution stats (avg_score, best_score, recent_scores)
- `list_available_apps()` -- all active apps on the network
- `list_orders(app_id, status)` -- current OrderBook state

### Strategy Development
- `test_strategy(app_id, code)` -- structural validation, returns passed/message
- `score_strategy(app_id, code, scenario_name="")` -- run ONE scenario through the validator's scoreIntent path (~3s). Fast; use for iteration. Pass scenario_name to target a specific manifest scenario.
- `score_strategy_all(app_id, code)` -- run EVERY chain-matching benchmark_scenario (~30-60s). Returns per-scenario scores + aggregate — this is the same scoring the validator runs at submission time. Use as the final check before accepting a strategy.
- `list_strategies()` -- list saved strategies on disk
- `get_score_feedback(app_id)` -- score analysis with trend detection

### On-Chain State
- `read_contract(address, function_sig, args, block)` -- call view functions with optional auto-decode. Add return types for auto-decode: `"balanceOf(address)(uint256)"` returns `{{"raw": "0x...", "decoded": ["100"]}}`
- `get_token_balance(token_address, holder_address)` -- ERC-20 balance with decimals, symbol, and formatted amount
- `resolve_token(token, chain_id)` -- instant registry lookup, no RPC. Resolves symbols (USDC) or addresses to full token info. Supports ETH→WETH.
- `get_token_info(token_address)` -- full ERC-20 metadata: name, symbol, decimals, totalSupply in one call
- `multicall_read(calls)` -- batch up to 20 view calls in one tool invocation. Input: JSON array of `{{"address", "function_sig", "args"}}` objects.
- `get_logs(address, topics, from_block, to_block, max_logs)` -- scan event logs via eth_getLogs
- `get_contract_code(address)` -- check if address is a contract (returns is_contract, code_size)

## Research Workflow

When researching protocols for strategy development, follow this order:

1. **`resolve_token`** first — get addresses for well-known tokens (USDC, WETH, etc.) without any RPC calls
2. **`get_contract_code`** — verify that an address is actually a contract before calling it
3. **`read_contract`** with return types — use `"function(args)(returns)"` syntax for auto-decoded results
4. **`multicall_read`** — batch multiple queries into one tool call (e.g. read pool state + token balances)
5. **`get_logs`** — scan for events (token transfers, swaps, etc.) to understand protocol activity
6. **`Bash(python3 ...)`** — use for complex computation (ABI encoding, math, keccak hashes)
7. **WebSearch** last — use for documentation and protocol specs, not for addresses or on-chain data

## Constraints

- `intent_id` MUST equal `intent.app_id`
- `deadline` MUST be > `snapshot.timestamp`
- All addresses: 0x-prefixed, 42 characters
- All calldata: 0x-prefixed hex
- File must end with: `STRATEGY_CLASS = YourClassName`
- The strategy file must be self-contained (all imports at the top)

## State Parameters

Prefer `state.typed_context` when it is available. Validators may attach typed
runtime views such as `SwapIntentContext`, `TwapIntentContext`, and
`RebalanceIntentContext`.

The structured runtime contract is:
- `state.typed_context`: authoritative typed app/runtime view
- `state.raw_params`: raw app/runtime params payload
- `state.control`: runtime control metadata such as `_intent_function`

## Common Patterns

```python
# Prefer typed params, fall back to raw structured payload
typed = getattr(state, "typed_context", None)
raw = getattr(state, "raw_params", {{}}) or {{}}
control = getattr(state, "control", {{}}) or {{}}
input_token = getattr(typed, "input_token", "") or raw.get("input_token", "")
output_token = getattr(typed, "output_token", "") or raw.get("output_token", "")
input_amount = getattr(typed, "input_amount", 0) or int(raw.get("input_amount", "0"))

intent_function = (
    getattr(typed, "intent_function", "")
    or control.get("_intent_function", "")
    or raw.get("intent_function", "")
)

# Build interactions
from common.abi_utils import encode_approve, encode_exact_input_single

interactions = [
    Interaction(
        target=input_token,
        value="0",
        call_data=encode_approve(router, int(input_amount)),
        chain_id=state.chain_id or 1,
    ),
    Interaction(
        target=router,
        value="0",
        call_data=encode_exact_input_single(...),
        chain_id=state.chain_id or 1,
    ),
]
```
"""


def build_generate_task(
    app_id: str,
    name: str,
    description: str,
    intent_type: str,
    supported_chains: list[int],
) -> str:
    """Build a task prompt for generating a new strategy via Claude CLI.

    Args:
        app_id: The app identifier.
        name: Human-readable app name.
        description: App description.
        intent_type: Type of intent (swap, vault, limit_order, etc).
        supported_chains: Supported chain IDs.
    """
    return f"""\
You are the **root miner agent** for Minotaur. Your job is to produce a
working `strategy.py` for app "{name}" (app_id: {app_id}, chains: {supported_chains})
by orchestrating the specialised sub-agents. You do NOT analyse or write
code directly — delegate.

Strategy file path: `{app_id}/strategy.py`

## Orchestration plan

1. **Delegate to `analyzer`**: have it read the current strategy (if any)
   and produce a diagnosis. For a brand-new app with no strategy yet,
   tell the analyzer "there is no current strategy; do a fresh read of
   the app manifest + contract and propose a first-pass design."
2. **Delegate to `strategy-writer`**: pass the diagnosis + the strategy
   file path. It writes the first version and saves to disk.
3. **Delegate to `benchmark-runner`**: it runs `score_strategy_all` and
   reports per-scenario scores.
4. **Decide**: if the aggregate is decent (>= 0.5 and above the champion
   score for at least one scenario), accept and stop.
   Otherwise, delegate back to `analyzer` with the benchmark report and
   go to step 2. Do at most TWO iteration loops total.
5. Report a short summary: final aggregate score, which scenarios improved.

## Constraints
- Do not call MCP tools yourself. The sub-agents have them.
- Do not edit the strategy file directly. The writer does that.
- Do not call score_strategy. The runner does that.
- **Build contract**: submissions build with `docker build --network=none`.
  The base image already has web3, eth-account, eth-keys, hexbytes,
  aiohttp, requests, numpy, pandas, pydantic. Never edit `Dockerfile`
  or `requirements.txt` — those are out of your scope.
- Use `Agent` for delegation, `TodoWrite` if you want to track plan
  state, `Read` only to confirm artifacts exist.

Budget: up to 20 minutes total. Keep orchestration under 2 minutes of
your own time — the sub-agents do the real work.
"""


def build_improve_task(
    app_id: str,
    avg_score: float,
    best_score: float,
    trend: str,
    recent_scores: list[float],
    champion_score: float = 0.0,
    target_score: float = 0.0,
    scenario_scores: dict[str, float] | None = None,
    quote_failure_rate: float = 0.0,
    recent_quote_errors: list[str] | None = None,
    last_score: float = 0.0,
    last_score_message: str = "",
    relative: dict | None = None,
    verdict: str = "",
    relative_headroom: float = 1.0,
) -> str:
    """Build a task prompt for improving an existing strategy via Claude CLI.

    Args:
        app_id: The app identifier.
        avg_score: Current average score (post-cutover: on-chain BPS, not 0..1).
        best_score: Current best score (post-cutover: on-chain BPS).
        trend: Trend (improving/declining/stable) of the better/compared ratio.
        recent_scores: Recent score values.
        champion_score: Vestigial — the champion has no absolute score now (it's
            the relative baseline). Kept for back-compat display only.
        target_score: Vestigial dethrone target; the real bar is the relative
            verdict (beat the champion on every order).
        scenario_scores: Per-scenario scores from champion's benchmark.
        quote_failure_rate: Fraction of quotes that failed (0.0-1.0).
        recent_quote_errors: Recent quote error strings.
        relative: Per-submission RELATIVE COUNTS vs the champion
            ({better, worse, matched, new, compared, verdict}) — the
            authoritative head-to-head signal. None until first benched.
        verdict: "dethrone" | "matched" | "behind" from the relative counts.
        relative_headroom: Fraction of orders NOT yet beating the champion.
    """
    # Post relative-cutover the adoption bar is the RELATIVE per-order verdict
    # (beat the champion on every order, strictly win ≥1), not a numeric score.
    # Lead with the counts when we have them; the 0..1 score numbers are now
    # saturated validity sentinels so they're shown only as secondary context.
    if relative:
        head = f"""\
You are the **root miner agent** for Minotaur. An existing strategy for
app `{app_id}` is live. Your job is to produce an improved `strategy.py`
by orchestrating the specialised sub-agents — you do NOT analyse or
write code yourself.

Strategy file path: `{app_id}/strategy.py`

Standing vs the champion (RELATIVE per-order counts — the adoption bar):
- better={relative.get('better', 0)}  worse={relative.get('worse', 0)}  \
matched={relative.get('matched', 0)}  new(blind-spot covers)={relative.get('new', 0)}  \
compared={relative.get('compared', 0)}
- verdict: {verdict or relative.get('verdict', '?')}  \
(headroom: {relative_headroom:.0%} of orders not yet beating the champion)
- To ADOPT you must beat the champion on EVERY order (0 'worse') AND strictly
  win at least one — matching everywhere is NOT enough. Trend of orders-won: {trend}."""
    else:
        head = f"""\
You are the **root miner agent** for Minotaur. An existing strategy for
app `{app_id}` is live. Your job is to produce an improved `strategy.py`
by orchestrating the specialised sub-agents — you do NOT analyse or
write code yourself.

Strategy file path: `{app_id}/strategy.py`

No relative counts yet (not benched since the cutover). The adoption bar is
the RELATIVE per-order rule: beat the current champion on every order and
strictly win at least one. Secondary context (saturated validity sentinels,
not quality grades): recent={recent_scores}, trend={trend}."""
    sections = [head]

    if scenario_scores:
        sorted_scenarios = sorted(scenario_scores.items(), key=lambda x: x[1])
        lines = ["", "Per-scenario champion outputs (lowest first — likely where you can win):"]
        for scenario_id, score in sorted_scenarios[:8]:
            label = scenario_id.split(":", 1)[-1] if ":" in scenario_id else scenario_id
            lines.append(f"  - {label}: {score:.3f}")
        sections.append("\n".join(lines))

    if last_score_message:
        sections.append(
            f"\nLast score result from the validator (score={last_score:.4f}):\n"
            f"  {last_score_message[:300]}"
        )

    if quote_failure_rate > 0:
        sections.append(
            f"\nLive quote failure rate: {quote_failure_rate:.0%}. "
            f"Recent errors (pass to analyzer as additional context): "
            f"{(recent_quote_errors or [])[:5]}"
        )

    sections.append(f"""
## Orchestration plan

1. **Delegate to `analyzer`**: ask it to read `{app_id}/strategy.py`
   plus the scenario outputs above and produce a prioritised diagnosis of
   which orders the champion still beats us on (the 'worse'/'matched' orders
   are the targets). Pass along any recent quote errors.
2. **Delegate to `strategy-writer`**: pass the analyzer's diagnosis +
   strategy file path. It edits the file and verifies structural
   validity with `test_strategy`.
3. **Delegate to `benchmark-runner`**: it runs `score_strategy_all` and
   reports per-scenario delivered outputs.
4. **Decide** (relative per-order rule — there is no aggregate score bar):
   - If you out-deliver the champion on at least one order and regress on
     NONE (no order delivers strictly less than the champion's): DONE.
   - If you still lose/tie on some orders and have budget left: feed the
     report back to `analyzer` for one more iteration (step 2 → 3),
     targeting the orders where the champion still delivers more.
   - Hard cap: max 2 write+benchmark iterations.
5. Report a short summary: which orders now out-deliver the champion, which
   still lose/tie.

## Constraints
- You do NOT call MCP tools yourself. Sub-agents have them.
- You do NOT edit `strategy.py`, `Dockerfile`, or `requirements.txt`.
- **Build contract**: the validator builds submissions with
  `docker build --network=none`. pip cannot reach PyPI. The base image
  already has web3, eth-account, eth-keys, hexbytes, aiohttp, requests,
  numpy, pandas, pydantic. If a sub-agent reports "I need dep X that
  isn't in the base", treat that as a blocker and stop — do not allow
  it to modify Dockerfile/requirements.txt. Adding a base dep is an
  operator task, not a miner task.
- Use `Agent` to delegate, `TodoWrite` to track the plan, `Read` only
  to check artifacts.

Budget: up to 20 minutes. Orchestration should take < 2 min of your
own time; the rest is sub-agent work.""")

    return "\n".join(sections)
