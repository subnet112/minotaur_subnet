# Code-Verified Runtime Guide

This document reflects current behavior from executable code paths in:

- `minotaur_subnet/api/server.py`
- `minotaur_subnet/api/routes/*`
- `minotaur_subnet/blockloop/loop.py`
- `minotaur_subnet/validator/main.py`
- `minotaur_subnet/miner/main.py`
- `platform/local_testnet/docker-compose.yml`

It is intended to be the canonical runtime reference when older notes diverge.

## Service Topology

Minotaur currently runs as a set of cooperating services:

- **API server (FastAPI)** at `:8080`
  - Mounts `/v1` routes for apps, orders, submissions, wallets, chains, and monitoring.
  - Can also run its own `OrderBook + BlockLoop + BenchmarkWorker` in-process.
- **Validator service (aiohttp)** at `:9100`
  - Handles validator-specific endpoints (consensus, leader state, weights, legacy plan submission).
  - Runs `BlockLoop` only when leader (or when `FORCE_LEADER=1`).
- **Relayer service** at `:8091` (optional separate process)
  - Submits approved plans on-chain.
- **Miner CLI**
  - `agent` mode discovers apps and submits source strategies to `/v1/submissions/source`.
  - `submit/status` use `/v1/submissions*` (git-based workflow).

## Active Contract Path

The current live contract path is the `AppIntentBase` family of app contracts.
In local testnet and the default swap workflow, the canonical swap app is
`DexAggregatorApp`.

Notes:

- `DexAggregatorApp` is the swap app seeded by `platform/local_testnet/seed.py`.
- The relayer submits `executeIntent(...)` transactions to `AppIntentBase`-derived contracts.

## Current Entrypoints

```bash
# API server
python -m minotaur_subnet.api.server --port 8080

# Validator service
python -m minotaur_subnet.validator.main --port 9100

# Miner agent loop (LLM strategy iteration)
python -m minotaur_subnet.miner.main agent --validator-url http://localhost:8080

# Git-based solver submission
python -m minotaur_subnet.miner.main submit --repo-url <url> --commit-hash <hash> --hotkey <wallet> --epoch <n> --validator-url http://localhost:8080
```

Notes:

- The CLI subcommands are `agent`, `submit`, `status` (no `run`, no `test`, no `submit-legacy`).
- In local testnet, `/v1/submissions*` are served by the API service on `:8080`.
- `submit` discovers the active round via `GET /v1/solver/round` and signs `{repo_url}:{commit_hash}:{round_id}`; the epoch is read from the round (`--epoch` is an optional override). There is no `/v1/status` epoch fallback.

## API Server Surface (`/v1`)

Mounted routers in `api/server.py`:

- `apps`, `chains`, `wallets`, `monitoring`, `submissions`, `orders`
- `intents` router exists in codebase but is not mounted in the current server bootstrap.
- `GET /health` includes sanitized `provenance_policy` and `runtime_security_policy` objects (readiness booleans/counts only; no secrets).

### Apps

- `POST /v1/apps/`
- `POST /v1/apps/validate`
- `POST /v1/apps/{app_id}/deploy`
- `GET /v1/apps/`
- `GET /v1/apps/{app_id}/status`
- `PUT /v1/apps/{app_id}/scoring`
- `GET /v1/apps/{app_id}/manifest`
- `GET /v1/apps/manifests`
- `POST /v1/apps/{app_id}/activate`
- `POST /v1/apps/{app_id}/score`

### Orders

- `POST /v1/apps/{app_id}/orders`
- `GET /v1/orders/{order_id}`
- `GET /v1/orders`
- `DELETE /v1/orders/{order_id}`
- `POST /v1/orders/{order_id}/dry-run`
- `POST /v1/apps/{app_id}/quote`
- `GET /v1/orders/{order_id}/bridge`
- `GET /v1/blockloop/status`
- `POST /v1/apps/{app_id}/prepare`

### Submissions

- `POST /v1/submissions` (git-based; signature required)
- `POST /v1/submissions/source` (direct source; queued straight to benchmarking)
- `GET /v1/submissions/{submission_id}/status`
- `GET /v1/submissions`

### Wallets / Chains / Monitoring

- `POST /v1/wallets/`
- `GET /v1/wallets/`
- `GET /v1/wallets/{address}/balances`
- `GET /v1/wallets/{address}`
- `POST /v1/apps/{app_id}/fund`
- `POST /v1/testnet/faucet`
- `POST /v1/testnet/faucet_erc20`
- `GET /v1/chains`
- `GET /v1/apps/{app_id}/monitor`

## Validator Service Surface (`:9100`)

The standalone validator currently exposes:

- `GET /health`
- `GET /intents/available`
- `POST /intents/{app_id}/submit`
- `POST /reload`
- `GET /weights`
- `GET /weights/history`
- `GET /blockloop/status`
- `POST /orders/submit`
- `GET /orders`
- `GET /intents/{app_id}/details`
- `GET /intents/{app_id}/scores`
- `POST /apps/{app_id}/quote`
- `POST /consensus/proposal`
- `GET /consensus/info`
- `GET /leader`

## Execution Flow (Order Lifecycle)

For orders submitted to `/v1/apps/{app_id}/orders`:

1. Request normalization:
   - Address parsing (plain `0x`, CAIP-10, ERC-7930).
   - Token symbol/address resolution.
   - Optional nonce auto-fill for managed wallets.
   - Optional permit/approval helpers.
2. `OrderBook` receives order (initial status `OPEN`).
3. `BlockLoop` claims orders and executes:
   - plan generation (`SOLVED`)
   - simulation (`SCORED`)
   - JS threshold gate
   - on-chain score gate
   - consensus gate (`APPROVED`)
   - relayer submission (`SUBMITTED`)
4. Final outcomes:
   - success: `FILLED`
   - score/consensus/replay failures: `REJECTED`
   - cross-chain source leg success: `BRIDGING`
   - bridge failure: `BRIDGE_FAILED`

## Dual Scoring and Thresholds

Both checks are enforced in `BlockLoop`:

- **JS score**: `score(plan, state, context)` against `app.config.score_threshold` (or global default).
- **On-chain score**: simulation-derived `on_chain_score` against `app.config.on_chain_threshold`.

If either gate fails, order is rejected.

## Solver Submission and Champion Adoption

### Git-based (`POST /v1/submissions`)

Pipeline:

1. Stage 1: static checks
2. Stage 2: Docker build/import checks
3. Stage 3: smoke tests
4. Benchmarking
5. Ranking + champion adoption

A challenger must beat the current champion by **0.5%** (`DETHRONE_MARGIN = 0.005`) to dethrone.

### Source-based (`POST /v1/submissions/source`)

- Accepts inline Python source.
- Writes temporary solver file.
- Skips screening/Docker.
- Moves directly to `BENCHMARKING`.

## Local Testnet Reality

From `platform/local_testnet/docker-compose.yml`:

- API is exposed on host `:8080`
- Validator runs on internal network (`:9100`), with `FORCE_LEADER=1`
- Relayer is exposed on `:8091`
- There is **no Docker miner service**; miner agent is expected on host (`make miner-agent`)
- API is configured with `USE_EVM_RELAYER=1`, and can run full order processing pipeline

## Known Drift (Now Corrected Here)

- `miner.main run` examples are stale; current command is `miner.main agent`.
- `/v1/submissions*` live on API server in local testnet.
- `intents` API router is present in code but not mounted by default.

## Submission Security Switches

Current policy controls in `api/routes/submissions.py` and worker/server wiring:

- `ENABLE_BENCHMARK_WORKER` (default `false`) — explicitly enables background screening/benchmark worker.
- `ENABLE_SOLVER_ROUND_COORDINATOR` (default `true` when the benchmark worker is enabled) — drives the closed-round solver lifecycle on the API server.
- `SOLVER_ROUND_COORDINATOR_INTERVAL_SECONDS` (default `5`) — how often the API server polls durable round state to resume explicitly closed rounds.
- `SOLVER_ROUND_OPEN_SECONDS` (default `300`) — how long the elected leader leaves a solver round `OPEN` before it auto-closes intake and freezes the replay cohort. Phase 1 of the two-phase round: a ~5-min OPEN window where submissions are collected and their images are built + distributed (benchmarking does **not** run yet — the round-anchored fork pin only seals on `close_epoch`, so `run_once` defers until close).
- `SOLVER_ROUND_DECISION_EPOCHS` (default `5`) — Phase 2 of the round: the post-close window (in `EPOCH_SECONDS` epochs) within which the leader must benchmark the champion + the round's submissions and certify a finalist, i.e. `decision_deadline_epoch = close_epoch + DECISION_EPOCHS`. Sized to fit the capped closed-phase batch; **too small silently aborts contested rounds** (`certification_deadline_elapsed`) instead of adopting. Leader-driven + broadcast (followers adopt the leader's `decision_deadline_epoch`), so keep it fleet-uniform across a rollout. With the default this gives a ~10-min two-phase round (5-min OPEN + up to 5-min CLOSED).
- `SOLVER_ROUND_ACTIVATION_DELAY_EPOCHS` (default `6`) — when the certified champion activates: `effective_epoch = close_epoch + ACTIVATION_DELAY_EPOCHS`. Keep `≥ SOLVER_ROUND_DECISION_EPOCHS` so certification has fully landed before the swap takes effect.
- Wall-clock epoch size is the fixed `EPOCH_SECONDS` protocol constant (`60`, in `minotaur_subnet/epoch/clock.py`) — used for `close_epoch` / `decision_deadline_epoch` / `effective_epoch` when native chain tempo and explicit block-based fallback are both unavailable. It is **consensus-critical and not operator-configurable**: the round-anchored fork pin anchors on `anchor_epoch * EPOCH_SECONDS`, so a divergent value causes `PACK_HASH_MISMATCH`. (Was the `SOLVER_ROUND_EPOCH_SECONDS` env var; removed 2026-06-11.)
- `SOLVER_ROUND_EPOCH_BLOCKS` (unset by default) — optional block-based fallback epoch size; used only when native chain tempo is unavailable from metagraph/subtensor state.
- `SUBMISSIONS_ACCEPTING` (default `true`) — global kill switch for new submissions.
- `SUBMISSIONS_API_KEY` (unset by default) — if set, requires header `x-submission-api-key` on submission create endpoints.
- `SOLVER_ROUND_INTERNAL_API_KEY` (unset by default) — shared secret for validator-to-validator round control and champion proposal traffic via header `x-solver-round-internal-key`.
- `SUBMISSIONS_RATE_LIMIT_PER_MINUTE` (default `60`) — per-route/per-principal create limit.
- `ENABLE_SOURCE_SUBMISSIONS` (default `false`) — required to use `/v1/submissions/source`.
- `ALLOW_SUBPROCESS_BENCHMARK` (default `false`) — required for `solver_path` benchmarking.
- `ALLOW_CHAMPION_HOT_SWAP` (default `false`) — enables runtime champion swaps when explicitly set.
- `CHAMPION_SWAP_TIMEOUT_SECONDS` (default `90`) — timeout when starting champion Docker runtime.
- `SUBMISSION_PROVENANCE_SIGNING_PRIVATE_KEY` (unset by default) — if set, submission screening signs provenance with EIP-191 secp256k1.
- `SUBMISSION_PROVENANCE_SIGNING_ADDRESS` (optional) — expected signer address for the configured private key.
- `SUBMISSION_PROVENANCE_ALLOWED_SIGNERS` (unset by default) — comma-separated EVM addresses allowed to sign provenance.
- `SUBMISSION_PROVENANCE_HMAC_KEY` (unset by default) — legacy HMAC signer/verifier key (kept for compatibility).
- `REQUIRE_SIGNED_PROVENANCE` (default `false`) — when enabled, champion adoption/hot-swap requires valid provenance under configured verifier policy.
- `REQUIRE_ASYMMETRIC_PROVENANCE` (default `false`) — asymmetric-only policy: disallows HMAC provenance and requires allowed signer verification.
- `VALIDATOR_HOTKEY_SS58` (optional) — explicit hotkey override for solver-round metagraph leader election when the API service cannot load a local Bittensor wallet.
- API startup now performs a provenance policy self-check and fails fast on inconsistent signer/verifier config.
- `ENFORCE_RUNTIME_SECURITY_PROFILE` (default `false`) — strict production guardrail; startup fails if unsafe runtime flags are enabled (source submissions, subprocess benchmarking, weak provenance verifier, missing API key/rate limit when accepting submissions).
- Champion adoption policy only allows `GENESIS` or Docker-screened submissions with an immutable local `image_id` (`sha256:...`).
- Source (`solver_path`) submissions can be benchmarked when enabled, but are never champion-eligible.
- The benchmark worker performs replay scoring only; live champion activation is handled by the solver round coordinator via `EpochManager`.
- Round closure may still be forced manually via `POST /v1/solver/round/close`, but when metagraph leader election is configured the elected leader now auto-closes rounds after `SOLVER_ROUND_OPEN_SECONDS`, replay-evaluates them, auto-certifies finalists, and activates certified champions once the current solver-round epoch reaches `effective_epoch`.
- Champion certification collects real validator quorum through `POST /v1/solver/round/certify`. The api service signs proposals when `VALIDATOR_PRIVATE_KEY` + `VALIDATOR_REGISTRY_<chain>` + `CHAMPION_REGISTRY_<chain>` are configured; the validator set comes from on-chain `ValidatorRegistry.getValidators()` and the quorum threshold from `ChampionRegistry.quorumBps()` (both refreshed every 60 s by `ProtocolConfig.refresh_loop`). Followers answer `POST /v1/solver/round/consensus/proposal`. Order consensus reads validator set + quorum from the same on-chain `ValidatorRegistry` on the order-execution chain (Base in production).
- Operators may abort a round explicitly via `POST /v1/solver/round/abort` to retain the incumbent and reopen intake.
- The leader and manual round-control endpoints now push explicit authenticated round state syncs (`/v1/solver/round/internal/close`, `/v1/solver/round/internal/certify`, `/v1/solver/round/internal/activate`, `/v1/solver/round/internal/abort`) so followers persist closed/certified/activated/aborted state before failover.
- The coordinator now enforces `decision_deadline_epoch`: once the current solver-round epoch passes the deadline without a certificate, the round aborts and the incumbent stays active.
- When metagraph sync can read native subnet tempo and `BlocksSinceLastStep`, solver-round epochs now follow the real subnet epoch/tempo rather than a local approximation.
- `/health` now reports `solver_round_role`, the current `solver_round_epoch`, the active epoch-clock mode/config, champion-consensus status, internal round-auth presence, and metagraph leader metadata when available.
- When hot-swap is enabled, champion runtime loading uses isolated Docker sessions (`docker run` harness protocol), not host-side Python imports.

## Production Security Checklist

Recommended production posture:

- Disable source submissions and subprocess benchmarking.
- Require asymmetric signed provenance (no HMAC fallback).
- Require submission API key and non-zero rate limiting if submissions are enabled.
- Enable strict runtime profile validation so startup fails on insecure config.
- Verify `/health` reports both `provenance_policy.valid=true` and `runtime_security_policy.valid=true`.

Example hardened env profile:

```bash
# Strict runtime guardrails
ENFORCE_RUNTIME_SECURITY_PROFILE=1

# Submission intake controls
SUBMISSIONS_ACCEPTING=1
SUBMISSIONS_API_KEY=__set_strong_shared_secret__
SOLVER_ROUND_INTERNAL_API_KEY=__set_distinct_internal_shared_secret__
SUBMISSIONS_RATE_LIMIT_PER_MINUTE=60
ENABLE_SOURCE_SUBMISSIONS=0
ALLOW_SUBPROCESS_BENCHMARK=0

# Champion/runtime hardening
ENABLE_SOLVER_ROUND_COORDINATOR=1
SOLVER_ROUND_COORDINATOR_INTERVAL_SECONDS=5
SOLVER_ROUND_OPEN_SECONDS=300
# Wall-clock epoch width is a fixed protocol constant (60s) — not configurable.
# Optional block-based epoch clock instead of wall-clock epochs:
# SOLVER_ROUND_EPOCH_BLOCKS=360
SUBTENSOR_URL=ws://127.0.0.1:9944
NETUID=112
WALLET_NAME=validator
HOTKEY_NAME=default
# Optional if the API cannot read a local Bittensor wallet:
# VALIDATOR_HOTKEY_SS58=5....
VALIDATOR_PRIVATE_KEY=0x__this_validator_evm_key__
# Peers come from on-chain ValidatorRegistry + metagraph axon discovery —
# no VALIDATOR_PEERS env required (refactor landed PR #10 / 2026-05-25).
VALIDATOR_REGISTRY_8453=0x__base_validator_registry__
VALIDATOR_REGISTRY_964=0x__btevm_validator_registry__
CHAMPION_REGISTRY_964=0x__btevm_champion_registry__
ALLOW_CHAMPION_HOT_SWAP=1
CHAMPION_SWAP_TIMEOUT_SECONDS=90

# Provenance policy (asymmetric-only)
REQUIRE_SIGNED_PROVENANCE=1
REQUIRE_ASYMMETRIC_PROVENANCE=1
SUBMISSION_PROVENANCE_SIGNING_PRIVATE_KEY=__set_validator_signing_key__
SUBMISSION_PROVENANCE_SIGNING_ADDRESS=__matching_signer_address__
SUBMISSION_PROVENANCE_ALLOWED_SIGNERS=__comma_separated_allowed_addresses__
SUBMISSION_PROVENANCE_HMAC_KEY=
```
