# App Quality Metric — de-DEX-ifying the adoption gate

**Status:** Phase 0 implemented (this PR). Phases 1–3 not started.

---

## 1. How we got here

Minotaur's champion contest compares a challenger against the champion **per
order**, on one number per order. Originally that number was the app's JS
`score` — a 0..1 float, mirrored on-chain as `scoreIntent` BPS (0..10000). That
was app-authored and app-agnostic: every app defined quality for itself.

Then came the DEX aggregator. For a swap, a 0..1 quality score is the wrong
quantity — the right one is **how much the receiver actually got** — and miner
competition is meaningless without it. So `metadata.raw_output` was wired in as
the per-order signal, under time pressure, and the adoption rule was written
around its shape.

The problem is that it didn't *extend* the generic metric, it **displaced** it.
Today there are three metric channels and only the DEX-shaped one matters:

| Channel | Author | Shape | Feeds adoption? |
|---|---|---|---|
| `ScoreResult.score` | app JS | float 0..1 | **No** — displaced |
| `ScoreResult.metadata.raw_output` | app JS | wei string | **Yes — sole gate** |
| `on_chain_score` | app Solidity `scoreIntent` | BPS 0..10000 | **No** — computed, ignored |

## 2. What that costs a non-DEX app

`epoch/relative_scoring.has_delivered_value_rows()` is the fleet-uniform
validity gate, imported at five production sites in `benchmark_worker`:

> *a submission is valid iff it delivered a usable output on >= 1 order… a
> zero-delivery solver is REJECTED, not merely scored 0.*

An app whose orders legitimately move no tokens to a receiver — a rebalancing
vault, whose quality is something like tracking error, and possibly where
**lower is better** — cannot produce a positive delivered amount. So every
submission is rejected as invalid and the app **can never hold a champion**.
That is a structural block, not a ranking nuance.

Three further DEX assumptions are baked into the same rule: higher-is-better
polarity, integer-wei value semantics (`MIN_VALID_OUTPUT`, exact-integer bps
cross-multiplication, the 1% `FLOOR_BPS`), and "delivered" as the meaning of
valid.

## 3. The contract

An app declares its metric in its manifest; the platform compares whatever that
is. Nothing in core needs to know what a swap is.

```jsonc
"quality_metric": {
  "source":   "js_metadata" | "js_score",
  "field":    "raw_output",            // js_metadata only
  "polarity": "higher_better" | "lower_better",
  "scale":    "integer" | "bps" | "ratio_1",
  "validity": "positive" | "any_row"
}
```

- **DexAggregatorV2** declares nothing and gets the default —
  `{js_metadata, raw_output, higher_better, integer, positive}` — which is
  today's behaviour bit-for-bit.
- **A rebalancing vault**: `{js_score, higher_better, ratio_1, any_row}` — the
  original 0..1 score, valid when produced rather than when positive.
- **A tracking-error metric**: `{js_metadata, tracking_error_bps, lower_better,
  bps, any_row}`.

### The metric always comes from the app's JS

There is deliberately **no `on_chain` source**. Instead the contract's
`scoreIntent` BPS is now exposed *to* the scorer as
`context.simulation.on_chain_score`, so an app that wants the contract's verdict
returns it — or a function of it — from its own JS. One source of truth, and the
app stays in charge of what quality means for it.

### Determinism

This feeds the sole adoption gate, so every conversion is exact-integer and
host-independent:

- `integer` — parsed with `int()`; wei above 2^53 keeps full precision, no
  `float` anywhere in the decision.
- `bps` / `ratio_1` — normalised to a 0..10000 integer; `ratio_1` quantises via
  `round(v * 10000)`, the same resolution the comparison band already uses.
- `lower_better` inverts as `ceiling - v`, which is why it **requires a bounded
  scale**. `lower_better` + `integer` has no exact inverse and is rejected at
  resolve time rather than silently mis-ranking.

### Where the contract lives at comparison time

Resolved once per benchmark run from the app manifest, then **persisted on the
row** (`per_intent[*].metric`). The adoption decision stays reproducible from
the stored artifact alone — the same compute-once-read-forever discipline as
`max_region_nodes` / `content_fingerprint` — and `epoch/relative_scoring` stays
a pure function of the rows it is handed.

A **default contract is stored as nothing at all**, so existing rows gain zero
bytes and every legacy row reads as default.

## 4. What Phase 0 changed

- `shared/quality_metric.py` — the contract: resolve, validate, extract,
  normalise, validity. Dependency-free.
- `engine/context.py` — `on_chain_score` / `onChainScore` exposed to the JS
  scorer.
- `benchmark_worker` — resolves each app's contract once per run (an invalid
  declaration logs loudly and falls back to default rather than taking down the
  round for every other app), extracts per contract, carries it onto the row.
- `epoch/relative_scoring` — the `_comparable_of` / `_row_has_value` seam.
  Everything else in that module is polarity-free ratio arithmetic that already
  worked for any metric. Rows without a declared contract take the original code
  path unchanged.

**Nothing changes for any app today**: no app declares a metric, so every row
resolves to default and every verdict is bit-identical.

## 5. Not in Phase 0 — the rest of the DEX residue

These are consensus-relevant in their own right (they change corpus membership
and case fingerprints, i.e. `benchmark_pack_hash`), so each needs its own atomic
change and promote rather than riding along here:

| Leak | Where |
|---|---|
| Corpus bucketing keyed on swap params | `order_sampler._BUCKETED_PARAMS = {input_token, output_token, input_amount, min_output_amount}` |
| Hardcoded WETH/USDC synthetic scenarios | `harness/snapshot.py` |
| `"For swap-style: tokens go to the executor"` | `blockloop/simulation.py` |
| `_KNOWN_SIGS = {swap, buy, rebalance}` with a DexAggregator comment | `blockloop/order_processor.py` |
| `input_token`/`input_amount` seeding fallback | `orchestrator._build_token_balances` (already prefers a manifest `_fund` map — the pattern the others should follow) |

Phase 1 moves each behind a manifest-driven equivalent; Phase 2 has
DexAggregator declare its contract explicitly so core's swap default stops being
load-bearing; Phase 3 deletes the fallbacks.

## 6. Open decision: mutability

The manifest is **per-app mutable data**, so the metric contract is now a
consensus input an app owner could change — flipping polarity would invert who
wins a champion contest without a fleet promote. Same hazard as putting the rule
in app JS. Two mitigations, neither implemented here:

1. **Fold the metric contract into `benchmark_pack_hash`.** Manifest fields
   already feed it, so a change would force a new pack and surface as visible
   divergence rather than a silent re-ranking — the fail-loud pattern used
   throughout.
2. **Freeze it after an app's first champion**, changeable only by a deliberate
   governance step.

Until one of these lands, treat a metric declaration as trusted operator input:
safe while we author every app, not safe for third-party apps.
