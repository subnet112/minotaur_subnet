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
- `VALIDATION_MIN_PRICE`, `VALIDATION_MAX_PRICE` (default: `0.0` = disabled)
- `VALIDATION_MIN_SIZE`, `VALIDATION_MAX_SIZE` (default: `0.0` = disabled)

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
- `SIMULATOR_RPC_URL` (required) – Ethereum RPC URL for order simulation
- `SIMULATOR_DOCKER_IMAGE` (default: `mino-simulation`) – Docker image for simulator
- `SIMULATOR_MAX_CONCURRENT` (default: `5`) – Maximum number of concurrent simulations
- `SIMULATOR_TIMEOUT_SECONDS` (default: `300`) – Simulation timeout in seconds (5 minutes)

## Validator identity
- `VALIDATOR_ID` (optional) – Unique validator ID for order filtering (defaults to hotkey)

## Epoch mode (optional)
- `VALIDATOR_EPOCH_MINUTES` (optional) – Run in epoch mode with specified epoch length in minutes
- `VALIDATOR_CONTINUOUS` (default: `true`) – Enable continuous epoch-based validation

## Logging (optional)
- `LOGURU_LEVEL` = `DEBUG` | `INFO` | `WARNING` | `ERROR`

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

