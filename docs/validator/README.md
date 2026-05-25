# Validator Overview

The Minotaur validator is the backbone of **Bittensor Subnet 112** -- a distributed intent execution platform. Validators run the Solving Engine, simulate execution plans on Ethereum forks, score results through a dual-scoring system, and reach consensus before relaying approved transactions on-chain.

## Contents

- [Quickstart](./quickstart.md) -- Get up and running
- [Configuration](./configuration.md) -- Full CLI and environment variable reference
- [Troubleshooting](./troubleshooting.md) -- Common issues and solutions

## What Validators Do

Validators perform six core functions:

1. **Run the Solving Engine** -- Execute miner-submitted solver code to generate execution plans for user orders in the Intent OrderBook.
2. **Simulate on Anvil forks** -- Plans are executed on Ethereum mainnet forks via Anvil, using snapshot/revert isolation so no real state is modified.
3. **Dual scoring** -- Every plan is scored twice. The JavaScript score (from the app's JS scoring module, range 0.0--1.0) and the on-chain score (from contract simulation on the Anvil fork) must both exceed the configured threshold.
4. **N-of-M consensus** -- The leader validator proposes plans; follower validators independently re-simulate, re-score, and sign EIP-712 approvals. Exact score match is not required -- followers sign if both scores pass their threshold.
5. **Weight emission** -- Champion-takes-all model. The miner who submitted the currently active (best-performing) solver receives 100% of emissions via `set_weights()`.
6. **Accept miner solver submissions** -- Validate incoming solver code, screen it through three stages, benchmark performance, and adopt the champion solver.

## Architecture

### Leader/Follower Model

Validators operate in a leader/follower topology:

- **Leader**: The validator with the highest TAO stake on subnet 112. Ties are broken by hotkey (lexicographic ascending). The leader runs the BlockLoop, processes all orders, and broadcasts proposals to followers.
- **Followers**: All other registered validators. They receive proposals from the leader, independently re-simulate and re-score each plan, and sign EIP-712 approvals if both scores pass threshold.
- **Leader failover**: When the leader changes (e.g., stake rebalancing), the Relayer drops all in-flight work. The new leader reprocesses everything from scratch.

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
| **JavaScript** | Validator Node.js sandbox | App-defined scoring logic via `score(plan, state, context)`. Reads simulation data including token transfers, gas usage, and state changes. Score range: 0.0--1.0. |
| **Solidity** | Anvil fork (simulated on-chain) | Contract-enforced invariants, user signature verification, validator quorum checks. Executed via ephemeral proxy (`CREATE2`) for state isolation. |

Both scores must independently exceed the threshold. This prevents any single layer from being bypassed.

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
| `GET` | `/v1/apps/` | List App Intents (read endpoint, no auth) |
| `POST` | `/v1/apps/` | Create an App Intent (gated by `X-Admin-Key` header when `ADMIN_API_KEY` is set) |
| `POST` | `/v1/apps/{app_id}/deploy` | Deploy an App Intent on-chain (admin-gated) |
| `POST` | `/v1/solver/round/consensus/proposal` | Receive a champion-consensus proposal from the leader (followers) |
| `POST` | `/v1/solver/round/certify` | Submit a certified champion (leader-only) |

Both ports must be reachable from the public internet so the current leader can deliver proposals; see [quickstart.md](./quickstart.md#ports) for the full firewall guidance.

Git/source solver submissions are currently served by the API server (`/v1/submissions*`), not by the standalone validator endpoint set above.

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

- **Python 3.12** with project dependencies installed
- **Node.js 20.x** for the JS scoring engine
- **Foundry** (anvil, forge, cast) for simulation and contract interaction
- **Bittensor wallet** with a registered hotkey on subnet 112
- **Ethereum RPC URL** (Alchemy or Infura) for Anvil mainnet fork
- **EVM private key** for EIP-712 consensus signing
- Sufficient **TAO stake** to participate in leader election

See [Quickstart](./quickstart.md) for step-by-step setup instructions.
