# Miner Configuration

This page summarizes all environment variables and command-line arguments for the miner. Use `.env` or export variables in your shell. See `env.example` for a copy-paste template.

## Miner Mode
- `MINER_MODE` = `simulation` | `bittensor` (default: `simulation`)
  - `simulation`: Testing mode (generates hotkey from miner_id)
  - `bittensor`: Production mode (uses configured wallet)

## Miner Identity
- `MINER_ID` (required in simulation mode) – Miner identifier
  - In simulation mode: Used to generate a unique hotkey
  - In bittensor mode: Defaults to wallet name if not provided

## Aggregator API
- `AGGREGATOR_URL` (default: `http://localhost:4000`) – Base URL for the aggregator API
- `AGGREGATOR_API_KEY` (optional) – General aggregator API key
- `MINER_API_KEY` (required) – Miner-specific API key for `/v1/solvers/*` endpoints
- `AGGREGATOR_TIMEOUT` (default: `10`) – HTTP timeout (seconds)
- `AGGREGATOR_VERIFY_SSL` (default: `1`) – Validate TLS certs (1=yes, 0=no)
- `AGGREGATOR_MAX_RETRIES` (default: `3`) – Retry budget
- `AGGREGATOR_BACKOFF_SECONDS` (default: `0.5`) – Retry backoff multiplier

## Solver Configuration
- `MINER_BASE_PORT` (default: `8000`) – Base port for solver servers (each solver uses `base_port + index`)
- `MINER_SOLVER_HOST` (default: `localhost`) – Host address for solver endpoint
  - Use `host.docker.internal` or host IP if aggregator is in Docker
  - Auto-detected on Mac/Windows
- `MINER_NUM_SOLVERS` (default: `1`) – Number of solvers to run
- `MINER_SOLVER_TYPE` (default: `v3`) – Solver type
  - Options: `v2`, `v3`, `uniswap-v2`, `uniswap-v3`, `base`, `base-v3`, `uniswap-v3-base`
  - `v2`/`uniswap-v2`: Uniswap V2 on Ethereum mainnet
  - `v3`/`uniswap-v3`: Uniswap V3 on Ethereum mainnet (default)
  - `base`/`base-v3`/`uniswap-v3-base`: Uniswap V3 on Base

## Ethereum RPC (Required)
The solver queries Uniswap on-chain for price quotes, so you **must** configure an Ethereum RPC URL:

- `ETHEREUM_RPC_URL` – Ethereum RPC URL (Infura, Alchemy, or local node)
- `ALCHEMY_API_KEY` (alternative) – Alchemy API key (auto-constructs Alchemy URL)
- `BASE_RPC_URL` (optional) – Base chain RPC URL (for Base solvers)

**Note:** Without an RPC URL, the solver will fail to provide quotes and may hit rate limits.

## Bittensor / Subtensor (Bittensor Mode Only)
- `NETUID` (required) – Subnet UID
- `SUBTENSOR_NETWORK` = `finney` | `test` | `local`
- `SUBTENSOR_ADDRESS` / `SUBTENSOR_WS` (optional) – Endpoint override
- `WALLET_NAME`, `WALLET_HOTKEY` – Wallet and hotkey names

## Logging (optional)
- `LOGURU_LEVEL` = `DEBUG` | `INFO` | `WARNING` | `ERROR`

## Example (.env)

### Simulation Mode
```bash
MINER_MODE=simulation
MINER_ID=my-test-miner
AGGREGATOR_URL=http://localhost:4000
MINER_API_KEY=your-miner-api-key  # Required for /v1/solvers/* endpoints
MINER_BASE_PORT=8000
MINER_SOLVER_HOST=localhost  # Use host.docker.internal if aggregator is in Docker
MINER_NUM_SOLVERS=1
MINER_SOLVER_TYPE=v3

ETHEREUM_RPC_URL=https://mainnet.infura.io/v3/YOUR_INFURA_KEY
# Or use Alchemy:
# ALCHEMY_API_KEY=your-alchemy-api-key
```

### Bittensor Mode
```bash
MINER_MODE=bittensor
WALLET_NAME=my-miner
WALLET_HOTKEY=my-hotkey
NETUID=2
SUBTENSOR_NETWORK=finney

AGGREGATOR_URL=http://your-aggregator:4000
MINER_API_KEY=your-miner-api-key  # Required for /v1/solvers/* endpoints
MINER_BASE_PORT=8000
MINER_NUM_SOLVERS=1
MINER_SOLVER_TYPE=v3

ETHEREUM_RPC_URL=https://mainnet.infura.io/v3/YOUR_INFURA_KEY
```

## Command-Line Arguments

All environment variables can also be passed as command-line arguments:

```bash
python -m neurons.miner \
  --miner.mode simulation \
  --miner.id my-test-miner \
  --aggregator.url http://localhost:4000 \
  --miner.api_key your-miner-api-key \
  --miner.base_port 8000 \
  --miner.solver_host localhost \
  --miner.num_solvers 2 \
  --miner.solver_type v3
```

Command-line arguments take precedence over environment variables.

See also: [Quickstart](./quickstart.md), [Solver API](./solver-api.md), [Troubleshooting](./troubleshooting.md).

