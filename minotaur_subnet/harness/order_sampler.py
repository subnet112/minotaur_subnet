"""Deterministic sampling of historical orders for benchmark Stage 2.

Samples filled orders from the app store to use as real-world benchmark
scenarios. Sampling is deterministic from the round_id — all validators
derive the same sample without needing to broadcast the selection.

Stage 2 replays these orders' parameters against the current benchmark
fork (weekly-pinned Anvil), and the resulting per-app scores feed the
adoption rule's per-app non-regression floor.
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
    validator_seed: str | None = None,
) -> list[dict[str, Any]]:
    """Deterministically sample historical FILLED orders for Stage 2.

    SURVIVORSHIP BIAS (issue #228 — intentional, documented): by default the
    corpus is FILLED orders only. Expired / rejected / unsolved demand is dropped
    (wrong status, and no ``block_number`` to re-fork against). The reason is
    replay determinism — a filled order has a known on-chain ``block_number`` to
    reconstruct fork state from; a never-executed order has no such anchor, so it
    cannot be trivially replayed. CONSEQUENCE: challengers are only ever graded on
    demand the champion ALREADY fills, so there is NO benchmark pressure to solve
    the order types the champion currently FAILS. Rewarding "challenger filled
    where the champion could not" (and reconstructing replayable fork-state for
    never-executed orders) is a deliberate design change tied to the same milestone
    that flips ``DISABLE_CHAMPION_ADOPTION=0`` — not a silent default.

    Seed = ``round_id`` alone (``validator_seed=None``, the default) → every
    validator draws the SAME subset (legacy determinism). When ``validator_seed``
    is supplied (e.g. the validator's hotkey/evm), it is mixed into the seed so
    each validator draws a DIFFERENT subset — distributed cross-validation: a
    challenger must beat the champion across the *union* of everyone's subsets,
    which broadens regression coverage and resists overfitting to one fixed set.
    The draw stays deterministic *per validator* (reproducible from
    round_id+identity), so no selection broadcast is needed either way.

    Args:
        app_store: AppIntentStore with list_orders().
        round_id: The current round identifier (part of the sample seed).
        chain_ids: Only include orders from these chains. None = all chains.
        n_per_chain: Target sample size per chain (may be smaller if
            insufficient historical orders).
        exclude_statuses: Order statuses to exclude (default: only include 'filled').
        records: Pre-built candidate orders (e.g. a chain-derived corpus, plan
            Phase 5b). When provided, they are the source instead of
            app_store.list_orders() — same filter/sample/PII logic. None (default)
            keeps the local-store path byte-for-byte.
        validator_seed: Per-validator seed component (None = shared/legacy draw).

    Returns:
        List of order dicts, PII-stripped. May be empty if no history exists.
    """
    if exclude_statuses is None:
        # Default = filled-only (survivorship bias, #228): see the docstring —
        # failed/unsolved demand exerts no benchmark pressure. Deliberate for
        # replay determinism; revisit when adoption is enabled.
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

    # Deterministic RNG seed: round_id alone (shared draw) or round_id+identity
    # (per-validator diverse draw). Mixing the identity in shifts which orders
    # this validator tests without making the draw non-deterministic.
    seed_material = (
        round_id if validator_seed is None else f"{round_id}:{validator_seed}"
    )
    seed = int.from_bytes(
        hashlib.sha256(seed_material.encode("utf-8")).digest()[:8], "big"
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
