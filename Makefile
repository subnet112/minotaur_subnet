.PHONY: test test-unit test-app test-cross-chain test-emulation test-integration test-forge test-e2e test-fork test-testnet test-all testnet-up testnet-down base-mainnet-up base-mainnet-down demo-preflight demo-up demo-check demo-prep miner-agent solver-base solver-base-push

# Quick: unit + app tests (no Docker, no Anvil, no RPC)
test: test-unit test-app

# Python unit tests (excludes dev-track cross-chain lane by default).
# --ignore entries skip test files broken by the refactor that moved
# solver code to external repos; restore them when those modules return.
test-unit:
	./.venv/bin/pytest tests/unit/ -v --tb=short -m "not cross_chain" \
		--ignore=tests/unit/test_v3_foundations.py \
		--ignore=tests/unit/test_vault_dip_solver.py \
		--ignore=tests/unit/test_agent_loop.py

# App Intents tests (MCP tools, API routes, submission pipeline)
test-app:
	./.venv/bin/pytest minotaur_subnet/ -v --tb=short -m "not cross_chain"

# Dev-track cross-chain lane — keeps bridge/ + multi-leg code compiling even
# while CROSS_CHAIN_ENABLED=0 in prod. Always run in CI on every PR.
# Runs unit + emulation cross-chain tests only; the E2E escrow test
# (tests/e2e/test_cross_chain_escrow.py) runs under the e2e lane when Anvil
# is available.
test-cross-chain:
	CROSS_CHAIN_ENABLED=1 ./.venv/bin/pytest \
		tests/unit/test_cross_chain_solver.py \
		tests/unit/test_cross_chain_primitive.py \
		tests/emulation/test_cross_chain.py \
		-v --tb=short -m cross_chain

# Emulation tests (requires Docker for local subtensor)
test-emulation:
	./.venv/bin/pytest tests/emulation/ -v --tb=short

# Integration tests
test-integration:
	@status=0; ./.venv/bin/pytest tests/integration/ -v --tb=short || status=$$?; \
	if [ "$$status" -eq 5 ]; then \
		echo "No integration tests collected; continuing."; \
	elif [ "$$status" -ne 0 ]; then \
		exit "$$status"; \
	fi

# Solidity tests (requires forge)
test-forge:
	cd contracts && forge test -v

# E2E tests on clean Anvil (no mainnet fork)
test-e2e:
	./.venv/bin/pytest tests/e2e/ -v --tb=short -k "not mainnet_fork and not fork_pipeline"

# Mainnet fork tests (requires ALCHEMY_API_KEY or ETHEREUM_RPC_URL)
test-fork:
	./.venv/bin/pytest tests/e2e/ -v --tb=short -k "mainnet_fork or fork_pipeline"

# Local testnet smoke tests (starts/reuses Docker Compose stack)
test-testnet:
	cd platform/local_testnet && docker compose down -v
	cd platform/local_testnet && MINOTAUR_TESTNET_ENABLE_SEED=0 MINOTAUR_TESTNET_NONINTERACTIVE=1 ./start.sh
	set -a; [ -f platform/local_testnet/.env ] && . platform/local_testnet/.env; set +a; \
	REQUIRE_LOCAL_TESTNET=1 \
	LOCAL_TESTNET_API_URL="http://localhost:$${HOST_API_PORT:-8080}" \
	LOCAL_TESTNET_API_PEER_1_URL="http://localhost:$${HOST_API_PEER_1_PORT:-8081}" \
	LOCAL_TESTNET_API_PEER_2_URL="http://localhost:$${HOST_API_PEER_2_PORT:-8082}" \
	LOCAL_TESTNET_VALIDATOR_URL="http://localhost:$${HOST_VALIDATOR_PORT:-9100}" \
	LOCAL_TESTNET_VALIDATOR_PEER_1_URL="http://localhost:$${HOST_VALIDATOR_PEER_1_PORT:-9101}" \
	LOCAL_TESTNET_VALIDATOR_PEER_2_URL="http://localhost:$${HOST_VALIDATOR_PEER_2_PORT:-9102}" \
	LOCAL_TESTNET_RELAYER_URL="http://localhost:$${HOST_RELAYER_PORT:-8091}" \
	LOCAL_TESTNET_ETH_RPC_URL="http://localhost:$${HOST_ANVIL_ETH_PORT:-8545}" \
	LOCAL_TESTNET_BASE_RPC_URL="http://localhost:$${HOST_ANVIL_BASE_PORT:-8546}" \
	./.venv/bin/pytest tests/testnet/ -v --tb=short

# Everything
test-all: test-unit test-app test-cross-chain test-emulation test-integration test-forge test-e2e test-fork

# ── Local Testnet ────────────────────────────────────────────────────────────

# Start the full local testnet stack (Docker Compose)
testnet-up:
	cd platform/local_testnet && ./start.sh

# Stop and clean up the local testnet
testnet-down:
	cd platform/local_testnet && docker compose down -v

# Start with Base mainnet overlay (real Base execution, Anvil simulation)
base-mainnet-up:
	cd platform/local_testnet && bash start-base-mainnet.sh

# Stop Base mainnet stack
base-mainnet-down:
	cd platform/local_testnet && docker compose -f docker-compose.yml -f docker-compose.base-mainnet.yml --env-file .env --env-file .env.base-mainnet down -v

# Validate host prerequisites for the local demo stack
demo-preflight:
	set -a; [ -f platform/local_testnet/.env ] && . platform/local_testnet/.env; set +a; ./.venv/bin/python platform/local_testnet/demo_preflight.py

# Start the local demo stack without interactive init logs
demo-up: demo-preflight
	cd platform/local_testnet && MINOTAUR_TESTNET_ENABLE_SEED=1 MINOTAUR_TESTNET_NONINTERACTIVE=1 ./start.sh

# Verify the seeded DexAggregatorApp demo end-to-end
demo-check:
	set -a; [ -f platform/local_testnet/.env ] && . platform/local_testnet/.env; set +a; ./.venv/bin/python platform/local_testnet/demo_check.py

# Start the local stack and verify the live demo path
demo-prep: demo-up demo-check

# Run miner agent locally against the testnet (requires Claude CLI + testnet running)
# Continuous miner loop (production — runs forever, iterates automatically)
miner-agent:
	.venv/bin/python -m minotaur_subnet.miner.main agent \
		--validator-url http://localhost:8080 \
		--anvil-rpc-url http://localhost:18545 \
		--strategy-dir strategies/ \
		--model sonnet \
		--claude-timeout 600

# Build the solver-base image locally, smoke-test it, tag as
# ghcr.io/subnet112/solver-base:v2. Does NOT push; use solver-base-push.
solver-base:
	bash platform/base-images/solver-base/build.sh

# Build + push solver-base. Requires `docker login ghcr.io` (a PAT with
# write:packages on subnet112) before running. After push, capture
# the printed sha256 digest and pin it in the upstream solver-repo's
# Dockerfile FROM line (and anywhere else the image is referenced).
solver-base-push:
	bash platform/base-images/solver-base/build.sh --push

# Build the validator image locally from source with the current git SHA
# stamped in, so /health.image_sha shows your commit (not "dev") and fleet
# monitoring can tell you're current. Source-builders should track the
# `main` branch (= the :stable line) and run this, then `docker compose up`.
# Tags as :stable to match the default MINOTAUR_IMAGE_TAG operators run.
build-validator:
	docker build -f minotaur_subnet/Dockerfile \
	  --build-arg MINOTAUR_IMAGE_SHA=$$(git rev-parse --short HEAD) \
	  -t ghcr.io/subnet112/minotaur-validator:stable .

# Single improvement cycle (testnet — run manually, one iteration at a time)
miner-cycle:
	@echo "Running one miner improvement cycle..."
	.venv/bin/python -c "import asyncio, logging; logging.basicConfig(level=logging.INFO, format='%(asctime)s %(name)s %(levelname)s: %(message)s', datefmt='%H:%M:%S'); from minotaur_subnet.miner.agent.loop import AgentLoop; agent = AgentLoop(validator_url='http://localhost:8080', strategy_dir='strategies', miner_id='test-miner-001', anvil_rpc_url='http://localhost:18545', model='sonnet', claude_timeout=600, cooldown=0); agent._load_existing_strategies(); asyncio.run(agent._cycle())"

# ── Quorum management ──────────────────────────────────────────────────
#
# Reconfigure the network-wide quorumBps on a chain by calling
# ValidatorRegistry.setQuorumBps(BPS) as the registry owner. Every
# AppIntentBase on that chain reads from the registry at execution time,
# and off-chain validators pick up the new value on the next ProtocolConfig
# refresh tick (default ~60s).
#
# Required env per chain:
#   <CHAIN>_RPC_URL                  RPC endpoint
#   <CHAIN>_VALIDATOR_REGISTRY       registry contract address
#   REGISTRY_OWNER_PRIVATE_KEY       hex key of the registry owner
# Usage:
#   make set-quorum-base BPS=8000
#   make set-quorum-eth  BPS=6666
#   make set-quorum-btevm BPS=7500

set-quorum-base:
	@test -n "$(BPS)" || (echo "BPS=<basis points> is required (e.g. BPS=6666)"; exit 1)
	cd contracts && QUORUM_BPS=$(BPS) \
	  VALIDATOR_REGISTRY=$$BASE_VALIDATOR_REGISTRY \
	  REGISTRY_OWNER_PRIVATE_KEY=$$REGISTRY_OWNER_PRIVATE_KEY \
	  forge script script/SetQuorum.s.sol:SetQuorum \
	  --rpc-url $$BASE_RPC_URL --broadcast

set-quorum-eth:
	@test -n "$(BPS)" || (echo "BPS=<basis points> is required (e.g. BPS=6666)"; exit 1)
	cd contracts && QUORUM_BPS=$(BPS) \
	  VALIDATOR_REGISTRY=$$ETH_VALIDATOR_REGISTRY \
	  REGISTRY_OWNER_PRIVATE_KEY=$$REGISTRY_OWNER_PRIVATE_KEY \
	  forge script script/SetQuorum.s.sol:SetQuorum \
	  --rpc-url $$ETH_RPC_URL --broadcast

set-quorum-btevm:
	@test -n "$(BPS)" || (echo "BPS=<basis points> is required (e.g. BPS=6666)"; exit 1)
	cd contracts && QUORUM_BPS=$(BPS) \
	  VALIDATOR_REGISTRY=$$BTEVM_VALIDATOR_REGISTRY \
	  REGISTRY_OWNER_PRIVATE_KEY=$$REGISTRY_OWNER_PRIVATE_KEY \
	  forge script script/SetQuorum.s.sol:SetQuorum \
	  --rpc-url $$BITTENSOR_EVM_UPSTREAM_RPC_URL --broadcast

# Read the current quorumBps from a chain's ValidatorRegistry.
get-quorum-base:
	cast call $$BASE_VALIDATOR_REGISTRY 'quorumBps()(uint256)' --rpc-url $$BASE_RPC_URL

get-quorum-eth:
	cast call $$ETH_VALIDATOR_REGISTRY 'quorumBps()(uint256)' --rpc-url $$ETH_RPC_URL

get-quorum-btevm:
	cast call $$BTEVM_VALIDATOR_REGISTRY 'quorumBps()(uint256)' --rpc-url $$BITTENSOR_EVM_UPSTREAM_RPC_URL

# ChampionRegistry on BT EVM has its own independent quorum knob (out of
# scope for the ValidatorRegistry-backed ProtocolConfig refactor). Keep
# in sync with ValidatorRegistry on BT EVM manually for now.
set-champion-quorum:
	@test -n "$(BPS)" || (echo "BPS=<basis points> is required"; exit 1)
	cast send $$CHAMPION_REGISTRY 'setQuorumBps(uint256)' $(BPS) \
	  --rpc-url $$BITTENSOR_EVM_UPSTREAM_RPC_URL \
	  --private-key $$REGISTRY_OWNER_PRIVATE_KEY
