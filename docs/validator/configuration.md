# Validator Configuration

Complete reference for all CLI arguments and environment variables used by the Minotaur validator (`python -m minotaur_subnet.validator.main`).

All settings can be provided as CLI arguments, environment variables, or a combination of both. CLI arguments take precedence over environment variables.

## CLI Arguments

| Argument | Default | Description |
|----------|---------|-------------|
| `--port` | `9100` | HTTP listen port for the validator API |
| `--epoch-seconds` | `60` | Epoch duration in seconds for the local emit clock. **Note:** since PR #524, weight *commit* timing is tempo-aligned (one commit per chain tempo epoch, ~360 blocks), not driven by this wall-clock value — see `TEMPO_ALIGNED_EMIT` under [Weight Emission](#weight-emission). This still governs the fallback wall-clock cadence when tempo state is unqueryable. |
| `--store-path` | `None` | Path to the `store.json` persistence file. If omitted, uses in-memory store. |
| `--tick-interval` | `12.0` | BlockLoop tick interval in seconds (matches Ethereum block time) |
| `--subtensor-url` | `None` | Subtensor WebSocket URL (e.g., `wss://entrypoint-finney.opentensor.ai:443`) |
| `--netuid` | `112` | Bittensor subnet UID |
| `--wallet-name` | `None` | Bittensor wallet name |
| `--hotkey-name` | `None` | Bittensor hotkey name |
| `--validator-key` | `""` | EVM private key (hex) for EIP-712 consensus signing |
| `--validator-registry-address` | `""` | Address of the on-chain `ValidatorRegistry` — the **canonical source of `quorumBps`** (and the authorized validator set). Read at startup and refreshed once per epoch. There is **no `--quorum-bps` flag**; quorum is not set on the CLI. |
| `--leader-api-url` | `None` | Leader API base URL to sync the app catalog from (e.g. `https://api.minotaursubnet.com`). Required for follower validators that don't receive `create_app` / `deploy_app` calls directly. Falls back to `LEADER_API_URL` env. |
| `--app-sync-interval` | `60.0` | Seconds between app catalog sync ticks. |

## Environment Variables

### Bittensor Identity

| Variable | Default | Description |
|----------|---------|-------------|
| `NETUID` | `112` | Subnet UID. Set to `1` for local testnet. |
| `WALLET_NAME` | -- | Bittensor wallet name (same as `--wallet-name`) |
| `HOTKEY_NAME` | -- | Bittensor hotkey name (same as `--hotkey-name`) |
| `BT_WALLET_PATH` | -- | Wallet **root** directory (parent of `<WALLET_NAME>/hotkeys/<HOTKEY_NAME>`). Unset → SDK default `$HOME/.bittensor/wallets` (in-container `/home/minotaur/.bittensor/wallets` for uid 1000). Set this when the wallet is mounted elsewhere, or when the default lookup can't see/read it — a wrong path or a mount **not readable by uid 1000** is the usual cause of `weights_emitter_configured=false` (a silent dead emitter). The hotkey file must be readable by uid 1000. Alias: `WALLET_PATH`. |
| `SUBTENSOR_URL` | -- | Subtensor WebSocket endpoint (same as `--subtensor-url`). Accepts the alias `finney` (= public `wss://entrypoint-finney.opentensor.ai:443`) or any explicit `ws://`/`wss://` URL. If you operate your own subtensor node, point this at it (e.g. `ws://your-subtensor:9944`) to avoid the per-IP rate limits on the public endpoint — see [Run your own subtensor](./quickstart.md#run-your-own-subtensor-recommended-for-datacenter-operators) in the quickstart. |

### Simulation (Anvil)

| Variable | Default | Description |
|----------|---------|-------------|
| `ANVIL_RPC_URL` | -- | URL of the Ethereum Anvil fork the validator should connect to for plan simulation. The validator does **not** spawn Anvil — start it separately (see the Validator Quickstart, Step 5). Usually `http://localhost:8545` when Anvil runs on the same host. |
| `BASE_RPC_URL` | -- | URL of the Base Anvil fork the validator should connect to (chain ID 8453). Started separately, same pattern as `ANVIL_RPC_URL`. |
| `BITTENSOR_EVM_RPC_URL` | -- | URL of the BT EVM Anvil fork the validator should connect to (chain ID 964). Started separately. |
| `ETH_UPSTREAM_RPC_URL` | -- | Upstream Ethereum RPC (e.g. Alchemy/Infura) that the validator uses to advance the local Anvil fork to current head between simulations. Without it, the fork stays frozen at startup. |
| `BASE_UPSTREAM_RPC_URL` | -- | Upstream Base RPC, same role as `ETH_UPSTREAM_RPC_URL` for the Base fork. |
| `BITTENSOR_EVM_UPSTREAM_RPC_URL` | -- | Upstream BT EVM RPC, same role for the BT EVM fork. Defaults to the public `https://lite.chain.opentensor.ai` (rate-limited per source IP). A self-hosted subtensor node serves the EVM JSON-RPC on the same port as its substrate RPC (Frontier is built into the binary); if you set `SUBTENSOR_URL=ws://your-subtensor:9944` you can typically set this to `http://your-subtensor:9944` against the same host. Use `http`/`https` here — Anvil's `--fork-url` does HTTP polling, not WS. |

### Benchmark Performance

| Variable | Default | Description |
|----------|---------|-------------|
| `BENCHMARK_CONCURRENCY` | `1` | Number of isolated solver runtimes to shard each benchmark across (the scenario pool). `1` (default) is the byte-identical sequential path — the **kill-switch**: leave/set at `1` to instantly revert with no code change. K>1 runs scenarios concurrently for roughly K× on the network-latency-bound segment (rounds are ~90% CPU-idle), each runtime fully isolated (own solver container + own block-pin proxy session + own read budget). **Per-validator, NOT consensus** — K is never folded into the benchmark pack hash, so a fleet running mixed K computes identical scores (no coordination needed). Each runtime costs ~1 solver container (≈4 GB / 2 CPU); the practical ceiling is upstream archive-RPC concurrency, not validator RAM. Recommended **2–4**; hard-clamped to `[1, 63]`. Roll out by bumping one validator, confirming byte-identical scores vs a `K=1` peer + faster wall-clock, then the fleet. |
| `RPC_PROXY_UPSTREAM_MAX_CONCURRENCY` | `24` | (block-pin proxy server) Max concurrent upstream connections the proxy opens to the archive RPC. Bounds the read storm at high `BENCHMARK_CONCURRENCY` so the provider isn't rate-limited into per-validator timeouts (a non-determinism source). Tune to your RPC tier; ≈ K × a few reads per scenario. |

### Consensus and Signing

| Variable | Default | Description |
|----------|---------|-------------|
| `VALIDATOR_PRIVATE_KEY` | `""` | EVM private key (hex, with `0x` prefix) for EIP-712 consensus signing (same as `--validator-key`) |
| `VALIDATOR_AXON_URL` | -- | Public URL where this daemon serves the `/identity` endpoint, e.g. `http://your-host:9100`. Used by peer discovery: the daemon signs this URL into its `/identity` attestation so other validators can verify the binding. If unset, `/identity` returns 503 and other validators can't include you in their peer set. |
| `VALIDATOR_REGISTRY_8453` | -- | **Required.** Address of the on-chain `ValidatorRegistry` on Base (chain 8453). Holds the authorized validator EVM list + canonical `quorumBps` for order-consensus; the daemon reads both at startup and refreshes once per epoch. See [Quorum management](../operator/quorum-management.md). |
| `VALIDATOR_REGISTRY_964` | -- | **Required.** Same contract on BT EVM (chain 964). Used by the api service for champion-consensus signer verification. |
| `VALIDATOR_REGISTRY_ADDRESS` | -- | Legacy single-chain form. Deprecated — use the chain-specific `VALIDATOR_REGISTRY_<chain>` variables above. The canonical `.env.example` ships the chain-specific forms with current production addresses pre-filled. |
| `QUORUM_BPS_OVERRIDE` | -- | Emergency / local-testnet escape hatch: forces a local quorum value and skips the on-chain read. Production deployments should leave this unset so `ValidatorRegistry.quorumBps()` stays authoritative. |
| `ORDER_CONSENSUS_PEERS` | `""` | **Internal-only escape hatch.** Pinned-peer list (`addr@url`, comma-separated) for order-consensus. Bypasses automatic discovery. Used only by the subnet team's prod (where metagraph axons aren't published yet) and by test harnesses. **Third-party validators should always leave this unset** — discovery via the metagraph + on-chain `ValidatorRegistry` is the supported path. |
| `CHAMPION_CONSENSUS_PEERS` | `""` | **Internal-only escape hatch.** Same pattern for champion-consensus. Same warning: third-party validators should leave it unset. |

### Leader Election

| Variable | Default | Description |
|----------|---------|-------------|
| `FORCE_LEADER` | `""` | Set to `"1"` to force this validator to act as the leader, bypassing stake-based election. Useful for local testnet. |

### App Catalog Sync

The follower validator pulls `AppIntentDefinition` (including `js_code`) and `DeploymentResult` records from the leader's API on a poll interval and writes them into the local `AppIntentStore`. Without this, a third-party validator's `JsExecutionEngine` has no scoring code loaded and cannot re-score incoming consensus proposals.

Since PR #584, sync also **propagates deletions**: after a successful, non-empty catalog fetch a follower prunes local apps the leader no longer lists — but only *non-operational* ones (no deployment / non-operational status); an app the follower can actively score against is never auto-deleted on a single listing (it logs a loud warning instead), and an empty catalog never mass-deletes. Deleting an app now cascades its deployment rows. The leader never self-syncs, so the source-of-truth store is untouched.

| Variable | Default | Description |
|----------|---------|-------------|
| `LEADER_API_URL` | -- | Leader API base URL (e.g. `https://api.minotaursubnet.com`). Set on every third-party validator. Leaders should leave this **unset** — they are the source of truth and would otherwise sync from themselves. |
| `--app-sync-interval` (CLI only) | `60.0` | Seconds between sync ticks. |

**Trust model (MVP):** `js_code` is fetched from the leader and trusted as-is. There is no on-chain hash anchor at this layer, so a compromised leader could push malicious JS to followers. Anchoring `keccak256(js_code)` on-chain via `AppRegistry` is a tracked follow-up; until then the daemon emits a `SECURITY NOTICE` log at startup whenever sync is enabled.

### Weight Emission

SN112 weights are commit-reveal: the chain keeps only **one** pending commit per validator per tempo epoch (≈360 blocks) and silently discards earlier commits in the same epoch. PR #524 schedules all emission into a short window just before the epoch step so the commit is the last of its tempo and reveals with the freshest champion snapshot.

| Variable | Default | Description |
|----------|---------|-------------|
| `TEMPO_ALIGNED_EMIT` | `1` (ON) | Tempo-aligned weight commits. Set to `0`/`false`/`no` to restore the legacy wall-clock cadence (every `--epoch-seconds`, plus an immediate emit on each round activation). When the chain tempo state can't be queried, the gate falls back to exact legacy behavior automatically. |
| `TEMPO_EMIT_LEAD_BLOCKS` | `20` | Size of the pre-step emit window in blocks (~4 min). The commit fires this many blocks before the tempo boundary. |

`/health` reports the current `emit_schedule` (mode / active / tempo / next boundary); mode is `"wall_clock"` when tempo alignment is disabled or unavailable.

### Deployment Benchmarking

| Variable | Default | Description |
|----------|---------|-------------|
| `BENCHMARK_ALL_DEPLOYMENT_CHAINS` | `0` (OFF) | **Consensus flag — must be fleet-uniform.** OFF keeps byte-identical Base-only benchmarking. When ON, submissions are benchmarked per-deployment-chain with per-chain fork pins; the setting folds into `benchmark_pack_hash`, so a mixed fleet computes different scores. Arm across the whole fleet at once or not at all (PR #621). |
| `ETH_SIM_RPC_URL` | -- | Optional chain-1 (Ethereum) simulation fork URL used when deployment benchmarking spans Ethereum. |

### Distributed Veto (Phase 0 — observe-only)

Phase 0 of distributed veto is **observe-only soak instrumentation**. It never gates certification and never changes round status; it only measures what a future enforcement phase would do. Leave it OFF unless you are helping the subnet team collect soak data.

| Variable | Default | Description |
|----------|---------|-------------|
| `DISTRIBUTED_VETO` | `0` (OFF) | Master arm for the observe-only veto pass. Even when ON, Phase 0 cannot veto or gate anything (enforcement requires further code). When ON, `/health` surfaces the last few observe records under `distributed_veto`. |
| `DISTRIBUTED_VETO_REVERIFY` | `0` (OFF) | Sub-flag: when on (and `DISTRIBUTED_VETO` is on), the leader fire-and-forget re-verifies dissents off the coordinator loop. Observe-only. |

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

# Consensus — peer set comes from on-chain ValidatorRegistry + metagraph,
# not from env. Just supply the signing key + the per-chain registry
# addresses (both chains are required — the canonical .env.example ships
# current values pre-filled).
VALIDATOR_PRIVATE_KEY=0xYOUR_EVM_PRIVATE_KEY
VALIDATOR_REGISTRY_8453=0x88a08d1105393EACE9B6f5ff678DbE508B8639aC
VALIDATOR_REGISTRY_964=0x0B5fE44e90515571761D86C28c4855F325EDE098
QUORUM_BPS=10000

# Chain
CHAIN_ID=1

# Logging
LOG_LEVEL=INFO
```

## Precedence Rules

1. CLI arguments take precedence over environment variables.
2. For `--netuid`, the CLI value is used only if it differs from the default (112); otherwise the `NETUID` environment variable is checked.
3. Quorum is **not** a CLI flag. The canonical `quorumBps` is read from the on-chain `ValidatorRegistry` (via `--validator-registry-address` / `VALIDATOR_REGISTRY_<chain>`); `QUORUM_BPS_OVERRIDE` is the only local escape hatch (it skips the on-chain read).

See also: [Quickstart](./quickstart.md), [Troubleshooting](./troubleshooting.md).
