#!/bin/bash
# Build and optionally push the solver-base Docker image.
#
# Usage:
#   ./build-solver-base.sh          # Build only
#   ./build-solver-base.sh --push   # Build and push to GHCR

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
IMAGE="ghcr.io/subnet112/solver-base:v1"

echo "Building solver-base image..."
docker build \
    -f "$SCRIPT_DIR/Dockerfile.solver-base" \
    -t "$IMAGE" \
    "$REPO_ROOT"

echo "Built: $IMAGE"

if [[ "${1:-}" == "--push" ]]; then
    echo "Pushing to GHCR..."
    docker push "$IMAGE"
    echo "Pushed: $IMAGE"
fi
