# Validator Quickstart

This guide walks you through setting up and running a Minotaur validator on Bittensor Subnet 112.

## Hardware Requirements

The "validator" role spans from a minimal consensus follower up to running the full leader stack. Pick the tier that matches what you intend to run.

### Tier 1 — Follower validator (minimum)

You run `python -m minotaur_subnet.validator.main` and participate in order-consensus quorum. You receive proposals from the leader, independently re-score them via the JS scoring engine, and return EIP-712 attestations. With the default `FOLLOWER_PROPOSAL_RESIMULATE=0`, you trust the leader's Anvil simulation and only re-run JS scoring.

| Spec | Value |
|------|-------|
| vCPU | 2 |
| RAM | 2 GB |
| Storage | 30 GB SSD |
| GPU | none |
| Network out | ~50 GB/month |

Realistic provider: $5-10/mo VPS (Hetzner CX21, DigitalOcean basic droplet, OVH VLE-1).

### Tier 2 — Independent-simulation validator (recommended)

Same as Tier 1 with `FOLLOWER_PROPOSAL_RESIMULATE=1`. You run your own Anvil ETH + Base forks and never trust the leader's simulation result. This is the configuration we recommend for any validator that wants the full trustless guarantees the protocol provides.

| Spec | Value |
|------|-------|
| vCPU | 4 |
| RAM | 8 GB |
| Storage | 100 GB SSD |
| GPU | none |
| Network out | ~200 GB/month (Anvil forks pull a lot of mainnet state) |

A **daily Anvil recycle cron** is required: Anvil's overlay filesystem grows ~15 GB/day per fork even with tmpfs mounted at `/root` and `/tmp`. The recommended cron at 03:00 UTC is:

```
0 3 * * * root docker compose -f /path/to/docker-compose.yml rm -fsv anvil-eth anvil-base && docker compose -f /path/to/docker-compose.yml up -d anvil-eth anvil-base
```

Realistic provider: $20-40/mo VPS (Hetzner CX31, DigitalOcean 4 GB droplet, OVH VLE-2).

### Tier 3 — Leader / full subnet stack

This bundle runs the entire subnet infrastructure: leader API, two API peers for champion-consensus, three validators for order-consensus, the relayer, four Anvil forks (ETH + Base + BT EVM + benchmark), the subtensor connection, and the Docker benchmark sandbox. This is the central operator role -- almost certainly **not** what you want as an external validator.

| Spec | Value |
|------|-------|
| vCPU | 8 |
| RAM | 16-32 GB |
| Storage | 200 GB SSD (daily Anvil recycle still required) |
| GPU | none |

Realistic provider: $80-150/mo dedicated or compute-optimized cloud (Hetzner AX52, AWS c6i.xlarge or larger, dedicated Linode).

## Third-Party APIs

Required for Tier 2 and Tier 3, optional for Tier 1.

| Provider | Used for | Free tier sufficient? |
|----------|----------|-----------------------|
| **Alchemy or Infura** (Ethereum mainnet) | Source RPC for the Anvil ETH fork; archive endpoint needed | Yes, the free tier handles one validator comfortably |
| **Alchemy or Infura** (Base mainnet, chain 8453) | Source RPC for the Anvil Base fork | Yes, same account |
| **Public BT EVM RPC** | `https://lite.chain.opentensor.ai` (chain 964) -- ChampionRegistry reads | Public endpoint, no signup |
| **Public Finney WS** | `wss://entrypoint-finney.opentensor.ai:443` -- metagraph reads | Public endpoint, no signup |
| **GitHub API (read-only)** | Cloning miner submissions for benchmark (Tier 3 only) | Anonymous works, but a PAT raises the rate limit |

No GPU compute or LLM API is required. The JS scoring engine is pure Node.js, deterministic, and CPU-bound.

## Ports

### Inbound (must be reachable from the public internet)

| Port | Service | Required for |
|------|---------|--------------|
| `9100/tcp` | Validator HTTP API -- consensus signing endpoint | All tiers |
| `8080/tcp` | Leader API | Tier 3 only |
| `8091/tcp` | Relayer | Tier 3 only |

If you are behind NAT, forward `9100/tcp` to the validator host. On a cloud VPS with a public IP, simply open `9100/tcp` in the firewall.

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
