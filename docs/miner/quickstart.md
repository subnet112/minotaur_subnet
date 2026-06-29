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

## 6) Optional: direct source submission (local/dev)

You can submit inline solver code directly:

```bash
curl -X POST http://localhost:8080/v1/submissions/source \
  -H "Content-Type: application/json" \
  -d '{
    "solver_source": "from minotaur_subnet.sdk.intent_solver import IntentSolver\nclass S(IntentSolver):\n    def initialize(self, config):\n        pass\n    def generate_plan(self, intent, state, snapshot=None):\n        raise NotImplementedError\n    def metadata(self):\n        from minotaur_subnet.sdk.intent_solver import SolverMetadata\n        return SolverMetadata(name=\"s\", version=\"0.1.0\", author=\"local\")\nSOLVER_CLASS = S",
    "hotkey": "local-miner",
    "epoch": 0,
    "solver_name": "local-dev"
  }'
```

This route skips screening and queues directly into benchmarking.

## 7) Check submission status

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
- `adopted` — promoted to champion; running in BlockLoop
- `rejected` — failed screening or scored below threshold

## What happens after submission

Once `submit` returns, the API queues your solver for evaluation. The lifecycle on the validator/API side:

1. **Screening (seconds–minutes)**: three stages run in sequence. Most failures show up here — Docker build errors, missing `SOLVER_CLASS`, banned imports.
2. **Benchmarking (minutes)**: the benchmark worker runs your solver against the active scenario suite for each live App. Each order produces a real result — for swaps, the raw delivered output in wei.
3. **Champion comparison (relative reference-bar)**: your result is compared to the champion **per order** — `win` / `regression` / `matched` within a ±0.1% (10 bps) band, plus `blind_spot_cover` and `dropped`. You dethrone the champion only with **zero regressions/drops and at least one strict win or blind-spot cover**. There is no absolute score or fixed percentage margin.
4. **Adoption**: champion adoption requires N-of-M validator signatures via champion-certification consensus (separate from order consensus). This typically completes in seconds once the leader proposes the new champion.
5. **Weight emission**: the active champion's submitter earns a share of miner emission weight that **scales with network usage** — a 5% floor (95% burns to the subnet owner) at low volume, ramping linearly to 100% at 1,000 orders in the trailing 24h — applied on the next subtensor epoch (~60s).

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

## Next steps

- [Configuration](./configuration.md) for full CLI flags
- [Solver API](./solver-api.md) for `IntentSolver` and `Strategy` contracts
- [Custom Solver](./custom-solver.md) for implementation guidance
- [Network reference](../operator/network-reference.md) for production endpoints and contract addresses
