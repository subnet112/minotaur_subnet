# Miner Quickstart (Current CLI)

This guide reflects the current `minotaur_subnet.miner.main` CLI and submission routes.

## Prerequisites

- Python 3.12+
- Git
- Docker (required for git-based submission screening; recommended for local testing)
- Bittensor wallet with a hotkey registered on subnet 112 (required for submitting against mainnet)

## Targets: local dev vs mainnet

The CLI flags are the same; only the `--validator-url` changes:

| Target | URL | When to use |
|---|---|---|
| Local testnet | `http://localhost:8080` | After `make testnet-up`. Submissions auto-benchmark, fast iteration. |
| Production | `https://api.minotaursubnet.com` | Real subnet 112 mining; emissions, real benchmarks. |

The rest of this guide uses `$VALIDATOR_URL` as a placeholder — set it to one of the above:

```bash
export VALIDATOR_URL=http://localhost:8080            # local dev
# export VALIDATOR_URL=https://api.minotaursubnet.com  # production (subnet 112)
```

See the [network reference](../operator/network-reference.md) for where to find the active production endpoint.

## 1) Install

```bash
git clone https://github.com/subnet112/minotaur_subnet.git
cd minotaur_subnet
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

## 2) Register on subnet 112 (mainnet only)

Local testnet auto-registers a test miner. For mainnet, register your hotkey first:

```bash
btcli subnet register --netuid 112 --subtensor.network finney \
  --wallet.name my-miner --wallet.hotkey my-miner-hotkey
```

Verify:

```bash
btcli subnet metagraph --netuid 112 --subtensor.network finney
```

Your hotkey should appear in the metagraph. The `--hotkey` argument in CLI commands below refers to the **hotkey name** (e.g. `my-miner-hotkey`), not the wallet name.

## 3) Start the target API (local testnet only)

For mainnet, skip — you submit against the production endpoint.

For local dev:

```bash
make testnet-up
# or, lighter, just the API:
python -m minotaur_subnet.api.server --port 8080
```

## 4) Run the agent loop (recommended)

Agent mode discovers active apps, generates strategies, tests them, commits them to a fork of the canonical solver repo, opens a PR, and submits that PR via `/v1/submissions` (the same PR-based path as the `submit` subcommand below).

```bash
python -m minotaur_subnet.miner.main agent \
  --validator-url "$VALIDATOR_URL" \
  --strategy-dir ./strategies \
  --miner-id my-miner-001
```

## 5) Submit a git-based solver

Current CLI subcommand:

```bash
python -m minotaur_subnet.miner.main submit \
  --pr-number 42 \
  --head-sha <40-char-head-sha> \
  --hotkey my-miner-hotkey \
  --validator-url "$VALIDATOR_URL" \
  --poll
```

`--pr-number` / `--head-sha` reference a PR you've opened against the canonical
solver repo (`subnet112/minotaur-solver`). Fork it, edit `solver.py`, push, open
a PR, then submit its number and head SHA.

Notes:

- `--hotkey` is the bittensor **hotkey name** (matches `--wallet.hotkey` in `btcli`), not the wallet name. The signed submission is verified against the metagraph by the API.
- `--round-id` and `--epoch` are optional — `submit` auto-detects both from the current open round (`GET /v1/solver/round`). The signed message is `{pr_number}:{head_sha}:{round_id}`.
- `--validator-url` defaults to `http://localhost:9100` if omitted, which is wrong for both local dev (use `:8080`) and mainnet — always set it explicitly.

> **⚠️ Important — base your PR on the current `main`.** Every champion's solver is squash-merged to the solver repo's `main`, so `main` always holds the **current champion's code**. Your submission replaces `solver.py` *on top of the current `main`*. If your fork is based on an older `main` (e.g. from before the latest champion), your PR will conflict and **cannot be adopted even if it wins the benchmark**. After any champion change, **rebase your fork onto the latest `main` and resubmit**. When a new champion is elected the validator auto-closes the now-stale submission PRs with a rebase reminder — that's your cue to rebase and resubmit.

## 5b) Optional: private-repo submission (front-run protection)

By default your PR is **public** on the canonical solver repo, so anyone can read
your solver before you earn from it. The **private path** keeps your code private
through screening + benchmarking — the validator clones it, scores it, and posts
the benchmark report onto your private PR — and **publishes it to canonical `main`
only if it wins** (leak-on-champion). So you develop and win without anyone seeing
your code until you're already champion.

How it works:

1. Put your solver in your **own private GitHub repo** and open a PR there (a
   branch → `main` PR in that private repo is fine).
2. Create a **fine-grained PAT** scoped to **that one repo**, with exactly:
   - **Metadata: Read** (mandatory baseline)
   - **Contents: Read** — lets the validator clone your code
   - **Pull requests: Read and write** — lets the validator read the PR and post
     benchmark reports / errors back onto it
   
   No write access to your repo, nothing on the canonical repo, no admin scope.
   Use a short expiry and revoke it after your submission is scored.
3. Submit with `--private-repo` + the token (prefer the env var so it stays out
   of your shell history):

```bash
export MINER_REPO_TOKEN=github_pat_xxxxx
python -m minotaur_subnet.miner.main submit \
  --pr-number 3 \
  --head-sha <40-char-sha> \
  --private-repo youruser/your-private-solver \
  --hotkey my-miner-hotkey \
  --validator-url "$VALIDATOR_URL" \
  --poll
```

Notes:

- The token is **transport only** — it is not part of the signed message, is sent
  over HTTPS, is held in validator memory for this submission only, and is purged
  when the submission reaches a terminal state. It is **never written to disk**.
- The validator (leader) sees your private source while building/benchmarking — the
  privacy guarantee is against **other miners and the public**, not the leader.
- If your private solver wins, the validator publishes its source to canonical
  `main` (preserving canonical CI) and you become champion — your code only becomes
  public once you've already won. No need to resubmit publicly.

## 6) Check submission status

```bash
python -m minotaur_subnet.miner.main status \
  --submission-id sub_xxx \
  --validator-url "$VALIDATOR_URL"
```

Common statuses:

- `queued`
- `screening_stage_1` — static checks (imports, no banned syscalls, basic shape)
- `screening_stage_2` — Docker build + import
- `screening_stage_3` — smoke-test run on a benchmark scenario
- `benchmarking` — full replay against the current scenario suite
- `scored` — benchmark complete; ranked against current champion
- `waitlisted` — no fault of yours: not selected for this round's benchmark slate, or the round's benchmark window elapsed before your turn. Carries a next-round priority and is **distinct from `rejected`** (PR #620). The status payload includes a `waitlist` object `{position, contenders, slots, next_round_priority}`.
- `adopted` — promoted to champion; running in BlockLoop
- `rejected` — failed screening or lost the champion comparison. Every terminal transition also carries a machine-readable `outcome_code` (e.g. `too_entangled`, `fingerprint_repeat`, `rotation_not_selected`, `benchmark_failed`) — switch on that, not on the free-text reason.

## What happens after submission

Once `submit` returns, the API queues your solver for evaluation. The lifecycle on the validator/API side:

1. **Screening (seconds–minutes)**: three stages run in sequence. Most failures show up here — Docker build errors, missing `SOLVER_CLASS`, banned imports.
2. **Benchmarking (minutes)**: the benchmark worker runs your solver against the active scenario suite for each live App. Each order produces a real result — for swaps, the raw delivered output in wei.
3. **Champion comparison (relative, per order)**: your result is compared to the champion **per order at the same pin** — `win` / `regression` / `matched` within a ±0.1% (10 bps) band, plus `blind_spot_cover` and `dropped`. Adoption runs a ladder: you dethrone on **output** if you are net better on breadth — `(wins + blind_spot_covers) − regressions ≥ 1` — where each tolerated regression must stay within the **1% hard floor** (a cut beyond 1%, or dropping any order the champion serves, is a hard veto regardless of wins). On a **fully-matched tie** (every order matched, zero regressions) you can still dethrone on the tie-break rungs — **gas** (≥200 bps cheaper total metered gas), then **factorization** (`max_region_nodes` smaller by ≥100), then **deadwood** (`unproductive_nodes` smaller by ≥2000). See [Champion/challenger model](./README.md#championchallenger-model) for the full rule. Scoring is raw delivered output only — quote quality no longer matters.
4. **Adoption**: champion adoption requires N-of-M validator signatures via champion-certification consensus (separate from order consensus). This typically completes in seconds once the leader proposes the new champion.
5. **Weight emission**: the active champion's submitter receives **75% of miner emissions** (champion-takes-all *among miners* — no runner-up earns anything — with the remaining 25% routed to the subnet owner; before a real miner champion exists, 100% routes to the owner), committed on the tempo-aligned schedule (see [validator config](../validator/configuration.md#weight-emission)).

Wall-clock times depend on the live network's queue depth. On a quiet network, screening + benchmarking takes 1–3 minutes. During a benchmark spike (multiple submissions queued), it can stretch to 10+ minutes.

Poll with `status` or watch the agent loop logs — both surface state transitions in real time.

## Dry-run: score your solver before submitting to production

There is **no endpoint to score a solver on the production validators without submitting** — running untrusted code on a validator only happens through the full sandboxed screening → benchmark flow. To iterate and get a score without touching production, run the **local testnet**, which executes the *same* screening + benchmark + scoring pipeline the production validator uses:

```bash
make testnet-up                                   # full stack on your machine
export VALIDATOR_URL=http://localhost:8080        # your local validator/API
python -m minotaur_subnet.miner.main submit \
  --pr-number <n> \
  --head-sha <sha> \
  --hotkey <local-test-hotkey> \
  --validator-url "$VALIDATOR_URL"
# then poll status as above
```

The local testnet auto-registers a test miner and auto-benchmarks each submission — same screening stages, same benchmark worker, same scorecard — with no real emissions or stake, and without consuming a production round. Iterate freely here until your solver scores where you want.

**Caveat — local scores are a strong predictor, not the exact production score.** A live production round also runs a hidden **shadow phase** (cases not in the public benchmark pack) to discourage overfitting. So a solver that scores well locally should score similarly in production, but the final on-validator score (including shadow cases) is only known once you submit for real. Don't tune to the public cases alone.

## Preview orders & find your champion's blind spots

The exact set you're scored against is a per-round, seeded random sample of real orders plus [hidden shadow cases](#dry-run-score-your-solver-before-submitting-to-production) — it's deliberately not published. But you *can* pull the same population of orders to test against locally, and after a submission you get a per-order breakdown of exactly where the champion is weak.

### Before you submit — pull real orders to test against

All three are public, read-only endpoints on any validator/API node. Hit the production API (`https://api.minotaursubnet.com/v1`) to get the live corpus:

- `GET /v1/apps/{app_id}/historical-scenarios?n_per_chain=10` — PII-stripped historical filled-order scenarios for one app: the same real orders the benchmark replays as Stage 2. Deterministic (seeded by `app_id`), so it's a **repeatable preview sample, not the set your submission is scored on**. `n_per_chain` is capped at 50.
- `GET /v1/apps/manifests` — every app's manifest in one call (bulk discovery), including its synthetic `benchmark_scenarios`. Single-app variant: `GET /v1/apps/{app_id}/manifest`.

Feed either of these into the local testnet or the plan dry-run below.

### Debug a single plan without your own archive node

Two authed endpoints score a single execution plan **you supply** (not your whole solver — that's the local testnet's job), each gated by a metagraph-registered hotkey signature or admin key:

- `POST /v1/orders/{order_id}/dry-run` — fast **mock** simulation, JS score only.
- `POST /v1/apps/{app_id}/score` — the validator's **real fork simulation** (the same `scoreIntent` path production uses): a full report with on-chain score, gas, transfers, and the decoded on-chain revert reason on failure. Rate-limited to 60 calls/hr per hotkey; pass `fork_block` to replay against historical pool state (archive-capable RPC required; clamped to ±100 blocks from head).

The reference client signs the request from your local Bittensor wallet:

```bash
python scripts/miner_dry_run.py \
  --api-url "$VALIDATOR_URL" \
  --wallet-name <wallet> --hotkey-name <hotkey> \
  --order-id <order_id> --plan plan.json
```

It calls `/orders/{id}/dry-run` by default; POST the same signed headers to `/apps/{app_id}/score` for the full real-sim report (see the script's header comment).

### After you submit — read where the champion is blind

The benchmark report and PR comment carry a per-order breakdown (`relative.per_order`), one row per order with the champion's output, yours, the ratio, and a verdict:

- `blind_spot_cover` — the champion delivered **nothing** and you delivered. Pure wins that count toward dethroning — hunt for more of them.
- `regression` — you delivered less than the champion. Optimization targets (each must stay within the 1% floor).
- `dropped` — you produced nothing on an order the champion serves. A **hard veto** — fix these first.
- `win` / `matched` — you beat / tied the champion within the 10 bps band.

Use the `blind_spot_cover` and `regression` rows to find the champion's weak orders, then reproduce them locally with the endpoints above. Remember the shadow phase: optimize the *class* of order you're losing on, not the exact `intent_id`.

## Next steps

- [Configuration](./configuration.md) for full CLI flags
- [Solver API](./solver-api.md) for `IntentSolver` and `Strategy` contracts
- [Custom Solver](./custom-solver.md) for implementation guidance
- [Network reference](../operator/network-reference.md) for production endpoints and contract addresses
