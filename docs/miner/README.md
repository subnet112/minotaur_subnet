# Miner Overview

This section covers the current miner-facing workflow for Minotaur Subnet 112.

## Contents

- [Quickstart](./quickstart.md) - Run the current CLI flow
- [Configuration](./configuration.md) - Flags, defaults, and environment variables
- [Solver API](./solver-api.md) - `IntentSolver` and `Strategy` interfaces
- [Custom Solver](./custom-solver.md) - Strategy implementation guidance
- [Troubleshooting](./troubleshooting.md) - Common errors and fixes

## How mining works now

Miners compete by improving solver quality, not by running a quote server.

Typical loop:

1. Build/iterate on strategies (often via `RoutingSolver`).
2. Submit candidate solver code.
3. Validator/API benchmark worker scores submissions against active app scenarios.
4. Best scorer can be adopted as champion if it clears the dethrone margin.
5. Champion solver is loaded into block loop execution.

## Submission paths

### 1) Git-based submission (`/v1/submissions`)

- Signed by Bittensor hotkey
- Runs 3-stage screening:
  - static checks
  - Docker build/import
  - smoke test
- Then benchmarked and ranked

### 2) Source-based submission (`/v1/submissions/source`)

- Inline Python source upload
- Skips screening
- Goes directly to benchmarking
- Used by the agent loop for rapid iteration

## Champion/challenger model

- Submissions are benchmarked and ranked by score.
- A challenger must exceed the current champion by `DETHRONE_MARGIN` (currently 5%).
- On adoption, block loop hot-swaps to the new solver.
