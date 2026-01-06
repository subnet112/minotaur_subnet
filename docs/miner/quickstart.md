# Miner Quickstart

This guide gets you running the miner quickly with Python.

## Prerequisites
- Python 3.12+
- **Bittensor Mode:** A wallet with a hotkey registered on the target subnet
- **Both Modes:** Access to Ethereum RPC (Infura, Alchemy, or local node) for price quotes
- **Both Modes:** `MINER_API_KEY` (required for `/v1/solvers/*` endpoints)

**Important:** The solver always queries Uniswap V2/V3 on-chain for price quotes, so you **must** configure an Ethereum RPC URL to avoid rate limiting errors.

## Setup
```bash
# Clone and enter the repo
git clone <repo>
cd minotaur

# Create and activate a virtual environment
python3 -m venv venv && source venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Configure Ethereum RPC (required)
export ETHEREUM_RPC_URL=https://mainnet.infura.io/v3/YOUR_INFURA_KEY
# Or use Alchemy:
export ALCHEMY_API_KEY=your-alchemy-api-key
```

## Run the miner

### Simulation Mode (Testing)
```bash
# Run miner in simulation mode (generates hotkey from miner_id)
export MINER_MODE=simulation
export MINER_ID=my-test-miner
export AGGREGATOR_URL=http://localhost:4000
export MINER_API_KEY=your-miner-api-key  # Required for /v1/solvers/* endpoints
export MINER_BASE_PORT=8000
# If aggregator is in Docker, set solver host (auto-detected on Mac/Windows)
export MINER_SOLVER_HOST=host.docker.internal  # or your host IP on Linux

python -m neurons.miner \
  --miner.mode simulation \
  --miner.id my-test-miner \
  --aggregator.url http://localhost:4000 \
  --miner.api_key your-miner-api-key \
  --miner.base_port 8000 \
  # Required if aggregator is in Docker:
  --miner.solver_host host.docker.internal \
  --miner.num_solvers 2  # Run 2 solvers (default: 1)
```

### Bittensor Mode (Production)
```bash
# Run miner in bittensor mode (uses configured wallet)
export MINER_MODE=bittensor
export WALLET_NAME=my-miner
export WALLET_HOTKEY=my-hotkey
export AGGREGATOR_URL=http://your-aggregator:4000
export MINER_API_KEY=your-miner-api-key  # Required for /v1/solvers/* endpoints

python -m neurons.miner \
  --miner.mode bittensor \
  --wallet.name my-miner \
  --wallet.hotkey my-hotkey \
  --aggregator.url http://your-aggregator:4000 \
  --miner.api_key your-miner-api-key
```

## Multiple Solvers

You can run multiple solvers from a single miner instance using the `--miner.num_solvers` flag:

```bash
python -m neurons.miner \
  --miner.mode simulation \
  --miner.id my-test-miner \
  --aggregator.url http://localhost:4000 \
  --miner.api_key your-miner-api-key \
  --miner.base_port 8000 \
  --miner.num_solvers 2  # Run 2 solvers on ports 8000 and 8001
```

Each solver will be registered independently with the aggregator. When an order comes in, the aggregator will send quote requests to all registered solvers (from all miners), and select the best quote.

**Note:** If you run 2 separate miner processes (with different `--miner.id` values), each miner will register its own solver(s), and the aggregator will receive quotes from all of them.

## Solver Types

Choose the solver type based on your needs:

```bash
# Uniswap V3 on mainnet (default)
--miner.solver_type v3

# Uniswap V2 on mainnet
--miner.solver_type v2

# Uniswap V3 on Base
--miner.solver_type base
```

Available options: `v2`, `v3`, `uniswap-v2`, `uniswap-v3`, `base`, `base-v3`, `uniswap-v3-base`

See also: [Configuration](./configuration.md), [Solver API](./solver-api.md), [Troubleshooting](./troubleshooting.md).

