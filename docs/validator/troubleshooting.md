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
- Verify peer discovery is finding the other validators: the `peers` field in `/consensus/info` should be non-empty. If it's empty, see [No peers discovered](#no-peers-discovered) below.
- Ensure peer axon URLs are reachable from this validator (network/firewall rules).
- Subnet-team operators only: if `ORDER_CONSENSUS_PEERS` or `CHAMPION_CONSENSUS_PEERS` is set (named manual override, used in our prod where metagraph axons aren't published yet), confirm the `addr@url` format is correct. Third-party validators should leave both unset and let discovery handle it.
- Check the live quorum value: `make get-quorum-<chain>` (or `cast call $VALIDATOR_REGISTRY 'quorumBps()(uint256)'`). 10000 (100%) requires every peer to sign — see [Quorum management](../operator/quorum-management.md) to change it via `make set-quorum-<chain> BPS=...`.
- For local testnet only: set `QUORUM_BPS_OVERRIDE` to force a local value without going through the registry. Production deployments should leave it unset.
- Verify `VALIDATOR_PRIVATE_KEY` is set and valid. The validator uses this to sign EIP-712 consensus messages.
- Followers independently re-simulate and re-score. If a follower's scores do not both exceed threshold, it will not sign.

## No peers discovered

**Symptom**: `curl http://localhost:9100/consensus/info` returns an empty `peers` list, or `/consensus/info` shows `peer-mode=discovered` but the daemon log says `ProtocolConfig: peer discovery: probed 0 candidates → 0 verified`.

Discovery requires four things to line up. Check each in order:

1. **Your `VALIDATOR_AXON_URL` is set and reachable**. From any other host: `curl $VALIDATOR_AXON_URL/identity`. Should return a JSON payload, not a 503. If 503, the daemon is missing one of: bittensor wallet (no `my_hotkey`), `VALIDATOR_AXON_URL` env, or a signing key.
2. **Your hotkey is on the metagraph with the correct axon URL.** Run `btcli subnet metagraph --netuid 112 --subtensor.network finney` and find your hotkey. The `axon` column must match `VALIDATOR_AXON_URL`. If it's wrong, call `btcli` to update it (or your bittensor wallet's `serve_axon` runner).
3. **Your EVM signing address is in the on-chain `ValidatorRegistry`.** Run `cast call $VALIDATOR_REGISTRY 'isValidator(address)(bool)' 0xYourEvmAddress --rpc-url $RPC_URL` — must return `true` on every chain you operate on. If false, see [validator quickstart Step 4](./quickstart.md#step-4-get-onboarded-to-the-on-chain-validatorregistry) for the handshake with the registry owner.
4. **Other validators' `/identity` endpoints are reachable from your host.** Test: `curl <their-axon-url>/identity`. If unreachable, it's a network issue (firewall, NAT).

**Symptom**: identity probe returns valid JSON but discovery still rejects it. Check the daemon logs for `Identity probe ... recovered EVM X but it is not in ValidatorRegistry.getValidators() — rejecting`. That's the on-chain handshake (step 3) for the OTHER validator — they need to be added to the registry too.

## JS Scoring Engine Issues

**Symptom**: JS scores are always 0.0, NaN, or scoring errors in logs.

- Verify Node.js 18.x is installed:
  ```bash
  node --version  # Should be v18.x
  ```
  (The validator image ships Node 18 — the isolated-vm scoring addon is built
  against the Node 18 ABI; do not swap in Node 20.)
- The JS engine runs app scoring code in an isolated V8 isolate (isolated-vm), not Node's built-in `vm`. Check that the app's JS code exports the required functions:
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
- Verify orders exist and are in OPEN status (orders live on the API service,
  not the `:9100` daemon):
  ```bash
  curl http://localhost:8080/v1/orders
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

- Weight *commits* are now **tempo-aligned** (PR #524): they fire in a short window (`TEMPO_EMIT_LEAD_BLOCKS`, default 20 blocks) just before each tempo epoch step, not every `--epoch-seconds`. This is expected — do not read "no commit this minute" as a fault. Check `/health` → `emit_schedule` for the mode and next boundary.
- A champion miner must exist -- weights are only emitted when a solver has been submitted and accepted:
  ```bash
  curl http://localhost:9100/weights
  curl http://localhost:9100/weights/history
  ```
- Verify Bittensor wallet configuration:
  - `WALLET_NAME` and `HOTKEY_NAME` must match a wallet with a registered hotkey on subnet 112.
  - The wallet directory must be accessible (default: `~/.bittensor/wallets/`). A wallet mount not readable by uid 1000 is the classic cause of `weights_emitter_configured=false` (a silent dead emitter) — see `BT_WALLET_PATH` in [Configuration](./configuration.md#bittensor-identity).
- On Bittensor 10.x, `set_weights` uses commit-reveal, and the chain keeps only **one** pending commit per validator per tempo epoch — earlier commits in the same tempo are silently discarded. That is exactly why emission is tempo-aligned; the old wall-clock cadence could commit 2–3×/tempo and a champion dethroned mid-tempo could reveal nothing. On fast local chains you may still need to wait a few blocks between emissions.

### Boot-time chain hiccup / phantom leader (PR #542)

**Symptom**: `/identity` returns 503, `weights_emitter_configured=false`, or the validator behaves as an unelected "leader" after a transient chain error at container start.

- Bittensor bring-up now **retries forever** with jittered exponential backoff (5s → 5min) on a background thread instead of latching a dead daemon on the first websocket failure. Check `/health` → `bt_init` (`{configured, ok, attempts, error, error_at, retrying}`); status reports `"degraded"` (still HTTP 200) while configured-but-broken.
- **Leadership now fails closed**: whenever `SUBTENSOR_URL` is configured, `_is_leader` is `False` until a metagraph sync actually succeeds — a failed initial sync stays follower (it no longer "assumes leader"). The real leader is promoted at most one sync cycle late. `FORCE_LEADER=1` still overrides for local testnets.
- If `bt_init.ok` never flips true, fix the underlying `SUBTENSOR_URL` / wallet issue; the daemon self-heals once the dependency recovers — no restart needed.

## Miner Submissions Rejected (leader-only)

**Symptom**: Miner solver submissions fail or are not adopted.

This section only applies if you are running the optional API service and are currently the leader (highest-stake validator). Third-party validators running only the canonical `platform/validator/` stack don't accept submissions — the leader does. If you're not the leader, miners shouldn't be hitting your box for submissions.

If you are the leader:

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
- The validator daemon waits for its three Anvil forks to report healthy before starting (see `depends_on` in `platform/validator/docker-compose.yml`). On a first cold start this can take 60-90 seconds — anvil-btevm in particular waits on a public RPC. If you see "dependency failed to start", wait a bit and `docker compose up -d` again; the `start_period` on each anvil healthcheck gives them grace time on subsequent retries.
- The validator daemon and the optional API service are independent processes — neither depends on the other for startup.
- Ensure the `.env` file in `platform/local_testnet/` has valid `ALCHEMY_RPC_URL` and `BASE_ALCHEMY_RPC_URL` values (for the local-testnet path; the canonical validator stack reads from `platform/validator/.env`).
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
