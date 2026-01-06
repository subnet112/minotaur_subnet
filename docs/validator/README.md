# Validator Documentation

This section contains all documentation for running the Minotaur validator.

## Contents
- [Quickstart](./quickstart.md) - Get started quickly
- [Configuration](./configuration.md) - Complete configuration reference
- [Troubleshooting](./troubleshooting.md) - Common issues and solutions

## Overview

The Minotaur validator polls the OIF Aggregator for **pending orders**, simulates them using Docker, computes scores locally, and publishes normalized weights on-chain via Subtensor.

### Key Features
- **Order-based validation:** Fetches pending orders and simulates execution
- **Docker-based simulation:** Uses Docker containers for realistic order validation
- **Dual modes:** Bittensor mode (production) and Mock mode (testing)
- **Epoch management:** Optional time-based validation windows
- **Burn allocation:** Supports Bittensor's creator emissions burning mechanism

### Architecture
The validator:
1. Fetches pending orders from the Aggregator `/v1/validators/orders` API
2. Simulates each order using Docker containers to validate execution
3. Submits validation results via `/v1/validators/validate` API
4. Computes per-hotkey metrics and scores based on validation success rates
5. Normalizes scores and applies burn allocation (if configured)
6. Emits weights on-chain when tempo spacing allows

For more details, see the main README in the repo root: `README.md`.

