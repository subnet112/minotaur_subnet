# Validator Quickstart

This guide gets you running the validator quickly with Python and optionally Docker.

## Prerequisites
- Python 3.12+
- Docker (for order simulation)
- **Bittensor Mode:** A wallet with a hotkey registered on the target subnet
- **Both Modes:** Access to Ethereum RPC (Infura, Alchemy, or local node) for order simulation
- **Both Modes:** `VALIDATOR_API_KEY` (required for validator endpoints)

## Setup
```bash
# Clone and enter the repo
git clone <repo>
cd minotaur

# Create and activate a virtual environment
python3 -m venv venv && source venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Minimal runtime configuration
export NETUID=2
export WALLET_NAME=my-validator
export WALLET_HOTKEY=my-hotkey
export AGGREGATOR_URL=http://127.0.0.1:4100
export VALIDATOR_API_KEY=your-validator-api-key  # Required
export SIMULATOR_RPC_URL=https://mainnet.infura.io/v3/YOUR_INFURA_KEY  # Required
export SIMULATOR_DOCKER_IMAGE=ghcr.io/subnet112/minotaur_contracts/mino-simulation:latest
```

## Run the validator

### Bittensor Mode (Production)
```bash
python -m neurons.validator \
  --validator.mode bittensor \
  --wallet.name "$WALLET_NAME" \
  --wallet.hotkey "$WALLET_HOTKEY" \
  --netuid "$NETUID" \
  --subtensor.network finney \
  --subtensor.chain_endpoint wss://entrypoint-finney.opentensor.ai:443 \
  --validator.api_key "$VALIDATOR_API_KEY" \
  --simulator.rpc_url "$SIMULATOR_RPC_URL"
```

### Simulation Mode (Testing - No Bittensor)
```bash
python -m neurons.validator \
  --validator.mode mock \
  --aggregator.url "$AGGREGATOR_URL" \
  --validator.api_key "$VALIDATOR_API_KEY" \
  --simulator.rpc_url "$SIMULATOR_RPC_URL"
```

### With Auto-Update (Recommended for Production)

We highly recommend running the validator with auto-updates. This will help ensure your validator is always running the latest release.

Prerequisites:
1. Install [PM2](https://pm2.keymetrics.io/): `npm install -g pm2`
2. Make sure your virtual environment is activated (the auto-updater will automatically update package dependencies with pip)
3. Make sure you're on the main branch: `git checkout main`

From the minotaur directory:
```bash
pm2 start --name minotaur-vali-updater --interpreter python scripts/start_validator.py -- \
  --pm2_name minotaur-vali \
  --wallet.name "$WALLET_NAME" \
  --wallet.hotkey "$WALLET_HOTKEY" \
  --netuid "$NETUID" \
  --subtensor.network finney \
  --validator.api_key "$VALIDATOR_API_KEY" \
  --simulator.rpc_url "$SIMULATOR_RPC_URL"
```

This starts a process called `minotaur-vali-updater` which periodically checks for new git commits. When one is found, it runs `pip install` for the latest packages and restarts the validator process (whose name is given by the `--pm2_name` flag).

Options:
- `--pm2_name` - Validator PM2 process name (default: `minotaur_vali`)
- `--no_autoupdate` - Disable automatic updates
- `--update_interval` - Update check interval in minutes (default: 15)

To view logs:
```bash
pm2 logs minotaur-vali-updater  # Auto-updater logs
pm2 logs minotaur-vali          # Validator logs
```

## Run tests
```bash
pytest tests/ -v
```

If you prefer a script, see `quick-start.sh` at the repo root.

See also: [Configuration](./configuration.md), [Troubleshooting](./troubleshooting.md).

