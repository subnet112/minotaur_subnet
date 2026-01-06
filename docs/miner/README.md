# Miner Documentation

This section contains all documentation for running the Minotaur miner.

## Contents
- [Quickstart](./quickstart.md) - Get started quickly
- [Configuration](./configuration.md) - Complete configuration reference
- [Solver API](./solver-api.md) - Solver endpoints and API specification
- [Custom Solver](./custom-solver.md) - Guide for writing your own solver
- [Troubleshooting](./troubleshooting.md) - Common issues and solutions

## Overview

The Minotaur miner manages one or more solver instances and registers them with the aggregator. Solvers provide swap quotes and execute orders using Uniswap V2/V3 integration.

### Key Features
- **Solver Management:** Creates and manages one or more solver instances
- **Multiple Solvers:** Run multiple solvers per miner using `--miner.num_solvers`
- **Solver Types:** Choose solver type with `--miner.solver_type` (v2, v3, base, etc.)
- **Automatic Registration:** Registers all solvers with the aggregator automatically
- **Token Discovery:** Polls each solver for supported tokens before registration
- **OIF v1 Compatible:** Solver implements the OIF v1 API specification
- **Uniswap Integration:** Supports Uniswap V2 (mainnet) and V3 (mainnet/Base) for quotes
- **Dual Modes:** Simulation mode (testing) and Bittensor mode (production)

### Architecture
The miner:
1. Creates one or more solver instances (each runs on a separate port)
2. Each solver implements the OIF v1 API specification
3. Solvers are automatically registered with the aggregator
4. The aggregator sends quote requests to all registered solvers
5. Solvers provide quotes using Uniswap V2/V3 integration
6. When an order is selected, the solver executes the swap

For more details, see the main README in the repo root: `README.md`.

