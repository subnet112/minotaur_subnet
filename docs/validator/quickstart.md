# Validator Quickstart

This guide walks you through setting up and running a Minotaur validator on Bittensor Subnet 112.

## Prerequisites

- **Python 3.12+**
- **Node.js 20.x** (for the JS scoring engine)
- **Foundry** (anvil, forge, cast) -- install via `curl -L https://foundry.paradigm.xyz | bash && foundryup`
- **Bittensor CLI** (`btcli`) with a registered wallet on subnet 112
- **Ethereum RPC URL** from Alchemy or Infura (for Anvil mainnet fork simulation)
- **EVM private key** for EIP-712 consensus signing (a fresh key is fine -- it does not hold funds)

## Step 1: Clone and Install

```bash
# Clone the repository
git clone https://github.com/subnet112/minotaur_subnet.git
cd minotaur_subnet

# Create and activate a virtual environment
python3.12 -m venv .venv
source .venv/bin/activate

# Install Python dependencies
pip install -r requirements.txt
```

## Step 2: Install Foundry

If you do not already have Foundry installed:

```bash
curl -L https://foundry.paradigm.xyz | bash
foundryup
```

Verify installation:

```bash
anvil --version
forge --version
cast --version
```

## Step 3: Register on Subnet 112

If your hotkey is not yet registered:

```bash
btcli subnet register --netuid 112 --subtensor.network finney \
  --wallet.name my-validator --wallet.hotkey my-hotkey
```

Verify registration:

```bash
btcli subnet metagraph --netuid 112 --subtensor.network finney
```

Your hotkey should appear in the metagraph. Ensure you have sufficient TAO staked to participate in leader election.

## Step 4: Configure Environment

Export the required environment variables:

```bash
# Bittensor identity
export WALLET_NAME=my-validator
export HOTKEY_NAME=my-hotkey
export NETUID=112
export SUBTENSOR_URL=wss://entrypoint-finney.opentensor.ai:443

# Simulation (Anvil fork)
export ANVIL_RPC_URL=https://eth-mainnet.g.alchemy.com/v2/YOUR_ALCHEMY_KEY

# Consensus signing (EVM private key, hex-encoded with 0x prefix)
export VALIDATOR_PRIVATE_KEY=0xYOUR_EVM_PRIVATE_KEY

# Optional: Base chain for multi-chain support
export BASE_RPC_URL=https://base-mainnet.g.alchemy.com/v2/YOUR_ALCHEMY_KEY
```

See [Configuration](./configuration.md) for the full list of options.

## Step 5: Run the Validator

### Standalone Mode (Production)

```bash
python -m minotaur_subnet.validator.main \
  --port 9100 \
  --netuid 112 \
  --wallet-name "$WALLET_NAME" \
  --hotkey-name "$HOTKEY_NAME" \
  --subtensor-url "$SUBTENSOR_URL" \
  --validator-key "$VALIDATOR_PRIVATE_KEY" \
  --tick-interval 12.0
```

The validator will:

1. Load app definitions from the store.
2. Start the BlockLoop (processing orders every ~12 seconds).
3. Listen on port 9100 for validator/consensus and execution endpoints.
4. Emit weights once per epoch (default: 60 seconds).

### Verify It Is Running

```bash
# Health check
curl http://localhost:9100/health

# Block loop status
curl http://localhost:9100/blockloop/status

# Leader info
curl http://localhost:9100/leader

# Current weight/champion view
curl http://localhost:9100/weights
```

## Local Testnet (Development)

For development and testing, use the full Docker Compose stack which runs subtensor, Anvil forks, API, validator, relayer, and frontend together. Run the miner agent on host (`make miner-agent`):

### Prerequisites

- Docker and Docker Compose
- An Alchemy API key (for Anvil mainnet fork)

### Setup

```bash
cd platform/local_testnet

# Create .env from the example
cp .env.example .env
# Edit .env and set ALCHEMY_RPC_URL and BASE_ALCHEMY_RPC_URL

# Start the full stack
make testnet-up
```

### Local Testnet Services

| Service | Port | URL |
|---------|------|-----|
| API | 8080 | http://localhost:8080 |
| Validator | 9100 | (internal, via Docker network) |
| Relayer | 8091 | http://localhost:8091 |
| Anvil (ETH fork) | 8545 | http://localhost:8545 |
| Anvil (Base fork) | 8546 | http://localhost:8546 |
| Subtensor | 9944 | ws://localhost:9944 |

The init container automatically registers the subnet (netuid=1 on local), registers validator and miner neurons, and deploys contracts. The validator starts with `FORCE_LEADER=1` so it immediately begins processing orders.

### Stop the Testnet

```bash
make testnet-down
```

## Running Tests

```bash
# Quick: unit + app tests (no Docker/Anvil needed)
make test

# Full suite including emulation and E2E
make test-all

# Live local_testnet smoke suite (recreates the Docker stack first)
make test-testnet

# Just E2E tests (requires Foundry/Anvil)
make test-e2e

# Mainnet-fork-only E2E tests (requires ALCHEMY_API_KEY or ETHEREUM_RPC_URL)
make test-fork
```

`make test-all` does not include `make test-testnet`; keep the latter as a
separate live-stack check when you change API, deployment, wallet, quoting, or
order execution flows.

See the [Makefile](../../Makefile) for all available test targets.

## Next Steps

- Review [Configuration](./configuration.md) for all available options.
- See [Troubleshooting](./troubleshooting.md) if you encounter issues.
- Check the [Solver Guide](../solver/solver_guide.md) to understand what miners submit.
