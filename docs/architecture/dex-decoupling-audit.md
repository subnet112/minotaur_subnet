# Making the DEX "just one of the apps" — audit

**Date:** 2026-07-27
**Goal:** nothing in Minotaur core should know what a swap is. An app that
isn't a DEX must be a first-class citizen: rankable, samplable, executable.

"Remove all the DEX stuff" needs a shared definition first, because a lot of
what greps as DEX *should* stay — an example swap solver ought to be a swap
solver. This is the full inventory with a verdict on each.

---

## Verdicts at a glance

| Tier | What | Status |
|---|---|---|
| 1 | Adoption gate assumes delivered output | **seamed** — #1141 |
| 2a | Sampler near-dup bucket names swap params | **deleted** — #1151 |
| 2b | Selector map hardcodes one app's ABI | **deleted** — this PR |
| 2c | Quote-case identity denylist | open — needs allowlist |
| 3 | `MarketSnapshot` is Uniswap-shaped | open — **breaks miners**, needs a decision |
| — | Example solvers, `dex_compare`, token registry | **keep** — correctly scoped |

---

## Tier 1 — the adoption gate (done, #1141)

`relative_scoring` compared every app on `metadata.raw_output` — a delivered
token amount, higher-is-better — and `has_delivered_value_rows` **rejected** any
submission that delivered nothing on every order. A vault whose orders move no
tokens could never hold a champion. Apps now declare a quality metric; the
default reproduces today's behaviour bit-for-bit.

## Tier 2a — sampler near-dup bucket (done, #1151)

`_dedup_key` collapsed same-pair + same-decade-amount orders via a hardcoded
`{input_token, output_token, input_amount, min_output_amount}` set. Measured on
the live 2246-row quote corpus: it collapsed **nothing** (identical partition,
pair agreement 1.0000). Deleted rather than generalised.

## Tier 2b — hardcoded selector map (this PR)

`order_processor` derived an intent's 4-byte selector from a literal map:

```python
_KNOWN_SIGS = {
    "swap":      "swap(address,address,uint256,uint256,address)",
    "execute":   "swap(address,address,uint256,uint256,address)",
    "buy":       "buy(address,address,uint256,uint256,address)",
    "rebalance": "rebalance(address[],uint256[],address)",
}
```

One app's ABI baked into a path every app goes through, with a comment naming
DexAggregatorApp. Replaced with `compute_selector_from_manifest` — the same
manifest-driven path the submit-order endpoint already uses.

Verified against the live manifest: both produce `d5bcb9b5`, so this is a
drop-in for the app it was written for and *correct* for an app core has never
heard of. An app with no manifest entry falls back to the no-arg convention and
logs — which is what the map did for any unlisted intent.

## Tier 2c — quote-case identity denylist (open)

`_QUOTE_IDENTITY_PARAMS` strips caller-supplied identity fields before a quote
case is stored and served publicly. It is a **denylist**, and the code already
flags the problem:

> *if a non-swap app is ever added whose trade legitimately needs an address
> param, revisit this as an allowlist — a denylist can miss a novel identity key*

The fix is available without new manifest surface: apps already declare
`source` per param (`user` / `quote` / `system`). Storing only `source=user`
params is that allowlist, and it closes the leak where a novel identity key
(`delegate`, `operator`) reaches a public endpoint by omission.

Not done here because it changes what is stored, and `quote_case_id` is
content-addressed on those params — it belongs with a corpus change, not
alongside a selector fix.

## Tier 3 — `MarketSnapshot` (open; needs a decision)

The deepest coupling, and the one that **cannot** be fixed by moving code:

```python
# sdk/intent_solver.py — the MINER-FACING contract
prices       # token price feeds
pool_states  # "DEX pool states… reserves for V2, liquidity/sqrtPriceX96/tick for V3"
balances     # token balances
dex_config   # "DEX router addresses and protocol configuration"
```

`harness/snapshot.py` populates it: Uniswap V3 factory/router per chain, a
hardcoded pool list, a V3 pool ABI, `_derive_prices` from pool state.

A vault gets handed a snapshot full of Uniswap pool states that mean nothing to
it, and there is **no field for what it needs** (target weights, current
allocations, NAV). But deleting `pool_states` / `dex_config` breaks every
existing solver, because this is the type miners code against.

The additive shape: keep the DEX fields (DexAggregator solvers use them) and add
an app-namespaced `app_data` blob populated by an app-declared snapshot builder
— the same "app declares, platform carries" pattern as the quality metric. That
is a miner-contract change and wants its own design pass.

## Keep as-is — correctly scoped

These grep as DEX and should stay:

| Where | Why |
|---|---|
| `dex_compare/` | An explicitly DEX-comparison service. Correctly named. |
| `sdk/solvers/anvil_swap_solver.py`, `docker/example-solver/` | Reference solvers. A swap example should be a swap. |
| `blockchain/tokens.py` | Token registry (symbol → address). Any ERC-20 app needs it; not DEX-specific. |
| `bridge/{across,cctp,hyperlane}.py` | Bridge adapters over WETH/USDC because those are the bridgeable assets. |
| `simulator/revert_decoder.py` | Decodes known DEX revert strings for diagnostics. Additive, no behaviour depends on it. |
| `miner/agent/prompts.py` | Miner-side tooling, not validator core. |

## Grey area — built-in app archetypes

`v3/manifest.py` carries `normalize_swap_intent_params`,
`normalize_rebalance_intent_params`, `normalize_twap_intent_params`, and
`blockloop/simulation.py` guesses the spend token/amount from alias lists
(`input_token` / `tokenIn` / `token_in` / `asset`, and
`input_amount` / `amountPerBuy` / `amount`).

This is not *DEX* hardcoding so much as **a fixed set of built-in archetypes**
(swap / DCA / rebalance / TWAP / yield). It is more defensible than a single
app's ABI, and it degrades gracefully — an unknown app just doesn't get seeded.
But it is still core guessing at app semantics from param names, and the
manifest already declares enough (`source`, `type`) to replace the guessing with
a lookup.

Worth doing, but it touches the live order path's token seeding, so it wants its
own change with an order-replay check rather than riding along here.
