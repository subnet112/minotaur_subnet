"""IntentSolver abstract base class (v2).

The IntentSolver is the core competition surface for miners. Miners extend
this class to build Solving Engine strategies that generate execution plans
for App Intents.

Solvers receive RPC URLs for on-chain data access. The ``initialize()``
config dict includes ``rpc_urls: dict[int, str]`` mapping chain IDs to
RPC endpoints (e.g. Anvil fork, Alchemy, Infura). Solvers query real pool
states, discover liquidity, and derive prices directly from on-chain data.

A ``MarketSnapshot`` may also be provided for backward compatibility and
benchmark scenarios, but production solvers should prefer RPC data.

Miners extend IntentSolver and submit their code as git repos with Dockerfiles.
Validators run all submissions in sandboxed containers, benchmark them against
active intents, and adopt the winning version.

Key design points:
    - initialize() receives ``rpc_urls`` for live on-chain data access
    - MarketSnapshot is optional — provided for benchmarks and fallback
    - on_benchmark_start/end() lifecycle hooks for batch optimization
    - serialize_state()/restore_state() for cross-epoch learning
    - metadata() for solver identification

Example usage::

    class MySwapSolver(IntentSolver):
        def initialize(self, config):
            self.rpc_urls = config.get("rpc_urls", {})
            self.routing_table = build_routing_table(config)

        def generate_plan(self, intent, state, snapshot=None):
            if getattr(state, "typed_context", None) is not None:
                params = state.typed_context.raw_params
            else:
                params = state.raw_params_view()

            # Prefer RPC for live data, fall back to snapshot
            if self.rpc_urls.get(state.chain_id):
                pools = self.query_pools_via_rpc(state.chain_id)
            elif snapshot and snapshot.pool_states:
                pools = snapshot.pool_states
            else:
                raise ValueError("No RPC or snapshot available")
            route = self.routing_table.find_best(pools, params)
            return ExecutionPlan(
                intent_id=intent.app_id,
                interactions=route.to_interactions(),
                deadline=int(time.time()) + 300,
                nonce=state.nonce,
            )

        def metadata(self):
            return SolverMetadata(
                name="my-swap-solver",
                version="1.0.0",
                author="5Grwva...",
                supported_intent_types=["swap"],
            )

    # Required: tells the benchmarking harness which class to instantiate
    SOLVER_CLASS = MySwapSolver

See also:
    - intentsolver_submission_spec.md for the full submission protocol
    - architecture_v2.md for the v2 architecture overview
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

import time as _time

from minotaur_subnet.shared.types import (
    AppIntentDefinition,
    ExecutionPlan,
    IntentState,
    QuoteResult,
)


# ═══════════════════════════════════════════════════════════════════════════════
#                          MARKET SNAPSHOT
# ═══════════════════════════════════════════════════════════════════════════════


@dataclass
class MarketSnapshot:
    """Point-in-time market data for plan generation.

    Used primarily for benchmarking and as a fallback when RPC access is
    unavailable. Production solvers should prefer querying on-chain state
    directly via RPC URLs provided in ``initialize(config["rpc_urls"])``.

    Attributes:
        chain_id: Target chain ID (e.g., 1 for Ethereum, 8453 for Base).
        block_number: The block at which this snapshot was taken.
        timestamp: Unix timestamp of the snapshot block.
        prices: Token price feeds keyed by pair
            (e.g., {"ETH/USD": 1850.0, "USDC/USD": 1.0}).
        pool_states: DEX pool states keyed by pool address. Each entry
            contains protocol-specific data (reserves for V2, liquidity/
            sqrtPriceX96/tick for V3).
        balances: Token balances for the intent contract address, keyed
            by token address (values as decimal strings in smallest unit).
        dex_config: DEX router addresses and protocol configuration
            (e.g., router addresses, factory addresses, fee tiers).
        raw_state: Additional contract storage data. Keyed by contract
            address, values are protocol-specific.
    """

    chain_id: int
    block_number: int
    timestamp: int

    prices: dict[str, float] = field(default_factory=dict)
    pool_states: dict[str, dict[str, Any]] = field(default_factory=dict)
    balances: dict[str, str] = field(default_factory=dict)
    dex_config: dict[str, Any] = field(default_factory=dict)
    raw_state: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def empty(cls, chain_id: int = 31337) -> "MarketSnapshot":
        """Create a minimal empty snapshot for use when no market data is needed.

        Useful as a placeholder when the solver builds its own data from RPC.
        """
        return cls(
            chain_id=chain_id,
            block_number=0,
            timestamp=int(_time.time()),
        )


# ═══════════════════════════════════════════════════════════════════════════════
#                          SOLVER METADATA
# ═══════════════════════════════════════════════════════════════════════════════


@dataclass
class SolverMetadata:
    """Solver identification and capabilities.

    Returned by IntentSolver.metadata() for logging, benchmarking reports,
    and miner attribution. The supported_intent_types field determines
    which intents this solver is tested against during benchmarking.

    Attributes:
        name: Human-readable solver name (e.g., "advanced-router").
        version: Semantic version string (e.g., "2.1.0").
        author: Miner hotkey or identifier.
        description: Brief description of the solver's approach.
        supported_chains: Chain IDs this solver supports.
        supported_intent_types: Intent types this solver handles.
            Must contain at least one type.
    """

    name: str
    version: str
    author: str
    description: str = ""
    supported_chains: list[int] = field(default_factory=list)
    supported_intent_types: list[str] = field(default_factory=lambda: ["swap"])


# ═══════════════════════════════════════════════════════════════════════════════
#                          INTENT SOLVER ABC
# ═══════════════════════════════════════════════════════════════════════════════


class IntentSolver(ABC):
    """Base class for v2 IntentSolver submissions.

    Miners extend this class to build solving strategies. Solvers receive
    RPC URLs via ``initialize(config)`` for direct on-chain data access,
    and an optional ``MarketSnapshot`` for benchmark/fallback scenarios.

    Lifecycle (per benchmark round):
        1. initialize(config) — called once when the solver is loaded.
           ``config["rpc_urls"]`` maps chain_id → RPC URL.
        2. restore_state(data) — called if state from a prior epoch exists
        3. on_benchmark_start(intent_count) — before the batch begins
        4. generate_plan() / check_trigger() — called per intent
        5. on_benchmark_end(results) — after the batch completes
        6. serialize_state() — persist learned state for next epoch

    Subclasses MUST implement:
        - initialize: one-time setup (store rpc_urls, build tables)
        - generate_plan: core solving logic
        - metadata: solver identification

    Subclasses MAY override:
        - check_trigger: for auto-triggered intents
        - on_benchmark_start / on_benchmark_end: batch lifecycle hooks
        - serialize_state / restore_state: state persistence across epochs
    """

    @abstractmethod
    def initialize(self, config: dict[str, Any]) -> None:
        """One-time initialization when the solver is loaded.

        Use this for expensive setup: loading ML models, building routing
        tables, creating Web3 instances from RPC URLs.

        Args:
            config: Validator-provided configuration. Contains:
                - chain_ids: list[int] — chains to support
                - rpc_urls: dict[int, str] — RPC URL per chain ID
                  (e.g. {1: "http://localhost:8545"})
                - timeout_per_plan_ms: int — per-plan time budget
                - supported_protocols: list[str] — available DEX protocols

        Raises:
            Any exception causes the solver to fail screening (Stage 2).
        """

    @abstractmethod
    def generate_plan(
        self,
        intent: AppIntentDefinition,
        state: IntentState,
        snapshot: MarketSnapshot | None = None,
    ) -> ExecutionPlan:
        """Generate an execution plan for the given intent.

        This is the core competition surface. Solvers should prefer querying
        on-chain state via RPC URLs (from ``initialize(config["rpc_urls"])``)
        and fall back to snapshot data when RPC is unavailable. When the
        validator attaches ``state.typed_context``, new solver code should
        prefer it over direct dictionary reads from ``state.raw_params``.

        Args:
            intent: App Intent definition (type, config, metadata).
            state: Current on-chain state of the intent contract.
            snapshot: Optional market data. May be None in production
                when the solver builds its own data from RPC.

        Returns:
            ExecutionPlan with ordered interactions to fulfill the intent.

        Raises:
            Any exception results in a score of 0.0 for this intent.
            Killed if execution exceeds the per-plan timeout (30s default).
        """

    def quote(
        self,
        intent: AppIntentDefinition,
        state: IntentState,
        snapshot: MarketSnapshot | None = None,
    ) -> QuoteResult:
        """Compute a quote without generating a full execution plan.

        Override to provide fast quoting. Prefer RPC for live data,
        fall back to snapshot pool state.

        Args:
            intent: App Intent definition.
            state: Current on-chain state. Prefer ``state.typed_context`` when
                present; ``state.raw_params`` remains the canonical raw payload.
            snapshot: Optional market data for fallback.

        Returns:
            QuoteResult with estimated output and routing info.

        Raises:
            NotImplementedError: If this solver does not support quoting.
        """
        raise NotImplementedError("This solver does not support quoting")

    def check_trigger(
        self,
        intent: AppIntentDefinition,
        state: IntentState,
        snapshot: MarketSnapshot | None = None,
    ) -> bool:
        """For auto-triggered intents: should this intent execute now?

        Called for intents with config.trigger_type == AUTO_TRIGGERED.
        The solver monitors on-chain conditions (via RPC or snapshot)
        and returns True when execution is warranted.

        Args:
            intent: The auto-triggered intent to evaluate.
            state: Current on-chain state.
            snapshot: Optional market data for fallback.

        Returns:
            True if conditions are met and the intent should trigger.
            Default: False (override for auto-triggered intent support).
        """
        return False

    def on_benchmark_start(self, intent_count: int) -> None:
        """Called before a benchmark batch begins.

        Use this for batch-level optimization: pre-computing shared data
        structures, warming caches, allocating buffers.

        Args:
            intent_count: Number of intents in this benchmark batch.
        """

    def on_benchmark_end(self, results: list[dict[str, Any]]) -> None:
        """Called after a benchmark batch completes.

        Receives summary results for all intents in the batch. Use this
        for learning: update models, tune parameters based on feedback.

        Args:
            results: List of dicts, each containing:
                - intent_id: str
                - score: float (0.0-1.0)
                - elapsed_ms: int
        """

    def serialize_state(self) -> bytes:
        """Serialize learned state for persistence across epochs.

        Called after benchmarking completes. The returned bytes are stored
        by the validator and passed back to restore_state() when the solver
        is loaded for the next epoch.

        Use this to persist ML model weights, routing tables, parameter
        histories, score-based tuning data, etc.

        Returns:
            Serialized state as bytes. Maximum 50MB.
            Return b"" if no state to persist.
        """
        return b""

    def restore_state(self, data: bytes) -> None:
        """Restore previously serialized state.

        Called after initialize() if state from a previous epoch exists.
        The data is exactly what serialize_state() returned last epoch.

        Args:
            data: Previously serialized state bytes.
        """

    @abstractmethod
    def metadata(self) -> SolverMetadata:
        """Return solver identification and capabilities.

        Used for logging, benchmarking reports, and miner attribution.
        The supported_intent_types field determines which intents this
        solver is tested against during benchmarking.

        Returns:
            SolverMetadata with solver name, version, author, and
            the intent types this solver handles.
        """
