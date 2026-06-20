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


def _order_partition(order_id: str, round_id: str, validator_count: int) -> int:
    """Deterministically assign an order to one of ``validator_count`` validators.

    ``hash(order_id + round_id) % V`` — stable across all validators (so the
    partition is verifiable, not leader-chosen) and re-shuffles each round so
    coverage rotates over time.
    """
    h = hashlib.sha256(f"{order_id}:{round_id}".encode("utf-8")).digest()
    return int.from_bytes(h[:8], "big") % validator_count


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
    validator_index: int | None = None,
    validator_count: int | None = None,
) -> list[dict[str, Any]]:
    """Deterministically sample historical TERMINAL-DEMAND orders for Stage 2.

    The corpus is terminal real demand — ``filled`` orders (the champion solved)
    PLUS ``rejected``/``expired`` orders (the champion FAILED). Previously it was
    filled-only, which was survivorship-biased: challengers were graded only on
    demand the champion already fills, so there was no benchmark pressure to solve
    what it fails (#228). Now a challenger that produces a valid fill where the
    champion could not earns credit through the normal score — the failed order is
    a scenario the champion scores ~0 on.

    This is sound because Stage-2 replay forks at the BENCHMARK pin
    (``self._epoch_block_number`` — the round-anchor / ``BENCHMARK_EPOCH_BLOCK``, or
    live head by default), NEVER the order's own block. So an unfilled order (no
    fill block) replays against current state exactly like a filled one — no
    per-order fork anchor is needed, and the draw stays deterministic per the seed.

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
        exclude_statuses: Order statuses to exclude. Default (None) = include the
            terminal-demand set {filled, rejected, expired}; pass a set to instead
            include all statuses except those given.
        records: Pre-built candidate orders (e.g. a chain-derived corpus, plan
            Phase 5b). When provided, they are the source instead of
            app_store.list_orders() — same filter/sample/PII logic. None (default)
            keeps the local-store path byte-for-byte.
        validator_seed: Per-validator seed component (None = shared/legacy draw).

    Returns:
        List of order dicts, PII-stripped. May be empty if no history exists.
    """
    if exclude_statuses is None:
        # Terminal demand: orders the network actually had to solve. Includes the
        # champion's FAILURES (rejected/expired), not just its successes (filled),
        # so a challenger gets benchmark credit for filling demand the champion
        # could not (#228). Safe because the benchmark forks at the round/live-head
        # pin, NOT the order's block — see the block_number note below — so unfilled
        # orders replay against current state exactly like filled ones. Excludes
        # in-flight (open/assigned) and user-cancelled orders (not solver signal).
        include_statuses = {"filled", "rejected", "expired"}
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

    # Filter by status + chain. NOTE: we do NOT require a block_number. The
    # benchmark forks at self._epoch_block_number (the round/env pin, or live head
    # by default) — never the order's own block — so an order without a fill block
    # (rejected/expired demand) replays against current state just like a filled
    # one. Requiring block_number here was the survivorship-bias source (#228), not
    # a replay necessity.
    candidates = []
    for order in all_orders:
        status = order.get("status", "").lower()
        if include_statuses is not None and status not in include_statuses:
            continue
        if exclude_statuses is not None and status in exclude_statuses:
            continue
        if chain_ids is not None and order.get("chain_id") not in chain_ids:
            continue
        candidates.append(order)

    # PARTITION mode (max coverage): when (validator_index, validator_count) are
    # given, each validator keeps a DISJOINT slice — order assigned to validator
    # ``hash(order_id+round_id) % V``. With V validators each benchmarking up to
    # n_per_chain, the fleet covers min(total, V*n_per_chain) DISTINCT orders
    # (filled or failed) instead of overlapping random subsets — more validators →
    # more coverage. Each validator's slice is deterministic + verifiable, so no
    # single party picks who benchmarks what.
    if validator_index is not None and validator_count and validator_count > 0:
        candidates = [
            o for o in candidates
            if _order_partition(o.get("order_id", ""), round_id, validator_count) == validator_index
        ]

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
