# Solver API Reference

This page documents the full IntentSolver API (v2), the Strategy API for per-app solvers, and all supporting data classes.

## IntentSolver (Abstract Base Class)

**Module:** `minotaur_subnet.sdk.intent_solver`

The `IntentSolver` is the core competition surface for miners. Miners subclass `IntentSolver`, implement the required methods, and submit their code to validators. The solver module must export `SOLVER_CLASS` at module level pointing to the solver class.

### Lifecycle

When a solver is loaded by the validator, the following sequence occurs:

1. **`initialize(config)`** -- called once when the solver is loaded. Use this for expensive setup (loading models, building routing tables, creating Web3 instances from RPC URLs).
2. **`restore_state(data)`** -- called if serialized state from a prior epoch exists.
3. **`on_benchmark_start(intent_count)`** -- called before a benchmark batch begins.
4. **`generate_plan()` / `check_trigger()`** -- called per intent during benchmarking or live execution.
5. **`on_benchmark_end(results)`** -- called after the batch completes.
6. **`serialize_state()`** -- called to persist learned state for the next epoch.

### Required Methods

#### `initialize(config: dict) -> None`

One-time initialization when the solver is loaded.

**Config keys:**

| Key | Type | Description |
|-----|------|-------------|
| `chain_ids` | `list[int]` | Chains to support (e.g., `[1, 8453]`) |
| `rpc_urls` | `dict[int, str]` | RPC URL per chain ID (e.g., `{1: "http://localhost:8545"}`) |
| `timeout_per_plan_ms` | `int` | Per-plan time budget in milliseconds |
| `supported_protocols` | `list[str]` | Available DEX protocols |

Any exception during `initialize()` causes the solver to fail screening (Stage 2).

#### `generate_plan(intent, state, snapshot=None) -> ExecutionPlan`

Generate an execution plan for the given intent. This is the primary competition surface.

**Parameters:**

| Name | Type | Description |
|------|------|-------------|
| `intent` | `AppIntentDefinition` | App Intent definition (type, config, metadata) |
| `state` | `IntentState` | Current on-chain state of the intent contract |
| `snapshot` | `MarketSnapshot \| None` | Optional point-in-time market data. May be `None` in production |

**Returns:** `ExecutionPlan` with ordered interactions to fulfill the intent.

**Error handling:** Any exception results in a score of 0.0 for this intent. The process is killed if execution exceeds the per-plan timeout (30s default).

Solvers should prefer querying on-chain state via RPC URLs (from `initialize(config["rpc_urls"])`) and fall back to snapshot data when RPC is unavailable.

#### `metadata() -> SolverMetadata`

Return solver identification and capabilities. Used for logging, benchmarking reports, and miner attribution.

### Optional Methods

#### `quote(intent, state, snapshot=None) -> QuoteResult`

Compute a quote without generating a full execution plan. Override for fast quoting support. Raises `NotImplementedError` by default.

> The reference solver shipped with the DEX Aggregator App (`subnet112/minotaur-solver`) implements `quote()` end-to-end, which is what powers the "binding quote before signature" UX on the launched product. App-defined Apps may opt out, in which case the UI falls back to plan-then-sign.

#### `check_trigger(intent, state, snapshot=None) -> bool`

For auto-triggered (perpetual) intents: should this intent execute now? Returns `True` when conditions are met. Default: `False`.

#### `on_benchmark_start(intent_count: int) -> None`

Called before a benchmark batch begins. Use for batch-level optimization: pre-computing shared data, warming caches, allocating buffers.

#### `on_benchmark_end(results: list[dict]) -> None`

Called after a benchmark batch completes. Receives a list of dicts, each containing:

| Key | Type | Description |
|-----|------|-------------|
| `intent_id` | `str` | Intent identifier |
| `score` | `float` | Score from 0.0 to 1.0 |
| `elapsed_ms` | `int` | Time taken in milliseconds |

Use this for learning: update models, tune parameters based on feedback.

#### `serialize_state() -> bytes`

Serialize learned state for persistence across epochs. Maximum 50MB. Return `b""` if no state to persist. Called after benchmarking completes.

#### `restore_state(data: bytes) -> None`

Restore previously serialized state. Called after `initialize()` if state from a prior epoch exists. The data is exactly what `serialize_state()` returned last epoch.

---

## Strategy (Abstract Base Class)

**Module:** `minotaur_subnet.sdk.strategy`

A `Strategy` is a lightweight, app-specific plan generator used with the `RoutingSolver`. Each Strategy handles a single app (identified by `APP_ID`). The `RoutingSolver` dispatches `generate_plan()` calls to the matching Strategy.

### Class Attributes

| Attribute | Type | Description |
|-----------|------|-------------|
| `APP_ID` | `str` | The app_id this strategy handles. Must be set by subclasses |
| `INTENT_FUNCTIONS` | `list[str]` | Intent function names this strategy handles. Empty list = all functions |

### Required Methods

#### `generate_plan(intent, state, snapshot=None) -> ExecutionPlan`

Generate an execution plan for the given intent. Same signature as `IntentSolver.generate_plan()`.

### Optional Methods

#### `quote(intent, state, snapshot=None) -> QuoteResult`

Compute a quote. Raises `NotImplementedError` by default.

#### `check_trigger(intent, state, snapshot=None) -> bool`

For auto-triggered intents. Default: `False`.

#### `accepts(app_id: str, intent_function: str = "") -> bool`

Check if this strategy handles the given app_id and intent function. Default implementation checks `APP_ID` match and, if `INTENT_FUNCTIONS` is set, verifies the function name is in the list.

### Example

```python
from minotaur_subnet.sdk.strategy import Strategy
from minotaur_subnet.shared.types import ExecutionPlan, Interaction

class MyVaultStrategy(Strategy):
    APP_ID = "vault-abc123"
    INTENT_FUNCTIONS = ["buyDip"]

    def generate_plan(self, intent, state, snapshot=None):
        return ExecutionPlan(
            intent_id=intent.app_id,
            interactions=[
                Interaction(
                    target="0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",
                    value="0",
                    call_data="0xd0e30db0",
                    chain_id=state.chain_id or 1,
                ),
            ],
            deadline=(snapshot.timestamp if snapshot else 0) + 300,
            nonce=state.nonce,
        )

# Export for the harness
STRATEGY_CLASS = MyVaultStrategy
```

---

## RoutingSolver

**Module:** `minotaur_subnet.sdk.routing_solver`

A concrete `IntentSolver` that dispatches `generate_plan()` calls to registered `Strategy` instances based on `app_id`. When no strategy matches, it generates a minimal fallback plan that passes structural validation but scores low.

### Usage

```python
from minotaur_subnet.sdk.routing_solver import RoutingSolver

solver = RoutingSolver()
solver.register_strategy(MyVaultStrategy())
solver.register_strategy(MySwapStrategy())
solver.initialize({"chain_ids": [1]})

plan = solver.generate_plan(intent, state, snapshot)
```

### Methods

| Method | Description |
|--------|-------------|
| `register_strategy(strategy)` | Register a Strategy for its `APP_ID` |
| `remove_strategy(app_id) -> bool` | Remove a registered strategy |
| `get_strategy(app_id) -> Strategy \| None` | Look up a strategy by app_id |

---

## Data Classes

### `ExecutionPlan`

**Module:** `minotaur_subnet.shared.types`

The output of `generate_plan()`. Contains the ordered interactions that fulfill an intent.

| Field | Type | Description |
|-------|------|-------------|
| `intent_id` | `str` | Which intent this plan fulfills |
| `interactions` | `list[Interaction]` | Ordered on-chain calls to execute |
| `deadline` | `int` | Unix timestamp -- plan expires after this time |
| `nonce` | `int` | Replay protection nonce |
| `metadata` | `dict[str, Any]` | App-specific data (route info, chain_id, etc.) |

### `Interaction`

**Module:** `minotaur_subnet.shared.types`

A single on-chain call within an execution plan.

| Field | Type | Description |
|-------|------|-------------|
| `target` | `str` | Contract address (`0x...`, 42 characters) |
| `value` | `str` | Wei value as decimal string (`"0"` for no ETH) |
| `call_data` | `str` | ABI-encoded calldata (`0x...`) |
| `chain_id` | `int` | Target chain (default: `1`) |

### `IntentState`

**Module:** `minotaur_subnet.shared.types`

Current on-chain state of an App Intent contract, passed to `generate_plan()`.

| Field | Type | Description |
|-------|------|-------------|
| `contract_address` | `str` | Address of the intent contract |
| `chain_id` | `int` | Chain ID |
| `nonce` | `int` | Current nonce for replay protection |
| `owner` | `str` | Owner address |
| `raw_params` | `dict[str, Any]` | Canonical raw app/runtime parameters |
| `control` | `dict[str, Any]` | Runtime control metadata such as `_intent_function` |
| `extra` | `dict[str, Any]` | Synthesized compatibility payload derived from `raw_params` and `control` |
| `typed_context` | `Any \| None` | Preferred typed runtime view when available (`SwapIntentContext`, `TwapIntentContext`, `RebalanceIntentContext`, etc.) |

When `typed_context` is present, new solver code should prefer it over direct
dictionary reads from `raw_params`. Runtime control metadata such as the intent
function lives in `control`, while `extra` remains a compatibility view only.

### `AppIntentDefinition`

**Module:** `minotaur_subnet.shared.types`

The full definition of an App Intent.

| Field | Type | Description |
|-------|------|-------------|
| `app_id` | `str` | Unique identifier |
| `name` | `str` | Human-readable name |
| `version` | `str` | Semantic version |
| `intent_type` | `str` | Intent type (`"swap"`, `"limit_order"`, `"rebalance"`, etc.) |
| `js_code` | `str` | JS scoring function source |
| `solidity_code` | `str \| None` | On-chain contract source |
| `config` | `AppIntentConfig` | Configuration (chains, thresholds, trigger type) |
| `deployer` | `str` | Deployer address |
| `description` | `str` | What this app does |
| `manifest` | `dict \| None` | JS manifest (intent functions, param schemas) |

### `MarketSnapshot`

**Module:** `minotaur_subnet.sdk.intent_solver`

Point-in-time market data for plan generation. Used primarily during benchmarking. Production solvers should prefer querying on-chain state directly via RPC URLs.

| Field | Type | Description |
|-------|------|-------------|
| `chain_id` | `int` | Target chain ID |
| `block_number` | `int` | Block at which this snapshot was taken |
| `timestamp` | `int` | Unix timestamp of the snapshot block |
| `prices` | `dict[str, float]` | Token price feeds (e.g., `{"ETH/USD": 1850.0}`) |
| `pool_states` | `dict[str, dict]` | DEX pool states keyed by pool address |
| `balances` | `dict[str, str]` | Token balances keyed by token address |
| `dex_config` | `dict[str, Any]` | DEX router/factory addresses and config |
| `raw_state` | `dict[str, Any]` | Additional contract storage data |

**Class method:** `MarketSnapshot.empty(chain_id=1)` creates a minimal empty snapshot for use when the solver builds its own data from RPC.

### `SolverMetadata`

**Module:** `minotaur_subnet.sdk.intent_solver`

Solver identification and capabilities. Returned by `metadata()`.

| Field | Type | Description |
|-------|------|-------------|
| `name` | `str` | Human-readable solver name (e.g., `"advanced-router"`) |
| `version` | `str` | Semantic version string (e.g., `"2.1.0"`) |
| `author` | `str` | Miner hotkey or identifier |
| `description` | `str` | Brief description of the solver's approach |
| `supported_chains` | `list[int]` | Chain IDs this solver supports (default: `[1]`) |
| `supported_intent_types` | `list[str]` | Intent types this solver handles (default: `["swap"]`) |

### `QuoteResult`

**Module:** `minotaur_subnet.shared.types`

Result of a solver quote computation.

| Field | Type | Description |
|-------|------|-------------|
| `estimated_output` | `str` | Best output amount as decimal string |
| `computed_params` | `dict[str, str]` | Matches manifest `source:"quote"` params |
| `route_summary` | `str` | Human-readable route description |
| `gas_estimate` | `int` | Estimated gas units |
| `metadata` | `dict[str, Any]` | Extra info (pool used, hops, protocol, etc.) |

---

## Module-Level Export

Every solver module must export `SOLVER_CLASS` at the top level:

```python
class MySolver(IntentSolver):
    ...

SOLVER_CLASS = MySolver
```

For Strategy modules (used with the agent loop), export `STRATEGY_CLASS`:

```python
class MyStrategy(Strategy):
    APP_ID = "my-app-id"
    ...

STRATEGY_CLASS = MyStrategy
```

See also: [Custom Solver](./custom-solver.md), [Configuration](./configuration.md), [Troubleshooting](./troubleshooting.md).
