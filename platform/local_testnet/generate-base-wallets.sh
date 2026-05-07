#!/bin/bash
# ──────────────────────────────────────────────────────────────────────────────
# Generate fresh wallets for Base mainnet deployment
#
# Creates 4 wallets (1 relayer + 3 validators) and outputs
# .env.base-mainnet format. Requires `cast` (Foundry).
#
# Usage:
#   bash generate-base-wallets.sh > .env.base-mainnet
#   # Then edit to add your BASE_MAINNET_RPC_URL
# ──────────────────────────────────────────────────────────────────────────────

set -euo pipefail

if ! command -v cast &>/dev/null; then
  echo "ERROR: 'cast' not found. Install Foundry: https://book.getfoundry.sh/getting-started/installation" >&2
  exit 1
fi

echo "# ── Base Mainnet Configuration ──"
echo "# Generated: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo ""
echo "# Base mainnet RPC (fill in your Alchemy/Infura key)"
echo "BASE_MAINNET_RPC_URL=https://base-mainnet.g.alchemy.com/v2/YOUR_KEY"
echo ""

# Generate relayer wallet
RELAYER_OUTPUT=$(cast wallet new 2>&1)
RELAYER_ADDR=$(echo "$RELAYER_OUTPUT" | grep -i "address" | awk '{print $NF}')
RELAYER_KEY=$(echo "$RELAYER_OUTPUT" | grep -i "private key" | awk '{print $NF}')

echo "# Relayer wallet — fund with ~0.01 ETH on Base for gas"
echo "# Address: $RELAYER_ADDR"
echo "BASE_RELAYER_PRIVATE_KEY=$RELAYER_KEY"
echo ""

# Generate validator wallets
VALIDATOR_ADDRS=()
for i in 0 1 2; do
  OUTPUT=$(cast wallet new 2>&1)
  ADDR=$(echo "$OUTPUT" | grep -i "address" | awk '{print $NF}')
  KEY=$(echo "$OUTPUT" | grep -i "private key" | awk '{print $NF}')
  VALIDATOR_ADDRS+=("$ADDR")
  echo "# Validator $i address: $ADDR"
  echo "BASE_VALIDATOR_KEY_$i=$KEY"
done

echo ""
echo "# Validator addresses (for on-chain ValidatorRegistry)"
echo "BASE_VALIDATOR_ADDRESSES=${VALIDATOR_ADDRS[0]},${VALIDATOR_ADDRS[1]},${VALIDATOR_ADDRS[2]}"
echo ""
echo "# Consensus peer addresses"
echo "BASE_ORDER_CONSENSUS_PEERS=${VALIDATOR_ADDRS[0]}@http://validator:9100,${VALIDATOR_ADDRS[1]}@http://validator-peer-1:9100,${VALIDATOR_ADDRS[2]}@http://validator-peer-2:9100"
echo "BASE_API_CLUSTER_PEERS=${VALIDATOR_ADDRS[1]}@http://api-peer-1:8080,${VALIDATOR_ADDRS[2]}@http://api-peer-2:8080"
echo ""
echo "# Relayer transaction timeout"
echo "RELAYER_RECEIPT_TIMEOUT=60"
echo ""
echo "# Optional: Finney subtensor"
echo "# BASE_SUBTENSOR_URL=wss://entrypoint-finney.opentensor.ai:443"
echo "# ENABLE_NATIVE_BITTENSOR_PROXY=1"
echo "# NATIVE_BITTENSOR_NETWORK=finney"
