"""Deterministic sampling of historical orders for the benchmark.

Samples terminal-demand orders (filled + the champion's failures) from the app
store as real-world benchmark scenarios. Sampling is deterministic from the
round_id alone — every validator derives the SAME shared subset without
broadcasting the selection (#242: partitioned/diverse per-validator draws retired).

The draw joins the synthetic manifest scenarios as ONE flat benchmark set (there is
no longer a synthetic/historical STAGE split or weighting). The orders replay against
the current benchmark fork (weekly-pinned Anvil), and their per-order RAW delivered
outputs feed the authoritative relative adoption rule
(``epoch/relative_scoring.evaluate_relative_adoption``).
"""

from __future__ import annotations

import hashlib
import json
import logging
import random
from typing import Any

logger = logging.getLogger(__name__)


# Fields to strip for privacy when exposing sampled orders to solvers.
# The solver only needs the trade parameters, not who submitted it.
_PII_FIELDS = {"submitted_by", "interop_address", "user_signature", "hotkey"}


# Params that vary per submission/quote but do not change the trade a solver must
# solve — excluded from the dedup identity so two submissions of the same trade
# with different quote snapshots still collapse to one scenario.
#
# intent_params_hex is DERIVED data: the ABI-encoded blob of these very params,
# rebuilt from the manifest at benchmark time (the stored copy is a stale
# quote-time encoding embedding deadline/nonce/min-output). Keeping it exact made
# it differ on essentially every submission and single-handedly blocked most of
# the collapse: on the live 2026-07-02 corpus the dedup went 393→330 (16%) with
# it in the identity vs 393→173 (55%) without — the other 39 points were all
# re-encodes of byte-identical trades.
_VOLATILE_PARAMS = {"quoted_output", "platform_fee_wei", "intent_params_hex"}

# Swap-style params handled specially by the near-dup bucket key: the pair is
# identity, the amount is bucketed by order of magnitude, and the slippage guard
# scales with the amount (so it would defeat the bucketing if kept exact).
_BUCKETED_PARAMS = {"input_token", "output_token", "input_amount", "min_output_amount"}


# Stage-2 SHARED corpus size per chain — THE SINGLE SOURCE OF TRUTH, consensus-
# relevant and fleet-uniform. The corpus is a round-seeded SHARED draw (#242), but
# the size is a MULTIPLIER on that draw: ``rng.sample(orders, k=min(N, len))`` with a
# different N selects a different-membership subset even from the identical round_id
# seed → the "shared" corpus is no longer shared → champion-vs-challenger scores
# differ fleet-wide → divergent independent verdicts → the adoption quorum cannot
# form, AND the leader's benchmark_pack_hash (built with this same constant) would no
# longer match the corpus it actually scored. So it is a CODE constant, NOT a per-
# validator env (was BENCHMARK_HISTORICAL_SAMPLES; our prod lead forced it to 10
# while bare followers defaulted to 50 — the concrete live split this removes).
STAGE2_CORPUS_SAMPLES: int = 50


def sample_historical_orders(
    app_store: Any,
    round_id: str,
    chain_ids: list[int] | None = None,
    n_per_chain: int = STAGE2_CORPUS_SAMPLES,
    exclude_statuses: set[str] | None = None,
    records: list[dict[str, Any]] | None = None,
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

    Duplicate demand is collapsed before the draw (see ``_dedup_key``): exact
    re-submissions of one trade, and near-dups on the same pair within the same
    order-of-magnitude amount, count as ONE candidate — the n_per_chain slots go
    to distinct scenarios instead of copies, and corpus-stuffing (spamming one
    trade to weight the draw) loses its cheapest form.

    The draw is seeded by ``round_id`` ALONE, so EVERY validator derives the
    IDENTICAL subset without broadcasting the selection — one shared corpus that
    the champion-vs-challenger comparison is run over and ratified by quorum (#242).
    Per-validator / partitioned draws were retired: a disjoint slice makes a
    *concentrated* improvement invisible (only validators holding the targeted
    orders would vote ADOPT → no quorum) and decentralized cross-validation needs
    reproducible cross-machine sim + a cheap verification + slashing we don't have.

    Args:
        app_store: AppIntentStore with list_orders().
        round_id: The current round identifier (the sole sample seed).
        chain_ids: Only include orders from these chains. None = all chains.
        n_per_chain: Target sample size per chain (may be smaller if
            insufficient historical orders).
        exclude_statuses: Order statuses to exclude. Default (None) = include the
            terminal-demand set {filled, rejected, expired}; pass a set to instead
            include all statuses except those given.
        records: Pre-built candidate orders (e.g. a chain-derived corpus). When
            provided, they are the source instead of app_store.list_orders() —
            same filter/sample/PII logic. None (default) keeps the local-store path.

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
    candidates = _filter_candidates(
        all_orders, include_statuses=include_statuses,
        exclude_statuses=exclude_statuses, chain_ids=chain_ids,
    )

    if not candidates:
        return []

    # Collapse duplicate demand BEFORE the draw so the fixed n_per_chain slots
    # carry distinct scenarios. Two orders with identical solver-relevant params
    # build literally identical IntentStates (replay forks at the round pin, never
    # the order's block), so a duplicate adds zero signal while burning a redundant
    # scoreIntent per submission through the serialized sim — and it lets anyone
    # weight the corpus toward their solver's best trade by spamming copies.
    # Deterministic (pure function of order content, order-independent min), so
    # every validator still derives the identical post-dedup pool.
    candidates = _dedup_candidates(candidates)

    # Deterministic RNG seed: round_id ALONE → every validator draws the identical
    # shared subset (no per-validator seed, no broadcast).
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


def _filter_candidates(
    all_orders: list[dict[str, Any]],
    *,
    include_statuses: set[str] | None,
    exclude_statuses: set[str] | None,
    chain_ids: list[int] | None,
) -> list[dict[str, Any]]:
    """Status/chain candidate filter shared by the canonical draw and the
    veto-slice partition — one filter so both derive from the identical pool."""
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
    return candidates


def _amount_decade(amount: Any) -> str:
    """Order-of-magnitude bucket for an integer amount string (wei/base units).

    Non-integer / non-positive values fall back to the raw value so they never
    wrongly collapse with anything else.
    """
    try:
        value = int(str(amount))
    except (TypeError, ValueError):
        return f"raw:{amount}"
    if value <= 0:
        return f"raw:{amount}"
    # len(str(v)) - 1 == floor(log10(v)) for positive ints — no float involved,
    # so the bucket is exact and platform-independent.
    return f"e{len(str(value)) - 1}"


def _dedup_key(order: dict[str, Any]) -> str:
    """Canonical identity of the trade an order asks a solver to solve.

    Swap-style orders (input_token/output_token/input_amount present) get a
    NEAR-dup key: same pair + same order-of-magnitude amount collapse, with the
    amount-scaled slippage guard excluded and every other param kept exact (so an
    app's extra meaningful params — recipient, path, … — never wrongly collapse).
    Orders without the swap triple fall back to EXACT-shape identity over all
    non-volatile params.
    """
    params = order.get("params") or {}
    core = {k: v for k, v in params.items() if k not in _VOLATILE_PARAMS}
    prefix = [order.get("app_id", ""), order.get("intent_function", ""),
              order.get("chain_id")]
    input_token = core.get("input_token")
    output_token = core.get("output_token")
    input_amount = core.get("input_amount")
    if input_token and output_token and input_amount is not None:
        rest = {k: v for k, v in core.items() if k not in _BUCKETED_PARAMS}
        parts = prefix + [str(input_token).lower(), str(output_token).lower(),
                          _amount_decade(input_amount), rest]
    else:
        parts = prefix + [core]
    return json.dumps(parts, sort_keys=True, separators=(",", ":"), default=str)


def _representative_rank(order: dict[str, Any]) -> tuple[int, str]:
    """Deterministic preference among duplicate orders: a filled order first (it
    carries real tx/block metadata), then lowest order_id."""
    filled_first = 0 if order.get("status", "").lower() == "filled" else 1
    return (filled_first, order.get("order_id", ""))


def _dedup_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep one deterministic representative per trade shape (see _dedup_key)."""
    best: dict[str, dict[str, Any]] = {}
    for order in candidates:
        key = _dedup_key(order)
        current = best.get(key)
        if current is None or _representative_rank(order) < _representative_rank(current):
            best[key] = order
    if len(best) < len(candidates):
        logger.info(
            "Stage-2 dedup: %d candidate orders -> %d distinct trade shapes",
            len(candidates), len(best),
        )
    return list(best.values())


def _strip_pii(order: dict[str, Any]) -> dict[str, Any]:
    """Remove personally identifying fields from an order dict."""
    return {k: v for k, v in order.items() if k not in _PII_FIELDS}


# ═════════════════════════════════════════════════════════════════════════════
# Distributed-veto slice partitioning (Phase 0 — observe-only coverage)
# ═════════════════════════════════════════════════════════════════════════════
# Fleet-uniform, consensus-relevant CODE constants (the STAGE2_CORPUS_SAMPLES
# precedent above — never per-validator envs).
VETO_SLICE_SIZE: int = 50
VETO_CALIBRATION_ORDERS: int = 5


def order_replay_hash(order: dict[str, Any]) -> str:
    """Content hash of an order's REPLAY IDENTITY — what the benchmark consumes.

    Covers exactly the fields the scenario builder replays: order_id, app_id,
    chain_id, intent_function, and the FULL params dict (including the
    dedup-volatile quoted_output/platform_fee_wei/intent_params_hex — they feed
    the IntentState verbatim, so they are replay-relevant even though they are
    excluded from the near-dup identity). Deliberately EXCLUDES everything
    mutable post-hoc or absent from the synced view — status, tx_hash,
    block_number, plan, scores, consensus_result, and the PII fields the
    /v1/orders?full=1 projection blanks (user_signature) — so the leader's local
    record and a follower's OrderSync copy hash identically. Do NOT reuse
    _dedup_key here: it drops replay-relevant fields by design.
    """
    params = order.get("params") or {}
    payload = {
        "order_id": order.get("order_id", ""),
        "app_id": order.get("app_id", ""),
        "chain_id": order.get("chain_id"),
        "intent_function": order.get("intent_function", "swap"),
        "params": {k: params[k] for k in sorted(params)},
    }
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def partition_follower_slices(
    app_store: Any,
    round_id: str,
    *,
    chain_ids: list[int],
    slice_size: int = VETO_SLICE_SIZE,
    records: list[dict[str, Any]] | None = None,
) -> list[list[dict[str, Any]]]:
    """Disjoint follower veto-slices from the corpus REMAINDER.

    The leader's canonical draw (``sample_historical_orders`` — byte-identical,
    untouched, still the sole adoption corpus and pack-hash input) is EXCLUDED
    first; only the remainder is partitioned, so arming the veto feature can
    never change the canonical corpus a mixed-version fleet re-derives.

    The #242 rationale for retiring partitioned draws (a disjoint slice makes a
    concentrated improvement invisible to the adoption quorum) does not apply
    here: slices never vote ADOPT — they only surface per-order HARD-VETO
    evidence (catastrophic cut / dropped order) that the leader re-benchmarks
    itself before honoring, which is also the cheap-verification piece #242
    noted was missing.

    ``chain_ids`` is REQUIRED and must stay within the round-anchored pin
    chains (``consensus/round_anchor.ROUND_ANCHOR_CHAINS``): the harness takes
    ONE scalar fork_block, so an order from an unpinned chain would replay at
    live head and produce unconfirmable veto claims. Widening needs the
    per-chain fork_block map the round_anchor comment demands.

    Deterministic from ``round_id`` alone (shuffle seed ``{round_id}:veto-slices``)
    — anyone can re-derive and audit the partition. Slice→validator ASSIGNMENT
    must NOT be derived from this (or round_id/committee_hash): it is seeded
    with post-close entropy at fan-out time (``epoch/distributed_veto``), which
    is what prevents an operator from pre-positioning a submission into its own
    colluding validator's slice.

    Returns slices of ``slice_size`` (last one may be short — partial coverage
    beats none), PII-stripped like the canonical draw. Empty when the remainder
    is empty.
    """
    if not chain_ids:
        raise ValueError(
            "chain_ids is required (round-anchored pin chains only — the "
            "harness has a single scalar fork_block)"
        )

    if records is not None:
        all_orders = records
    else:
        try:
            all_orders = app_store.list_orders()
        except Exception as exc:
            logger.warning("Failed to list orders for veto-slice partition: %s", exc)
            return []

    candidates = _filter_candidates(
        all_orders,
        include_statuses={"filled", "rejected", "expired"},
        exclude_statuses=None,
        chain_ids=list(chain_ids),
    )
    if not candidates:
        return []
    candidates = _dedup_candidates(candidates)

    # Exclude the TRUE canonical draw: same call shape production uses
    # (chain_ids=None — the per-chain rng consumption order matters, so a
    # chain-filtered recomputation could diverge from the real draw the moment
    # a second chain appears in the store). Pass the SNAPSHOT we just took
    # (records=all_orders): the store keeps growing, and re-listing inside the
    # exclusion call would derive the draw from a different corpus than the
    # candidates being partitioned — draw membership is corpus-size-sensitive,
    # so that skew leaks canonical-draw orders into slices. Same reason the
    # wire layer must pass ``records`` from the corpus snapshot that sealed
    # the round's pack hash, not re-list at fan-out time.
    leader_ids = {
        o.get("order_id")
        for o in sample_historical_orders(app_store, round_id, records=all_orders)
    }

    remainder = [o for o in candidates if o.get("order_id") not in leader_ids]
    if not remainder:
        return []
    remainder.sort(key=lambda o: o.get("order_id", ""))

    seed = int.from_bytes(
        hashlib.sha256(f"{round_id}:veto-slices".encode("utf-8")).digest()[:8],
        "big",
    )
    random.Random(seed).shuffle(remainder)

    slices = [
        [_strip_pii(o) for o in remainder[i:i + slice_size]]
        for i in range(0, len(remainder), slice_size)
    ]
    logger.info(
        "[distributed-veto] partitioned %d remainder orders into %d slice(s) "
        "of <=%d for round %s (corpus %d, canonical draw %d)",
        len(remainder), len(slices), slice_size, round_id,
        len(candidates), len(leader_ids),
    )
    return slices


def calibration_overlap(
    app_store: Any,
    round_id: str,
    *,
    chain_ids: list[int],
    n: int = VETO_CALIBRATION_ORDERS,
    records: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Deterministic calibration orders drawn FROM the canonical leader draw.

    Appended to every follower slice as a shared cross-validator overlap: the
    leader has its own same-pin rows for these orders, so a follower whose
    calibration rows drift beyond the noise band is flagged as ENVIRONMENT
    divergence (no reliability strike) instead of having its slice evidence
    misread. Never contributes veto evidence itself.
    """
    draw = sample_historical_orders(app_store, round_id, records=records)
    pool = [
        o for o in draw
        if o.get("chain_id") in set(chain_ids)
    ]
    pool.sort(key=lambda o: o.get("order_id", ""))
    if not pool:
        return []
    seed = int.from_bytes(
        hashlib.sha256(f"{round_id}:veto-calibration".encode("utf-8")).digest()[:8],
        "big",
    )
    return random.Random(seed).sample(pool, min(n, len(pool)))
