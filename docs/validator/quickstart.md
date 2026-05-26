# Validator Quickstart

Run a Minotaur validator on **Bittensor Subnet 112** and earn weight
emissions from the subnet. You join as an **order-consensus signer** —
re-simulating and re-scoring the locked leader's swap proposals on your
own Anvil forks, and signing if you agree. No gas wallet is held on
your node; the subnet team's singleton relayer pays for execution gas.

This page is the canonical onboarding flow. Follow it end-to-end the
first time; once your stack is live, the day-2 operating sections at the
bottom (auto-update, peer discovery, disk-bloat cron, troubleshooting
links) are the parts you'll come back to.

## 1. Hardware

One spec, sized so every validator can take leadership if the network
unlocks election or fails over to stake-based selection:

- **8 vCPU / 16-32 GB RAM / 200 GB SSD** (NVMe strongly preferred)
- **Public IPv4 with a static address** — your axon URL is published
  on the metagraph and must stay reachable for cross-attestation
- **Linux** (Ubuntu 22.04+ tested; Amazon Linux works)
- **Docker 24+ and Docker Compose v2**

> A smaller box (4 vCPU / 8 GB) can keep up with follower-only work
> while the leader-election lock is in place, but you risk being
> under-provisioned the moment a chain is added or the lock is cleared.
> Provision at the full spec from the start unless you have a strong
> reason to scale up later.

## 2. Register on Bittensor

You need a registered hotkey on subnet 112. If you do not already have
one:

```bash
# Install btcli
pip install bittensor-cli

# Create wallet + hotkey (skip if you already have one)
btcli wallet new --wallet-name <your_wallet> --hotkey <your_hotkey>

# Fund the coldkey with ~1 TAO for the registration burn
# (transfer from an exchange or another wallet)

# Register on subnet 112
btcli subnet register --netuid 112 \
  --wallet-name <your_wallet> --hotkey <your_hotkey>
```

Verify:

```bash
btcli s metagraph --netuid 112 | grep <your_hotkey_ss58>
```

During the early-network operating period, **leader election is locked
to the subnet team's hotkey** via a hardcoded `LOCKED_LEADER_HOTKEY`
constant (see PR #27). Your stake still drives your weight share but
does not make you eligible for leadership while the lock is active —
this is intentional. The lock is removed by clearing both
`LOCKED_LEADER_HOTKEY` and `LOCKED_LEADER_EVM_ADDRESS` together; the
team announces it ahead of time.

## 3. Generate an EVM consensus key

This is the key your validator uses to sign EIP-712 order-consensus
approvals. **It holds no funds** — it is purely a signing identity.
The subnet team's singleton relayer pays gas; your node never
broadcasts a transaction.

```bash
# Install foundry's cast if you don't have it
curl -L https://foundry.paradigm.xyz | bash && foundryup

# Generate a fresh EVM key
cast wallet new
```

Copy the **private key** (the `0x…` line) and the **address** (also
`0x…`) into a password manager. You paste the private key into your
`.env` in Step 8, and you send the address to the subnet team in
Step 4.

## 4. Get added to the on-chain ValidatorRegistry

Send your validator EVM **address** (not the private key) to the
subnet team. The supported channels are:

- Open an issue using the
  [Request validator onboarding template](https://github.com/subnet112/minotaur_subnet/issues/new?template=onboard-validator.yml)
- Or DM the team via the contact channel published in the project
  README.

The team will add your address to two on-chain registries:

- **Base mainnet** `ValidatorRegistry`
  at `0x88a08d1105393EACE9B6f5ff678DbE508B8639aC` (chain 8453)
- **BT EVM** `ValidatorRegistry`
  at `0x0B5fE44e90515571761D86C28c4855F325EDE098` (chain 964)

Until your address is in both registries your signatures do not
count toward quorum and your `/identity` self-attestation fails
upstream verification. Once the team responds with tx hashes,
confirm registration is live with:

```bash
# Verify on Base
cast call 0x88a08d1105393EACE9B6f5ff678DbE508B8639aC \
  "getValidators()(address[])" \
  --rpc-url https://mainnet.base.org \
  | tr ',' '\n' | grep -i <your_evm_address>

# And on BT EVM
cast call 0x0B5fE44e90515571761D86C28c4855F325EDE098 \
  "getValidators()(address[])" \
  --rpc-url https://lite.chain.opentensor.ai \
  | tr ',' '\n' | grep -i <your_evm_address>
```

(Both commands also work as
`cast call <addr> "isValidator(address)(bool)" <your_addr> --rpc-url <rpc>`
if you prefer a boolean check.)

The current authoritative addresses live in
[`docs/operator/network-reference.md`](../operator/network-reference.md).
If that page disagrees with what you see here, the network reference
wins — re-check before opening the onboarding issue.

## 5. Open firewall + decide your axon URL

Open **TCP port 9100** inbound on your validator host. Other validators
fetch `/identity` from your axon URL to verify your hotkey ↔
EVM-address binding.

```bash
# Example for AWS EC2 security groups
aws ec2 authorize-security-group-ingress \
  --group-id <your-sg-id> \
  --protocol tcp --port 9100 --cidr 0.0.0.0/0

# Or on Ubuntu with ufw
sudo ufw allow 9100/tcp comment "minotaur validator daemon"
```

Decide how third parties will reach you:

- **Static IP**: `VALIDATOR_AXON_URL=http://203.0.113.7:9100`
- **DNS**: `VALIDATOR_AXON_URL=http://validator.example.com:9100`

You will fill this into `.env` in Step 8.

> **Port 8080 (api service)** is also exposed by the canonical compose
> for the champion-consensus loop's `/identity` mirror. Open it inbound
> only if you intend to participate in champion-consensus signing once
> the registry-consolidation work goes live; until then 9100 alone is
> sufficient to be a useful order-consensus follower.

## 6. Get upstream RPC keys (Alchemy / Infura / QuickNode)

This is the step that catches most operators. Your validator runs
three local Anvil instances that fork Ethereum mainnet, Base mainnet,
and BT EVM. Every time a swap proposal arrives, your forks re-execute
it locally to re-score and decide whether to sign — that means
**archive reads against your upstream RPCs on every order**, plus a
fresh fork every time the recycle cron triggers (every 6 hours by
default, configurable).

**Public RPC endpoints will not survive prod load.** Free-tier
`eth.merkle.io`, `cloudflare-eth.com`, `mainnet.base.org`, etc.
rate-limit at thresholds you hit within the first few simulations of
a single order. When that happens your validator silently fails
proposals, consensus drops to the leader plus the remaining peers,
and you stop earning emissions.

| Chain | Env var | Provider |
|---|---|---|
| Ethereum mainnet (chain 1) | `ETH_UPSTREAM_RPC_URL` | Alchemy / Infura / QuickNode |
| Base mainnet (chain 8453) | `BASE_UPSTREAM_RPC_URL` | Alchemy / Infura / QuickNode |
| BT EVM (chain 964) | `BITTENSOR_EVM_UPSTREAM_RPC_URL` | Public endpoint OK at single-validator load |

**Provision (Alchemy example):**

1. Sign up at https://www.alchemy.com.
   The **Growth plan (~$49/mo)** handles a single validator
   comfortably; the **free Sandbox plan** is borderline-OK during the
   early-network phase and may rate-limit once swap volume picks up.
2. Create one app per chain (Ethereum → Mainnet, Base → Base Mainnet).
3. Copy the HTTPS endpoint URL — looks like
   `https://eth-mainnet.g.alchemy.com/v2/<your_long_key>`.
4. **Enable Archive Node access** on each app. Alchemy Growth+ enables
   this by default; on Infura you may need to opt in. Your Anvil forks
   issue `eth_getStorageAt` / `eth_getProof` calls at historical
   blocks during simulation — non-archive endpoints return 400 on
   those and your validator silently fails to sign.

**Request volume to expect (per validator):**

- ~1-5 requests per swap proposal per chain it touches
- ~50-200 requests on a fresh `anvil --fork-url` startup (every 6h on
  the default recycle cron)
- Steady-state under low traffic: well under 1 RPS per chain
- Burst during heavy trading: tens of RPS per chain briefly

**Cheap alternative:** if you already operate Bittensor validators
with your own archive Ethereum nodes, point the env vars at your local
endpoints. The Anvil containers fork from there with zero rate-limit
risk.

**BT EVM (chain 964):** the public `https://lite.chain.opentensor.ai`
endpoint works for a single home-IP validator. If you see throttling,
switch to a private endpoint, or — if you already run your own
subtensor node — point at it: the same node serves the BT EVM
JSON-RPC on its substrate RPC port (Frontier is built into the
subtensor binary). See
[Running your own subtensor](#running-your-own-subtensor) below.

## 7. Clone the canonical compose

```bash
mkdir -p ~/minotaur && cd ~/minotaur
curl -fsSL https://raw.githubusercontent.com/subnet112/minotaur_subnet/main/platform/validator/docker-compose.yml -o docker-compose.yml
curl -fsSL https://raw.githubusercontent.com/subnet112/minotaur_subnet/main/platform/validator/.env.example -o .env
```

You only need these two files. The validator + api Python code runs
inside the Docker image — no local venv or pip install required.

## 8. Configure `.env`

Edit `.env` and fill in every `YOUR_*` placeholder:

```bash
# Bittensor identity
WALLET_NAME=<your_wallet>
HOTKEY_NAME=<your_hotkey>

# Where btcli stored your wallets on this host
# Default is the standard btcli location; override only if elsewhere
# BITTENSOR_WALLET_PATH=/home/ubuntu/.bittensor/wallets

# Public URL on port 9100 — must match what you registered in Step 4
VALIDATOR_AXON_URL=http://<your-public-host>:9100

# EVM signing key from Step 3
VALIDATOR_PRIVATE_KEY=0x<your_evm_private_key>

# Upstream RPCs — use your own Alchemy/Infura keys for production load
ETH_UPSTREAM_RPC_URL=https://eth-mainnet.g.alchemy.com/v2/<your_key>
BASE_UPSTREAM_RPC_URL=https://base-mainnet.g.alchemy.com/v2/<your_key>

# (Optional) Watchtower auto-update poll interval. Default is 1 hour.
# Drop to 300s (5 min) during the early-network shake-out so audit
# fixes propagate faster across the network.
WATCHTOWER_POLL_INTERVAL=300
```

Leave the on-chain registry addresses (`VALIDATOR_REGISTRY_8453`,
`VALIDATOR_REGISTRY_964`, `APP_REGISTRY_*`, `CHAMPION_REGISTRY_964`)
at their defaults — they're pre-filled with the current production
addresses from the 2026-05-21 quorum-refactor deployment. If startup
errors mention `quorumBps() reverted` you're pointing at a stale
address; refresh from the
[network reference](../operator/network-reference.md).

> **Internal-only envs — do NOT set as a third party.**
> `ORDER_CONSENSUS_PEERS` and `CHAMPION_CONSENSUS_PEERS` are pinned-peer
> escape hatches used by the subnet team's own deployment where
> metagraph axon URLs are not published yet. Setting them as a
> third-party operator pins you to a stale set that excludes the rest
> of the network. Discovery via the metagraph + on-chain
> ValidatorRegistry is the supported path and works out of the box once
> Step 4 completes.

## 9. Start the stack

```bash
# With auto-update (recommended during early-network — pulls every 5
# min from the team-promoted :stable tag)
docker compose --profile autoupdate up -d

# Or without (you'll need `docker compose pull && up -d` manually
# after the team announces a new :stable promotion)
docker compose up -d
```

The first cold start takes ~60-90 seconds while the three Anvil forks
fetch their initial state from the upstream RPCs. The validator + api
services both wait for all three Anvils to report healthy before they
start.

## 10. Verify

```bash
# Validator daemon health
curl http://<your-public-host>:9100/health
# expect: {"status":"ok","loaded_intents":N,"block_loop_running":true,...}

# Self-attested identity (other validators fetch this)
curl http://<your-public-host>:9100/identity
# expect: {"evm_address":"0x<yours>","hotkey":"<yours>","axon_url":"...","signature":"0x..."}

# Local API gateway (champion-consensus + admin surface)
curl http://localhost:8080/health

# Confirm the validator read the right quorum from chain
curl http://localhost:9100/consensus/info
# {"consensus_enabled": true, "quorum_bps": <N>, "validator_id": "0x...", ...}

# Consensus participation: tail logs and look for proposal/approval lines
docker compose logs -f validator | grep -iE "consensus|proposal|approval"
```

A bundled check script runs every endpoint, verifies registry state,
and prints a green/red summary. The onboarding issue template asks
you to paste its output:

```bash
bash scripts/check_validator.sh
```

If `/consensus/info` returns `quorum_bps=0` or the api's
`champion_consensus.quorum_required` looks wrong, your
`VALIDATOR_REGISTRY_*` envs point at a stale contract — re-check the
[network reference](../operator/network-reference.md).

## 11. What you're signing up for

- **Role: order-consensus follower.** During the early-network
  operating period leader election is locked to the subnet team's
  hotkey (`LOCKED_LEADER_HOTKEY`). You receive proposals from the
  leader, re-simulate them on your Anvil forks, and sign approvals.
- **You don't hold gas.** No `RELAYER_PRIVATE_KEY` on your node. The
  team's singleton relayer at `https://relayer.minotaursubnet.com` is
  the only address that ever pays gas for swap execution. Your
  validator signs an EIP-191 wrapper around each quorum bundle using
  `VALIDATOR_PRIVATE_KEY`; the relayer verifies the wrapper signer is
  in the on-chain `ValidatorRegistry` before submitting.
- **Auto-updates from `:stable`.** New code is promoted to the
  `:stable` tag by the subnet team after soak-testing on prod.
  Watchtower (if you enabled the `autoupdate` profile) pulls on its
  poll interval and recreates your containers. To pin to a specific
  build, set `MINOTAUR_IMAGE_TAG=sha-<short_sha>` in `.env` and skip
  the `autoupdate` profile.

---

# Operating the validator

The rest of this page covers day-2 operating concerns: peer discovery,
auto-update mechanics, disk-bloat maintenance, alternatives to Docker,
and how to point at your own subtensor instead of the public endpoint.

## Peer discovery

Both consensus loops discover peers automatically — no peer-list env
required. The flow:

1. **Your daemon publishes its identity.** `GET /identity` on port
   9100 returns a fresh EIP-712 signed payload binding
   `(evm_address, hotkey, axon_url)`. Each request regenerates the
   signature so it's never stale.
2. **You publish your axon URL.** `VALIDATOR_AXON_URL` (Step 8) is what
   the signed payload claims. Also call `btcli` to register your axon
   on the Bittensor metagraph so other validators can find you.
3. **Other validators discover you.** Their `ProtocolConfig.refresh_loop`
   (default 60s tick) walks the metagraph axon list, probes each
   `/identity`, verifies the EIP-712 signature, and cross-checks the
   recovered EVM address is in `ValidatorRegistry.getValidators()` and
   that the hotkey matches the metagraph.

What this gives you operationally:

- **No coordinated restart when a new validator joins.** The new
  validator's EVM gets added on-chain, they start their daemon, others
  pick them up within one refresh tick.
- **No peer-list env to maintain.** Discovery plus the on-chain
  registry are the source of truth.
- **IP changes are self-served.** A validator changing hosts just
  updates `VALIDATOR_AXON_URL` and restarts; the signed `/identity`
  payload re-publishes the new URL automatically.

### Verifying discovery is working

```bash
# Confirm your daemon publishes a valid identity
curl http://localhost:9100/identity

# Confirm your daemon sees other peers (via /consensus/info)
curl http://localhost:9100/consensus/info
# .peers should list discovered peers; refreshes on each ProtocolConfig tick.
```

## Auto-update mechanics

The default `MINOTAUR_IMAGE_TAG=stable` (in `.env.example`) plus the
optional Watchtower container together give you hands-off updates:

1. New commit lands on `main` → `docker-publish.yml` builds + pushes
   `:latest` and `:sha-<short>` (immutable per-commit) to GHCR.
2. The new image runs on the subnet team's prod for a soak period.
3. When the team is happy with the soak, the `promote-stable.yml`
   workflow re-tags `:sha-<short>` as `:stable` on GHCR.
4. Your Watchtower polls GHCR within the next interval, pulls the new
   image, recreates the `validator` and `api` containers with the new
   SHA. ~30-60 seconds of downtime during the recreate.

The poll interval is controlled by `WATCHTOWER_POLL_INTERVAL` (seconds)
in your `.env`. The canonical default is `3600` (1 hour). **During the
early-network shake-out phase, set `WATCHTOWER_POLL_INTERVAL=300`
(5 minutes)** so audit fixes and config changes propagate faster.

Once the network is stable and `:stable` promotions are infrequent,
bump it back up to the hourly default to save GHCR bandwidth.

If you prefer manual control, leave the `autoupdate` profile off and
update on the team's announced cadence:

```bash
docker compose pull validator api
docker compose up -d --force-recreate validator api
```

To pin a specific SHA (opt out of auto-update entirely without removing
Watchtower):

```bash
# In .env:
MINOTAUR_IMAGE_TAG=sha-abc1234
```

then `docker compose up -d` — Watchtower won't update a container
whose image tag isn't tracking `:stable`.

## Required maintenance cron (Anvil disk bloat)

Anvil's overlay filesystem grows fast even with tmpfs mounted at
`/root` and `/tmp`. A production deployment in May 2026 measured
**~40-50 GB per fork per day**; the rate has grown over time as the
chain head moves further from the fork block and as user/simulation
load increases. With three forks that's ~150 GB/day of bloat. Without
a frequent recycle, a 200 GB volume fills in well under two days and
the host OS hangs (status check: impaired) once the disk hits 100% —
at which point SSH is dead and the only recovery is a force stop+start
of the VM.

Install this cron — **every 6 hours**, not daily:

```
0 */6 * * * root docker compose -f /home/<user>/minotaur/docker-compose.yml rm -fsv anvil anvil-base anvil-btevm && docker compose -f /home/<user>/minotaur/docker-compose.yml up -d anvil anvil-base anvil-btevm
```

(Adjust the path to where you put `docker-compose.yml` in Step 7.)

At every-6h cadence, max accumulation between recycles is ~37 GB
across three forks, which fits comfortably in 200 GB with other
services taking ~10-15 GB. If you skip a recycle (cron failure, host
unreachable, manual stop without restart), the disk can fill in 12-15
hours from there — monitor `df -h /` and treat low disk as a paging
event. If the rate grows further (more chains added, much higher
load), drop the cadence to every 4 or 3 hours.

Each recycle window drops in-flight Anvil state for ~60 seconds while
the containers restart. During that window your follower cannot
re-simulate proposals — the leader's order-consensus tick will see a
missing signature from you and fall back to the remaining peers. If
the rest of the active validator set is small enough that quorum
needs your signature, stagger your cron a few minutes offset from
peers to avoid simultaneous reorg pauses.

**If you hit a disk-full OS hang**: the SSH daemon is dead at that
point, so `docker compose down`, cron tightening, or any in-VM
cleanup won't help. The only recovery is a force stop+start at the
hypervisor layer (on AWS: `aws ec2 stop-instances --force
--instance-ids <id>`, wait for `stopped`, then `start-instances`).
EBS-backed instances preserve all state across this; containers with
`restart: unless-stopped` come back automatically. Once the host is
up, immediately `docker compose rm -fsv` the anvil services to release
their snapshot overlays — `docker system prune` alone won't reclaim
them.

## Running your own subtensor

The public `wss://entrypoint-finney.opentensor.ai:443` (substrate /
metagraph) and `https://lite.chain.opentensor.ai` (BT EVM RPC)
endpoints rate-limit **per source IP**. Operators sharing egress with
other tenants in a datacenter colo often see throttling at modest
validator load — the cap is consumed by neighbors before their own
traffic lands.

If you already run your own `subtensor` node — and if you operate
Bittensor validators at scale you probably do — **the same node also
serves the BT EVM JSON-RPC on the same port**. Frontier is built into
the subtensor binary; no separate process, no separate port, no extra
flag beyond the standard `--rpc-external --rpc-cors all`. The
substrate RPC port (default `9944`) accepts both substrate WS *and*
Ethereum-shaped JSON-RPC (`eth_call`, `eth_getStorageAt`, etc.) on the
same listener.

To point this validator stack at your own subtensor:

```bash
# In ~/minotaur/.env
SUBTENSOR_URL=ws://your-subtensor-host:9944
BITTENSOR_EVM_UPSTREAM_RPC_URL=http://your-subtensor-host:9944
```

(Use `wss://` / `https://` if you've terminated TLS in front of your
node. Anvil's `--fork-url` does HTTP polling, so use `http`/`https`
for the EVM upstream — not `ws`/`wss`.)

On the subtensor node itself, the standard `opentensor/subtensor`
mainnet run script already does what you need:

```
subtensor --chain finney --rpc-external --rpc-cors all \
  --rpc-max-connections 10000
```

See [opentensor/subtensor scripts/run/subtensor.sh](https://github.com/opentensor/subtensor/blob/main/scripts/run/subtensor.sh)
for the upstream reference. Verify with
`cast chain-id --rpc-url http://your-subtensor-host:9944` — it should
return `964` (BT EVM mainnet). If it doesn't, you're either pointed
at a non-finney chain or at a non-subtensor node.

## Running without Docker (advanced)

If you prefer Anvil under systemd plus the daemon as a native Python
process, the equivalent invocations are:

```bash
anvil --host 0.0.0.0 --port 8545 --fork-url "$ETH_UPSTREAM_RPC_URL" \
  --block-time 2
anvil --host 0.0.0.0 --port 8546 --fork-url "$BASE_UPSTREAM_RPC_URL" \
  --chain-id 8453 --no-storage-caching --block-time 2
anvil --host 0.0.0.0 --port 8547 --fork-url "$BITTENSOR_EVM_UPSTREAM_RPC_URL" \
  --chain-id 964 --no-storage-caching --block-time 2

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

Wrap each in its own systemd unit with `Restart=on-failure`. The
Anvil disk-bloat issue described in the maintenance cron section
above applies either way — adjust the cron to bounce your systemd
units instead of `docker compose rm -fsv`.

Example systemd unit:

```ini
# /etc/systemd/system/minotaur-validator.service
[Unit]
Description=Minotaur Validator Daemon
After=network-online.target docker.service

[Service]
EnvironmentFile=/etc/minotaur/env
ExecStart=/opt/minotaur/.venv/bin/python -m minotaur_subnet.validator.main \
  --port 9100 --epoch-seconds 1200
Restart=on-failure
RestartSec=5
User=minotaur

[Install]
WantedBy=multi-user.target
```

Put shared env (RPC URLs, registry addresses, validator key) in
`/etc/minotaur/env` with mode `0600`. Enable with
`systemctl enable --now minotaur-validator`. Repeat the pattern for
each Anvil unit.

## Operator help + reporting issues

- File issues at https://github.com/subnet112/minotaur_subnet/issues
- Current image: `ghcr.io/subnet112/minotaur-validator:stable`
- Current contract addresses live in
  [`docs/operator/network-reference.md`](../operator/network-reference.md)
- The validator's HTTP surface on port 9100 exposes:
  - `/health`, `/identity`, `/consensus/proposal` (load-bearing)
  - `/weights`, `/weights/history`, `/blockloop/status`,
    `/consensus/info`, `/leader` (ops-debug)

## Next steps

- Review [Configuration](./configuration.md) for the full env-var
  reference.
- See [Troubleshooting](./troubleshooting.md) for common failure modes.
- Check the [Solver Guide](../solver/solver_guide.md) to understand
  what miners submit and how scoring works.
