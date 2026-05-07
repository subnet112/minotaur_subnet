"""Processor context provided to IntentProcessors during plan generation.

The ProcessorContext contains everything a miner's solver has access to when
generating execution plans. Critically, it does NOT include the JS scoring
function -- solvers never see how they are scored. They only receive score
feedback after submitting plans.
"""

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ProcessorContext:
    """Context available to IntentProcessors during plan generation.

    This is what miners have access to -- notably, they do NOT have access
    to the JS scoring function. They only get scores back after submitting plans.

    Attributes:
        chain_id: The chain ID for the current execution context.
        timestamp: Current unix timestamp (seconds).
        block_number: Current block number on the target chain.
        rpc_url: JSON-RPC endpoint for reading on-chain state.
        prices: Token price feeds from oracles, keyed by pair
            (e.g. {"ETH/USD": 1850.0, "USDC/USD": 1.0}).
        score_history: Historical scores for this intent, useful for learning.
            Each entry contains at minimum: plan_hash, score, timestamp.
        dex_config: DEX router addresses, pool data, and protocol-specific
            configuration. Structure varies by chain and protocol.
    """

    chain_id: int
    timestamp: int
    block_number: int

    # RPC access for reading chain state
    rpc_url: str = ""

    # Token prices (from oracles, updated periodically)
    prices: dict[str, float] = field(default_factory=dict)

    # Historical scores for this intent (for learning)
    score_history: list[dict[str, Any]] = field(default_factory=list)

    # DEX router addresses and pool data
    dex_config: dict[str, Any] = field(default_factory=dict)
