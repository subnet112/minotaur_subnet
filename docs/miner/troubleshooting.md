# Miner Troubleshooting (Current Workflow)

Common issues when submitting and iterating on solver code.

## Submission endpoint errors

### Error: cannot reach `/v1/submissions*`

Typical causes:

- Wrong base URL (using validator `:9100` when submissions are served by API `:8080` in local testnet)
- API service not running

Checks:

```bash
curl http://localhost:8080/health
curl http://localhost:8080/v1/submissions
```

## No open solver round

### Error: `Cannot submit: solver round unavailable` / `Cannot submit: no open solver round`

`submit` discovers the active round via `GET /v1/solver/round` and requires one that is **open and accepting submissions** — there is no epoch fallback. Check the round:

```bash
curl http://localhost:8080/v1/solver/round
```

If there's no `round_id`, or `accepting_submissions` is `false`, wait for the next open round (on local testnet, ensure the API has opened one). `--epoch` is no longer required — the epoch is read from the round.

## Signature or hotkey issues (git submission)

### Error: HTTP 400/401 on `POST /v1/submissions`

Checks:

- `--hotkey` points to a real Bittensor wallet name
- wallet exists under `BT_WALLET_PATH` (or default `~/.bittensor/wallets`)
- commit hash and repo URL match the signed message payload

## Screening failures

For git submissions, failures often come from:

- Missing required files in repo root (`Dockerfile`, `solver.py`, `README.md`)
- Invalid `Dockerfile` base image
- `CMD`/`ENTRYPOINT` present in `Dockerfile`
- `SOLVER_CLASS` import/init issues

Local checks:

```bash
docker build --network=none --memory=4g -t test-solver .
docker run --rm --network=none --read-only --tmpfs=/tmp:size=64m --memory=2g --cpus=1.0 --entrypoint python test-solver -c "from solver import SOLVER_CLASS; print(SOLVER_CLASS.__name__)"
```

## Source submissions not adopted

`/v1/submissions/source` skips screening and goes straight to benchmarking. If not adopted:

- score may be lower than champion
- challenger must beat champion by at least 0.5% (`DETHRONE_MARGIN = 0.005`)

Use status endpoint to inspect:

```bash
python -m minotaur_subnet.miner.main status \
  --submission-id <id> \
  --validator-url http://localhost:8080
```

## Score is always zero

Common causes:

- `generate_plan()` raises exceptions
- malformed `ExecutionPlan` / calldata
- invalid addresses, deadlines, or empty interaction list

Quick check by submitting source and reviewing benchmark/status details:

```bash
curl -X POST http://localhost:8080/v1/submissions/source \
  -H "Content-Type: application/json" \
  -d '{"solver_source":"<python source>","hotkey":"local-miner","epoch":0}'
```

## Agent loop does not generate submissions

Checks:

- Claude CLI is installed and available in `PATH`
- API URL points to a reachable server exposing `/v1/apps/manifests` and `/v1/submissions/source`
- strategy directory is writable

Run with explicit options:

```bash
python -m minotaur_subnet.miner.main agent \
  --validator-url http://localhost:8080 \
  --strategy-dir ./strategies \
  --loop-interval 30
```

## Useful commands

```bash
# API health
curl http://localhost:8080/health

# List submissions
curl http://localhost:8080/v1/submissions

# Poll one submission
python -m minotaur_subnet.miner.main status --submission-id <id> --validator-url http://localhost:8080
```

See also: [Quickstart](./quickstart.md), [Configuration](./configuration.md), [Custom Solver](./custom-solver.md).
