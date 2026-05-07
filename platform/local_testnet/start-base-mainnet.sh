#!/bin/bash
# ──────────────────────────────────────────────────────────────────────────────
# Start Minotaur with Base Mainnet overlay
#
# Runs the full stack against real Base mainnet for execution,
# while keeping Anvil for simulation (fork of real Base).
#
# Prerequisites:
#   1. cp .env.base-mainnet.example .env.base-mainnet
#   2. Fill in real RPC URL, relayer key, validator keys
#   3. Fund relayer wallet with ~0.01 ETH on Base
# ──────────────────────────────────────────────────────────────────────────────

set -euo pipefail
cd "$(dirname "$0")"

# Verify secrets file exists
if [ ! -f .env.base-mainnet ]; then
  echo "ERROR: .env.base-mainnet not found."
  echo "  cp .env.base-mainnet.example .env.base-mainnet"
  echo "  Then fill in your RPC URL and wallet keys."
  exit 1
fi

# Verify .env (base config for Anvil ETH fork) also exists
if [ ! -f .env ]; then
  echo "ERROR: .env not found. Copy from .env.example first."
  exit 1
fi

echo "Starting Minotaur with Base Mainnet overlay..."
echo "  Execution: Real Base mainnet"
echo "  Simulation: Anvil (forking real Base)"
echo ""

docker compose \
  -f docker-compose.yml \
  -f docker-compose.base-mainnet.yml \
  --env-file .env \
  --env-file .env.base-mainnet \
  up --build -d

echo ""
echo "Services starting. Monitor with:"
echo "  docker compose -f docker-compose.yml -f docker-compose.base-mainnet.yml logs -f api"
