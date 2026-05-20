# Minotaur — Bittensor Subnet 112

Minotaur is a distributed intent execution platform on [Bittensor](https://bittensor.com/) (NETUID 112). Developers define **App Intents** — a Solidity contract paired with a JavaScript scoring module — and deploy them permissionlessly. Users submit orders to the **Intent OrderBook**, and the network's **Solving Engine** generates optimal execution plans. Validators simulate plans on Anvil forks, apply dual scoring (JS + on-chain), reach off-chain consensus, and the Relayer submits approved plans on-chain.

## Core Concepts

### App Intents

An App Intent is a smart contract (inheriting `AppIntentBase`) paired with a JavaScript scoring module. The Solidity contract is immutable and enforces on-chain safety — invariants, user signature verification, and validator quorum checks. The JS module is hot-upgradeable and handles off-chain scoring via `score(plan, state, context)`.

Each App declares its own intent functions. There is no global intent type taxonomy.

For swap execution, the canonical built-in app is `DexAggregatorApp`. The older
`SwapApp` contract remains only as an example/legacy artifact and is not the
active runtime path.

### Intent OrderBook

The universal entry point for all intent execution. Users submit signed orders — either one-shot (execute once) or perpetual (execute every tick when score exceeds threshold). The highest-stake validator serves as OrderBook leader.

### Solving Engine

A single Solving Engine handles all Apps across the entire network. Miners compete to write the best engine. Validators run the winning engine in sandboxed Docker containers to generate execution plans for pending orders.

### Dual Scoring

Every plan must pass two independent scoring layers:

| Layer | Location | Purpose |
|-------|----------|---------|
| **JavaScript** | Validator (off-chain) | `score(plan, state, context)` returns 0.0-1.0 |
| **Solidity** | On-chain (`AppIntentBase`) | Enforces invariants, returns numeric score |

Both scores must exceed the app's threshold for a plan to be approved.

### Consensus

The leader validator proposes a plan. Follower validators independently re-simulate and re-score. If both scores pass, followers sign with EIP-712. The leader collects N-of-M signatures. The Relayer submits the co-signed transaction on-chain.

### Wallets

Users can connect their own wallet (MetaMask, any EIP-712 signer) or use a managed wallet (Lit Protocol MPC, 2-of-2 custody). No pre-deposit is required either way.

### Fees

Fixed fees in wTAO, paid upfront. Users pay zero EVM gas — the Relayer fronts all gas costs.

## How It Works

```
User submits signed order to OrderBook
        |
        v
Leader validator's Solving Engine generates plan
        |
        v
Plan simulated on Anvil fork (captures on-chain score + transfer events)
        |
        v
JS scoring engine evaluates plan (0.0 - 1.0)
        |
        v
BOTH scores must exceed threshold
        |
        v
Leader broadcasts proposal to follower validators
        |
        v
Followers independently re-simulate + re-score
        |
        v
Followers sign with EIP-712 if both scores pass
        |
        v
Leader collects N-of-M quorum
        |
        v
Relayer submits co-signed transaction on-chain
        |
        v
AppIntentBase.executeIntent() verifies quorum + user sig + executes via proxy
```

## Documentation

### Getting Started

- **[Introduction](./README.md)** — This page
- **[Code-Verified Runtime Guide](./code-verified-runtime.md)** — Current behavior from executable code paths (recommended first read)

### Miner

- **[Miner Overview](./miner/README.md)** — What miners do and how incentives work
- **[Miner Quickstart](./miner/quickstart.md)** — Get a miner running
- **[Miner Configuration](./miner/configuration.md)** — Complete configuration reference
- **[Solver API](./miner/solver-api.md)** — Solver submission endpoints
- **[Custom Solver](./miner/custom-solver.md)** — Writing a custom solver
- **[Miner Troubleshooting](./miner/troubleshooting.md)** — Common issues and solutions

### Validator

- **[Validator Overview](./validator/README.md)** — What validators do
- **[Validator Quickstart](./validator/quickstart.md)** — Get a validator running
- **[Validator Configuration](./validator/configuration.md)** — Complete configuration reference
- **[Validator Troubleshooting](./validator/troubleshooting.md)** — Common issues and solutions

### Operator

- **[Network reference](./operator/network-reference.md)** — Mainnet addresses, endpoints, cluster expectations
- **[Quorum management](./operator/quorum-management.md)** — Reading and changing the network-wide quorum threshold

### Solver

- **[Solver Guide](./solver/solver_guide.md)** — Comprehensive guide to writing solvers

## Getting Started

The fastest way to explore Minotaur is with the local testnet, which starts the full stack (Anvil forks, subtensor, API, validator, relayer) in Docker. The miner agent runs on host via `make miner-agent`:

```bash
git clone https://github.com/subnet112/minotaur_subnet.git
cd minotaur_subnet
make testnet-up
```

Services available after startup:

| Service | Port | URL |
|---------|------|-----|
| API | 8080 | http://localhost:8080 |
| Relayer | 8091 | http://localhost:8091 |
| Anvil (ETH fork) | 8545 | http://localhost:8545 |
| Anvil (Base fork) | 8546 | http://localhost:8546 |
| Subtensor | 9944 | ws://localhost:9944 |

Stop with `make testnet-down`.

To run smoke tests against the live local testnet stack:

```bash
make test-testnet
```

This recreates a clean Docker Compose testnet, then validates service health,
creates and deploys a fresh `DexAggregatorApp` through the real API path,
checks faucet and balance helpers, and exercises live prepare and quote against
that freshly deployed flagship app.

## Testing

Use the `Makefile` targets as the canonical entrypoints:

```bash
# Quick confidence: unit + app tests
make test

# Full local regression sweep
make test-all

# Live Docker testnet smoke path
make test-testnet

# Mainnet-fork-only E2E path
make test-fork
```

Contributor note: DexAggregator-focused E2E tests should reuse
`tests/e2e/dex_test_helpers.py` for token funding and approval, the current
9-field intent payload encoding, deployment recording, and the orderbook
"submit then sign real order id" flow. That keeps the tests aligned with the
current `DexAggregatorApp` ABI and execution path.

## Current Status

Minotaur is live on Bittensor testnet as **NETUID 112**. The platform supports Ethereum mainnet and cross-chain execution via bridge adapters.

## Getting Help

- Check the troubleshooting guides for common issues
- See the [Solver Guide](./solver/solver_guide.md) for writing custom solvers
- Review `CLAUDE.md` in the repo root for the full architecture overview
