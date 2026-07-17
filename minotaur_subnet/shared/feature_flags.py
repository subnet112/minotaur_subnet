"""Process-wide feature flags read from environment.

Keep this module dependency-free so it can be imported from any layer
without creating cycles.
"""

from __future__ import annotations

import os


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name, "")
    if raw == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def cross_chain_enabled() -> bool:
    """Multi-leg / bridge execution is dev-track, not in beta.

    When this is off:
      - The /orders API rejects orders whose input_chain_id differs from
        output_chain_id (or any other multi-chain marker).
      - The blockloop refuses to process a plan whose metadata flags it
        as cross-chain, even if a solver tried to return one.

    Flip on only on dedicated staging/dev environments until the
    cross-chain path clears its Phase 5 exit criteria.
    """
    return _env_bool("CROSS_CHAIN_ENABLED", default=False)


CROSS_CHAIN_DISABLED_MESSAGE = (
    "Cross-chain / multi-leg orders are not enabled in this environment. "
    "Beta scope is single-chain Base (chain 8453). "
    "Set CROSS_CHAIN_ENABLED=1 on a staging target to exercise the dev-track path."
)


def quote_capture_enabled() -> bool:
    """Persist each served /quote as a durable quote-CASE for demand tracking.

    Capture is a LOCAL, best-effort side effect of the quote endpoint (it never
    affects the quote response) and is NOT consensus-relevant on its own — it
    only fills the ``quotes`` store that ``QuoteSync`` then replicates leader →
    follower. It defaults ON so the store fills during the Phase-1 soak while the
    corpus-inclusion flag below is still OFF; flip to 0 as a kill switch if
    capture ever misbehaves. Turning capture off does NOT change any pack hash.
    """
    return _env_bool("BENCHMARK_QUOTE_CAPTURE", default=True)


def quote_corpus_enabled() -> bool:
    """Include sampled historical QUOTES in the scored benchmark corpus.

    CONSENSUS-RELEVANT. When ON, the round-seeded quote draw is folded into the
    benchmark pack hash (``benchmark_pack.compute_pack_hash`` — a separate
    ``QUOTES`` section) and replayed as scored scenarios
    (``benchmark_worker._load_historical_scenarios`` — ``quote:`` prefix).

    DEFAULT ON (Phase 2). The fold stays fully gated: setting
    ``BENCHMARK_QUOTE_CORPUS=0`` restores the INERT path — the pack hash becomes
    byte-identical to a fleet with no quote-corpus code at all — which is the kill
    switch to disable the behaviour fleet-wide without a code revert.

    ROLLOUT DISCIPLINE. Turning this ON changes corpus membership, so — like
    ``STAGE2_CORPUS_SAMPLES`` / ``BENCHMARK_PACK_V2`` — it MUST be uniform across
    the fleet before champion quorum is ever raised above 1, or a mixed fleet
    computes divergent pack hashes and strands quorum (PACK_HASH_MISMATCH).
    Phase 1 soaked it via a leader-only env override at the production quorum=1
    (the leader alone benchmarks + certifies; followers trust-adopt and do not
    gate) while ``QuoteSync`` replicated the quote store to every follower. This
    default flip is Phase 2: the deliberate, atomic, separately-reviewed step that
    makes the flag ON fleet-wide via the image. It reaches followers as they roll
    to the new ``:stable`` image; during that window a still-OFF follower only
    emits benign, non-gating PACK_HASH_MISMATCH dissent (harmless at quorum=1).

    STATUS:
      DONE:
        - Round-anchored SAMPLING CUTOFF: sample_historical_quotes includes only
          quotes first-captured in a strictly earlier round (captured_opened_epoch <
          drawing round's opened_epoch), so the draw is a pure function of round_id +
          frozen pre-round membership — immune to the capture/prune/QuoteSync race.
        - Round-anchored RETENTION: store.prune_quotes drops by captured_opened_epoch
          (keep last QUOTE_RETENTION_EPOCHS), not wall-clock newest-N.
        - Veto DEFENSIVE fixes: veto_wire._order_label / _production_order_lookup and
          benchmark_worker.build_explicit_scenarios handle q_ quote ids, so the leader
          reverify + coverage assert hold the moment quotes enter the scored corpus.
        - Distributed-veto SLICE partition: partition_follower_slices merges the quote
          REMAINDER into the same follower slice pool, so followers independently
          cross-check quote scenarios — a quote-overfit throne no longer escapes the
          per-order HARD-VETO. Ids-only assignment + q_ prefix means no wire change.
        - Phase-1 leader soak (env override, quorum=1): quote draw folds into the pack
          hash + scores (verified live: "Loaded N historical scenarios (… quote)"),
          the round closes clean at quorum=1, QuoteSync replicates the store to followers.
        - Phase-2 default ON fleet-wide.
        - Distributed veto covers EVERY fork-pinned chain (Base + Ethereum under
          BENCHMARK_ALL_DEPLOYMENT_CHAINS), via per-chain single-chain slices, so the
          burden-of-proof cross-check matches the scored set on both chains.

      REMAINING before raising champion quorum above 1:
        1. benchmark_anchor_epoch plumbed leader→follower through the champion
           proposal/certify path (B1 #904 + B2 #907 + B3 #908) — the standing blocker
           for quorum>1 on the WHOLE benchmark (orders included), not just quotes.
    """
    return _env_bool("BENCHMARK_QUOTE_CORPUS", default=True)
