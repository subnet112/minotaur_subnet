# Writing a Custom Solver

This guide covers everything you need to build, package, test, and submit a custom IntentSolver to Minotaur Subnet 112.

## Overview

A solver submission is a git repository containing:

```
my-solver/
├── Dockerfile        # FROM ghcr.io/subnet112/solver-base:v1
├── solver.py         # class MySolver(IntentSolver): ... ; SOLVER_CLASS = MySolver
├── requirements.txt  # Additional pip dependencies (optional)
└── README.md         # Description of your solver's approach
```

The validator clones this repo, runs it through a three-stage screening pipeline, benchmarks it against active App Intents, and adopts it if it beats the current champion by at least 0.5% (`DETHRONE_MARGIN = 0.005`).

## Dockerfile Requirements

Your Dockerfile must meet these requirements:

1. **Base image:** Must use `FROM ghcr.io/subnet112/solver-base:v1`
2. **No CMD or ENTRYPOINT:** The harness manages the entry point. Including either directive causes screening failure.
3. **Repo size:** Total repository must be under 100MB (excluding `.git`).
4. **No suspicious binaries:** Binary files (`.so`, `.dll`, `.exe`, `.bin`, etc.) over 10MB outside of `models/` directories are rejected.

### Minimal Dockerfile

```dockerfile
FROM ghcr.io/subnet112/solver-base:v1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . /app
WORKDIR /app
```

### Container Execution Environment

When the validator runs your solver, the container is launched with strict isolation:

| Constraint | Value |
|------------|-------|
| Network | `--network=none` (no internet access) |
| Filesystem | `--read-only` (with `/tmp` tmpfs) |
| Memory | `--memory=4g` |
| CPU | `--cpus=2.0` |

Your solver must work entirely offline. Any data your solver needs (routing tables, model weights, pool lists) must be bundled in the Docker image.

## solver.py Structure

Your solver module must:

1. Subclass `IntentSolver` from `minotaur_subnet.sdk.intent_solver`
2. Implement `initialize()`, `generate_plan()`, and `metadata()`
3. Export `SOLVER_CLASS` at module level

### Complete Example

```python
import time
from typing import Any

from minotaur_subnet.sdk.intent_solver import IntentSolver, MarketSnapshot, SolverMetadata
from minotaur_subnet.shared.types import (
    AppIntentDefinition,
    ExecutionPlan,
    Interaction,
    IntentState,
)
from minotaur_subnet.v3.contexts import SwapIntentContext


class AdvancedSwapSolver(IntentSolver):
    """Solver with RPC-based pool discovery and multi-hop routing."""

    def __init__(self):
        self.rpc_urls: dict[int, str] = {}
        self.chain_ids: list[int] = []
        self.routing_table: dict = {}

    def initialize(self, config: dict[str, Any]) -> None:
        self.chain_ids = config.get("chain_ids", [1])
        self.rpc_urls = config.get("rpc_urls", {})
        # Build routing tables, load models, etc.
        self.routing_table = self._build_routing_table()

    def generate_plan(
        self,
        intent: AppIntentDefinition,
        state: IntentState,
        snapshot: MarketSnapshot | None = None,
    ) -> ExecutionPlan:
        chain_id = state.chain_id or 1
        if isinstance(state.typed_context, SwapIntentContext):
            input_token = state.typed_context.input_token
            output_token = state.typed_context.output_token
            input_amount = state.typed_context.input_amount
        else:
            input_token = state.raw_params.get("input_token", "")
            output_token = state.raw_params.get("output_token", "")
            input_amount = int(state.raw_params.get("input_amount", "0"))

        # Query pool states via RPC if available, else use snapshot
        if self.rpc_urls.get(chain_id):
            pool_states = self._query_pools_rpc(chain_id, input_token, output_token)
        elif snapshot and snapshot.pool_states:
            pool_states = snapshot.pool_states
        else:
            pool_states = {}

        # Find best route and build interactions
        route = self._find_route(pool_states, input_token, output_token, input_amount)
        interactions = self._build_interactions(route, chain_id)

        return ExecutionPlan(
            intent_id=intent.app_id,
            interactions=interactions,
            deadline=int(time.time()) + 300,
            nonce=state.nonce,
            metadata={
                "route": "custom_multi_hop",
                "hops": len(route),
                "chain_id": chain_id,
            },
        )

    def check_trigger(
        self,
        intent: AppIntentDefinition,
        state: IntentState,
        snapshot: MarketSnapshot | None = None,
    ) -> bool:
        # For perpetual intents: check if market conditions warrant execution
        return False

    def metadata(self) -> SolverMetadata:
        return SolverMetadata(
            name="advanced-swap-solver",
            version="1.0.0",
            author="5Grwva...",
            description="Multi-hop DEX aggregation with cross-protocol routing",
            supported_chains=[1, 8453],
            supported_intent_types=["swap"],
        )

    def serialize_state(self) -> bytes:
        # Persist learned routing data for next epoch
        import json
        return json.dumps(self.routing_table).encode()

    def restore_state(self, data: bytes) -> None:
        import json
        self.routing_table = json.loads(data.decode())

    # --- Private methods ---

    def _build_routing_table(self) -> dict:
        return {}

    def _query_pools_rpc(self, chain_id, token_in, token_out) -> dict:
        return {}

    def _find_route(self, pool_states, token_in, token_out, amount) -> list:
        return []

    def _build_interactions(self, route, chain_id) -> list[Interaction]:
        return [
            Interaction(
                target="0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",
                value="0",
                call_data="0xd0e30db0",
                chain_id=chain_id,
            ),
        ]


# Required: tells the harness which class to instantiate
SOLVER_CLASS = AdvancedSwapSolver
```

Prefer `state.typed_context` when the validator provides it. The raw
`state.raw_params` dict remains available for untyped access, and runtime
control metadata such as the intent function lives in `state.control`.

## Three-Stage Screening Pipeline

Every submission goes through progressive screening before benchmarking. Screening stops at the first failure.

### Stage 1: Static Checks (~10 seconds)

| Check | Requirement |
|-------|-------------|
| Required files | `Dockerfile`, `solver.py`, `README.md` must exist |
| Base image | Dockerfile must contain `FROM ghcr.io/subnet112/solver-base` |
| No entrypoint | Dockerfile must not contain `CMD` or `ENTRYPOINT` |
| Repo size | Total size must be under 100MB |
| No suspicious binaries | No `.so`, `.dll`, `.exe`, etc. over 10MB outside `models/` |

### Stage 2: Build Check (~2 minutes)

| Step | What happens |
|------|-------------|
| Docker build | `docker build --network=none --memory=4g -t <tag> <repo>` |
| Import check | `from solver import SOLVER_CLASS` -- verifies the module loads |
| Subclass check | Verifies `SOLVER_CLASS` is a subclass of `IntentSolver` |
| Init check | Calls `SOLVER_CLASS().initialize({"chain_ids": [1]})` |
| Metadata check | Calls `metadata()` and verifies `name` and `version` are non-empty |

### Stage 3: Smoke Test (~5 minutes)

| Step | What happens |
|------|-------------|
| Synthetic intents | 3 synthetic intents are generated and passed to `generate_plan()` |
| Plan validation | Each plan is checked for structural correctness (see below) |
| Trigger check | `check_trigger()` is called for auto-triggered intents; must return `bool` |
| Per-plan timeout | Each `generate_plan()` call must complete within 30 seconds |

### Plan Validation Rules

A valid `ExecutionPlan` must satisfy:

- `intent_id` matches the intent's `app_id`
- `interactions` list is non-empty
- `deadline` is after the snapshot timestamp
- Each interaction's `target` is a 42-character hex address starting with `0x`
- Each interaction's `call_data` starts with `0x`

## Benchmarking

After passing all three screening stages, the solver is benchmarked against active App Intents on the network.

### Scoring

Plans are scored by each app's JS scoring function (`score(plan, state, context)`). The JS score ranges from 0.0 to 1.0. Plans are also simulated on an Anvil fork to capture on-chain scores. Both scores must exceed the app's threshold.

### Champion/Challenger Model

- The currently active solver is the **champion**.
- A new submission is a **challenger**.
- The challenger must beat the champion's average score by at least **0.5%** to be adopted.
- Once adopted, the challenger becomes the new champion and processes real orders.

### Auto-Triggered Intents

For perpetual (auto-triggered) intents, solvers are also evaluated on trigger accuracy. The composite score is:

```
composite = 0.4 * trigger_accuracy + 0.6 * plan_quality
```

Where `trigger_accuracy` measures how well `check_trigger()` predicts when execution is warranted.

## Using BaselineSwapSolver as Reference

The `BaselineSwapSolver` at `minotaur_subnet/sdk/solvers/baseline_solver.py` is the default champion. Study it to understand:

- **RPC-first architecture:** Queries Uniswap V3 pool states via RPC, falls back to snapshot.
- **Factory-based pool discovery:** Uses the Uniswap V3 Factory contract to find pools for any token pair across all fee tiers (100, 500, 3000, 10000).
- **Multi-hop routing:** Finds optimal routes through intermediary tokens (WETH, USDC) when direct pools have poor liquidity.
- **Cross-chain support:** Generates multi-leg plans (source swap + bridge + destination action) when `dest_chain_id` differs from the source chain.
- **Pool state caching:** Caches pool states with a 12-second TTL (one Ethereum block).
- **Price derivation:** Derives USD prices from pool `sqrtPriceX96` values.

### Strategies to Beat the Baseline

- **More pool discovery:** Scan factory events for all deployed pools, not just known addresses.
- **Cross-DEX aggregation:** Route through multiple DEXes (SushiSwap, Curve, Balancer) for better prices.
- **Split routing:** Split large orders across multiple pools to reduce price impact.
- **MEV protection:** Use Flashbots-style techniques to protect orders from sandwich attacks.
- **ML-based parameter tuning:** Use `serialize_state()` / `restore_state()` to learn optimal slippage tolerances and routing preferences across epochs.
- **Gas optimization:** Minimize the number of interactions and calldata size.

## Using the RoutingSolver with Strategies

If your solver needs to handle multiple apps, use the `RoutingSolver` with per-app `Strategy` instances:

```python
from minotaur_subnet.sdk.routing_solver import RoutingSolver
from minotaur_subnet.sdk.strategy import Strategy
from minotaur_subnet.shared.types import ExecutionPlan, Interaction

class SwapStrategy(Strategy):
    APP_ID = "swap-app-001"
    INTENT_FUNCTIONS = ["execute"]

    def generate_plan(self, intent, state, snapshot=None):
        # Swap-specific logic
        ...

class VaultStrategy(Strategy):
    APP_ID = "vault-app-002"
    INTENT_FUNCTIONS = ["buyDip", "withdraw"]

    def generate_plan(self, intent, state, snapshot=None):
        intent_function = (
            getattr(state.typed_context, "intent_function", "")
            or state.control.get("_intent_function", "")
        )
        if intent_function == "buyDip":
            return self._buy_dip_plan(intent, state, snapshot)
        else:
            return self._withdraw_plan(intent, state, snapshot)

    def check_trigger(self, intent, state, snapshot=None):
        # Check price conditions for auto-triggered buyDip
        return True

    # ... private methods ...


# Wire it up
solver = RoutingSolver()
solver.register_strategy(SwapStrategy())
solver.register_strategy(VaultStrategy())

SOLVER_CLASS = RoutingSolver
```

The `RoutingSolver` generates a minimal fallback plan for any intent that does not match a registered strategy.

## Testing Before Submission

### 1. Local smoke submission

```bash
curl -X POST http://localhost:8080/v1/submissions/source \
  -H "Content-Type: application/json" \
  -d '{"solver_source":"<python source>", "hotkey":"local-miner", "epoch":0, "solver_name":"local-smoke"}'
```

### 2. Static Check Only

Run just Stage 1 on your repo directory to verify file structure before pushing:

```python
from minotaur_subnet.harness.screening import run_stage_1
result = run_stage_1("/path/to/my-solver")
print(result.passed, result.details)
```

### 3. Full Screening Locally

If you have Docker available, run the full screening pipeline:

```python
import asyncio
from minotaur_subnet.harness.screening import ScreeningPipeline

async def test():
    pipeline = ScreeningPipeline()
    result = await pipeline.run_all("/path/to/my-solver", commit_hash="abc123")
    print(result.to_dict())

asyncio.run(test())
```

## Submission Checklist

- [ ] `solver.py` subclasses `IntentSolver` and exports `SOLVER_CLASS`
- [ ] `initialize()`, `generate_plan()`, and `metadata()` are implemented
- [ ] `Dockerfile` uses `FROM ghcr.io/subnet112/solver-base:v1` with no CMD/ENTRYPOINT
- [ ] `README.md` exists with a description of the solver's approach
- [ ] `metadata()` returns a non-empty `name` and `version`
- [ ] All `generate_plan()` outputs pass plan validation (correct `intent_id`, non-empty interactions, valid addresses and calldata)
- [ ] `check_trigger()` returns `bool`
- [ ] Solver works offline (no network access at runtime)
- [ ] Total repo size is under 100MB
- [ ] Local source submission passes through benchmarking (`POST /v1/submissions/source`)

See also: [Solver API](./solver-api.md), [Configuration](./configuration.md), [Troubleshooting](./troubleshooting.md).
