#!/usr/bin/env sh
set -eu

print_help() {
  cat <<'EOF'
Minotaur container entrypoint

Usage:
  docker run ... <image> miner [extra args...]
  docker run ... <image> validator [extra args...]

Or use MODE env var:
  docker run -e MODE=miner ... <image> [extra args...]
  docker run -e MODE=validator ... <image> [extra args...]

Examples:
  docker run --rm --network host \
    -e MINER_MODE=simulation -e MINER_ID=my-miner -e AGGREGATOR_URL=http://127.0.0.1:4000 -e MINER_API_KEY=... \
    -e ETHEREUM_RPC_URL=... \
    <image> miner --miner.num_solvers 1 --miner.base_port 8000

  docker run --rm --network host \
    -e VALIDATOR_MODE=mock -e AGGREGATOR_URL=http://127.0.0.1:4100 -e VALIDATOR_API_KEY=... \
    -e SIMULATOR_RPC_URL=... \
    -e SIMULATOR_DOCKER_IMAGE=mino-simulation \
    -v /var/run/docker.sock:/var/run/docker.sock \
    <image> validator --validator.mode mock --simulator.rpc_url "$SIMULATOR_RPC_URL"
EOF
}

MODE="${MODE:-}"
CMD="${1:-}"

if [ -z "$MODE" ]; then
  MODE="$CMD"
fi

case "${MODE:-}" in
  miner)
    shift || true
    exec python -m neurons.miner "$@"
    ;;
  validator|vali)
    shift || true
    exec python -m neurons.validator "$@"
    ;;
  help|-h|--help|"")
    print_help
    exit 0
    ;;
  *)
    echo "Unknown mode: ${MODE}" 1>&2
    echo "Use: miner | validator" 1>&2
    echo "" 1>&2
    print_help 1>&2 || true
    exit 2
    ;;
esac




