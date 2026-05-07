#!/usr/bin/env bash
#
# Build and (optionally) push the Minotaur solver-base image.
#
# Usage:
#   ./build.sh                  # build + smoke-test + tag locally
#   ./build.sh --push           # above, then push to REGISTRY
#   ./build.sh --push --tag v3  # override tag (default: v2)
#
# Env:
#   REGISTRY   registry prefix (default: ghcr.io/subnet112)
#   IMAGE_NAME image name (default: solver-base)
#
# The build is already tested by smoke_test.py during `docker build`
# (see the RUN line in Dockerfile), so this script doesn't re-run the
# test. If you pushed a broken image, you broke the Dockerfile.
#
# The Dockerfile's FROM line currently references ghcr.io/subnet112
# (the original upstream base). `docker build` will pull that at build
# time — make sure you have credentials / network for that registry.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REGISTRY="${REGISTRY:-ghcr.io/subnet112}"
IMAGE_NAME="${IMAGE_NAME:-solver-base}"
TAG="v2"
PUSH=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --push) PUSH=1; shift ;;
        --tag) TAG="$2"; shift 2 ;;
        *) echo "Unknown arg: $1" >&2; exit 1 ;;
    esac
done

FULL_TAG="${REGISTRY}/${IMAGE_NAME}:${TAG}"

echo "==> Building ${FULL_TAG}"
docker build -t "${FULL_TAG}" "${HERE}"

# Also tag :latest for operator convenience. Pins in Dockerfiles should
# use ${TAG} (or an @sha256 digest), not :latest.
docker tag "${FULL_TAG}" "${REGISTRY}/${IMAGE_NAME}:latest"

DIGEST="$(docker image inspect --format '{{index .RepoDigests 0}}' "${FULL_TAG}" 2>/dev/null || echo 'local-build')"
echo "==> Built: ${FULL_TAG}"
echo "    Digest: ${DIGEST}"

if [[ "${PUSH}" -eq 1 ]]; then
    echo "==> Pushing ${FULL_TAG}"
    docker push "${FULL_TAG}"
    docker push "${REGISTRY}/${IMAGE_NAME}:latest"
    # Record the pushed digest — callers who pin by digest need this.
    PUSHED_DIGEST="$(docker image inspect --format '{{index .RepoDigests 0}}' "${FULL_TAG}")"
    echo "==> Pushed"
    echo "    Digest (pin this in FROM lines): ${PUSHED_DIGEST}"
fi
