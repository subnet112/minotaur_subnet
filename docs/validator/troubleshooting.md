# Validator Troubleshooting

## No weights published
- Ensure the aggregator has pending orders available for validation
- Verify the miner hotkey appears in the metagraph (registered miner)
- Check that `VALIDATOR_API_KEY` is set correctly
- Verify Docker is running (required for order simulation)
- Ensure `SIMULATOR_RPC_URL` is configured and accessible

## Aggregator health shows degraded
- Check `AGGREGATOR_URL` and connectivity; try `curl $AGGREGATOR_URL/health`
- Inspect timeouts and retry settings: `AGGREGATOR_TIMEOUT`, `AGGREGATOR_MAX_RETRIES`
- Verify `VALIDATOR_API_KEY` is valid and has proper permissions

## No orders being validated
- Check that the aggregator has pending orders (validator endpoints require `VALIDATOR_API_KEY`):
  - `curl -H "X-API-Key: $VALIDATOR_API_KEY" "$AGGREGATOR_URL/v1/validators/orders?validator_id=YOUR_ID"`
- Verify Docker is running and the simulator image is available
- Check `SIMULATOR_RPC_URL` is accessible and has sufficient rate limits
- Review logs for simulation errors or timeouts

## Simulation failures
- Ensure Docker daemon is running: `docker ps`
- Check simulator image exists: `docker images | grep mino-simulation`
- Verify RPC URL is accessible and has sufficient rate limits
- Check `SIMULATOR_TIMEOUT_SECONDS` if simulations are timing out
- Review logs for specific error messages

## UID mapping warnings
- `miner_id` must be a valid hotkey (SS58) that exists in the subnet's metagraph

## Tests fail with "Connection refused"
- Ensure required services are running; wait 10–15s after starting validators
- Verify with `curl` and check logs under `logs/`

## "Insufficient balance" errors (local chains)
- Re‑run local Subtensor setup to fund wallets
- Check balances with `btcli w balance --wallet.name <name> --subtensor.network <net>`

## Port conflicts
- Check processes holding ports: `lsof -i :8000 -i :9999`
- Stop or reconfigure conflicting services

## Mock services not responding
- Tail logs: `tail -f logs/*.log`
- Confirm processes are running (`ps aux | grep <name>`) and restart if needed

