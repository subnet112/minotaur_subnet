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
    (``benchmark_worker._load_historical_scenarios`` — ``quote:`` prefix). The
    fold is INERT while OFF: with this flag off the pack hash is byte-identical to
    a fleet that has no quote-corpus code at all, so a default-OFF rollout is
    hash-invisible and safe to promote.

    This is an env var ONLY to enable the Phase-1 SOAK: at the production quorum=1
    the leader alone benchmarks + certifies (followers do not independently gate),
    so the leader can flip this ON to soak the behaviour while the store replicates
    quotes to every follower. It is otherwise held to the SAME fleet-uniform
    discipline as ``STAGE2_CORPUS_SAMPLES`` / ``BENCHMARK_PACK_V2``: turning it ON
    changes corpus membership, so before quorum is ever raised above 1 it MUST be
    ON fleet-wide (Phase 2 flips the default and promotes to main), or a mixed
    fleet computes divergent pack hashes and strands quorum (PACK_HASH_MISMATCH).

    PHASE-2 STATUS:
      DONE (this PR, inert while the flag is OFF):
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
          REMAINDER into the same follower slice pool (gated on this flag), so followers
          independently cross-check quote scenarios — a quote-overfit throne no longer
          escapes the per-order HARD-VETO. Ids-only assignment + q_ prefix means no
          wire/protocol change. INERT while the flag is off.

      REMAINING before flipping this default ON / raising quorum:
        1. Flag ON fleet-wide (same image + env), verified via a soak where followers
           have synced quotes and recompute MATCHING pack hashes. The flip is a
           deliberate, atomic, separately-reviewed step (per the staging model).
        2. [quorum>1 only] benchmark_anchor_epoch plumbed leader→follower through the
           champion proposal/certify path (B1 #904 + B2 #907 + B3 #908) — the standing
           blocker for quorum>1 on the WHOLE benchmark (orders included), not just quotes.
    """
    return _env_bool("BENCHMARK_QUOTE_CORPUS", default=False)
