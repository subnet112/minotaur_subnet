# Miner Configuration (Current CLI)

This page documents the current subcommands implemented in `minotaur_subnet/miner/main.py`.

## Available subcommands

- `agent` - LLM-driven strategy loop
- `submit` - signed git-based solver submission
- `status` - submission status polling

## `agent`

```bash
python -m minotaur_subnet.miner.main agent \
  [--validator-url <url>] \
  [--strategy-dir <dir>] \
  [--miner-id <id>] \
  [--loop-interval <seconds>] \
  [--improvement-threshold <float>] \
  [--max-llm-calls <n>] \
  [--stale-after <seconds>] \
  [--model <name>] \
  [--claude-timeout <seconds>] \
  [--anvil-rpc-url <url>]
```

Defaults:

- `--validator-url`: `http://localhost:8080`
- `--strategy-dir`: `strategies`
- `--miner-id`: `miner-agent-001`
- `--loop-interval`: `60.0`
- `--improvement-threshold`: `0.7`
- `--max-llm-calls`: `3`
- `--stale-after`: `600.0`
- `--model`: `sonnet`
- `--claude-timeout`: `300.0`
- `--anvil-rpc-url`: not set

## `submit`

```bash
python -m minotaur_subnet.miner.main submit \
  --pr-number <n> \
  --head-sha <40-char-sha> \
  --hotkey <wallet-name> \
  [--wallet-path <path>] \
  [--validator-url <url>] \
  [--round-id <id>] \
  [--epoch <n>] \
  [--private-repo <owner/repo>] \
  [--repo-token <fine-grained-PAT>] \
  [--poll]
```

The PR must target the canonical solver repo (`subnet112/minotaur-solver`).
The leader resolves `--pr-number` to the fork's `clone_url` + live head SHA and
rejects the submission if the live head no longer matches `--head-sha`
(force-push guard).

`--private-repo` + `--repo-token` opt into the **private submission path**: your
solver stays private through screening + benchmarking, you get the benchmark report
on your private PR, and **if it wins it is published to canonical `main`** (you
become champion — your code only goes public once you've already won). With it,
`--pr-number`/`--head-sha` refer to a PR in YOUR private repo. The PAT must be a
fine-grained token scoped to that one repo with `Metadata:Read`, `Contents:Read`,
`Pull requests:Read+Write`. Prefer the `MINER_REPO_TOKEN` env var over the flag to
keep it out of shell history. See the
[quickstart](./quickstart.md#5b-optional-private-repo-submission-front-run-protection)
for the full walkthrough.

Defaults:

- `--validator-url`: `http://localhost:9100`
- `--round-id`: optional; auto-detected from the open round (`GET {validator_url}/v1/solver/round`)
- `--epoch`: optional override; auto-detected from the open round
- `--wallet-path`: `~/.bittensor/wallets` (or `BT_WALLET_PATH`)

Important notes:

- In local testnet/API flows, `--validator-url` is usually `http://localhost:8080` for `/v1/submissions*`.
- Submissions target the current open round; if none is open or it isn't accepting, `submit` errors clearly (there is no epoch fallback).
- Request payload includes:
  - `pr_number`, `head_sha`, `round_id`, `epoch`
  - `hotkey` (SS58)
  - `signature` over `{pr_number}:{head_sha}:{round_id}`

## `status`

```bash
python -m minotaur_subnet.miner.main status \
  --submission-id <id> \
  [--validator-url <url>]
```

Defaults:

- `--validator-url`: `http://localhost:9100`
- internal timeout: 30 seconds for this command path

Terminal states:

- `scored`
- `adopted`
- `rejected`
- `waitlisted` — no-fault: not selected onto this round's benched slate (or its bench window elapsed); carries a next-round priority. (The `status` poll command treats only scored/adopted/rejected as terminal, so a waitlisted submission polls until the 30s timeout — re-check next round.)

## Environment variables

- `BT_WALLET_PATH` - fallback wallet root for signed submissions
- `SUBMISSIONS_API_KEY` - when the target validator runs with submission-API-key gating (hardened/production), the `submit` and `agent` CLIs send this as the `x-submission-api-key` header; a gated validator rejects submissions without a matching key (HTTP 401).

## Examples

Agent loop:

```bash
python -m minotaur_subnet.miner.main agent \
  --validator-url http://localhost:8080 \
  --strategy-dir ./strategies
```

Git submission:

```bash
python -m minotaur_subnet.miner.main submit \
  --pr-number 42 \
  --head-sha abc123def4567890abc123def4567890abc12345 \
  --hotkey my-hotkey \
  --validator-url http://localhost:8080 \
  --poll
```

Status check:

```bash
python -m minotaur_subnet.miner.main status \
  --submission-id sub_abc123 \
  --validator-url http://localhost:8080
```
