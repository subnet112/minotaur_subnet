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

## Submission not adopted

> The inline source path (`/v1/submissions/source`) was removed (PR #599). All
> submissions now go through the git PR path.

Check the `outcome_code` on your submission status first — it names the exact
reason. Common non-adoption cases:

- **Tied on output.** You matched the champion on every order (within the ±0.1%
  band) and lost or tied every tie-break rung — the incumbent is kept. To win an
  all-matched tie you must be **cheaper on total metered gas** (≥200 bps),
  **better factored** (`max_region_nodes` smaller by ≥100), or carry **less dead
  code** (`unproductive_nodes` smaller by ≥2000). Your report names the target.
- **Not net-better on output.** Your regressions were not outnumbered by wins:
  adoption needs `(wins + blind_spot_covers) − regressions ≥ 1`.
- **Hard veto.** You cut at least one order by more than 1% (`n_catastrophic`), or
  you **dropped** an order the champion serves (`n_dropped`) — either vetoes
  adoption regardless of how many other orders you won.
- **Screening reject (`too_entangled` / `static_checks_failed`).** Stage 1 now rejects a
  solver whose largest AST region exceeds **4,200 nodes** (`outcome_code` = `too_entangled`) or that
  uses bare `exec()`/`eval()` (`outcome_code` = `static_checks_failed`; `dynamic_code` appears only in the
  human-readable reason, not as an `outcome_code`). The reject still reports your
  `max_region_nodes` so you can see the number to get under.
- **`waitlisted`, not rejected.** If your `outcome_code` is `rotation_not_selected`
  or `window_elapsed`, you were not benchmarked this round through no fault of your
  own — you keep a next-round priority. Resubmitting an identical solver does not
  help (see below).
- **`fingerprint_repeat`.** A comment-only / nonce-only resubmit of an identical
  code tree is rejected pre-build — "comments don't make code new". Change at least
  one semantic byte. A **cross-hotkey** quota also applies by default
  (`SUBMISSIONS_MAX_ROUNDS_PER_FINGERPRINT=2`): the same normalized code fingerprint
  can be benched at most twice **across all hotkeys**, so copy-pasting another
  miner's solver under a fresh hotkey is rejected once the quota is spent.

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

Quick check: submit through the git PR path and review the benchmark/status
details on your submission (`python -m minotaur_subnet.miner.main status
--submission-id <id>`), or reproduce locally against the testnet, which runs the
same screening → benchmark → scoring pipeline.

## Agent loop does not generate submissions

Checks:

- Claude CLI is installed and available in `PATH`
- API URL points to a reachable server exposing `/v1/apps/manifests` and `/v1/submissions`
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
