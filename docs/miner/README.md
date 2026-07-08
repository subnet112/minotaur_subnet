# Miner Overview

This section covers the current miner-facing workflow for Minotaur Subnet 112.

## Contents

- [Quickstart](./quickstart.md) - Run the current CLI flow
- [Configuration](./configuration.md) - Flags, defaults, and environment variables
- [Solver API](./solver-api.md) - `IntentSolver` and `Strategy` interfaces
- [Custom Solver](./custom-solver.md) - Strategy implementation guidance
- [Troubleshooting](./troubleshooting.md) - Common errors and fixes

## How mining works now

Miners compete by improving solver quality, not by running a quote server.

Typical loop:

1. Build/iterate on strategies (often via `RoutingSolver`).
2. Submit candidate solver code.
3. Validator/API benchmark worker scores submissions against active app scenarios.
4. A challenger is adopted if it is **net better on breadth** — more delivered output across the order set than the champion — or, on a fully-matched tie, if it is cheaper/cleaner on the tie-break ladder (see [Champion/challenger model](#championchallenger-model) below).
5. Champion solver is loaded into block loop execution.

## Submission paths

### Git-based submission (`/v1/submissions`)

- Signed by Bittensor hotkey
- Runs 3-stage screening:
  - static checks
  - Docker build/import
  - smoke test
- Then benchmarked and ranked

> **Removed:** the inline source-submission path (`POST /v1/submissions/source`)
> was retired (PR #599). All submissions — including the agent loop's — now go
> through the git PR path above. The `ENABLE_SOURCE_SUBMISSIONS` flag is gone.

## Champion/challenger model

Every submission is benchmarked against the current champion **per order at the
same fork pin**, comparing the raw delivered output (exact wei). The champion is
the baseline and carries no absolute score of its own. Adoption runs a fixed
ladder, highest priority first:

1. **Output — the primary rule (always armed).** A challenger is adopted if it is
   **net better on breadth**: `(wins + blind-spot covers) − regressions ≥ 1`
   (`DETHRONE_WIN_MARGIN = 1`). This is a **bounded-regression, net-better** rule,
   *not* the old "any regression = reject": a challenger **may** regress some
   orders and still win, provided every regression stays within the **1% hard
   floor** (`FLOOR_BPS = 100`) and its wins outnumber its regressions by at least
   one. Per-order results within a ±0.1% band (`RELATIVE_TOL_BPS = 10`) count as
   matches (ties), not wins.
2. **Tie-breaks — only on a fully-matched, saturated tie** (every compared order
   matched; zero regressions). A challenger that ties on output can still dethrone
   by being cheaper or cleaner, in this order:
   - **Gas** — same outputs on materially less **total metered (pre-refund) gas**,
     cheaper by ≥ `GAS_MARGIN_BPS = 200` bps (armed).
   - **Factorization** — its worst code region is smaller by ≥ `FACTOR_MARGIN = 100`
     AST nodes (`max_region_nodes`); only splitting into named helpers lowers it,
     minification does not (armed).
   - **Deadwood** — when factorization is genuinely tied, materially less dead code
     by ≥ `UNPRODUCTIVE_MARGIN = 2000` nodes (`unproductive_nodes`); only deleting
     dead code lowers it (armed).

**Hard vetoes (override every rung):** no order may be cut by more than 1%
(`n_catastrophic == 0`), and the challenger may not **drop** any order the champion
serves (`n_dropped == 0`).

The tie-break rungs are armed but fire **"by data"**: each is inert until *both*
the champion and challenger records carry the metric, so a rung may be armed yet
not yet biting until the standing champion's metric is backfilled. Your benchmark
report and PR comment name exactly how you won (`Won on gas/factorization/deadwood`)
or, on an all-matched tie, the precise target to hit (e.g. "get it to ≤ N nodes",
"delete ≥ N more dead nodes", "get total gas below N").

Scoring is defined purely by **raw delivered output** — the benchmark no longer
runs a quote (`solver.quote()` is no longer called; static-quote is the scoring
definition, PR #595/#600), so **quote quality no longer affects adoption at all**.

On adoption, the block loop hot-swaps to the new solver.
