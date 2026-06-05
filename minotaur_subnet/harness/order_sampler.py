"""Deterministic sampling of historical orders for benchmark Stage 2.

Samples filled orders from the app store to use as real-world benchmark
scenarios. Sampling is deterministic from the round_id — all validators
derive the same sample without needing to broadcast the selection.

Stage 2 replays these orders' parameters against the current benchmark
fork (weekly-pinned Anvil). Stage 3 replays the subset where the
challenger failed against a fresh Anvil pinned to each order's original
execution block.
"""

from __future__ import annotations

import hashlib
import logging
import random
from typing import Any

logger = logging.getLogger(__name__)


# Fields to strip for privacy when exposing sampled orders to solvers.
# The solver only needs the trade parameters, not who submitted it.
_PII_FIELDS = {"submitted_by", "interop_address", "user_signature", "hotkey"}


def sample_historical_orders(
    app_store: Any,
    round_id: str,
    chain_ids: list[int] | None = None,
    n_per_chain: int = 10,
    exclude_statuses: set[str] | None = None,
    records: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Deterministically sample historical FILLED orders for Stage 2.

    Uses round_id as RNG seed so all validators produce the same sample
    without needing to broadcast the selection.

    Args:
        app_store: AppIntentStore with list_orders().
        round_id: The current round identifier (determines sample).
        chain_ids: Only include orders from these chains. None = all chains.
        n_per_chain: Target sample size per chain (may be smaller if
            insufficient historical orders).
        exclude_statuses: Order statuses to exclude (default: only include 'filled').
        records: Pre-built candidate orders (e.g. a chain-derived corpus, plan
            Phase 5b). When provided, they are the source instead of
            app_store.list_orders() — same filter/sample/PII logic. None (default)
            keeps the local-store path byte-for-byte.

    Returns:
        List of order dicts, PII-stripped. May be empty if no history exists.
    """
    if exclude_statuses is None:
        include_statuses = {"filled"}
    else:
        include_statuses = None  # include all except excluded

    if records is not None:
        all_orders = records
    else:
        try:
            all_orders = app_store.list_orders()
        except Exception as exc:
            logger.warning("Failed to list orders for Stage 2 sampling: %s", exc)
            return []

    # Filter: only orders with a block_number (can be replayed) and the right status
    candidates = []
    for order in all_orders:
        status = order.get("status", "").lower()
        if include_statuses is not None and status not in include_statuses:
            continue
        if exclude_statuses is not None and status in exclude_statuses:
            continue
        if order.get("block_number") is None:
            continue
        if chain_ids is not None and order.get("chain_id") not in chain_ids:
            continue
        candidates.append(order)

    if not candidates:
        return []

    # Deterministic RNG from round_id
    seed = int.from_bytes(
        hashlib.sha256(round_id.encode("utf-8")).digest()[:8], "big"
    )
    rng = random.Random(seed)

    # Group by chain and sample n_per_chain from each
    by_chain: dict[int, list[dict[str, Any]]] = {}
    for order in candidates:
        chain_id = order.get("chain_id")
        if chain_id is None:
            continue
        by_chain.setdefault(chain_id, []).append(order)

    sampled: list[dict[str, Any]] = []
    for chain_id, orders in sorted(by_chain.items()):
        # Sort by order_id for determinism before sampling
        orders_sorted = sorted(orders, key=lambda o: o.get("order_id", ""))
        k = min(n_per_chain, len(orders_sorted))
        chosen = rng.sample(orders_sorted, k)
        sampled.extend(chosen)

    # Strip PII
    return [_strip_pii(o) for o in sampled]


def _strip_pii(order: dict[str, Any]) -> dict[str, Any]:
    """Remove personally identifying fields from an order dict."""
    return {k: v for k, v in order.items() if k not in _PII_FIELDS}
