# Minotaur — Agentic Intent Execution Platform

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Tests](https://github.com/subnet112/minotaur_subnet/actions/workflows/test.yml/badge.svg)](https://github.com/subnet112/minotaur_subnet/actions/workflows/test.yml)
[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/)

Minotaur is a Bittensor subnet (Subnet 112) focused on distributed intent execution. Developers define App Intents (outcome + scoring), and the network's Solving Engine figures out optimal execution. Miners compete to write the best Solving Engine; validators run it, simulate plans, double-score results, and reach off-chain consensus.

## Contents
- [Overview](#overview)
- [Core Goals](#core-goals)
- [Roles and Components](#roles-and-components)
- [How It Works](#how-it-works)
- [Getting Started](#getting-started)
- [Configuration](#configuration)
- [Production Hardening](#production-hardening)
- [Roadmap](#roadmap)
- [Documentation](#documentation)
- [Official Links](#official-links)
- [License](#license)

## Overview

Minotaur is designed for high-frequency execution subnets where solvers (miners) compete in real time. The Aggregator coordinates live execution and records every submission with a cryptographic signature; validators later replay that history and reward miners deterministically.

The active on-chain runtime is built around `AppIntentBase`-derived app contracts.
For swaps, the canonical built-in app is `DexAggregatorApp`.

**Key Attributes:**
- Dual scoring: BOTH a JavaScript scoring module and an on-chain simulation constraint must pass.
- Cryptographic accountability: each submission must be signed by the solver's hotkey
- Leader-based OrderBook: The highest-stake validator maintains the OrderBook of App Intents.
- Permissionless Model Context Protocol (MCP): Agents can generate and propose new apps seamlessly, paying 0 gas fees to deploy.

**Two Operation Modes (Validator & Miner):**
- **Bittensor Mode:** Full validator/miner with real blockchain operations (default)
- **Simulation Mode:** Real aggregator + real simulation, but no Bittensor operations

## Core Goals

- **Automated Agentic Workflows** via MCP abstractions and Natural Language.
- **Better prices and reliability** via a continuous competitive market.
- **Cross-chain reach** to access deeper liquidity
- **Practical optimization tools** (fee reuse, gas optimization, etc.)

During the initial training phase, we collect real auctions from multiple swap aggregators and submit them to validators. Scoring benchmarks against competitor solves; miners strive to outperform. Additional tooling is prioritized based on miner feedback.

## Roles and Components

### Users and Apps
- Deploy App Intents containing a JavaScript scoring layer and a Solidity Layer
- Utilize Agentic MCP integrations to build end-to-end financial products without writing code.
- Submit signed orders to the universal Intent OrderBook.

### Miners (Software Developers and Operators)
- Write, maintain, and optimize the unified Solving Engine.
- Compete for best optimization (tokens/gas usage/speed).
- Assume the "champion" slot by hot-swapping the active engine when their benchmark score is highest.

### Validators (Execution environment and Attestation)
- Leaders scan the OrderBook and generate execution plans via the Solving Engine.
- Execute the solver software written by miners within isolated Docker containers to simulate plans on an Anvil fork.
- Double-score the simulation against both the JS and On-chain logic.
- Run epochs (time windows, default: 5 minutes) to collect validation results
- Compute miner scores based on validation success rates
- Compute and emit weights on the Bittensor chain
- Submit weights to the aggregator for transparency

### Settlement Contracts
- Verify user signatures, constraints, cancels/expiries, and validator quorum attestations
- Move tokens only when checks pass

## How It Works

### High-level Flow

1. **Ingestion:** Users or Agents submit intents to the OrderBook (signed).
2. **Solver competition:** Miners provide and continuously update their Solving Engine software. The highest-scoring engine becomes the Champion and runs across all validators.
3. **Execution & Simulation:** The Leader Validator takes the pending orders and generates execution plans using the Champion's engine.
4. **Scoring:** The Leader simulates the plans on Anvil. The plan must pass both the JS scoring module and the on-chain constraint score. 
5. **Consensus:** The Leader broadcasts the plan. Follower Validators re-simulate and sign if it passes the dual-score.
6. **Settlement:** Once a quorum is reached, the single Relayer submits the transaction. The immutable `AppIntentBase` smart contract finalizes the data.

### What Solvers Can Do

- **Direct matching:** Match intents when buyers and sellers cross
- **Routing:** Use AMMs, RFQs, and aggregators to fill residuals
- **Internal arbitrage legs** are allowed only if they strictly improve user outcomes

### Scoring and Economic Alignment

- **Primary:** User surplus (minOut respected; higher effective price wins)
- **Secondary:** Correctness, gas efficiency, revert risk
- **Tertiary:** Protocol fee contribution (tie-breaker only; never at expense of user surplus)

## Getting Started

### Prerequisites
- Python 3.12+
- Node.js 20+ (for JS scoring engine runtime)
- Docker (for local testnet and emulation scenarios)
- Foundry (`forge`) for Solidity tests and E2E on Anvil

### Install
```bash
git clone <repo>
cd minotaur_subnet
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

### Run Core Services
```bash
# App Intents API (FastAPI)
python -m minotaur_subnet.api.server --port 8080

# Validator service (leader/follower depending on metagraph state)
python -m minotaur_subnet.validator.main --port 9100

# Miner process (submits solver strategy updates)
python -m minotaur_subnet.miner.main agent --validator-url http://localhost:8080
```

### Local Testnet
```bash
# Full local stack (API, validator, relayer, supporting services)
make testnet-up

# Presenter-friendly local demo prep: boot + verify seeded DexAggregatorApp
make demo-prep

# Tear down
make testnet-down
```

For the canonical safe local demo flow, see
`platform/local_testnet/README.md`. The Docker demo path runs with
`MVP_DEMO_MODE=1` and keeps native Bittensor proxy execution off unless you
explicitly enable it for the local subtensor.

For controlled demos with a private solver repo, the API can also be given a
separate read-only HTTPS clone credential via the `SUBMISSION_GIT_CLONE_*`
environment variables. Do not reuse miner account credentials on validator/API
infrastructure.

### Testing
```bash
# Quick local confidence (unit + app tests)
make test

# Full local regression sweep
make test-all

# Live local_testnet smoke suite; recreates the Docker stack first
make test-testnet

# Mainnet-fork E2E only (requires ALCHEMY_API_KEY or ETHEREUM_RPC_URL)
make test-fork
```

DexAggregator-focused E2E contributors should reuse
`tests/e2e/dex_test_helpers.py` for funding, approval, current intent param
encoding, deployment save, and "submit then sign real order id" flows so the
tests stay aligned with the live `DexAggregatorApp` contract path.

## Configuration

Most runtime behavior is controlled via environment variables.

### Core Runtime
| Variable | Description |
|----------|-------------|
| `ANVIL_RPC_URL` | RPC used for local/mainnet-fork simulation |
| `BASE_RPC_URL` | Base chain RPC (enables multi-chain paths) |
| `ETHEREUM_RPC_URL` | Ethereum mainnet RPC (solver quoting + relayer) |
| `USE_EVM_RELAYER` | Enable real EVM relayer in API/block loop |
| `RELAYER_PRIVATE_KEY` | Relayer signer key for on-chain tx submission |
| `BLOCK_LOOP_TICK_INTERVAL` | Block loop cadence (seconds) |
| `BLOCK_LOOP_SCORE_THRESHOLD` | Default JS score threshold |

### Validator + Consensus
| Variable | Description |
|----------|-------------|
| `SUBTENSOR_URL` | Subtensor endpoint for metagraph sync |
| `NETUID` | Subnet ID (112) |
| `WALLET_NAME`, `HOTKEY_NAME` | Validator wallet identifiers |
| `VALIDATOR_HOTKEY_SS58` | Optional explicit hotkey override for solver-round leader election |
| `VALIDATOR_PRIVATE_KEY` | EVM key used for consensus signatures |
| `VALIDATOR_PEERS` | Comma-separated `validatorAddress@http://host:port` list |
| `VALIDATOR_REGISTRY_ADDRESS` | On-chain `ValidatorRegistry` holding the canonical `quorumBps`. Order-consensus daemons read it at startup and refresh once per epoch. See [Quorum management](docs/operator/quorum-management.md) for changing the value. |
| `CHAMPION_QUORUM_BPS` | Quorum for champion-certification consensus (separate from order consensus; ChampionRegistry on BT EVM holds its own value to mirror) |
| `SOLVER_ROUND_INTERNAL_API_KEY` | Shared secret for validator-to-validator round control (`x-solver-round-internal-key`) |
| `SOLVER_ROUND_EPOCH_BLOCKS` | Optional block-based fallback solver-round epoch size when native tempo is unavailable |
| `FORCE_LEADER` | Overrides follower mode in local testing |

### Multi-chain Deployment
| Variable | Description |
|----------|-------------|
| `APP_INTENT_BASE_<CHAIN_ID>` | AppIntent contract address per chain |
| `VALIDATOR_REGISTRY_<CHAIN_ID>` | Shared validator registry per chain |
| `RELAYER_WALLET_<CHAIN_ID>` | Relayer EOA per chain |

See `platform/.env.example` and `platform/local_testnet/.env.example` for concrete templates.

## Production Hardening

For production validators/APIs, use strict runtime guardrails and asymmetric provenance.

Recommended baseline:

- `ENFORCE_RUNTIME_SECURITY_PROFILE=1`
- `ENABLE_SOURCE_SUBMISSIONS=0`
- `ALLOW_SUBPROCESS_BENCHMARK=0`
- `REQUIRE_SIGNED_PROVENANCE=1`
- `REQUIRE_ASYMMETRIC_PROVENANCE=1`
- `SUBMISSION_PROVENANCE_ALLOWED_SIGNERS` configured
- `SUBMISSION_PROVENANCE_HMAC_KEY` unset
- `SUBMISSIONS_API_KEY` configured if submissions are enabled
- `SOLVER_ROUND_INTERNAL_API_KEY` configured if `VALIDATOR_PEERS` is configured
- `SUBMISSIONS_RATE_LIMIT_PER_MINUTE` > 0

Example profile:

```bash
ENFORCE_RUNTIME_SECURITY_PROFILE=1
SUBMISSIONS_ACCEPTING=1
SUBMISSIONS_API_KEY=__set_strong_shared_secret__
SOLVER_ROUND_INTERNAL_API_KEY=__set_distinct_internal_shared_secret__
SUBMISSIONS_RATE_LIMIT_PER_MINUTE=60
ENABLE_SOURCE_SUBMISSIONS=0
ALLOW_SUBPROCESS_BENCHMARK=0

ENABLE_SOLVER_ROUND_COORDINATOR=1
SOLVER_ROUND_COORDINATOR_INTERVAL_SECONDS=5
SOLVER_ROUND_OPEN_SECONDS=300
# Wall-clock epoch width is a fixed protocol constant (60s, EPOCH_SECONDS in
# minotaur_subnet/epoch/clock.py) — consensus-critical, not operator-configurable.
# Optional block-based epoch clock instead of wall-clock epochs:
# SOLVER_ROUND_EPOCH_BLOCKS=360
SUBTENSOR_URL=ws://127.0.0.1:9944
NETUID=112
WALLET_NAME=validator
HOTKEY_NAME=default
# Optional if the API cannot read a local Bittensor wallet:
# VALIDATOR_HOTKEY_SS58=5....
VALIDATOR_PRIVATE_KEY=0x__this_validator_evm_key__
VALIDATOR_PEERS=0xPeer1@http://peer1-api:8080,0xPeer2@http://peer2-api:8080
VALIDATOR_REGISTRY_ADDRESS=0x__validator_registry_on_this_chain__
CHAMPION_QUORUM_BPS=8000

ALLOW_CHAMPION_HOT_SWAP=1
CHAMPION_SWAP_TIMEOUT_SECONDS=90

REQUIRE_SIGNED_PROVENANCE=1
REQUIRE_ASYMMETRIC_PROVENANCE=1
SUBMISSION_PROVENANCE_SIGNING_PRIVATE_KEY=__set_validator_signing_key__
SUBMISSION_PROVENANCE_SIGNING_ADDRESS=__matching_signer_address__
SUBMISSION_PROVENANCE_ALLOWED_SIGNERS=__comma_separated_allowed_addresses__
SUBMISSION_PROVENANCE_HMAC_KEY=
```

Runtime verification:

- `GET /health` should report `provenance_policy.valid=true`
- `GET /health` should report `runtime_security_policy.valid=true`

## Roadmap

**Start date:** 2025-09-01

### Phase 0 - Launch (Month 0–1): Subnet Activation
- Network bring-up: validator code
- Swap intent forwarder: copying real-time, live swap intents and pushing them to validators' API
- Project website & branding updates
- Ecosystem Partnerships

### Phase A - Training (Month 1–3): Miner Onboarding and Training
- Solver interface + scoring (user surplus, correctness, gas efficiency)
- Observability alpha: epoch metrics, basic dashboards between miners and competitor solvers
- Initial marketing

### Phase B - Release (Month 3–6): Deployment on Base
- MEV protection
- Settlement contract deployment on Base
- Fee manager deployment on Base
- Swap app deployment
- Advanced protocol fee management
- Continuous benchmarking versus competition
- User marketing
- Introduction to fee → alpha tokenomics

### Phase C - Advancement (Month 6–11): Full-Featured Subnet (Core v1 Complete)
- Multi-chain adapters: extend swaps cross-chain (Ethereum after Base)
- Executor incentives: bonds, slashing, submission rewards
- Anti-spam hardening: quotas, dust limits, adaptive rate limiting
- Observability: new validator/solver leaderboards
- Security reviews and audits (contracts + validator code)
- Additional optimization tooling for miners

## Documentation

### Code-Verified Runtime (Current)
- [Runtime Guide](docs/code-verified-runtime.md) - Canonical runtime behavior from current code paths

### Validator Documentation
- [Validator Overview](docs/validator/README.md) – Introduction to the validator
- [Validator Quickstart](docs/validator/quickstart.md) – Get started quickly
- [Validator Configuration](docs/validator/configuration.md) – Complete configuration reference
- [Validator Troubleshooting](docs/validator/troubleshooting.md) – Common issues and solutions

### Miner Documentation
- [Miner Overview](docs/miner/README.md) – Introduction to the miner
- [Miner Quickstart](docs/miner/quickstart.md) – Get started quickly
- [Miner Configuration](docs/miner/configuration.md) – Complete configuration reference
- [Solver API](docs/miner/solver-api.md) – Solver endpoints and API specification
- [Custom Solver Guide](docs/miner/custom-solver.md) – Guide for writing your own solver
- [Miner Troubleshooting](docs/miner/troubleshooting.md) – Common issues and solutions

## Operations

### Health Checks
- Aggregator availability: `curl $AGGREGATOR_URL/health`
- Validator logs: `logs/*.log` (structured logging with prefixes `INIT`, `LOOP`, `CHAIN`, `SCORES`, etc.)

### Common Issues
| Symptom | Checks |
|---------|--------|
| No weights emitted | Ensure the aggregator has pending orders; confirm tempo spacing has elapsed; verify hotkeys exist in the metagraph |
| Aggregator errors | Validate `AGGREGATOR_URL`, networking, TLS settings, and API keys |
| UID mapping warnings | Miner hotkey must appear in the subnet metagraph |
| Slow recovery after downtime | The validator replays missed epochs using the persisted state store; monitor logs for catch-up progress |

### State Persistence
Runtime state (watermarks, last scores, last emitted block) is stored under the validator's data directory (`StateStore`). Each update writes a JSON snapshot plus a `.backup`, ensuring recoverability across restarts.

## Testing
```bash
# Unit tests (no external services)
pytest tests/unit -v

# Integration tests (async events pipeline)
pytest tests/integration -v
```
Integration tests rely on stubbed aggregator clients and in-memory state stores; no subnet access is required.

## Official Links
- **Website:** https://minotaursubnet.com
- **X (formerly Twitter):** https://x.com/minotaursubnet
- **Taostats:** https://taostats.io/subnets/112/chart

## License
MIT License (see `LICENSE`).

---

## Disclaimer
This repository does not contain production code. Nothing herein constitutes investment, legal, or tax advice. Features, timelines, and economics are forward-looking and subject to change. Participation in crypto systems carries risk (including smart-contract, market, and operational risks). Do your own research and consult qualified professionals where appropriate.
