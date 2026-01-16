# Validator Configuration

This page summarizes all environment variables and recommended defaults for the validator. Use `.env` or export variables in your shell. See `env.example` for a copy-paste template.

## Aggregator API
- `AGGREGATOR_URL` (e.g., `http://127.0.0.1:4100`) – Base URL for the aggregator API
- `VALIDATOR_API_KEY` (required) – Validator-specific API key for validator endpoints (`/v1/validators/orders`, `/v1/validators/validate`, `/health`)
- `AGGREGATOR_TIMEOUT` (default: `10`) – HTTP timeout (seconds)
- `AGGREGATOR_VERIFY_SSL` (default: `1`) – Validate TLS certs (1=yes, 0=no)
- `AGGREGATOR_MAX_RETRIES` (default: `3`) – Retry budget
- `AGGREGATOR_BACKOFF_SECONDS` (default: `0.5`) – Retry backoff multiplier
- `AGGREGATOR_PAGE_LIMIT` (default: `500`) – Pagination size cap per request

## Loop & Tempo
- `VALIDATOR_POLL_SECONDS` (default: `12`) – Polling cadence for new epochs
- `VALIDATOR_FINALIZATION_BUFFER_BLOCKS` (default: `6`) – Blocks to wait after epoch close before processing
- `VALIDATOR_BACKOFF_FACTOR` (default: `2.0`) – Multiplier when the loop hits an error
- `VALIDATOR_BACKOFF_MAX_SECONDS` (default: `120`) – Upper bound for backoff sleep

## Validation bounds (optional)
- `VALIDATION_DEFAULT_TTL_MS` (default: `1000`)
- `VALIDATION_MAX_RESPONSE_LATENCY_MS` (default: `1500`)
- `VALIDATION_MAX_CLOCK_SKEW_SECONDS` (default: `1`)

Notes:
- The validator currently uses these bounds to validate **aggregator event submissions** and to enforce latency/TTL constraints.
- Price/size bound env vars are not wired in the current Python validator implementation.

## Burn allocation (optional)
- `BURN_PERCENTAGE` (default: `0.0`) – Fraction of emissions to allocate to creator hotkey for burning (0.0-1.0)
- `CREATOR_MINER_ID` (optional) – Creator miner ID (SS58) for burn allocation. In Bittensor mode, defaults to UID 0 hotkey. In mock mode, must be provided manually if burn_percentage > 0

## Bittensor / Subtensor
- `NETUID` (required) – Subnet UID
- `SUBTENSOR_NETWORK` = `finney` | `test` | `local`
- `SUBTENSOR_ADDRESS` / `SUBTENSOR_WS` (optional) – Endpoint override
- `WALLET_NAME`, `WALLET_HOTKEY` – Wallet and hotkey names
- `VALIDATOR_WAIT_FINALIZATION` (0/1) – Wait for chain finalization
- `VERSION_KEY` (optional) – Weights version (defaults to on-chain value)
- `VALIDATOR_SET_WEIGHTS_TIMEOUT_SECONDS` (default: `120`)

## Validator mode
- `VALIDATOR_MODE` = `bittensor` | `mock` (default: `bittensor`)
  - `bittensor`: Full validator with real blockchain operations
  - `mock`: Simulation mode with real aggregator but no Bittensor operations

## Order simulation
- `SIMULATOR_RPC_URL` (recommended) – Ethereum RPC URL for order simulation (chain ID 1)
- `ETHEREUM_RPC_URL` / `INFURA_RPC_URL` – Also accepted by the simulator as fallbacks for chain ID 1
- `BASE_RPC_URL` – Base RPC URL for simulating Base orders (chain ID 8453)
- `SIMULATOR_DOCKER_IMAGE` (default: `ghcr.io/subnet112/minotaur_contracts/mino-simulation:latest`) – Docker image for simulator
- `SIMULATOR_MAX_CONCURRENT` (default: `5`) – Maximum number of concurrent simulations
- `SIMULATOR_TIMEOUT_SECONDS` (default: `300`) – Simulation timeout in seconds (5 minutes)
- `SIMULATOR_AUTO_PULL` (default: `true`) – If true, the validator will `docker pull` the simulator image on startup (set `false/0/no` to disable)

## State persistence (optional)
- `VALIDATOR_STATE_DIR` (optional) – Directory where the validator stores persistent state (defaults to the validator run directory)

## Validator identity
- `VALIDATOR_ID` (optional) – Unique validator ID for order filtering (defaults to hotkey)

## Epoch mode (optional)
- `VALIDATOR_EPOCH_MINUTES` (optional) – Run in epoch mode with specified epoch length in minutes
- `VALIDATOR_CONTINUOUS` (default: `true`) – Enable continuous epoch-based validation

## Logging (optional)
- `LOGURU_LEVEL` = `DEBUG` | `INFO` | `WARNING` | `ERROR`

## Advanced Bittensor wallet settings (optional)
These are used by the on-chain weights emitter (`neurons/onchain_emitter.py`) in some deployment setups:
- `VALIDATOR_WALLET_PATH` (or `BT_WALLET_PATH` / `WALLET_PATH`) – Override bittensor wallet directory
- `VALIDATOR_HOTKEY_PASSWORD` (or `WALLET_HOTKEY_PASSWORD` / `HOTKEY_PASSWORD`) – Hotkey password
- `EXCLUDE_QUANTILE` (default: `0`) – Weight filtering quantile used when processing weights for on-chain constraints

## Example (.env)
```bash
NETUID=2
WALLET_NAME=my-validator
WALLET_HOTKEY=my-hotkey
SUBTENSOR_NETWORK=finney

AGGREGATOR_URL=http://127.0.0.1:4100
VALIDATOR_API_KEY=your-validator-api-key  # Required for validator endpoints
AGGREGATOR_TIMEOUT=10
AGGREGATOR_VERIFY_SSL=1
AGGREGATOR_MAX_RETRIES=3
AGGREGATOR_BACKOFF_SECONDS=0.5

VALIDATOR_POLL_SECONDS=12
VALIDATOR_FINALIZATION_BUFFER_BLOCKS=6
VALIDATOR_BACKOFF_FACTOR=2.0
VALIDATOR_BACKOFF_MAX_SECONDS=120

SIMULATOR_RPC_URL=https://mainnet.infura.io/v3/YOUR_INFURA_KEY
SIMULATOR_MAX_CONCURRENT=5
SIMULATOR_TIMEOUT_SECONDS=300

VALIDATOR_MODE=bittensor
VALIDATOR_CONTINUOUS=true
```

See also: [Quickstart](./quickstart.md), [Troubleshooting](./troubleshooting.md).

