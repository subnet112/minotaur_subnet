#!/usr/bin/env bash
# Run the full E2E test suite with Docker Compose for Anvil.
#
# Usage:
#   bash tests/e2e/run_e2e.sh
#
# This script:
#   1. Starts Anvil via Docker Compose
#   2. Waits for health check
#   3. Runs pytest E2E tests
#   4. Tears down Anvil
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
COMPOSE_FILE="$SCRIPT_DIR/docker-compose.yml"

cleanup() {
    echo "==> Stopping Anvil..."
    docker compose -f "$COMPOSE_FILE" down --remove-orphans 2>/dev/null || true
}
trap cleanup EXIT

echo "==> Starting Anvil via Docker Compose..."
docker compose -f "$COMPOSE_FILE" up -d

echo "==> Waiting for Anvil to be ready..."
for i in $(seq 1 30); do
    if cast block-number --rpc-url http://127.0.0.1:8545 >/dev/null 2>&1; then
        echo "    Anvil ready (attempt $i)"
        break
    fi
    if [ "$i" = "30" ]; then
        echo "ERROR: Anvil did not start within 30 attempts"
        exit 1
    fi
    sleep 1
done

echo "==> Running E2E tests..."
cd "$REPO_ROOT"
python3 -m pytest tests/e2e/ -v --tb=short
exit_code=$?

echo "==> Done (exit code: $exit_code)"
exit $exit_code
