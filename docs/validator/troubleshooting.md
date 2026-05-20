# Validator Troubleshooting

Common issues and solutions for running the Minotaur validator.

## Anvil Connection Issues

**Symptom**: Simulation failures, "connection refused" errors, or plans that never score.

- Verify `ANVIL_RPC_URL` is set and reachable:
  ```bash
  curl -X POST -H "Content-Type: application/json" \
    --data '{"jsonrpc":"2.0","method":"eth_blockNumber","params":[],"id":1}' \
    $ANVIL_RPC_URL
  ```
- If using Alchemy or Infura, confirm your API key is valid and has not exceeded rate limits.
- For local testnet, ensure the Anvil container is healthy:
  ```bash
  docker compose ps anvil
  ```
- Check that the Anvil fork is on the expected chain (mainnet = chain ID 1, local = 31337).

**Symptom**: Simulations hang or time out.

- Anvil has a 2-second block time by default. The simulator calls `evm_mine` after sending transactions. If the RPC endpoint is slow, simulations may time out.
- Increase logging to `DEBUG` to see simulation details: `export LOG_LEVEL=DEBUG`

## Subtensor Sync Issues

**Symptom**: Validator cannot read metagraph, weight emission fails, or "connection refused" to subtensor.

- Verify `SUBTENSOR_URL` is correct:
  - Mainnet: `wss://entrypoint-finney.opentensor.ai:443`
  - Local testnet: `ws://localhost:9944`
- Test the connection:
  ```bash
  # Using wscat
  wscat -c "$SUBTENSOR_URL" -x '{"id":1,"jsonrpc":"2.0","method":"system_health","params":[]}'
  ```
- For local testnet, confirm the subtensor container is running and healthy:
  ```bash
  docker compose ps subtensor
  ```
- Archive subtensor nodes can lag 20+ hours behind. For chain-head queries (block hashes, current block), finney is preferred.

## Leader Election

**Symptom**: Validator is running but not processing orders (BlockLoop idle).

- Only the **leader** runs the BlockLoop. Check if this validator is the leader:
  ```bash
  curl http://localhost:9100/leader
  ```
- The leader is the validator with the highest TAO stake on subnet 112. If you have less stake than other validators, you will be a follower.
- For local testnet or development, set `FORCE_LEADER=1` to bypass stake-based election.
- On leader change, all in-flight work is dropped and the new leader reprocesses from scratch. This is expected behavior.

## Consensus Failures

**Symptom**: Plans are scored but never relayed on-chain. "Quorum not reached" in logs.

- Check peer configuration:
  ```bash
  curl http://localhost:9100/consensus/info
  ```
- Verify `VALIDATOR_PEERS` contains all peer validators in the correct format: `0xAddress@http://host:port`
- Ensure all peers are reachable from this validator (network/firewall rules).
- Check the live quorum value: `make get-quorum-<chain>` (or `cast call $VALIDATOR_REGISTRY 'quorumBps()(uint256)'`). 10000 (100%) requires every peer to sign — see [Quorum management](../operator/quorum-management.md) to change it via `make set-quorum-<chain> BPS=...`.
- For local testnet only: set `QUORUM_BPS_OVERRIDE` to force a local value without going through the registry. Production deployments should leave it unset.
- Verify `VALIDATOR_PRIVATE_KEY` is set and valid. The validator uses this to sign EIP-712 consensus messages.
- Followers independently re-simulate and re-score. If a follower's scores do not both exceed threshold, it will not sign.

## JS Scoring Engine Issues

**Symptom**: JS scores are always 0.0, NaN, or scoring errors in logs.

- Verify Node.js 20.x is installed:
  ```bash
  node --version  # Should be v20.x
  ```
- The JS engine runs app scoring code in a Node.js sandbox. Check that the app's JS code exports the required functions:
  ```javascript
  module.exports = { config, manifest, score };
  ```
- The `score(plan, state, context)` function receives:
  - `plan` -- ExecutionPlan dict (with `metadata`, `interactions`, etc.)
  - `state` -- Structured IntentState export with `raw_params`, `control`, and
    `typed_context` (plus compatibility `extra` / `rawParams` aliases)
  - `context` -- Full context with `context.simulation` (token transfers, gas, state changes), `context.state`, and `context.oracle`
- Common mistake: Writing `score(plan, simulation, state)` -- the second parameter is `state`, not `simulation`. Simulation data is in `context.simulation`.
- Check the validator logs for JS execution errors. Enable debug logging:
  ```bash
  export LOG_LEVEL=DEBUG
  ```

## BlockLoop Not Processing Orders

**Symptom**: Orders are submitted but never processed.

- Confirm you are the leader (see "Leader Election" above).
- Check BlockLoop status:
  ```bash
  curl http://localhost:9100/blockloop/status
  ```
- Verify orders exist and are in OPEN status:
  ```bash
  curl http://localhost:9100/orders
  ```
- Check that app definitions are loaded:
  ```bash
  curl http://localhost:9100/health
  ```
  The response should show the number of loaded intents.
- If using `--store-path`, verify the file exists and is readable.
- Review logs for errors during plan generation, simulation, or scoring.

## Weight Emission Not Working

**Symptom**: Validator runs but never emits weights on-chain.

- Weights are emitted once per epoch (default: 60 seconds, configurable via `--epoch-seconds`).
- A champion miner must exist -- weights are only emitted when a solver has been submitted and accepted:
  ```bash
  curl http://localhost:9100/weights
  curl http://localhost:9100/weights/history
  ```
- Verify Bittensor wallet configuration:
  - `WALLET_NAME` and `HOTKEY_NAME` must match a wallet with a registered hotkey on subnet 112.
  - The wallet directory must be accessible (default: `~/.bittensor/wallets/`).
- On Bittensor 10.x, `set_weights` uses commit-reveal. On fast local chains, you may need to wait a few blocks between weight emissions.

## Miner Submissions Rejected

**Symptom**: Miner solver submissions fail or are not adopted.

- Check submission endpoints on the API service:
  ```bash
  curl http://localhost:8080/v1/submissions
  ```
- Solver code goes through three screening stages before adoption. Check logs for rejection reasons.
- Verify the miner is registered on the subnet and its hotkey appears in the metagraph.
- Ensure the miner is pointing at the correct API URL for `/v1/submissions*`.

## Port Conflicts

**Symptom**: "Address already in use" on startup.

- Check what is using the port:
  ```bash
  lsof -i :9100
  ```
- Change the port with `--port`:
  ```bash
  python -m minotaur_subnet.validator.main --port 9101
  ```
- For local testnet, port 9100 is used internally by Docker networking and is not exposed to the host by default.

## Docker / Local Testnet Issues

**Symptom**: Containers fail to start or are unhealthy.

- Check container status and logs:
  ```bash
  docker compose ps
  docker compose logs validator
  docker compose logs anvil
  ```
- The validator depends on the API service being healthy. If the API is unhealthy, the validator will not start.
- Ensure the `.env` file in `platform/local_testnet/` has valid `ALCHEMY_RPC_URL` and `BASE_ALCHEMY_RPC_URL` values.
- If containers are stuck, do a clean restart:
  ```bash
  make testnet-down
  make testnet-up
  ```
- The init container runs once on startup (registers subnet, deploys contracts). Check its logs if other services fail:
  ```bash
  docker compose logs init
  ```

## Insufficient TAO Balance

**Symptom**: Registration or weight emission fails with balance errors.

- Check your balance:
  ```bash
  btcli wallet balance --wallet.name my-validator --subtensor.network finney
  ```
- Subnet 112 registration requires a burn fee. Ensure you have enough TAO.
- For local testnet, the init container handles registration and funding automatically.

See also: [Configuration](./configuration.md), [Quickstart](./quickstart.md).
