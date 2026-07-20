# Validator Overview

The Minotaur validator is the backbone of **Bittensor Subnet 112** -- a distributed intent execution platform. Validators run the Solving Engine, simulate execution plans on Ethereum forks, score results through a dual-scoring system, and reach consensus before relaying approved transactions on-chain.

## Contents

- [Quickstart](./quickstart.md) -- Get up and running
- [Configuration](./configuration.md) -- Full CLI and environment variable reference
- [Updating safely](./updating.md) -- Health-gated updates with auto-rollback (recommended over Watchtower for high-stake validators)
- [Troubleshooting](./troubleshooting.md) -- Common issues and solutions

## What Validators Do

Validators perform six core functions:

1. **Run the Solving Engine** -- Execute miner-submitted solver code to generate execution plans for user orders in the Intent OrderBook.
2. **Simulate on Anvil forks** -- Plans are executed on Ethereum mainnet forks via Anvil, using snapshot/revert isolation so no real state is modified.
3. **Dual scoring** -- Every plan passes two layers: the app's JS module (validity + the real per-order result it emits, e.g. raw delivered output) and the on-chain `scoreIntent` gate. Champion adoption then compares challenger-vs-champion **per order** at the same fork pin, resolved by a fixed ladder — **output** (net better on breadth, with regressions bounded to a 1% floor), then, on a fully-matched tie, **gas → factorization → deadwood** tie-breaks. See [Champion adoption](#dual-scoring) below and the [miner champion/challenger model](../miner/README.md#championchallenger-model).
4. **N-of-M consensus** -- The leader validator proposes plans; follower validators independently re-simulate, re-score, and sign EIP-712 approvals. Exact score match is not required -- followers sign if both scores pass their threshold. For champion certification, the leader now re-broadcasts the round's current submission snapshot before the proposal fan-out, so followers vote on the candidate's ladder metrics (gas / factorization / deadwood) rather than close-time `None` values — leader-only deploy heals the whole fleet (PR #601).
5. **Weight emission** -- Champion-takes-all model. The miner who submitted the currently active (best-performing) solver receives 75% of the miner emission pool via `set_weights()`; the remaining 25% routes to the subnet owner (`CHAMPION_MINER_WEIGHT_FRACTION` in `weight_policy.py` — a fleet-uniform code constant, not an env). Weight commits are now **tempo-aligned** (PR #524): SN112 uses commit-reveal and the chain keeps only one pending commit per validator per tempo epoch, so emission is scheduled into a short window just before the epoch step (`TEMPO_ALIGNED_EMIT=1` by default, lead window `TEMPO_EMIT_LEAD_BLOCKS=20` blocks). This replaces the old wall-clock cadence that could commit 2–3×/tempo and leave a freshly-dethroned champion earning nothing.
6. **Accept miner solver submissions** -- Validate incoming solver code, screen it through three stages, benchmark performance, and adopt the champion solver.

## Architecture

### Leader/Follower Model

Validators operate in a leader/follower topology:

- **Leader**: During the early-network operating period, leadership is **locked** to the subnet team's hotkey via `LOCKED_LEADER_HOTKEY` (default set in `validator/metagraph_sync.py`; the matching EVM signer is pinned by `LOCKED_LEADER_EVM_ADDRESS`). `elect_leader()` returns **only** the peer whose hotkey equals `LOCKED_LEADER_HOTKEY` and **ignores stake entirely** — a third-party validator does **not** become leader while the lock is active, regardless of stake. Highest-TAO-stake election (ties broken by hotkey, lexicographic ascending) is only the fallback that applies once the lock is cleared (both env vars set empty). The leader runs the BlockLoop, processes all orders, and broadcasts proposals to followers.
- **Followers**: All other registered validators. They receive proposals from the leader, independently re-simulate and re-score each plan, and sign EIP-712 approvals if both scores pass threshold.
- **Leader failover**: The leader changes only when the lock is repointed or cleared (not by stake rebalancing under the default). On a leader change, the Relayer drops all in-flight work and the new leader reprocesses everything from scratch.

### BlockLoop Pipeline

The BlockLoop is the core runtime for validators, executing once per tick (default: every 12 seconds, matching Ethereum block time).

Each tick:

1. **Expire** stale orders past their deadline.
2. **Snapshot** all OPEN orders from the Intent OrderBook.
3. **Process** each order through the full pipeline:
   - Generate an execution plan (via the Solving Engine / miner solver)
   - Simulate the plan on an Anvil fork (captures on-chain score and token transfer events)
   - Run JS scoring (`score(plan, state, context)`)
   - Both scores must exceed threshold (default: 0.5)
   - Broadcast proposal to follower validators for consensus
   - Collect N-of-M EIP-712 signatures
   - Submit the approved plan via the Relayer
4. **Cross-chain orders**: Two-phase lifecycle -- source leg execution, then BRIDGING status while the bridge transfer completes, then destination leg execution.

### Dual Scoring

Every execution plan is scored at two layers:

| Layer | Where it runs | What it checks |
|-------|---------------|----------------|
| **JavaScript** | Validator Node.js sandbox | App-defined module via `score(plan, state, context)`. Reads simulation data (token transfers, gas, state changes) and emits the real per-order result the relative comparison uses — for `DexAggregatorApp`, a validity sentinel plus the **raw delivered output** (exact wei) in `metadata.raw_output`. |
| **Solidity** | Anvil fork (simulated on-chain) | Contract-enforced invariants, user signature verification, validator quorum checks. Executed via ephemeral proxy (`CREATE2`) for state isolation. |

Both layers must pass. Champion adoption is then **relative** and resolved by a fixed ladder, highest priority first (source of truth: `epoch/relative_scoring.py`):

1. **Output (primary, always armed).** Adopt if net better on breadth: `(wins + blind-spot covers) − regressions ≥ 1`. Regressions are **tolerated within a 1% per-order floor** and netted against wins — this is a bounded-regression, net-better rule, not the older "any regression = reject".
2. **Gas → Factorization → Deadwood tie-breaks** — fire only on a *fully-matched, saturated tie* (every compared order matched, zero regressions): cheaper total metered (pre-refund) gas by ≥200 bps, then smaller worst AST region (`max_region_nodes`) by ≥100, then less dead code (`unproductive_nodes`) by ≥2000. All three are **armed** on `develop` but fire "by data" — inert until both the champion and challenger records carry the metric.

**Hard vetoes** (override every rung): no order cut by more than 1% and no dropped order the champion serves. The blind-spot *repeat* bar is wired but **disarmed** (`BLIND_SPOT_BAR_TTL_S = None`), so it does not yet affect adoption.

### Intent OrderBook

The Intent OrderBook is the universal entry point for all intent execution:

- **One-shot orders**: Execute once and complete.
- **Perpetual orders**: Re-execute every tick when score exceeds threshold. No explicit trigger gate -- validators try every tick.
- Orders are signed by users (EIP-712) and submitted to the OrderBook.
- The leader validator's BlockLoop drains the OrderBook each tick.

## HTTP API Endpoints

The canonical third-party validator stack exposes HTTP on two ports.

### Validator daemon — port 9100

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/health` | Service health, loaded intents, uptime |
| `GET` | `/identity` | Self-attested EIP-712 binding `(evm_address, hotkey, axon_url)` for peer discovery |
| `GET` | `/intents/available` | Active intents available for miners |
| `GET` | `/intents/{app_id}/details` | Detailed info for a specific app |
| `GET` | `/intents/{app_id}/scores` | Score history for a specific app |
| `POST` | `/intents/{app_id}/submit` | Accept a miner plan submission |
| `GET` | `/weights` | Current champion and weight mapping |
| `GET` | `/weights/history` | Historical weight emissions |
| `GET` | `/blockloop/status` | Block loop tick statistics |
| `POST` | `/orders/submit` | Submit an order to the OrderBook |
| `GET` | `/orders` | List orders in the OrderBook |
| `POST` | `/apps/{app_id}/quote` | Get a dry-run quote for an intent |
| `POST` | `/consensus/proposal` | Receive an order-consensus proposal from the leader (followers) |
| `GET` | `/consensus/info` | Order-consensus configuration and peer info |
| `GET` | `/leader` | Leader status and metagraph info |
| `POST` | `/reload` | Reload app definitions from store |

### API service — port 8080

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/health` | Service health + champion-consensus state |
| `GET` | `/identity` | Same EIP-712 binding as 9100 (api-side peer discovery) |
| `GET` | `/v1/apps/` | List App Intents (read; each item now carries a per-chain `deployments` map + a unified `status` — `partial` for mixed multi-chain states, PR #598) |
| `POST` | `/v1/apps/` | Create an App Intent — `X-Admin-Key` **or** a self-serve EIP-712 `owner_signature` binding the recovered signer as the app `deployer` (PR #535). Non-admin create is rate-limited (`APP_CREATE_RATE_PER_MIN`, default 5/min). |
| `POST` | `/v1/apps/{app_id}/deploy` | Deploy on-chain. **Async by default** (`?wait=true` for the legacy synchronous body); wallet-signature or fee-payment authorized, no shared admin key required (PRs #611/#555/#534). See the [API reference](../api/app-management.md). |
| `POST` | `/v1/solver/round/consensus/proposal` | Receive a champion-consensus proposal from the leader (followers) |
| `POST` | `/v1/solver/round/certify` | Submit a certified champion (leader-only) |

App-management (create/validate/deploy) and the app-lifecycle, registry, and registration endpoints now use a wallet-signature auth model that retires the shared admin key. The full endpoint set, headers, and env flags are documented in the [App-management API reference](../api/app-management.md).

Both ports must be reachable from the public internet so the current leader can deliver proposals; see [quickstart.md](./quickstart.md#ports) for the full firewall guidance.

Git-based solver submissions are served by the API server (`/v1/submissions*`), not by the standalone validator endpoint set above. (The inline source-submission endpoint `/v1/submissions/source` was removed in PR #599.)

## Entry Points

There are three ways to run a validator:

1. **Canonical validator stack (recommended)** -- Docker Compose with the daemon and its three Anvil forks pre-configured. Start here:
   ```bash
   cd platform/validator
   cp .env.example .env  # fill in YOUR_* placeholders
   docker compose up -d
   ```
   This is what a third-party validator runs in production. See the [quickstart](./quickstart.md) for the full end-to-end setup including on-chain `ValidatorRegistry` onboarding.

2. **Standalone validator daemon** -- Direct Python process, you bring your own Anvil + Subtensor connections. Useful for advanced operators who want systemd supervision instead of Docker:
   ```bash
   python -m minotaur_subnet.validator.main --port 9100 --epoch-seconds 1200
   ```

3. **Local testnet (development only)** -- Full Docker Compose stack including subtensor, Anvil forks, API, validator, miner, relayer, and frontend. For local development of the protocol itself, not for connecting to mainnet:
   ```bash
   make testnet-up
   ```

## Requirements

For the **canonical Docker stack** (the path third-party operators take):

- **8 vCPU / 16-32 GB RAM / 200 GB SSD** (NVMe strongly preferred)
- **Public IPv4 with a static address** — your axon URL is published on
  the metagraph and must stay reachable for peer cross-attestation
- **Linux** (Ubuntu 22.04+ tested; Amazon Linux works)
- **Docker 24+ and Docker Compose v2**
- **Foundry** (`cast`) — used to generate your EVM signing key + read
  on-chain state
- **Bittensor CLI** (`btcli`) — used to register your hotkey on
  subnet 112
- **Bittensor wallet** with a registered hotkey on subnet 112
- **Archive RPC URLs** (Alchemy / Infura / QuickNode) for Ethereum
  mainnet + Base mainnet
- **EVM private key** for EIP-712 consensus signing — holds no funds

The Python code, Node.js scoring engine, and Anvil binaries all run
inside the Docker image — you do not need them installed on the host.

For the standalone Python path (advanced, no Docker), you additionally
need Python 3.12, Node.js 20.x, and Anvil installed natively. See the
"Running without Docker" section in the
[quickstart](./quickstart.md#running-without-docker-advanced).

See [Quickstart](./quickstart.md) for the canonical step-by-step setup.
