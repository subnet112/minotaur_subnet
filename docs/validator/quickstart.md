# Validator Quickstart

This guide walks you through setting up and running a Minotaur validator on Bittensor Subnet 112.

## Hardware Requirements

**Every validator must be leader-capable.** Leadership on Subnet 112 is awarded by stake at the metagraph level -- the highest-stake validator runs the leader role, ties broken by hotkey lexicographic order. Leadership rotates whenever stakes shift on chain, with no advance notice. A validator that cannot immediately accept the leader role on stake change is a free-rider: it collects the same emissions as a fully-provisioned validator while only signing follower attestations, and it drags down the network's resilience to leader rotation.

The spec below is sized for *today's* network and scales up as the protocol expands. Start at the baseline; scale up on the trigger events listed.

### Baseline (today)

Subnet 112 currently operates one App (DexAggregator) across two real chains (Ethereum mainnet, Base) plus BT EVM. User volume is light. The baseline spec handles this comfortably with room for the leader role.

| Spec | Value |
|------|-------|
| vCPU | 4 (modern x86_64) |
| RAM | 8 GB |
| Storage | 100 GB SSD/NVMe |
| GPU | none |
| Network out | ~100 GB/month |
| Public IPv4 | yes (consensus + leader API + relayer must be reachable) |

What runs on this box (always, whether you are currently leader or follower):

- Validator service (`python -m minotaur_subnet.validator.main`) -- order-consensus signing.
- API service (`python -m minotaur_subnet.api.server`) -- user gateway, champion-consensus participation, benchmark coordinator. Only serves real user traffic when you are leader, but stays warm so promotion is instant.
- Relayer (`python -m minotaur_subnet.relayer.main`) -- transaction submission. Only signs on-chain when you are leader.
- Anvil forks for each supported chain (currently Ethereum mainnet, Base, BT EVM). All running warm so the leader role can simulate plans immediately. Cold-starting an Anvil fork costs 30+ seconds, which would drop the first minute of orders after every stake-change-driven leader rotation.
- Docker benchmark sandbox -- spins up containers from miner submissions for scoring (active mainly during champion benchmarks).
- JS scoring engine (Node.js subprocess) -- deterministic, on-demand.
- Subtensor connection -- WebSocket to `wss://entrypoint-finney.opentensor.ai:443` or your own node.

Steady-state at this spec uses about 5-6 GB RAM and well under 1 vCPU. Active leader load (handling orders + running a benchmark) peaks around 9-10 GB RAM and 3-4 vCPU. 8 GB / 4 vCPU leaves a healthy margin.

### Growth path

The baseline grows mostly along two axes: number of chains we support (each adds an Anvil fork) and concurrent user volume (each adds parallel scoring + simulation work). Scale up when one of these triggers fires.

| Trigger event | Recommended spec |
|---------------|------------------|
| Baseline (today) | **4 vCPU / 8 GB / 100 GB SSD** |
| +1 chain added (e.g. Arbitrum or Optimism announcement) -- one extra Anvil fork per chain costs ~1.5-2 GB RAM | **4 vCPU / 12 GB / 150 GB SSD** |
| 2+ new chains, or sustained user volume making JS scoring run continuously in parallel | **8 vCPU / 16 GB / 200 GB SSD** |
| Multi-app phase (several Apps live simultaneously, each with independent benchmarks running in parallel) | **8 vCPU / 32 GB / 200 GB SSD** |

Scaling is vertical -- no horizontal sharding needed at the validator level. You can typically resize an existing VPS in under five minutes with a reboot. Plan to upsize at the announcement of each new chain integration; the subnet roadmap publishes these ahead of activation.

### Required maintenance cron

Anvil's overlay filesystem grows roughly 15 GB per fork per day even with tmpfs mounted at `/root` and `/tmp`. Without a daily recycle, a 100 GB volume fills in under a week with three forks. Install this cron:

```
0 3 * * * root docker compose -f /path/to/docker-compose.yml rm -fsv anvil anvil-base anvil-btevm && docker compose -f /path/to/docker-compose.yml up -d anvil anvil-base anvil-btevm
```

The recycle window (03:00 UTC by default) drops in-flight Anvil state for ~60 seconds while the containers restart. During that window the leader cannot simulate new plans; if you are running a high-stake validator, stagger your cron a few minutes from peers to avoid simultaneous reorg pauses.

### Realistic hosting (baseline 4 vCPU / 8 GB)

| Provider | Plan | Approx. monthly cost |
|----------|------|----------------------|
| Hetzner | CCX13 (4 vCPU dedicated / 16 GB / 80 GB NVMe) -- already at the next tier with headroom | EUR 25-30 |
| OVH | VPS Comfort | EUR 18-25 |
| DigitalOcean | Premium AMD 4 vCPU / 8 GB / 160 GB | USD 48 |
| Vultr | Cloud Compute 4 vCPU / 8 GB | USD 40 |
| AWS | c6i.xlarge (4 vCPU / 8 GB) + 100 GB gp3 | USD 130-160 |

Most validators will run on a $25-50/month box at the baseline tier and resize up when chain expansions are announced.

## Third-Party APIs

| Provider | Used for | Free tier sufficient? |
|----------|----------|-----------------------|
| **Alchemy or Infura** (Ethereum mainnet) | Source RPC for the Anvil ETH fork; archive endpoint needed | Yes for moderate load. Premium tier recommended once you take leader for non-trivial periods (free-tier quotas can throttle under burst). |
| **Alchemy or Infura** (Base mainnet, chain 8453) | Source RPC for the Anvil Base fork | Same as above, same account |
| **Public BT EVM RPC** | `https://lite.chain.opentensor.ai` (chain 964) -- ChampionRegistry reads | Public endpoint, no signup |
| **Public Finney WS** | `wss://entrypoint-finney.opentensor.ai:443` -- metagraph reads | Public endpoint, no signup |
| **GitHub API (read-only)** | Cloning miner submissions for benchmark during leader role | Anonymous works for small subnets, but provision a PAT to raise rate limits before you ever take leader |

No GPU compute or LLM API is required. The JS scoring engine is pure Node.js, deterministic, and CPU-bound.

## Ports

### Inbound (must be reachable from the public internet)

| Port | Service | Notes |
|------|---------|-------|
| `9100/tcp` | Validator HTTP API -- consensus signing | Reachable by peer validators and the current leader. |
| `8080/tcp` | API service -- user gateway + champion-consensus | Reachable by users and other validators. Only handles real user traffic while you are leader, but the port stays open for champion-consensus participation regardless. |
| `8091/tcp` | Relayer | Only active during leader role, but keep the port open so it works immediately on promotion. |

If you are behind NAT, forward all three to the validator host. On a cloud VPS with a public IP, open all three in the firewall.

### Outbound (egress, no special configuration)

| Destination | Port | Purpose |
|-------------|------|---------|
| `entrypoint-finney.opentensor.ai` | 443 (WSS) | Subtensor metagraph reads |
| `lite.chain.opentensor.ai` | 443 (HTTPS) | BT EVM RPC |
| Alchemy / Infura host | 443 (HTTPS) | ETH and Base fork source RPCs |
| Other validator peers | 9100 (HTTPS) | Consensus signing |
| Leader API host | 8080 (HTTPS) | Proposal pull (followers) |
| `github.com`, `ghcr.io` | 443 (HTTPS) | Image and repo pulls |

### Internal only (not exposed externally)

Anvil ports (`8545` for ETH, `8546` for Base, `8547` for BT EVM) are bound to the Docker network and must never be exposed to the public internet.

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

# Anvil forks the validator will *connect to* (it does not spawn them; see Step 5).
# Point these at wherever you start the forks — localhost when they run on the
# same box, or your internal Docker hostnames if you bridge networks.
export ANVIL_RPC_URL=http://localhost:8545          # Ethereum fork
export BASE_RPC_URL=http://localhost:8546           # Base fork
export BITTENSOR_EVM_RPC_URL=http://localhost:8547  # BT EVM fork

# Upstream RPCs — used by the validator to advance each Anvil fork to the
# current chain head between simulations. Without these the fork stays frozen
# at startup and sims run against stale state.
export ETH_UPSTREAM_RPC_URL=https://eth-mainnet.g.alchemy.com/v2/YOUR_ALCHEMY_KEY
export BASE_UPSTREAM_RPC_URL=https://base-mainnet.g.alchemy.com/v2/YOUR_ALCHEMY_KEY
export BITTENSOR_EVM_UPSTREAM_RPC_URL=https://lite.chain.opentensor.ai

# Consensus signing (EVM private key, hex-encoded with 0x prefix)
export VALIDATOR_PRIVATE_KEY=0xYOUR_EVM_PRIVATE_KEY
```

See [Configuration](./configuration.md) for the full list of options.

## Step 5: Start the Anvil Forks

The validator process does **not** spawn Anvil itself — it opens RPC connections to whatever URLs you set in `ANVIL_RPC_URL`, `BASE_RPC_URL`, and `BITTENSOR_EVM_RPC_URL`. You start the three forks separately and keep them running. Docker Compose is the supported pattern: it provides restart policies, health checks, and dovetails with the [recycle cron](#required-maintenance-cron).

Save this as `/opt/minotaur/docker-compose.yml` (any stable path works — the cron just needs to reference the same file):

```yaml
services:
  anvil:
    image: ghcr.io/foundry-rs/foundry:latest
    restart: unless-stopped
    entrypoint: ["anvil"]
    command:
      - "--host"
      - "0.0.0.0"
      - "--port"
      - "8545"
      - "--fork-url"
      - "${ETH_UPSTREAM_RPC_URL}"
      - "--block-time"
      - "2"
    tmpfs:
      - /root:size=2g
      - /tmp:size=512m
    ports:
      - "8545:8545"
    healthcheck:
      test: ["CMD-SHELL", "cast block-number --rpc-url http://localhost:8545 || exit 1"]
      interval: 5s
      timeout: 5s
      retries: 20

  anvil-base:
    image: ghcr.io/foundry-rs/foundry:latest
    restart: unless-stopped
    entrypoint: ["anvil"]
    command:
      - "--host"
      - "0.0.0.0"
      - "--port"
      - "8546"
      - "--fork-url"
      - "${BASE_UPSTREAM_RPC_URL}"
      - "--chain-id"
      - "8453"
      - "--no-storage-caching"
      - "--block-time"
      - "2"
    tmpfs:
      - /root:size=2g
      - /tmp:size=512m
    ports:
      - "8546:8546"
    healthcheck:
      test: ["CMD-SHELL", "cast block-number --rpc-url http://localhost:8546 || exit 1"]
      interval: 5s
      timeout: 5s
      retries: 20

  anvil-btevm:
    image: ghcr.io/foundry-rs/foundry:latest
    restart: unless-stopped
    entrypoint: ["anvil"]
    command:
      - "--host"
      - "0.0.0.0"
      - "--port"
      - "8547"
      - "--fork-url"
      - "${BITTENSOR_EVM_UPSTREAM_RPC_URL}"
      - "--chain-id"
      - "964"
      - "--no-storage-caching"
      - "--fork-retry-backoff"
      - "5000"
      - "--retries"
      - "10"
      - "--timeout"
      - "60000"
      - "--block-time"
      - "2"
    tmpfs:
      - /root:size=2g
      - /tmp:size=512m
    ports:
      - "8547:8547"
    healthcheck:
      test: ["CMD-SHELL", "cast block-number --rpc-url http://localhost:8547 || exit 1"]
      interval: 5s
      timeout: 5s
      retries: 20
```

The `tmpfs` mounts on `/root` and `/tmp` keep Anvil's writable layer in RAM rather than the host disk. Without them, each fork bloats its container overlay by ~15 GB/day — the daily [recycle cron](#required-maintenance-cron) is still needed on top of this, but the tmpfs mounts buy you the time between recycles.

Start the forks (the `ETH_UPSTREAM_RPC_URL`, `BASE_UPSTREAM_RPC_URL`, and `BITTENSOR_EVM_UPSTREAM_RPC_URL` you exported in Step 4 are read from the environment):

```bash
docker compose -f /opt/minotaur/docker-compose.yml up -d
```

Wait for all three to report healthy:

```bash
docker compose -f /opt/minotaur/docker-compose.yml ps
```

Quick sanity check (each should return a block number that increments over a few seconds):

```bash
cast block-number --rpc-url http://localhost:8545
cast block-number --rpc-url http://localhost:8546
cast block-number --rpc-url http://localhost:8547
```

### Running without Docker

If you prefer to run Anvil directly (e.g. under systemd), the equivalent commands are:

```bash
anvil --host 0.0.0.0 --port 8545 --fork-url "$ETH_UPSTREAM_RPC_URL" --block-time 2
anvil --host 0.0.0.0 --port 8546 --fork-url "$BASE_UPSTREAM_RPC_URL" --chain-id 8453 --no-storage-caching --block-time 2
anvil --host 0.0.0.0 --port 8547 --fork-url "$BITTENSOR_EVM_UPSTREAM_RPC_URL" --chain-id 964 --no-storage-caching --block-time 2
```

Wrap each in its own systemd unit with `Restart=on-failure`. The Anvil disk-bloat issue described in the [maintenance cron](#required-maintenance-cron) section applies either way — adjust the cron to bounce your systemd units instead of `docker compose up`.

## Step 6: Run the Validator

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
