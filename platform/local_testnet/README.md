# Local Demo Runbook

Use this stack when you want the full Minotaur demo locally without touching
real Bittensor.

## Safe Defaults

- The API container runs with `MVP_DEMO_MODE=1`.
- Native Bittensor proxy execution is disabled by default.
- `SUBTENSOR_URL` resolves to the Docker-local subtensor at `ws://subtensor:9944`.
- If you enable native proxy execution but point it at a non-local target, API
  startup should fail fast.

## Start

```bash
cd platform/local_testnet
cp .env.example .env
```

Edit `.env` and set:

```bash
ALCHEMY_RPC_URL=https://eth-mainnet.g.alchemy.com/v2/your_key
BASE_ALCHEMY_RPC_URL=https://base-mainnet.g.alchemy.com/v2/your_key
```

If your machine already has local chains on the default ports, also override the
host bindings in `.env`, for example:

```bash
HOST_ANVIL_ETH_PORT=18545
HOST_SUBTENSOR_WS_PORT=19944
HOST_SUBTENSOR_RPC_PORT=19945
```

Then launch the stack:

```bash
make testnet-up
```

Expected endpoints:

- Frontend: `http://localhost:4000`
- API: `http://localhost:8080`
- Relayer: `http://localhost:8091`
- Ethereum Anvil fork: `http://localhost:8545`
- Base Anvil fork: `http://localhost:8546`
- Local subtensor: `ws://localhost:9944`

## Demo Prep

Use the repo-root helper when you want a presenter-friendly startup plus a real
end-to-end verification of the seeded flagship app:

```bash
make demo-prep
```

This:

- starts the local stack non-interactively
- waits for API, relayer, and block loop health
- finds the seeded `DexAggregatorApp`
- creates and funds a managed wallet
- prepares, quotes, submits, and fills a real demo swap
- verifies the relayed transaction on-chain
- prints the important IDs you may want during the demo

If the stack is already running, you can run just:

```bash
make demo-check
```

Successful output should include:

- seeded app ID and contract address
- managed wallet address
- order ID
- relayed transaction hash
- route summary
- output balance evidence showing the swap actually worked

If a required host port is already occupied, `make demo-prep` now fails fast
before Docker startup with a clearer preflight error.

## Private Solver Repo Demo

If you want the local screening pipeline to clone a private solver repo, give
the API service its own read-only HTTPS credential in `platform/local_testnet/.env`:

```bash
SUBMISSION_ALLOWED_REPO_HOSTS=github.com
SUBMISSION_GIT_CLONE_ALLOWED_HOSTS=github.com
SUBMISSION_GIT_CLONE_USERNAME=x-access-token
SUBMISSION_GIT_CLONE_PASSWORD=github_pat_your_read_only_token
```

Rules for this demo path:

- use a dedicated read-only credential scoped only to the solver repo
- do not reuse a miner's personal GitHub account credential on API or validator infrastructure
- keep production submissions public so independent validators can clone the pinned commit without shared secrets

## Stop

```bash
make testnet-down
```

## Optional Native Bittensor Demo

Only do this for local testing against the Docker subtensor in this stack.

In `platform/local_testnet/.env`:

```bash
ENABLE_NATIVE_BITTENSOR_PROXY=1
NATIVE_BITTENSOR_NETWORK=ws://subtensor:9944
```

Keep `MVP_DEMO_MODE=1`. Do not point the demo stack at `finney` or another
real network.
