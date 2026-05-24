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
| Public IPv4 | yes (port 9100/tcp must be reachable by the current leader for proposal broadcast) |

What runs on this box:

- Validator daemon (`python -m minotaur_subnet.validator.main`) -- order-consensus signing + weight emission to the metagraph.
- Anvil forks for each supported chain (currently Ethereum mainnet, Base, BT EVM). Used to independently re-simulate execution plans the leader proposes. Cold-starting an Anvil fork costs 30+ seconds, so they run warm.
- JS scoring engine (Node.js subprocess inside the validator container) -- deterministic, on-demand.
- Subtensor connection -- WebSocket to `wss://entrypoint-finney.opentensor.ai:443` or your own node.

The transaction submitter (relayer) is a singleton operated by the subnet team — third-party validators don't run it. Champion-consensus participation (the API service) is an optional add-on covered separately when ready.

Steady-state at this spec uses about 4-5 GB RAM and well under 1 vCPU. Re-simulation bursts (when the leader proposes a new order) peak around 6-7 GB RAM and 2-3 vCPU. 8 GB / 4 vCPU leaves a healthy margin.

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

Anvil's overlay filesystem grows fast even with tmpfs mounted at `/root` and `/tmp`. A production deployment in May 2026 measured **~40-50 GB per fork per day**; the rate has grown over time as the chain head moves further from the fork block and as user/simulation load increases. With three forks that's ~150 GB/day of bloat. Without a frequent recycle, a 100 GB volume fills in well under a day and the host OS hangs (status check: impaired) once the disk hits 100% — at which point SSH is dead and the only recovery is a force stop+start of the VM.

Install this cron — **every 6 hours**, not daily:

```
0 */6 * * * root docker compose -f /opt/minotaur/docker-compose.yml rm -fsv anvil anvil-base anvil-btevm && docker compose -f /opt/minotaur/docker-compose.yml up -d anvil anvil-base anvil-btevm
```

(The path matches the compose file you write in Step 6. Adjust if you put it elsewhere.)

At every-6h cadence, max accumulation between recycles is ~37 GB across three forks, which fits in a 100 GB volume with other services taking ~10-15 GB. If you skip a recycle (cron failure, host unreachable, manual stop without restart), the disk can fill in 12-15 hours from there — monitor `df -h /` and treat low disk as a paging event. If the rate grows further (more chains added, much higher load), drop the cadence to every 4 or 3 hours.

Each recycle window drops in-flight Anvil state for ~60 seconds while the containers restart. During that window the leader cannot simulate new plans; if you are running a high-stake validator, stagger your cron a few minutes from peers to avoid simultaneous reorg pauses.

**If you do hit a disk-full OS hang**: the SSH daemon is dead at that point, so `docker compose down`, cron tightening, or any in-VM cleanup won't help. The only recovery is a force stop+start at the hypervisor layer (on AWS: `aws ec2 stop-instances --force --instance-ids <id>`, wait for stopped, then `start-instances`). EBS-backed instances preserve all state across this; containers with `restart: unless-stopped` come back automatically. Once the host is up, immediately `docker compose rm -fsv` the anvil services to release their snapshot overlays — `docker system prune` alone won't reclaim them.

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
| `9100/tcp` | Validator HTTP API -- consensus signing + `/identity` | Must be reachable by the current leader to receive proposals, and by other peers for peer discovery. |

If you are behind NAT, forward 9100 to the validator host. On a cloud VPS with a public IP, open 9100 in the firewall.

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
- **Docker + Docker Compose** (for running Anvil forks; see Step 6)
- **Bittensor CLI** (`btcli`) with a registered wallet on subnet 112
- **Ethereum RPC URL** from Alchemy or Infura (for Anvil mainnet fork simulation)
- **EVM private key** for EIP-712 consensus signing (a fresh key is fine -- it does not hold funds)
- **Coordination with the subnet operator** to be added to the on-chain `ValidatorRegistry` (Step 4) — without this, your signatures won't count

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

## Step 4: Get onboarded to the on-chain ValidatorRegistry

Before your signatures count toward quorum, your **EVM signing address** (the one derived from `VALIDATOR_PRIVATE_KEY`) must be added to the `ValidatorRegistry` contract on each chain you'll operate on. This is a coordinated step with the current registry owner (typically the subnet operator).

What you need to send to the registry owner:

```
Validator hotkey (SS58):  5...
EVM signing address:      0x...   (from your VALIDATOR_PRIVATE_KEY)
Public axon URL:          http://your-host:9100
```

What the registry owner runs on their side, once per chain:

```bash
# Read the current set
cast call $VALIDATOR_REGISTRY 'getValidators()(address[])' --rpc-url $RPC_URL

# Add you to the set (replace with the full new list, sorted ascending)
cast send $VALIDATOR_REGISTRY \
  'updateValidators(address[])' \
  '[0xExistingValidator1,0xExistingValidator2,0xYourEvmAddress]' \
  --rpc-url $RPC_URL \
  --private-key $REGISTRY_OWNER_KEY
```

Repeat per chain (Ethereum, Base, BT EVM — addresses listed in the [network reference](../operator/network-reference.md)).

**Verify you've been added** before continuing:

```bash
cast call $VALIDATOR_REGISTRY 'isValidator(address)(bool)' 0xYourEvmAddress --rpc-url $RPC_URL
```

If this returns `true` on every chain, you're cleared to bring up the daemon. If it returns `false`, your consensus signatures will be ignored and your validator will be a free-rider — emissions but no real participation.

> **Note**: until this handshake exists as an on-chain registration flow (similar to Bittensor's subnet-register), it's a manual coordination step. The subnet operator publishes a process; check the project README for the current contact channel.

## Step 5: Configure Environment

The recommended path (Step 6) reads configuration from `platform/validator/.env`. The variables below also work as shell exports if you're running the daemon directly under systemd instead of Docker Compose ("Running without Docker", below).

```bash
# Bittensor identity
export WALLET_NAME=my-validator
export HOTKEY_NAME=my-hotkey
export NETUID=112
export SUBTENSOR_URL=wss://entrypoint-finney.opentensor.ai:443

# Anvil forks the validator will *connect to* (it does not spawn them; see Step 6).
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

# Public URL where your daemon serves /identity. Required for peer
# discovery — other validators sign and verify against this. See the
# Peer discovery section below.
export VALIDATOR_AXON_URL=http://your-public-host:9100

# On-chain ValidatorRegistry that holds the canonical quorum threshold.
# These addresses come from the subnet operator — see "Onboarding" below
# and the [network reference](../operator/network-reference.md) for current
# mainnet values per chain. The daemon reads quorumBps from this contract
# at startup and refreshes once per epoch.
export VALIDATOR_REGISTRY_ADDRESS=0xYOUR_VALIDATOR_REGISTRY_ON_BASE
# Optional per-chain forms if you run the daemon against a non-default
# CHAIN_ID and don't want to set VALIDATOR_REGISTRY_ADDRESS directly:
#   export VALIDATOR_REGISTRY_1=0x...     # Ethereum mainnet
#   export VALIDATOR_REGISTRY_8453=0x...  # Base
#   export VALIDATOR_REGISTRY_964=0x...   # BT EVM
```

See [Configuration](./configuration.md) for the full list of options and
[Quorum management](../operator/quorum-management.md) for how to change the
network-wide quorum value once you're an operator.

## Step 6: Bring up the validator stack

The validator role for a third-party operator is **six containers**:

- Three Anvil forks (Ethereum mainnet, Base, BT EVM)
- The validator daemon (order-consensus signer + weight emitter, port 9100)
- An api service (champion-consensus signer, port 8080)
- A docker-socket-proxy (limits the api's Docker access to spawning sandboxed solver containers — used during reactive benchmarking of champion candidates)

Plus an OPT-IN seventh container: Watchtower, for auto-update on `:stable` tag promotion. Bring it up via the `autoupdate` compose profile.

The canonical compose ships in this repo at `platform/validator/`:

```bash
cd platform/validator
cp .env.example .env
# Open .env in your editor and fill in every YOUR_* placeholder
$EDITOR .env

docker compose --profile autoupdate up -d         # with Watchtower auto-update
# ── or ──
docker compose up -d                                # without (manual pulls)
```

The first cold start takes ~60-90 seconds while the three Anvil forks fetch their initial state from the upstream RPCs (`ETH_UPSTREAM_RPC_URL`, `BASE_UPSTREAM_RPC_URL`, `BITTENSOR_EVM_UPSTREAM_RPC_URL`). The validator and api services both wait for all three anvils to report healthy before they start.

Check the state:

```bash
docker compose ps
# All six services should show "(healthy)" or "Up" after ~90s.

docker compose logs -f validator api
# Validator should show:
#   "ProtocolConfig: loaded quorum_bps=6666 from ValidatorRegistry ..."
#   "Consensus enabled (id=0x..., peer-mode=discovered, quorum=6666 bps)"
#   "Validator starting as ORDER CONSENSUS PEER ..."
#   "BlockLoop started (tick_interval=12.0s)"
# Api should show:
#   "Champion consensus enabled (validator=0x..., quorum=N bps from
#    ChampionRegistry 0x..., validator-set from VR 0x... on chain 964)"
```

### Verify

```bash
# Validator health (order-consensus)
curl http://localhost:9100/health
# {"status": "ok", "service": "app-intents-validator", "block_loop_running": true, ...}

# Api health (champion-consensus)
curl http://localhost:8080/health
# {"status": "ok", "champion_consensus": {"enabled": true, ...}, ...}

# Confirm the validator read the right quorum from chain
curl http://localhost:9100/consensus/info
# {"consensus_enabled": true, "quorum_bps": 6666, "validator_id": "0x...", ...}

# Confirm /identity serves a fresh signed EIP-712 payload (requires a working
# Bittensor wallet — returns 503 if WALLET_NAME/HOTKEY_NAME aren't loaded).
# Both the validator daemon (port 9100) and the api service (port 8080)
# expose /identity so peer-discovery works on both consensus loops.
curl http://localhost:9100/identity
curl http://localhost:8080/identity
```

If either `/consensus/info` or the api's `champion_consensus.quorum_required` looks wrong, your `VALIDATOR_REGISTRY_*` envs may point at a stale contract — check the [network reference](../operator/network-reference.md).

### What each service does

| Service | Role |
|---|---|
| `validator` | Order-consensus signing (re-simulates leader's order proposals on Anvil, signs approvals if both JS + on-chain scores meet threshold). Emits weights to the metagraph every ~20 min. |
| `api` | Champion-consensus signing (reactively re-benchmarks the leader's champion candidate inside a sandboxed Docker container, signs the certification if scores hold up). |
| `anvil-eth`, `anvil-base`, `anvil-btevm` | Local forks of each chain so independent re-simulation doesn't touch mainnet. |
| `docker-socket-proxy` | Filtered Docker socket access for the api service. Only `CONTAINERS + IMAGES + NETWORKS + POST` allowed; no `EXEC`, no `BUILD`, no `SWARM`. |
| `watchtower` (opt-in) | Polls GHCR every hour for a new `:stable` SHA; recreates `validator` + `api` when the tag moves. |

### What this stack does NOT include

- **Relayer**: the transaction submitter is a singleton service operated by the subnet team. Your validator signs proposals; the leader's submission path uses our relayer. You never deal with a gas wallet.
- **Order/champion proposing**: only the highest-stake validator's leader-api does that. As a follower, you receive proposals + verify + sign.

## Auto-update

The default `MINOTAUR_IMAGE_TAG=stable` (in `.env.example`) and the optional Watchtower container together give you hands-off updates:

1. New commit lands on `main` → `docker-publish.yml` builds + pushes `:latest` and `:sha-<short>` (immutable per-commit) to GHCR.
2. The new image runs on the subnet team's prod for a soak period.
3. When the team is happy with the soak, they run the `promote-stable.yml` workflow with the short SHA. That re-tags `:sha-<short>` as `:stable` on GHCR.
4. Your Watchtower polls GHCR within the next hour, pulls the new image, recreates the `validator` and `api` containers with the new SHA. ~30-60 second downtime during the recreate.

If you want manual control instead of Watchtower, leave the `autoupdate` profile off and run on the subnet team's announced cadence:

```bash
docker compose pull validator api
docker compose up -d --force-recreate validator api
```

To pin a specific SHA (opt out of auto-update entirely without removing Watchtower):

```bash
# In .env:
MINOTAUR_IMAGE_TAG=sha-abc1234
```

then `docker compose up -d` — Watchtower won't update a container whose image tag isn't tracking `:stable`.

## On-chain registration

Your EVM signing address needs to be in the on-chain `ValidatorRegistry` on both Base and BT EVM before your signatures count toward quorum. The subnet team runs the on-chain handshake for you — open an issue using the [Request validator onboarding template](https://github.com/subnet112/minotaur_subnet/issues/new?template=onboard-validator.yml).

The template captures the three required fields:
- EVM signing address (checksummed)
- Bittensor SS58 hotkey
- Public axon URL (`http://<host>:9100`)

Once the team comments with the tx hashes, your validator is live. Within ~60 seconds, every existing validator's `ProtocolConfig` refresh loop discovers your `/identity` endpoint, verifies the EIP-712 binding, and starts including you in proposals.

### Running without Docker

If you prefer Anvil under systemd plus the daemon as a native process, the equivalent invocations are:

```bash
anvil --host 0.0.0.0 --port 8545 --fork-url "$ETH_UPSTREAM_RPC_URL" --block-time 2
anvil --host 0.0.0.0 --port 8546 --fork-url "$BASE_UPSTREAM_RPC_URL" --chain-id 8453 --no-storage-caching --block-time 2
anvil --host 0.0.0.0 --port 8547 --fork-url "$BITTENSOR_EVM_UPSTREAM_RPC_URL" --chain-id 964 --no-storage-caching --block-time 2

python -m minotaur_subnet.validator.main \
  --port 9100 \
  --netuid 112 \
  --wallet-name "$WALLET_NAME" \
  --hotkey-name "$HOTKEY_NAME" \
  --subtensor-url "$SUBTENSOR_URL" \
  --validator-key "$VALIDATOR_PRIVATE_KEY" \
  --tick-interval 12.0 \
  --epoch-seconds 1200
```

Wrap each in its own systemd unit with `Restart=on-failure`. The Anvil disk-bloat issue described in the [maintenance cron](#required-maintenance-cron) section applies either way — adjust the cron to bounce your systemd units instead of `docker compose rm -fsv`.

## Peer discovery

Order-consensus peers are discovered automatically — no `VALIDATOR_PEERS` env required for the daemon's order-consensus path. The flow:

1. **Your daemon publishes its identity**: a `GET /identity` endpoint on port 9100 returns a fresh EIP-712 signed payload binding `(evm_address, hotkey, axon_url)`. Each request regenerates the signature so it's never stale.
2. **You publish your axon URL**: set `VALIDATOR_AXON_URL=http://your-host:9100` in the daemon env. This is what the signed payload claims. Also call `btcli` to register your axon on the Bittensor metagraph so other validators can find you.
3. **Other validators discover you**: their `ProtocolConfig.refresh_loop` (default 60s tick) walks the metagraph axon list, probes each `/identity`, verifies the EIP-712 signature, and cross-checks the recovered EVM address is in `ValidatorRegistry.getValidators()` (Step 4 handshake) and the hotkey matches the metagraph.

What this gives operators:

- **No coordinated restart when a new validator joins.** The new validator's EVM gets added on-chain by the registry owner, they start their daemon, others pick them up within one refresh tick.
- **No `VALIDATOR_PEERS` env to maintain across the cluster.** Discovery + the on-chain registry are the source of truth.
- **IP changes are self-served.** A validator changing hosts just updates `VALIDATOR_AXON_URL` and restarts; the signed `/identity` payload re-publishes the new URL automatically.

### Required env for discovery

| Variable | Purpose |
|---|---|
| `VALIDATOR_AXON_URL` | The public URL you serve `/identity` on. Typically `http://<your-public-ip>:9100`. The signed payload includes this; if it's missing, `/identity` returns 503. |
| `SUBTENSOR_URL` | Required so the daemon can read the metagraph for axon discovery (already required in Step 5). |
| `VALIDATOR_REGISTRY_ADDRESS` | Required so the daemon can read the authorized EVM set (already required in Step 5). |

### Optional override

If you need to pin the peer list (local testnet, isolated cluster, debugging a discovery failure), set `VALIDATOR_PEERS` env or pass `--validator-peers` on the daemon CLI. The pinned list overrides discovery entirely. Production deployments should leave it unset.

### Verifying discovery is working

```bash
# Confirm your daemon publishes a valid identity
curl http://localhost:9100/identity
# Should return {"evm_address": "0x...", "hotkey": "5...", "axon_url": "...",
#                "expiry": <timestamp>, "nonce": "0x...", "signature": "0x..."}

# Confirm your daemon sees other peers (via /consensus/info)
curl http://localhost:9100/consensus/info
# .peers should list discovered peers; this list refreshes on each ProtocolConfig tick.
```

## Production process supervision

The canonical compose in `platform/validator/` already sets `restart: unless-stopped` on every container — Docker handles crash recovery and reboot survival. No additional process supervisor needed for the Docker path.

If you went with "Running without Docker" instead, wrap the daemon in systemd:

```ini
# /etc/systemd/system/minotaur-validator.service
[Unit]
Description=Minotaur Validator Daemon
After=network-online.target docker.service

[Service]
EnvironmentFile=/etc/minotaur/env
ExecStart=/opt/minotaur/.venv/bin/python -m minotaur_subnet.validator.main --port 9100 --epoch-seconds 1200
Restart=on-failure
RestartSec=5
User=minotaur

[Install]
WantedBy=multi-user.target
```

Put shared env (RPC URLs, registry addresses, validator key) in `/etc/minotaur/env` with mode 0600. Enable with `systemctl enable --now minotaur-validator`. Repeat the pattern for each Anvil systemd unit.

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
| Anvil (BT EVM fork) | 8547 | http://localhost:8547 |
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
