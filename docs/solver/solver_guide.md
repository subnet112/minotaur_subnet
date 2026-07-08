# Minotaur Solver Guide

## Table of Contents

- [Overview](#overview)
- [IntentSolver ABC](#intentsolver-abc)
  - [Lifecycle](#lifecycle)
  - [Core Methods](#core-methods)
- [Data Types](#data-types)
  - [ExecutionPlan](#executionplan)
  - [Interaction](#interaction)
  - [MarketSnapshot](#marketsnapshot)
  - [IntentState](#intentstate)
  - [SolverMetadata](#solvermetadata)
- [Strategy ABC](#strategy-abc)
- [RoutingSolver](#routingsolver)
- [Reference Solver + Helpers](#reference-solver--helpers)
- [Docker Requirements](#docker-requirements)
- [Screening Pipeline](#screening-pipeline)
- [Benchmarking](#benchmarking)
- [JSON-over-stdio Protocol](#json-over-stdio-protocol)
- [Security](#security)
- [Agentic Solver Development](#agentic-solver-development)

---

## Overview

Miners on Minotaur compete by writing the best **Solving Engine** — code that generates optimal execution plans for App Intents. The Solving Engine is a single engine that handles all Apps across the entire network. Validators run the winning solver in sandboxed Docker containers, benchmark it against active intents, and adopt a challenger that is **net better than the current champion** on delivered output — or, on a fully-matched tie, cheaper/cleaner on the tie-break ladder (see [Benchmarking](#benchmarking)).

The competition surface is the `IntentSolver` abstract base class. Miners extend it, package their code in a Docker image, and submit it. Validators screen the submission (3 stages), benchmark it against the current champion, and adopt it if it scores better.

## IntentSolver ABC

**Module:** `minotaur_subnet.sdk.intent_solver`

The `IntentSolver` is the core competition surface. Miners extend this class to build solving strategies.

```python
from minotaur_subnet.sdk.intent_solver import IntentSolver, MarketSnapshot, SolverMetadata
from minotaur_subnet.shared.types import AppIntentDefinition, ExecutionPlan, IntentState

class MySolver(IntentSolver):
    def initialize(self, config):
        self.rpc_urls = config.get("rpc_urls", {})

    def generate_plan(self, intent, state, snapshot=None):
        # Build an execution plan for this intent
        ...

    def metadata(self):
        return SolverMetadata(
            name="my-solver",
            version="1.0.0",
            author="5Grwva...",
            supported_intent_types=["swap"],
        )

# Required: tells the harness which class to instantiate
SOLVER_CLASS = MySolver
```

### Lifecycle

The validator runs your solver through this lifecycle for each benchmark round:

1. **`initialize(config)`** — Called once when the solver is loaded. The `config` dict contains:
   - `chain_ids: list[int]` — Chains to support
   - `rpc_urls: dict[int, str]` — RPC URL per chain ID (e.g., `{1: "http://localhost:8545"}`)
   - `timeout_per_plan_ms: int` — Per-plan time budget (default: 30000)
   - `supported_protocols: list[str]` — Available DEX protocols

2. **`restore_state(data)`** — Called if serialized state from a prior epoch exists

3. **`on_benchmark_start(intent_count)`** — Before the benchmark batch begins. Use this for pre-computing shared data structures or warming caches.

4. **`generate_plan(intent, state, snapshot)`** — Called per intent. This is the core competition surface.

5. **`check_trigger(intent, state, snapshot)`** — Called for auto-triggered intents to check if conditions are met.

6. **`on_benchmark_end(results)`** — After the batch completes. Receives a list of `{intent_id, score, elapsed_ms}` dicts.

7. **`serialize_state()`** — Persist learned state for the next epoch (max 50MB).

### Core Methods

#### `initialize(config: dict) -> None` (required)

One-time setup. Store RPC URLs, build routing tables, load ML models.

```python
def initialize(self, config):
    self.rpc_urls = config.get("rpc_urls", {})
    self.chain_ids = config.get("chain_ids", [1])
    # Create Web3 instances, build routing tables, etc.
```

Any exception during `initialize()` causes the solver to fail screening (Stage 2).

#### `generate_plan(intent, state, snapshot) -> ExecutionPlan` (required)

Generate an execution plan for the given intent. Prefer querying on-chain state via RPC URLs from `initialize()`. Fall back to snapshot data when RPC is unavailable.

```python
def generate_plan(self, intent, state, snapshot=None):
    if getattr(state, "typed_context", None) is not None:
        params = state.typed_context.raw_params
    else:
        params = state.raw_params

    # Query live pool states via RPC
    pools = self.query_pools(state.chain_id)
    route = self.find_best_route(pools, params)

    return ExecutionPlan(
        intent_id=intent.app_id,
        interactions=route.to_interactions(),
        deadline=int(time.time()) + 300,
        nonce=state.nonce,
    )
```

Any exception results in a score of 0.0 for this intent. The process is killed if execution exceeds the per-plan timeout (30s default).

#### `quote(intent, state, snapshot) -> QuoteResult` (optional)

Compute a quote without generating a full execution plan. Override for fast quoting support.

#### `check_trigger(intent, state, snapshot) -> bool` (optional)

For auto-triggered (perpetual) intents: return `True` when conditions are met and the intent should execute. Default returns `False`.

#### `metadata() -> SolverMetadata` (required)

Return solver identification and capabilities. Used for logging, benchmarking reports, and miner attribution.

#### `serialize_state() -> bytes` / `restore_state(data: bytes)` (optional)

Persist and restore learned state across epochs. Use this for ML model weights, routing tables, or parameter tuning data.

## Data Types

### ExecutionPlan

**Module:** `minotaur_subnet.shared.types`

The output of `generate_plan()`. Defines the exact on-chain calls to execute.

```python
@dataclass
class ExecutionPlan:
    intent_id: str                    # Which intent this plan fulfills (app_id)
    interactions: list[Interaction]   # Ordered calls to execute
    deadline: int                     # Unix timestamp — plan expires after this
    nonce: int                        # Replay protection
    metadata: dict[str, Any] = {}     # App-specific data (plan_type, route info, etc.)
```

### Interaction

A single on-chain call in an execution plan.

```python
@dataclass
class Interaction:
    target: str       # Contract address (0x-prefixed, 42 chars)
    value: str        # Wei value as decimal string ("0" for no ETH)
    call_data: str    # ABI-encoded calldata (0x-prefixed hex)
    chain_id: int     # Target chain (default: 1)
```

### MarketSnapshot

Point-in-time market data for plan generation. Used primarily for benchmarking and as a fallback when RPC access is unavailable. Production solvers should prefer querying on-chain state directly via RPC URLs provided in `initialize()`.

```python
@dataclass
class MarketSnapshot:
    chain_id: int                              # Target chain ID
    block_number: int                          # Block at which this snapshot was taken
    timestamp: int                             # Unix timestamp of the snapshot block
    prices: dict[str, float] = {}              # Token price feeds (e.g., {"ETH/USD": 1850.0})
    pool_states: dict[str, dict] = {}          # DEX pool states keyed by pool address
    balances: dict[str, str] = {}              # Token balances keyed by token address
    dex_config: dict[str, Any] = {}            # DEX router/factory addresses
    raw_state: dict[str, Any] = {}             # Additional contract storage
```

Pool states contain protocol-specific data:
- **Uniswap V3:** `token0`, `token1`, `fee`, `sqrtPriceX96`, `liquidity`
- **Uniswap V2:** `token0`, `token1`, `reserve0`, `reserve1`

### IntentState

Current on-chain state of an App Intent contract.

```python
@dataclass
class IntentState:
    contract_address: str                      # App's deployed contract address
    chain_id: int                              # Chain where the contract lives
    nonce: int                                 # Current nonce for replay protection
    owner: str                                 # Contract owner address
    raw_params: dict[str, Any] = {}            # Canonical raw app/runtime params
    control: dict[str, Any] = {}               # Runtime control metadata
    extra: dict[str, Any] = {}                 # Derived compatibility payload
    typed_context: Any | None = None           # Preferred typed runtime view when available
```

Prefer `typed_context` when it is present. It exposes manifest-driven typed fields
such as `SwapIntentContext`, `TwapIntentContext`, and `RebalanceIntentContext`.
New solver code should read untyped values from `raw_params` and runtime metadata
such as `_intent_function` from `control`. `extra` remains a compatibility view only.

### SolverMetadata

Solver identification and capabilities.

```python
@dataclass
class SolverMetadata:
    name: str                                  # Human-readable name
    version: str                               # Semantic version (e.g., "2.1.0")
    author: str                                # Miner hotkey or identifier
    description: str = ""                      # Brief description
    supported_chains: list[int] = [1]          # Chain IDs this solver supports
    supported_intent_types: list[str] = ["swap"]  # Intent types handled
```

## Strategy ABC

**Module:** `minotaur_subnet.sdk.strategy`

A `Strategy` is a lightweight, app-specific plan generator. Unlike `IntentSolver` (which handles lifecycle, serialization, benchmarking), `Strategy` focuses on one thing: generating plans for a specific app.

```python
from minotaur_subnet.sdk.strategy import Strategy

class MyVaultStrategy(Strategy):
    APP_ID = "vault-abc123"
    INTENT_FUNCTIONS = ["buyDip"]  # Empty list = handle all functions

    def generate_plan(self, intent, state, snapshot=None):
        return ExecutionPlan(
            intent_id=intent.app_id,
            interactions=[...],
            deadline=int(time.time()) + 300,
            nonce=state.nonce,
        )

    def check_trigger(self, intent, state, snapshot=None):
        # For perpetual orders: check if conditions are met
        return self.should_buy(state)

STRATEGY_CLASS = MyVaultStrategy
```

Key attributes:
- **`APP_ID`** — The app_id this strategy handles. Must be set.
- **`INTENT_FUNCTIONS`** — List of intent function names. Empty list means handle all functions.
- **`accepts(app_id, intent_function)`** — Checks if this strategy handles the given app/function.

## RoutingSolver

**Module:** `minotaur_subnet.sdk.routing_solver`

The `RoutingSolver` is an `IntentSolver` that dispatches `generate_plan()` calls to registered `Strategy` instances based on `app_id`. This is the recommended pattern for solvers that handle multiple apps.

```python
from minotaur_subnet.sdk.routing_solver import RoutingSolver

solver = RoutingSolver()
solver.register_strategy(MySwapStrategy())
solver.register_strategy(MyVaultStrategy())
solver.initialize({"chain_ids": [1], "rpc_urls": {1: "http://localhost:8545"}})

# Dispatches to MySwapStrategy or MyVaultStrategy based on intent.app_id
plan = solver.generate_plan(intent, state, snapshot)
```

When no strategy matches, the RoutingSolver generates a minimal fallback plan (WETH deposit). This plan scores low but passes structural validation, ensuring the solver never crashes on unknown apps.

The `RoutingSolver` class is exported as `SOLVER_CLASS` in the module, making it the default submission target.

## Reference Solver + Helpers

This SDK ships only the abstract interfaces — `IntentSolver`, `IntentProcessor`,
`Strategy`, `RoutingSolver` — plus the data types (`ExecutionPlan`,
`Interaction`, etc.) and a thin selectors-only `abi_utils` shim.

The reference DEX-aggregator solver, the Uniswap V3 / Aerodrome routing math,
the V3 calldata encoders, and the per-app strategy modules all live in a
**separate repository** that miners fork:

> **[`subnet112/minotaur-solver`](https://github.com/subnet112/minotaur-solver)**

Key directories there:

| Path | What's in it |
|---|---|
| `solver.py` | The `MinerSolver` entry that exports `SOLVER_CLASS`. Fork target. |
| `common/abi_utils.py` | `encode_approve` (generic ERC-20). |
| `common/parsing.py` | App-agnostic input normalisation. |
| `strategies/dex_aggregator/baseline_solver.py` | The reference DEX baseline miners are trying to beat — RPC-first pool discovery, V3 math, multi-hop routing across Uniswap V3 + Aerodrome Slipstream on Base. |
| `strategies/dex_aggregator/aerodrome.py` | Aerodrome Slipstream pool discovery + calldata. |
| `strategies/dex_aggregator/pool_math.py` | Uniswap V3 single-tick math + best-pool / best-route finder. |
| `strategies/dex_aggregator/swap_solver.py` | Single-hop V3 plan emitter. |
| `strategies/dex_aggregator/v3_codec.py` | Uniswap V3 SwapRouter calldata encoders (V1/V2 auto-select, multi-hop path). |
| `strategies/dex_aggregator/uniswap_v3.py`, `token_math.py` | Per-strategy helpers. |
| `strategies/<other_app>/` | Miner-defined per-app strategy modules. |

The split is intentional — miners own and improve everything in
`strategies/`, while the SDK in this repo just ships the contracts (ABCs +
types) the validator harness needs in order to load and run any solver.

Example — building a single-hop swap interaction using the solver-repo helpers:

```python
from common.abi_utils import encode_approve
from strategies.dex_aggregator.v3_codec import encode_exact_input_single
from minotaur_subnet.shared.types import Interaction

USDC = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
WETH = "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"
ROUTER = "0xE592427A0AEce92De3Edee1F18E0157C05861564"

interactions = [
    # 1. Approve the router to spend USDC.
    Interaction(
        target=USDC,
        value="0",
        call_data=encode_approve(ROUTER, 1_000_000_000),
    ),
    # 2. Swap USDC -> WETH via the 0.3% pool.
    Interaction(
        target=ROUTER,
        value="0",
        call_data=encode_exact_input_single(
            token_in=USDC,
            token_out=WETH,
            fee=3000,
            recipient=contract_address,
            deadline=int(time.time()) + 300,
            amount_in=1_000_000_000,
            amount_out_minimum=0,
            chain_id=1,
        ),
    ),
]
```

## Docker Requirements

Solver submissions are packaged as Docker images. The requirements are strict:

### Required Files

- **`Dockerfile`** — Must use the official base image
- **`solver.py`** — Must export `SOLVER_CLASS` (an `IntentSolver` subclass)
- **`README.md`** — Description of the solver's approach

### Dockerfile Rules

```dockerfile
FROM ghcr.io/subnet112/solver-base:v1

COPY . /app
WORKDIR /app
RUN pip install -r requirements.txt
```

**Required:**
- `FROM ghcr.io/subnet112/solver-base:v1` — Must use the official base image

**Forbidden:**
- `CMD` or `ENTRYPOINT` directives — The harness manages the entry point

### Size Limits

- Maximum repo size: **100 MB**
- Maximum single binary (outside `models/`): **10 MB**
- Maximum serialized state: **50 MB**

### solver.py Entry Point

Your `solver.py` must export a `SOLVER_CLASS` variable pointing to your `IntentSolver` subclass:

```python
from minotaur_subnet.sdk.intent_solver import IntentSolver, SolverMetadata

class MySolver(IntentSolver):
    # ... implementation ...

SOLVER_CLASS = MySolver
```

## Screening Pipeline

Before a solver reaches benchmarking, it passes through a 3-stage screening pipeline that filters broken, malformed, or malicious submissions.

### Stage 1 — Static Checks (~10s)

No Docker required. Validates:
- Required files exist (`Dockerfile`, `solver.py`, `README.md`)
- Dockerfile uses `FROM ghcr.io/subnet112/solver-base`
- No `CMD` or `ENTRYPOINT` in Dockerfile
- Repo size is within limits (100 MB)
- No suspicious binaries larger than 10 MB (outside `models/`)

### Stage 2 — Build Check (~2 min)

Builds the Docker image and verifies the solver can be imported and initialized:
1. `docker build --network=none --memory=4g`
2. Import check: `from solver import SOLVER_CLASS`
3. Init check: `SOLVER_CLASS().initialize({"chain_ids": [1]})`
4. Metadata validation: `name` and `version` must be non-empty
5. Must be a subclass of `IntentSolver`

### Stage 3 — Smoke Test (~5 min)

Runs 3 synthetic intents and verifies valid plans:
- Plans must have non-empty `interactions`
- `intent_id` must match the intent's `app_id`
- `deadline` must be in the future (relative to snapshot timestamp)
- All interaction `target` addresses must be valid (0x-prefixed, 42 chars)
- All `call_data` must be 0x-prefixed hex
- For auto-triggered intents: `check_trigger()` must return a boolean

## Benchmarking

After passing screening, solvers enter the benchmarking phase where they compete against the current champion.

### Champion / Challenger Model

- The **champion** is the currently active solver used for live order processing
- A new submission is the **challenger**
- Both are benchmarked against the same set of active intents
- Scoring is **relative and per-order**: the challenger is compared to the champion order by order, not by an absolute number. The champion is the baseline — it has no score of its own.

### Relative, per-order scoring

There is no absolute 0–1 score. For every order in the benchmark set the challenger's result is compared to the champion's at the **same block pin**:

| Per-order outcome | Meaning |
|-------------------|---------|
| `win` | challenger delivered **more** (beyond the ±0.1% / 10 bps tie band) |
| `regression` | challenger delivered **less** (beyond the band; tolerated only within the 1% floor) |
| `matched` | within the ±0.1% band (effectively tied) |
| `blind_spot_cover` | champion can't serve this order at all; the challenger can (counts as a win) |
| `dropped` | champion serves it; the challenger produced nothing (a hard veto) |
| `skip` | neither side produced comparable output |

Adoption resolves a fixed ladder (exact-integer, cross-multiplied wei — so the verdict is identical on every validator):

1. **Output (primary).** Dethrone if net better on breadth: `(wins + blind_spot_covers) − regressions ≥ 1`. A challenger **may** regress some orders and still win, provided each regression stays within the **1% hard floor** and its wins outnumber its regressions by at least one. (This replaces the older "any regression = reject / matching everywhere rejected" rule.)
2. **Tie-breaks (fully-matched, saturated tie only):** cheaper total metered (pre-refund) gas (≥200 bps), then smaller worst AST region `max_region_nodes` (by ≥100), then less dead code `unproductive_nodes` (by ≥2000). Armed, but each fires only once both champion and challenger carry the metric.

**Hard vetoes** override every rung: no order may be cut by more than 1%, and the challenger may not drop any order the champion serves. The verdict dict carries `adopt_via` (`performance`/`gas`/`factorization`/`deadwood`).

### Scoring Pipeline

For each intent in the benchmark set:

1. Solver generates an `ExecutionPlan` via `generate_plan()`
2. Plan is simulated on an Anvil fork (captures token transfers, gas usage, state changes)
3. The app's JS module runs `score(plan, state, context)`. For `DexAggregatorApp` it returns a **validity sentinel** plus the **raw delivered output** (exact wei to the recipient) in `metadata.raw_output` — the real per-order signal the relative comparison uses. (`context` carries `context.simulation` token transfers / gas / state changes, `context.state`, `context.oracle`.)
4. The challenger's per-order output is compared to the champion's (above). Because adoption is on **real delivered assets**, not quoted amounts, a solver cannot win by under- or over-quoting.

### Timeouts

Per-command timeouts enforced by the harness:

| Command | Timeout |
|---------|---------|
| `initialize` | 60s |
| `generate_plan` | 30s |
| `check_trigger` | 10s |
| `on_benchmark_start` | 10s |
| `on_benchmark_end` | 30s |
| `serialize_state` | 30s |
| `restore_state` | 30s |
| `metadata` | 5s |

Total container lifetime: **10 minutes** maximum.

## JSON-over-stdio Protocol

Communication between the host-side orchestrator and the in-container runner uses JSON-over-stdin/stdout (newline-delimited JSON).

### Message Format

**Request** (host to container):
```json
{"command": "generate_plan", "intent": {...}, "state": {...}, "snapshot": {...}}
```

**Success response** (container to host):
```json
{"success": true, "result": {...}}
```

**Error response** (container to host):
```json
{"success": false, "error": "Something went wrong", "error_type": "ValueError"}
```

### Commands

| Command | Description | Params |
|---------|-------------|--------|
| `initialize` | One-time setup | `config` dict |
| `generate_plan` | Generate execution plan | `intent`, `state`, `snapshot` |
| `check_trigger` | Check auto-trigger condition | `intent`, `state`, `snapshot` |
| `on_benchmark_start` | Before benchmark batch | `intent_count` |
| `on_benchmark_end` | After benchmark batch | `results` list |
| `serialize_state` | Persist state | (none) |
| `restore_state` | Restore state | `state_b64` (base64-encoded) |
| `metadata` | Get solver info | (none) |
| `shutdown` | Graceful exit | (none) |

Each command gets exactly one response. stderr is captured for logging but does not affect scoring.

## Security

Solver containers run in a locked-down environment:

- **`--network=none`** — No network access during benchmarking (RPC URLs are provided via the orchestrator for live execution)
- **`--read-only`** — Read-only filesystem (with `/tmp` tmpfs for scratch space)
- **`--cap-drop=ALL`** — All Linux capabilities dropped
- **`--memory=2g`** (screening) / **`--memory=4g`** (build) — Memory limits enforced
- **`--cpus=1.0`** — CPU limit during screening

The harness manages the container entry point. Solvers cannot override it because `CMD` and `ENTRYPOINT` are forbidden in the Dockerfile.

## Agentic Solver Development

The miner includes an `agent` subcommand that uses an LLM (Claude) to automatically develop and improve solver strategies.

### How It Works

The agent loop (`minotaur_subnet.miner.agent`) runs continuously:

1. **Discovers active apps** from the validator — fetches all deployed App Intents and their JS scoring modules
2. **Monitors per-app scores** — tracks how the current solver performs on each app
3. **Identifies underperformers** — apps where the solver scores below threshold
4. **Generates/improves strategies via Claude CLI** — the LLM reads the app's JS scoring module, Solidity contract, and current strategy code, then writes an improved `Strategy` class
5. **Tests strategies locally** — validates the generated code compiles and produces valid plans
6. **Bundles into RoutingSolver** — combines all per-app strategies into a single solver
7. **Submits to validator** — pushes the updated solver for screening and benchmarking

### Running the Agent

```bash
python -m minotaur_subnet.miner.main agent \
    --validator-url http://localhost:9100 \
    --interval 300
```

The agent generates `Strategy` subclasses (one per app) and registers them with a `RoutingSolver`. Each strategy targets a specific `APP_ID` and set of `INTENT_FUNCTIONS`.

### When to Use

The agentic approach is most useful for:
- Bootstrapping strategies for newly deployed apps
- Iterating on strategies for apps with complex scoring logic
- Miners who want to compete without deep DeFi expertise

For maximum performance, experienced miners will typically write custom strategies by hand and use the agent as a starting point or supplement.
