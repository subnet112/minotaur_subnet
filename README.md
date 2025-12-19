# Minotaur — Distributed DEX Aggregator & Swap Intent Solver Engine

Minotaur is a Bittensor subnet (Subnet 112) focused on swap-intent processing and execution optimization. It leverages a subnet-native incentive mechanism to deliver better, cheaper, and faster trades for users.

## Contents
- [Overview](#overview)
- [Core Goals](#core-goals)
- [Roles and Components](#roles-and-components)
- [How It Works](#how-it-works)
- [Getting Started](#getting-started)
- [Configuration](#configuration)
- [Roadmap](#roadmap)
- [Documentation](#documentation)
- [Official Links](#official-links)
- [License](#license)

## Overview

Minotaur is designed for high-frequency execution subnets where solvers (miners) compete in real time. The Aggregator coordinates live execution and records every submission with a cryptographic signature; validators later replay that history and reward miners deterministically.

**Key Attributes:**
- Deterministic scoring: every validator processes the same event window and produces the same weight vector
- Cryptographic accountability: each submission must be signed by the solver's hotkey; invalid or unknown hotkeys are discarded
- Tempo-aware emission: weights are submitted once per tempo (epoch) after the chain-finalization buffer has elapsed
- On-chain constraint compliance: normalization, max-weight clamping, min-weight requirements, and waiting for inclusion/finalization are handled automatically

**Two Operation Modes (Validator & Miner):**
- **Bittensor Mode:** Full validator/miner with real blockchain operations (default)
- **Simulation Mode:** Real aggregator + real simulation, but no Bittensor operations

## Core Goals

- **Better prices and reliability** via competition and verifiable execution
- **Reduced user fees** by incentivizing miners with phased-in solver fees and emissions
- **Cross-chain reach** to access deeper liquidity
- **Practical optimization tools** (fee reuse, gas optimization, etc.)

During the initial training phase, we collect real auctions from multiple swap aggregators and submit them to validators. Scoring benchmarks against competitor solves; miners strive to outperform. Additional tooling is prioritized based on miner feedback.

## Roles and Components

### Users and Apps
- Submit signed swap intents
- Cancel/replace orders; monitor fills via APIs/streams

### Miners (Ingress and Availability)
- Execute quotes
- Compute candidate settlements that maximize user surplus
- Compete for best optimization (tokens/gas usage/speed)
- Use direct matches, AMMs, RFQ/MMs, and aggregators when helpful
- Earn solver fees on target chains (paid in buy tokens)
- Earn emissions based on performance (winner-takes-most model)

### Validators (Canonical State and Attestation)
- Fetch pending orders from the aggregator
- Validate order execution through Docker-based simulation
- Run epochs (time windows, default: 5 minutes) to collect validation results
- Compute miner scores based on validation success rates
- Compute and emit weights on the Bittensor chain
- Submit weights to the aggregator for transparency

### Settlement Contracts
- Verify user signatures, constraints, cancels/expiries, and validator quorum attestations
- Move tokens only when checks pass

## How It Works

### High-level Flow

1. **Ingestion:** Users submit swap intents to the aggregator (signed; include minOut/slippage and deadline)
2. **Solver competition:** Solvers provide quotes for orders. The aggregator selects the best quote and creates an order
3. **Validation epochs:** Validators run time-based epochs (default: 5 minutes) during which they:
   - Fetch pending orders from the aggregator
   - Simulate each order using Docker containers to verify execution correctness
   - Collect validation results over the epoch duration
4. **Scoring and weights:** At the end of each epoch, validators compute miner scores based on validation success rates, normalize to weights, and apply burn allocation (if configured)
5. **Weight emission:** Validators submit computed weights to the Bittensor chain, which distributes emissions to miners based on their performance
6. **Settlement:** Orders are executed on-chain by the aggregator or executors. Settlement contracts verify signatures, constraints, and bounds before moving tokens
7. **Fees and payouts:** In fee-on phase, the winning solver/miner receives solver fees (and optional surplus share) per settlement; fee routing and split are enforced by settlement contracts

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
- Docker (for order simulation)
- **Bittensor Mode:** A registered wallet/hotkey on the target subnet (Subnet 112)
- **Simulation Mode:** Access to the Aggregator API and Docker + RPC for simulation
- **Both Modes:** API keys (VALIDATOR_API_KEY for validators, MINER_API_KEY for miners)

### Quick Start

#### Validator - Simulation Mode (Testing)
```bash
git clone <repo>
cd minotaur
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt

# Test with real aggregator and real simulation, but don't affect blockchain
export VALIDATOR_MODE=mock
export AGGREGATOR_URL=http://your-aggregator:4000
export VALIDATOR_API_KEY=your-validator-api-key  # Required
export SIMULATOR_RPC_URL=https://mainnet.infura.io/v3/YOUR_INFURA_KEY

python -m neurons.validator \
  --validator.mode mock \
  --aggregator.url http://your-aggregator:4000 \
  --validator.api_key your-validator-api-key \
  --simulator.rpc_url "https://mainnet.infura.io/v3/YOUR_INFURA_KEY"
```

#### Validator - Bittensor Mode (Production)
```bash
# Production configuration
export NETUID=112
export WALLET_NAME=my-validator
export WALLET_HOTKEY=my-hotkey
export AGGREGATOR_URL=http://127.0.0.1:4000
export VALIDATOR_API_KEY=your-validator-api-key  # Required
export SIMULATOR_RPC_URL=https://mainnet.infura.io/v3/YOUR_INFURA_KEY
export VALIDATOR_EPOCH_MINUTES=5
export BURN_PERCENTAGE=0.95

python -m neurons.validator \
  --validator.mode bittensor \
  --wallet.name "$WALLET_NAME" \
  --wallet.hotkey "$WALLET_HOTKEY" \
  --netuid "$NETUID" \
  --subtensor.network finney \
  --subtensor.chain_endpoint wss://entrypoint-finney.opentensor.ai:443 \
  --validator.api_key "$VALIDATOR_API_KEY" \
  --simulator.rpc_url "$SIMULATOR_RPC_URL" \
  --burn_percentage 0.95
```

#### Miner - Simulation Mode (Testing)
```bash
# Run miner in simulation mode (generates hotkey from miner_id)
export MINER_MODE=simulation
export MINER_ID=my-test-miner
export AGGREGATOR_URL=http://localhost:4000
export MINER_API_KEY=your-miner-api-key  # Required for /v1/solvers/* endpoints
export MINER_BASE_PORT=8000
export ETHEREUM_RPC_URL=https://mainnet.infura.io/v3/YOUR_INFURA_KEY

python -m neurons.miner \
  --miner.mode simulation \
  --miner.id my-test-miner \
  --aggregator.url http://localhost:4000 \
  --miner.api_key your-miner-api-key \
  --miner.base_port 8000 \
  --miner.num_solvers 2  # Run 2 solvers (default: 1)
```

#### Miner - Bittensor Mode (Production)
```bash
# Run miner in bittensor mode (uses configured wallet)
export MINER_MODE=bittensor
export WALLET_NAME=my-miner
export WALLET_HOTKEY=my-hotkey
export AGGREGATOR_URL=http://your-aggregator:4000
export MINER_API_KEY=your-miner-api-key  # Required
export ETHEREUM_RPC_URL=https://mainnet.infura.io/v3/YOUR_INFURA_KEY

python -m neurons.miner \
  --miner.mode bittensor \
  --wallet.name my-miner \
  --wallet.hotkey my-hotkey \
  --aggregator.url http://your-aggregator:4000 \
  --miner.api_key your-miner-api-key
```

**Important:** The solver always queries Uniswap V2/V3 on-chain for price quotes, so you **must** configure an Ethereum RPC URL (`ETHEREUM_RPC_URL` or `ALCHEMY_API_KEY`) to avoid rate limiting errors.

### Automatic Updates

Minotaur includes an automatic upgrader script that keeps your validator up-to-date:

```bash
# Check for updates only
python scripts/upgrade_validator.py --check-only

# Perform automatic upgrade and restart
python scripts/upgrade_validator.py

# Force upgrade even if no newer version available
python scripts/upgrade_validator.py --force
```

**Note:** To use the automatic upgrader, you must set the `GITHUB_REPO` environment variable with your repository (e.g., `export GITHUB_REPO="username/repo-name"`). If not set, the upgrader will skip update checks.

## Configuration

All options can be exported as environment variables or passed via CLI flags where supported. The most common settings are listed below (see `docs/validator/configuration.md` and `docs/miner/configuration.md` for complete references).

### Aggregator API
| Variable | Description | Default |
|----------|-------------|---------|
| `AGGREGATOR_URL` | Base URL for the aggregator API | – |
| `VALIDATOR_API_KEY` | Validator-specific API key (required for validator endpoints) | – |
| `MINER_API_KEY` | Miner-specific API key (required for miner endpoints) | – |
| `AGGREGATOR_TIMEOUT` | HTTP timeout (seconds) | 10 |
| `AGGREGATOR_VERIFY_SSL` | TLS verification (1/0) | 1 |
| `AGGREGATOR_MAX_RETRIES` | Retry attempts | 3 |
| `AGGREGATOR_BACKOFF_SECONDS` | Retry backoff multiplier | 0.5 |

### Validator Settings
| Variable | Description | Default |
|----------|-------------|---------|
| `VALIDATOR_MODE` | `bittensor` or `mock` | `bittensor` |
| `VALIDATOR_POLL_SECONDS` | Poll interval for new epochs | 12 |
| `VALIDATOR_EPOCH_MINUTES` | Epoch duration in minutes | 5 |
| `VALIDATOR_CONTINUOUS` | Enable continuous epoch mode | true |
| `SIMULATOR_RPC_URL` | Ethereum RPC URL for simulation | Required |
| `SIMULATOR_MAX_CONCURRENT` | Maximum concurrent simulations | 5 |
| `SIMULATOR_TIMEOUT_SECONDS` | Simulation timeout (seconds) | 300 |
| `BURN_PERCENTAGE` | Fraction of emissions to burn (0.0-1.0) | 0.0 |

### Miner Settings
| Variable | Description | Default |
|----------|-------------|---------|
| `MINER_MODE` | `simulation` or `bittensor` | `simulation` |
| `MINER_ID` | Miner identifier (required in simulation mode) | – |
| `MINER_BASE_PORT` | Base port for solver servers | 8000 |
| `MINER_NUM_SOLVERS` | Number of solvers to run | 1 |
| `MINER_SOLVER_TYPE` | Solver type (v2, v3, base, etc.) | v3 |
| `ETHEREUM_RPC_URL` | Ethereum RPC URL (required) | – |
| `ALCHEMY_API_KEY` | Alchemy API key (alternative to RPC URL) | – |

### Bittensor / Subtensor
| Variable | Description |
|----------|-------------|
| `NETUID` | Target subnet (112 for Minotaur) |
| `SUBTENSOR_NETWORK` | `finney`, `test`, or `local` |
| `SUBTENSOR_ADDRESS` / `SUBTENSOR_WS` | Endpoint override |
| `WALLET_NAME`, `WALLET_HOTKEY` | Wallet identifiers |

Refer to `env.example` for a ready-to-use template.

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
