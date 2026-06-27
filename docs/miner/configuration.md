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
  --repo-url <https-url> \
  --commit-hash <hash> \
  --hotkey <wallet-name> \
  [--wallet-path <path>] \
  [--validator-url <url>] \
  [--epoch <n>] \
  [--poll]
```

Defaults:

- `--validator-url`: `http://localhost:9100`
- `--epoch`: optional override; auto-detected from the open round (`GET {validator_url}/v1/solver/round`)
- `--wallet-path`: `~/.bittensor/wallets` (or `BT_WALLET_PATH`)

Important notes:

- In local testnet/API flows, `--validator-url` is usually `http://localhost:8080` for `/v1/submissions*`.
- Submissions target the current open round; if none is open or it isn't accepting, `submit` errors clearly (there is no epoch fallback).
- Request payload includes:
  - `repo_url`, `commit_hash`, `round_id`, `epoch`
  - `hotkey` (SS58)
  - `signature` over `{repo_url}:{commit_hash}:{round_id}`

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

## Environment variables

- `BT_WALLET_PATH` - fallback wallet root for signed submissions

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
  --repo-url https://github.com/myuser/my-solver \
  --commit-hash abc123def456 \
  --hotkey my-hotkey \
  --epoch 0 \
  --validator-url http://localhost:8080 \
  --poll
```

Status check:

```bash
python -m minotaur_subnet.miner.main status \
  --submission-id sub_abc123 \
  --validator-url http://localhost:8080
```
