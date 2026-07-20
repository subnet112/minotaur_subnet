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
  - `submit/status` use `/v1/submissions*` (git-based workflow). The inline source-submission endpoint (`/v1/submissions/source`) was removed in PR #599 — all submissions go through the git PR path.

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

Create / validate / deploy:

- `POST /v1/apps/` (create; `X-Admin-Key` **or** EIP-712 `owner_signature`, rate-limited via `APP_CREATE_RATE_PER_MIN`)
- `POST /v1/apps/validate` (open preflight — no longer admin-gated; rate-limited via `APP_VALIDATE_RATE_PER_MIN`)
- `POST /v1/apps/{app_id}/deploy` (**async by default**; `?wait=true` for the synchronous body. Wallet-signature or fee-payment authorized)
- `GET /v1/apps/`  (per-chain `deployments` map + unified `status`, `partial` for mixed states)
- `GET /v1/apps/{app_id}/status`

App-management, lifecycle & registry (wallet-signature auth — headers `X-App-Auth-Signer/Signature/Nonce/Deadline`; see the [App-management API reference](./api/app-management.md)):

- `GET /v1/apps/{app_id}/admin-state`
- `GET /v1/apps/{app_id}/auth-nonce`
- `PUT /v1/apps/{app_id}/solidity`
- `POST /v1/apps/{app_id}/deployments/{chain_id}/retire`
- `POST /v1/apps/{app_id}/deployments/{chain_id}/float/deposit`
- `POST /v1/apps/{app_id}/deployments/{chain_id}/float/withdraw`
- `PATCH /v1/apps/{app_id}/deployments/{chain_id}/config`
- `POST /v1/apps/{app_id}/deployments/{chain_id}/registry/allow-developer`
- `GET /v1/apps/{app_id}/deployments/{chain_id}/registry-calldata`
- `POST /v1/apps/{app_id}/registration/request` · `.../approve` (admin) · `.../reject` (admin)

Other:

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
- `GET /v1/submissions/{submission_id}/status`
- `GET /v1/submissions`

`GET /v1/submissions` rows and the status payload now include `outcome_code` (machine-readable terminal-transition taxonomy — switch on this, not on the free-text reason), `miner_uid` (current-metagraph UID, null when unsynced/deregistered, PR #522), and a `waitlist` object when the status is `waitlisted` (PR #620). The `waitlisted` status is distinct from `rejected` (rotation-not-selected / benchmark-window-elapsed carry a next-round priority). The inline source endpoint was removed in PR #599.

### Wallets / Chains / Monitoring

- `POST /v1/wallets/`
- `GET /v1/wallets/`
- `GET /v1/wallets/{address}/balances`
- `GET /v1/wallets/{address}`
- `POST /v1/apps/{app_id}/fund`
- `POST /v1/testnet/faucet`
- `POST /v1/testnet/faucet_erc20`
- `GET /v1/chains` (each chain now carries `app_registry_address` — the AppRegistry gate on that chain — alongside `registry_address`, PR #553)
- `GET /v1/apps/{app_id}/monitor`

## Validator Service Surface (`:9100`)

The standalone validator daemon (`validator/main.py`) registers exactly these 9 routes:

- `GET /health`
- `GET /identity`
- `POST /consensus/proposal`
- `POST /internal/weights/queue`
- `GET /weights`
- `GET /weights/history`
- `GET /blockloop/status`
- `GET /consensus/info`
- `GET /leader`

The former `/intents/*`, `/orders/*`, `/apps/{app_id}/quote`, and `/reload` daemon routes were removed in the 2026-05-25 audit cleanup; the miner-/order-facing equivalents live on the API service (`:8080`, `/v1/…`).

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

Adoption is **relative** and resolved by a fixed ladder (`epoch/relative_scoring.py`), high→low priority:

1. **Output (primary, always armed).** Per-order `win` / `regression` / `matched` (within ±0.1% / `RELATIVE_TOL_BPS=10`) / `blind_spot_cover` / `dropped`; adopt if net better on breadth: `(wins + blind_spot_covers) − regressions ≥ DETHRONE_WIN_MARGIN (1)`. Regressions are **tolerated within a 1% floor** (`FLOOR_BPS=100`) and netted against wins — the older "any regression = reject / matching everywhere rejected" rule is gone.
2. **Tie-breaks (fully-matched saturated tie only):** gas (`GAS_MARGIN_BPS=200`, pre-refund metered gas) → factorization (`FACTOR_MARGIN=100`, `max_region_nodes`) → deadwood (`UNPRODUCTIVE_MARGIN=2000`, `unproductive_nodes`). All armed on `develop`; each fires "by data" (inert until both champion and challenger carry the metric). The verdict dict carries `adopt_via` (`performance`/`gas`/`factorization`/`deadwood`), `factor_delta`, `deadwood_delta`.

**Hard vetoes** (override every rung): `n_catastrophic == 0` (no order cut > 1%) and `n_dropped == 0`. The blind-spot *repeat* bar is wired but disarmed (`BLIND_SPOT_BAR_TTL_S = None`).

**Scoring definition — static quote.** The benchmark no longer calls `solver.quote()` or runs a champion reference-quote pre-pass; it injects a static zero quote purely to satisfy the on-chain ABI, and scores on the raw per-order delivered output. The `BENCHMARK_STATIC_QUOTE` / `BENCHMARK_REFQUOTE_CHECKPOINT` flags and the legacy quote path were deleted (PRs #595/#600) — those host envs and `/data/refquote_checkpoints.json` are now inert.

**Stage-1 screening floor (PR #585):** new submissions are rejected `too_entangled` when their largest AST region exceeds `MAX_REGION_NODES=4200`, or `dynamic_code` on bare `exec()`/`eval()`. The metric is persisted even on reject so the miner sees the number. The standing champion is never re-screened.

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
- `SOLVER_ROUND_DECISION_EPOCHS` (default `20`) — Phase 2 of the round: the post-close window (in `EPOCH_SECONDS` epochs) within which the leader must benchmark the champion + the round's submissions and certify a finalist, i.e. `decision_deadline_epoch = close_epoch + DECISION_EPOCHS`. Must fit the post-close benchmark batch, which routinely runs well over 5 min with several submissions and/or a slow champion reference (each scenario re-quotes the champion); **too small silently aborts contested rounds** (`certification_deadline_elapsed`) the instant after the leader decides to adopt, instead of adopting. Leader-driven + broadcast (followers adopt the leader's `decision_deadline_epoch`), so keep it fleet-uniform across a rollout. With the default this gives a ~25-min two-phase round (5-min OPEN + up to ~20-min CLOSED).
- `SOLVER_ROUND_ACTIVATION_DELAY_EPOCHS` (default `22`) — when the certified champion activates: `effective_epoch = close_epoch + ACTIVATION_DELAY_EPOCHS`. Keep `≥ SOLVER_ROUND_DECISION_EPOCHS` so certification has fully landed before the swap takes effect.
- Wall-clock epoch size is the fixed `EPOCH_SECONDS` protocol constant (`60`, in `minotaur_subnet/epoch/clock.py`) — used for `close_epoch` / `decision_deadline_epoch` / `effective_epoch` when native chain tempo and explicit block-based fallback are both unavailable. It is **consensus-critical and not operator-configurable**: the round-anchored fork pin anchors on `anchor_epoch * EPOCH_SECONDS`, so a divergent value causes `PACK_HASH_MISMATCH`. (Was the `SOLVER_ROUND_EPOCH_SECONDS` env var; removed 2026-06-11.)
- `SOLVER_ROUND_EPOCH_BLOCKS` (unset by default) — optional block-based fallback epoch size; used only when native chain tempo is unavailable from metagraph/subtensor state.
- `SUBMISSIONS_ACCEPTING` (default `true`) — global kill switch for new submissions.
- `SUBMISSIONS_API_KEY` (unset by default) — if set, requires header `x-submission-api-key` on submission create endpoints.
- `SOLVER_ROUND_INTERNAL_API_KEY` (unset by default) — shared secret for validator-to-validator round control and champion proposal traffic via header `x-solver-round-internal-key`.
- `SUBMISSIONS_RATE_LIMIT_PER_MINUTE` (default `60`) — per-route/per-principal create limit.
- `ALLOW_SUBPROCESS_BENCHMARK` (default `false`) — required for `solver_path` benchmarking.
- `SCREENING_BUILD_CONCURRENCY` (default `1`) — bounds concurrent stage-2 Docker builds (PR #583; AST metrics also moved off the event loop).
- `SUBMISSION_BENCHMARK_DETAILS_RETENTION` (default `300`) — caps stored `benchmark_details` to the N most-recent terminal submissions so the store can't freeze the event loop (PR #569). In-flight submissions always keep details.
- `SUBMISSIONS_MAX_ROUNDS_PER_FINGERPRINT` (default `0` = OFF) — leader-gateway quota on distinct benched rounds per normalized code fingerprint, **across all hotkeys** (PR #594). Only benched statuses burn quota; a comment-only / nonce-only resubmit of an identical tree is rejected pre-build (`fingerprint_repeat`).
- `SUBMISSION_INTAKE_ACK` (default on; `0` disables) — posts a "Submission received" ACK on the miner's PR to probe `Pull requests: Write` scope, so an under-scoped private-repo PAT is rejected at intake with a clear 400 rather than silently 403-ing days later (PR #526).
- **Removed:** `ENABLE_SOURCE_SUBMISSIONS` and the `/v1/submissions/source` endpoint (PR #599); `BENCHMARK_STATIC_QUOTE` and `BENCHMARK_REFQUOTE_CHECKPOINT` (PR #600). These envs are now inert.
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
- `ENFORCE_RUNTIME_SECURITY_PROFILE` (default `false`) — strict production guardrail; startup fails if unsafe runtime flags are enabled (subprocess benchmarking, weak provenance verifier, missing API key/rate limit when accepting submissions).
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

- Disable subprocess benchmarking (`ALLOW_SUBPROCESS_BENCHMARK=0`).
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
