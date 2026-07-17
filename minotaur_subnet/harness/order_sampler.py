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
import itertools
import json
import logging
import random
from typing import Any

from minotaur_subnet.harness.round_store import opened_epoch_from_round_id

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


# Identity / derived params that must NEVER enter a stored quote CASE. A quote case
# is served PUBLICLY (/v1/quotes) and replicated fleet-wide, so any caller- or
# server-supplied address / authorization / identity field is stripped before
# storage. This is a DENYLIST (consistent with _PII_FIELDS): the trade-defining
# params (input_token/output_token/input_amount/min_output_amount and app-generic
# trade keys) are deliberately NOT listed and survive. Union'd with _PII_FIELDS and
# the volatile quote fields into QUOTE_PARAM_STRIP_FIELDS. NOTE: if a non-swap app
# is ever added whose trade legitimately needs an address param, revisit this as an
# allowlist — a denylist can miss a novel identity key.
_QUOTE_IDENTITY_PARAMS = {
    "receiver", "recipient", "to", "beneficiary", "owner", "user_address",
    "from", "sender", "spender", "user_nonce", "nonce", "deadline",
    "app_address", "intent_selector", "intent_params_hex",
    "permit", "permit_signature", "signature",
}

# The full set stripped from a quote's params at capture time (identity + PII +
# volatile). quote_case_id already ignores _VOLATILE_PARAMS internally; capture
# strips the whole set so the STORED (and publicly served) params carry only the
# trade descriptor.
QUOTE_PARAM_STRIP_FIELDS = _PII_FIELDS | _VOLATILE_PARAMS | _QUOTE_IDENTITY_PARAMS


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

# Stage-2 quote-corpus size per chain. Capped at the SAME fleet-uniform constant
# as the historical-order draw (per the user requirement) so quotes cannot
# out-weight real orders in the scored set, and — like STAGE2_CORPUS_SAMPLES —
# a per-validator value would split the pack hash. A CODE constant, never env.
QUOTE_CORPUS_SAMPLES: int = STAGE2_CORPUS_SAMPLES

# Round-anchored quote retention window, in opened_epoch units (EPOCH_SECONDS=60s,
# so 20160 ≈ 14 days). A quote is kept while its first-seen capturing opened_epoch
# is >= current_opened_epoch − this. CONSENSUS-RELEVANT and fleet-uniform (it bounds
# the population the round draw sees), so a CODE constant, never env — same class as
# QUOTE_CORPUS_SAMPLES. Must comfortably exceed the span between any live round's
# opened_epoch and the oldest still-benchmarkable round so retention can never delete
# a row inside a live sampling window.
QUOTE_RETENTION_EPOCHS: int = 20160


def quote_case_id(
    app_id: str, chain_id: Any, intent_function: str, params: dict[str, Any] | None,
) -> str:
    """Content-addressed id for a quote CASE — the storage-time collapse key.

    Keyed by the SAME trade-SHAPE the sampler dedups on (``_dedup_key``): same pair +
    order-of-magnitude amount + other non-bucketed params collapse to ONE id. So exact-
    amount spam upserts to a single stored row at CAPTURE — bounding table growth to
    distinct demand shapes with no wall-clock/arrival ordering (the removed newest-N
    row cap was arrival-ordered, hence non-deterministic; this is not). The storage key
    and the draw-time dedup are now the SAME function, so ``_dedup_candidates`` is a
    no-op over the stored set and the corpus stores exactly what it scores. Fleet-
    uniform → the round-seeded quote draw and the pack hash agree across validators.
    ``q_`` prefix + 32 hex chars.
    """
    order_shaped = {
        "app_id": app_id or "",
        "intent_function": intent_function or "swap",
        "chain_id": chain_id,
        "params": params or {},
    }
    return "q_" + hashlib.sha256(_dedup_key(order_shaped).encode("utf-8")).hexdigest()[:32]


def retired_app_chain_keys(
    app_store: Any, at_epoch: int | None = None,
) -> set[tuple[str, int]]:
    """``(app_id, chain_id)`` pairs effectively retired for a round at ``at_epoch``
    — dropped from the draw.

    Deregistration is deregister-NOT-delete: retiring a deployment keeps every
    order row in the store (still queryable via ``/orders?app_id=...``), but its
    historical orders leave the Stage-2 corpus so a deregistered app stops driving
    scoring. Because the benchmark_pack_hash draws from this SAME function, the
    exclusion propagates to the hash automatically — the scored corpus and the
    fingerprint stay identical, and a fleet that disagrees on an app's retirement
    diverges LOUDLY (PACK_HASH_MISMATCH → no adoption) instead of silently scoring
    different corpora under one hash.

    ``at_epoch`` is the round's ``opened_epoch`` (parsed from round_id). RETIRED
    deployments drop immediately; RETIRING ones drop only once the round reaches
    their ``retire_effective_epoch`` — a round-anchored, fleet-uniform cutover
    (see ``DeploymentResult.is_effectively_retired``). The exclusion is
    corpus-membership-affecting, so it MUST be fleet-uniform (same consensus class
    as ``STAGE2_CORPUS_SAMPLES``): both status and effective-epoch are replicated by
    app-sync, so every validator computes the identical set. Fail-open to empty on a
    store error: a transient read must never silently shrink one validator's corpus
    (that validator's hash then simply fails to match and it drops out of quorum,
    rather than corrupting adoption).
    """
    retired: set[tuple[str, int]] = set()
    try:
        apps = app_store.list_apps()
    except Exception as exc:
        logger.warning("retired_app_chain_keys: list_apps failed: %s", exc)
        return retired
    for app in apps:
        app_id = getattr(app, "app_id", None)
        if not app_id:
            continue
        try:
            deployments = app_store.get_deployments(app_id)
        except Exception:
            continue
        for chain_id, dep in deployments.items():
            if dep.is_effectively_retired(at_epoch):
                try:
                    retired.add((app_id, int(chain_id)))
                except (TypeError, ValueError):
                    continue
    return retired


def sample_historical_orders(
    app_store: Any,
    round_id: str,
    chain_ids: list[int] | None = None,
    n_per_chain: int = STAGE2_CORPUS_SAMPLES,
    exclude_statuses: set[str] | None = None,
    records: list[dict[str, Any]] | None = None,
    exclude_app_chains: set[tuple[str, int]] | None = None,
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
        exclude_app_chains: ``(app_id, chain_id)`` pairs to drop from the draw
            (retired/deregistered deployments — see ``retired_app_chain_keys``).
            None (default) auto-derives them from ``app_store`` so EVERY call site
            (runtime draw, pack hash, veto slice) excludes the identical set; pass
            an explicit set to override (tests, or to reuse a precomputed set).

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

    # Deregistered deployments drop out of the draw — auto-derived from the store
    # when not supplied so the runtime draw, the pack hash, and the veto slice all
    # exclude the identical set. The round-anchored cutover (RETIRING → effective at
    # a stamped epoch) is applied via the round's opened_epoch, parsed from round_id
    # so this stays a pure function of the id that already seeds the draw. See
    # ``retired_app_chain_keys``.
    if exclude_app_chains is None:
        at_epoch = opened_epoch_from_round_id(round_id)
        exclude_app_chains = retired_app_chain_keys(app_store, at_epoch)

    # Filter by status + chain. NOTE: we do NOT require a block_number. The
    # benchmark forks at self._epoch_block_number (the round/env pin, or live head
    # by default) — never the order's own block — so an order without a fill block
    # (rejected/expired demand) replays against current state just like a filled
    # one. Requiring block_number here was the survivorship-bias source (#228), not
    # a replay necessity.
    candidates = _filter_candidates(
        all_orders, include_statuses=include_statuses,
        exclude_statuses=exclude_statuses, chain_ids=chain_ids,
        exclude_app_chains=exclude_app_chains,
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


def _quote_candidates(
    app_store: Any,
    round_id: str,
    *,
    chain_ids: list[int] | None = None,
    records: list[dict[str, Any]] | None = None,
    exclude_app_chains: set[tuple[str, int]] | None = None,
) -> list[dict[str, Any]]:
    """The deduped, round-anchored-cutoff quote CANDIDATE pool (order-shaped).

    Shared by :func:`sample_historical_quotes` (the canonical draw) and the veto
    quote-remainder partition, so BOTH see the identical eligible set. If the veto
    partition rebuilt candidates any other way, the remainder could include quotes
    the canonical draw never saw — quotes with no leader row to re-verify a veto
    claim against → junk vetoes. Applies the TWO-SIDED round-anchored cutoff (see
    ``sample_historical_quotes`` docstring), the quote_id→order_id alias, the chain
    filter, retirement exclusion, and near-dup dedup. NO status filter (quotes have
    no status).
    """
    if records is not None:
        rows = records
    else:
        try:
            rows = app_store.list_quotes()
        except Exception as exc:
            logger.warning("Failed to list quotes for Stage 2 sampling: %s", exc)
            return []

    # Round-anchored TWO-SIDED cutoff: eligible iff captured_opened_epoch is in
    # [draw_epoch - QUOTE_RETENTION_EPOCHS, draw_epoch). Unstamped (None) excluded.
    # See sample_historical_quotes for the full determinism rationale.
    draw_epoch = opened_epoch_from_round_id(round_id)
    _floor_epoch = None if draw_epoch is None else int(draw_epoch) - QUOTE_RETENTION_EPOCHS

    # Order-shape (alias quote_id -> order_id) so the shared order helpers apply.
    candidates_raw: list[dict[str, Any]] = []
    for q in rows:
        qid = q.get("quote_id")
        if not qid:
            continue
        if draw_epoch is not None:
            ce = q.get("captured_opened_epoch")
            if ce is None or int(ce) >= int(draw_epoch) or int(ce) < _floor_epoch:
                continue  # unanchored, captured this-round-or-later, or below retention
        o = dict(q)
        o["order_id"] = qid
        candidates_raw.append(o)

    if exclude_app_chains is None:
        exclude_app_chains = retired_app_chain_keys(
            app_store, opened_epoch_from_round_id(round_id))

    candidates = _filter_candidates(
        candidates_raw, include_statuses=None, exclude_statuses=None,
        chain_ids=chain_ids, exclude_app_chains=exclude_app_chains,
    )
    if not candidates:
        return []
    return _dedup_candidates(candidates)


def sample_historical_quotes(
    app_store: Any,
    round_id: str,
    chain_ids: list[int] | None = None,
    n_per_chain: int = QUOTE_CORPUS_SAMPLES,
    records: list[dict[str, Any]] | None = None,
    exclude_app_chains: set[tuple[str, int]] | None = None,
) -> list[dict[str, Any]]:
    """Deterministically sample historical QUOTE cases for the Stage-2 corpus.

    The quote analogue of :func:`sample_historical_orders`. Quotes are DEMAND the
    champion may or may not serve — a quote for a pair the champion can't route is
    exactly the blind-spot signal we want challengers scored on (it becomes a
    ``blind_spot_cover`` win the instant a solver fills it). Fake/low-provenance
    demand is acceptable here BY DESIGN: it still pushes miners to widen coverage.

    Quote cases are ORDER-SHAPED (app_id / chain_id / intent_function / params),
    so this reuses the SAME dedup, chain filter, retirement exclusion and PII
    strip as the order draw — the quote_id is aliased onto ``order_id`` so those
    shared helpers key on it unchanged. The draw is seeded by ``{round_id}:quotes``
    (a distinct salt from the order draw, so the two selections are independent
    yet each is a pure function of round_id — every validator derives the identical
    quote subset without broadcasting it). Capped at ``QUOTE_CORPUS_SAMPLES`` per
    chain — the same fleet-uniform cap as historical orders.

    Returns a list of quote dicts (PII-stripped, each carrying both ``quote_id``
    and the aliased ``order_id``). Empty when no quotes exist.
    """
    candidates = _quote_candidates(
        app_store, round_id, chain_ids=chain_ids, records=records,
        exclude_app_chains=exclude_app_chains,
    )
    if not candidates:
        return []

    seed = int.from_bytes(
        hashlib.sha256(f"{round_id}:quotes".encode("utf-8")).digest()[:8], "big"
    )
    rng = random.Random(seed)

    by_chain: dict[int, list[dict[str, Any]]] = {}
    for q in candidates:
        chain_id = q.get("chain_id")
        if chain_id is None:
            continue
        by_chain.setdefault(chain_id, []).append(q)

    sampled: list[dict[str, Any]] = []
    for chain_id, quotes in sorted(by_chain.items()):
        quotes_sorted = sorted(quotes, key=lambda o: o.get("order_id", ""))
        k = min(n_per_chain, len(quotes_sorted))
        sampled.extend(rng.sample(quotes_sorted, k))

    return [_strip_pii(q) for q in sampled]


def _filter_candidates(
    all_orders: list[dict[str, Any]],
    *,
    include_statuses: set[str] | None,
    exclude_statuses: set[str] | None,
    chain_ids: list[int] | None,
    exclude_app_chains: set[tuple[str, int]] | None = None,
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
        if (
            exclude_app_chains
            and (order.get("app_id"), order.get("chain_id")) in exclude_app_chains
        ):
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

    ``chain_ids`` is REQUIRED and must stay within the round's FORK-PINNED chains
    (``RoundState.fork_pins`` keys — the anchor chain plus, when
    BENCHMARK_ALL_DEPLOYMENT_CHAINS is armed, every deployment chain incl.
    Ethereum). Each slice is single-chain (grouped by chain below) and
    ``run_slice_bench`` forks that chain at the pin the assignment carries for it,
    so multi-chain coverage is just multiple single-chain slices. An order on a
    chain with NO pin would replay at live head → an unconfirmable veto, so the
    caller derives chain_ids from ``fork_pins`` and never passes an unpinned chain.

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
            "chain_ids is required (must be the round's fork-pinned chains; "
            "each slice is single-chain and forked at that chain's pin)"
        )

    if records is not None:
        all_orders = records
    else:
        try:
            all_orders = app_store.list_orders()
        except Exception as exc:
            logger.warning("Failed to list orders for veto-slice partition: %s", exc)
            return []

    # Retired deployments are excluded from BOTH the canonical draw and the
    # remainder, so a deregistered app's orders never leak into a veto slice. Uses
    # the SAME round-anchored at_epoch (parsed from round_id) as the canonical draw,
    # or the RETIRING cutover could diverge between the two.
    exclude_app_chains = retired_app_chain_keys(
        app_store, opened_epoch_from_round_id(round_id))
    candidates = _filter_candidates(
        all_orders,
        include_statuses={"filled", "rejected", "expired"},
        exclude_statuses=None,
        chain_ids=list(chain_ids),
        exclude_app_chains=exclude_app_chains,
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
        for o in sample_historical_orders(
            app_store, round_id, records=all_orders,
            exclude_app_chains=exclude_app_chains,
        )
    }

    remainder = [o for o in candidates if o.get("order_id") not in leader_ids]
    n_order_remainder = len(remainder)

    # QUOTE-REMAINDER MERGE (quorum>1 hardening). When BENCHMARK_QUOTE_CORPUS is armed
    # the quote draw is part of the SCORED corpus, so followers must independently
    # cross-check quote scenarios too. Merge the quote remainder INTO this same slice
    # pool (rather than a parallel quote-slice set): the veto registry is strictly one
    # assignment + one response per validator, so a parallel set would be dropped as a
    # stale assignment; and merging gives every assigned follower a quote cross-check
    # regardless of fleet size (a concatenated set would starve later quote slices at
    # low follower count via assign_slices' opportunistic coverage). The quote CANONICAL
    # draw is excluded exactly as the order one above, so slices never overlap either
    # adoption corpus. Quote cases are order-shaped + share the anchor chains, so they
    # keep each slice single-chain (run_slice_bench refuses multi-chain). INERT while the
    # flag is off → slices byte-identical to today. order_replay_hash / _order_label /
    # _production_order_lookup already handle the content-addressed q_ ids (Phase 2).
    from minotaur_subnet.shared.feature_flags import quote_corpus_enabled
    if quote_corpus_enabled():
        try:
            all_quotes = app_store.list_quotes()
        except Exception as exc:
            logger.warning("veto: list_quotes failed, skipping quote slices: %s", exc)
            all_quotes = []
        q_candidates = _quote_candidates(
            app_store, round_id, chain_ids=list(chain_ids),
            records=all_quotes, exclude_app_chains=exclude_app_chains,
        )
        if q_candidates:
            # Exclude the canonical QUOTE draw with the SAME-snapshot / chain_ids=None
            # discipline the order path uses above (per-chain rng-consumption order).
            q_leader_ids = {
                q.get("order_id")
                for q in sample_historical_quotes(
                    app_store, round_id, records=all_quotes,
                    exclude_app_chains=exclude_app_chains,
                )
            }
            remainder.extend(
                q for q in q_candidates if q.get("order_id") not in q_leader_ids
            )

    if not remainder:
        return []
    remainder.sort(key=lambda o: o.get("order_id", ""))

    seed = int.from_bytes(
        hashlib.sha256(f"{round_id}:veto-slices".encode("utf-8")).digest()[:8],
        "big",
    )
    random.Random(seed).shuffle(remainder)

    # Per-chain slices: run_slice_bench forks exactly ONE chain per slice (at that
    # chain's pin from the assignment), so a slice must never mix chains. Group the
    # already-shuffled remainder by chain — preserving the shuffle order WITHIN each
    # chain — then chunk each chain independently. A single-chain chain_ids yields one
    # group whose slices are byte-identical to the pre-multichain flat chunking; a
    # multi-chain chain_ids (e.g. Base + Ethereum under BENCHMARK_ALL_DEPLOYMENT_CHAINS)
    # yields disjoint single-chain slices per chain. Chains are sorted so the partition
    # stays deterministic and auditable.
    by_chain: dict[Any, list[dict[str, Any]]] = {}
    for o in remainder:
        by_chain.setdefault(o.get("chain_id"), []).append(o)
    per_chain = [
        [col[i:i + slice_size] for i in range(0, len(col), slice_size)]
        for _chain, col in sorted(by_chain.items(), key=lambda kv: str(kv[0]))
    ]
    # ROUND-ROBIN interleave across chains. Slice→validator assignment
    # (epoch/distributed_veto) covers the LOWEST slice indices first, so a plain
    # per-chain concatenation would hand the first-sorted chain every low index and
    # STARVE the others (notably the corpus-dominant Base) of coverage at a small
    # validator count. Interleaving spreads coverage evenly across chains. A single
    # chain yields one column → order unchanged → byte-identical to the old chunking.
    slices = [
        [_strip_pii(o) for o in sl]
        for tier in itertools.zip_longest(*per_chain)
        for sl in tier
        if sl is not None
    ]
    logger.info(
        "[distributed-veto] partitioned %d remainder (%d order + %d quote) into %d "
        "slice(s) of <=%d across %d chain(s) for round %s (order corpus %d, canonical draw %d)",
        len(remainder), n_order_remainder, len(remainder) - n_order_remainder,
        len(slices), slice_size, len(by_chain), round_id, len(candidates), len(leader_ids),
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
    # ``n`` calibration orders PER chain: each single-chain slice is appended only its
    # OWN chain's calibration (a slice mixing an order-chain with a calibration-chain
    # would be REFUSED as multi_chain by run_slice_bench). Flat, chain-sorted result;
    # a single-chain chain_ids returns exactly ``n`` as before.
    result: list[dict[str, Any]] = []
    for chain_id in sorted(set(chain_ids), key=str):
        pool = [o for o in draw if o.get("chain_id") == chain_id]
        pool.sort(key=lambda o: o.get("order_id", ""))
        if not pool:
            continue
        seed = int.from_bytes(
            hashlib.sha256(
                f"{round_id}:veto-calibration:{chain_id}".encode("utf-8")
            ).digest()[:8],
            "big",
        )
        result.extend(random.Random(seed).sample(pool, min(n, len(pool))))
    return result
