# Validator Configuration

Complete reference for all CLI arguments and environment variables used by the Minotaur validator (`python -m minotaur_subnet.validator.main`).

All settings can be provided as CLI arguments, environment variables, or a combination of both. CLI arguments take precedence over environment variables.

## CLI Arguments

| Argument | Default | Description |
|----------|---------|-------------|
| `--port` | `9100` | HTTP listen port for the validator API |
| `--epoch-seconds` | `60` | Epoch duration in seconds for weight emission |
| `--store-path` | `None` | Path to the `store.json` persistence file. If omitted, uses in-memory store. |
| `--tick-interval` | `12.0` | BlockLoop tick interval in seconds (matches Ethereum block time) |
| `--subtensor-url` | `None` | Subtensor WebSocket URL (e.g., `wss://entrypoint-finney.opentensor.ai:443`) |
| `--netuid` | `112` | Bittensor subnet UID |
| `--wallet-name` | `None` | Bittensor wallet name |
| `--hotkey-name` | `None` | Bittensor hotkey name |
| `--validator-key` | `""` | EVM private key (hex) for EIP-712 consensus signing |
| `--validator-peers` | `None` | Peer validators in `addr@url` format (space-separated) |
| `--quorum-bps` | `10000` | Quorum threshold in basis points (10000 = 100%) |

## Environment Variables

### Bittensor Identity

| Variable | Default | Description |
|----------|---------|-------------|
| `NETUID` | `112` | Subnet UID. Set to `1` for local testnet. |
| `WALLET_NAME` | -- | Bittensor wallet name (same as `--wallet-name`) |
| `HOTKEY_NAME` | -- | Bittensor hotkey name (same as `--hotkey-name`) |
| `SUBTENSOR_URL` | -- | Subtensor WebSocket endpoint (same as `--subtensor-url`) |

### Simulation (Anvil)

| Variable | Default | Description |
|----------|---------|-------------|
| `ANVIL_RPC_URL` | -- | URL of the Ethereum Anvil fork the validator should connect to for plan simulation. The validator does **not** spawn Anvil — start it separately (see the Validator Quickstart, Step 5). Usually `http://localhost:8545` when Anvil runs on the same host. |
| `BASE_RPC_URL` | -- | URL of the Base Anvil fork the validator should connect to (chain ID 8453). Started separately, same pattern as `ANVIL_RPC_URL`. |
| `BITTENSOR_EVM_RPC_URL` | -- | URL of the BT EVM Anvil fork the validator should connect to (chain ID 964). Started separately. |
| `ETH_UPSTREAM_RPC_URL` | -- | Upstream Ethereum RPC (e.g. Alchemy/Infura) that the validator uses to advance the local Anvil fork to current head between simulations. Without it, the fork stays frozen at startup. |
| `BASE_UPSTREAM_RPC_URL` | -- | Upstream Base RPC, same role as `ETH_UPSTREAM_RPC_URL` for the Base fork. |
| `BITTENSOR_EVM_UPSTREAM_RPC_URL` | -- | Upstream BT EVM RPC (typically `https://lite.chain.opentensor.ai`), same role for the BT EVM fork. |

### Consensus and Signing

| Variable | Default | Description |
|----------|---------|-------------|
| `VALIDATOR_PRIVATE_KEY` | `""` | EVM private key (hex, with `0x` prefix) for EIP-712 consensus signing (same as `--validator-key`) |
| `VALIDATOR_PEERS` | `""` | Comma-separated list of peer validators in `addr@url` format (same as `--validator-peers`) |
| `VALIDATOR_REGISTRY_ADDRESS` | -- | Address of the on-chain `ValidatorRegistry` (same as `--validator-registry-address`). Holds the canonical `quorumBps`; the daemon reads it at startup and refreshes once per epoch. See [Quorum management](../operator/quorum-management.md) for how to change it. |
| `QUORUM_BPS_OVERRIDE` | -- | Emergency / local-testnet escape hatch: forces a local quorum value and skips the on-chain read. Production deployments should leave this unset so `ValidatorRegistry.quorumBps()` stays authoritative. |

### Leader Election

| Variable | Default | Description |
|----------|---------|-------------|
| `FORCE_LEADER` | `""` | Set to `"1"` to force this validator to act as the leader, bypassing stake-based election. Useful for local testnet. |

### Chain Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `CHAIN_ID` | `1` | Default EVM chain ID. Set to `31337` for local Anvil testnet, `1` for Ethereum mainnet. |

### Logging

| Variable | Default | Description |
|----------|---------|-------------|
| `LOG_LEVEL` | `INFO` | Log level: `DEBUG`, `INFO`, `WARNING`, `ERROR` |

## Docker Configuration (Local Testnet)

When running in the local testnet via Docker Compose, the validator is configured as follows:

```yaml
validator:
  command: >-
    python -m minotaur_subnet.validator.main
    --port 9100
    --store-path /data/store.json
  environment:
    ANVIL_RPC_URL: http://anvil:8545
    BASE_RPC_URL: http://anvil-base:8546
    SUBTENSOR_URL: ws://subtensor:9944
    NETUID: "1"
    WALLET_NAME: validator
    HOTKEY_NAME: default
    VALIDATOR_PRIVATE_KEY: "${VALIDATOR_KEY_0}"   # see platform/local_testnet/.env.example
    QUORUM_BPS: "10000"
    CHAIN_ID: "31337"
    FORCE_LEADER: "1"
  volumes:
    - testnet-config:/config:ro
    - store-data:/data
    - ~/.bittensor/wallets:/root/.bittensor/wallets:ro
```

Key points:

- `FORCE_LEADER=1` makes the validator act as leader immediately (no stake-based election on local testnet).
- `CHAIN_ID=31337` is the Anvil local chain ID.
- `NETUID=1` is the local subnet (not mainnet's 112).
- The store volume (`store-data`) is shared between the API and validator containers.
- Wallet directory is mounted read-only from the host.

## Example: Production .env

```bash
# Bittensor
NETUID=112
WALLET_NAME=my-validator
HOTKEY_NAME=my-hotkey
SUBTENSOR_URL=wss://entrypoint-finney.opentensor.ai:443

# Simulation
ANVIL_RPC_URL=https://eth-mainnet.g.alchemy.com/v2/YOUR_ALCHEMY_KEY
BASE_RPC_URL=https://base-mainnet.g.alchemy.com/v2/YOUR_ALCHEMY_KEY

# Consensus
VALIDATOR_PRIVATE_KEY=0xYOUR_EVM_PRIVATE_KEY
VALIDATOR_PEERS=0xPeer1Addr@http://peer1:9100,0xPeer2Addr@http://peer2:9100
QUORUM_BPS=10000

# Chain
CHAIN_ID=1

# Logging
LOG_LEVEL=INFO
```

## Precedence Rules

1. CLI arguments take precedence over environment variables.
2. For `--netuid`, the CLI value is used only if it differs from the default (112); otherwise the `NETUID` environment variable is checked.
3. For `--quorum-bps`, the same logic applies (default 10000 defers to `QUORUM_BPS` env var).
4. `--validator-peers` on the CLI is space-separated; `VALIDATOR_PEERS` as an env var is comma-separated.

See also: [Quickstart](./quickstart.md), [Troubleshooting](./troubleshooting.md).
