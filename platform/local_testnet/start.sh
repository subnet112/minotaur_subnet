#!/bin/bash
set -e
cd "$(dirname "$0")"

if [ ! -f .env ]; then
    echo "Error: .env file not found."
    echo "Copy .env.example to .env and set ALCHEMY_RPC_URL:"
    echo "  cp .env.example .env"
    exit 1
fi

# Validate required env vars
source .env
if [ -z "$ALCHEMY_RPC_URL" ] || [ "$ALCHEMY_RPC_URL" = "https://eth-mainnet.g.alchemy.com/v2/YOUR_KEY" ]; then
    echo "Error: Set ALCHEMY_RPC_URL in .env to your Alchemy/Infura URL"
    exit 1
fi
if [ -z "$BASE_ALCHEMY_RPC_URL" ] || [ "$BASE_ALCHEMY_RPC_URL" = "https://base-mainnet.g.alchemy.com/v2/YOUR_KEY" ]; then
    echo "Error: Set BASE_ALCHEMY_RPC_URL in .env to your Alchemy Base URL"
    exit 1
fi

LOCAL_TESTNET_SUBMISSION_HOST_ROOT="${LOCAL_TESTNET_SUBMISSION_HOST_ROOT:-/tmp/minotaur-testnet-submissions}"
mkdir -p "$LOCAL_TESTNET_SUBMISSION_HOST_ROOT"
export LOCAL_TESTNET_SUBMISSION_HOST_ROOT

echo "Starting Minotaur local testnet..."
services=(
    subtensor
    anvil
    anvil-base
    lit-bridge
    init
    api
    api-peer-1
    api-peer-2
    relayer
    validator
    validator-peer-1
    validator-peer-2
)

if [ "${MINOTAUR_TESTNET_ENABLE_SEED:-1}" = "1" ]; then
    services+=(seed)
else
    docker compose rm -sf seed >/dev/null 2>&1 || true
fi

docker compose up --build -d "${services[@]}"

echo ""
echo "Waiting for init to complete..."
if [ "${MINOTAUR_TESTNET_NONINTERACTIVE:-0}" = "1" ]; then
    init_id="$(docker compose ps -aq init)"
    if [ -z "$init_id" ]; then
        echo "Error: init container was not created."
        exit 1
    fi

    while true; do
        status="$(docker inspect -f '{{.State.Status}} {{.State.ExitCode}}' "$init_id" 2>/dev/null || true)"
        case "$status" in
            "exited 0")
                docker compose logs init
                break
                ;;
            exited\ *)
                docker compose logs init
                echo "Error: init container failed."
                exit 1
                ;;
        esac
        sleep 2
    done
else
    docker compose logs -f init
fi

echo ""
echo "Local testnet ready!"
echo "  Frontend:     http://localhost:${HOST_FRONTEND_PORT:-4000}"
echo "  API:          http://localhost:${HOST_API_PORT:-8080}"
echo "  API Peer 2:   http://localhost:${HOST_API_PEER_1_PORT:-8081}"
echo "  API Peer 3:   http://localhost:${HOST_API_PEER_2_PORT:-8082}"
echo "  Relayer:      http://localhost:${HOST_RELAYER_PORT:-8091}"
echo "  Validator:    http://localhost:${HOST_VALIDATOR_PORT:-9100}"
echo "  Validator 2:  http://localhost:${HOST_VALIDATOR_PEER_1_PORT:-9101}"
echo "  Validator 3:  http://localhost:${HOST_VALIDATOR_PEER_2_PORT:-9102}"
echo "  Anvil (ETH):  http://localhost:${HOST_ANVIL_ETH_PORT:-8545}"
echo "  Anvil (Base): http://localhost:${HOST_ANVIL_BASE_PORT:-8546}"
echo "  Subtensor:    ws://localhost:${HOST_SUBTENSOR_WS_PORT:-9944}"
echo "  Lit Bridge:   http://localhost:${HOST_LIT_BRIDGE_PORT:-3100}"
