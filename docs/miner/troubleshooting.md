# Miner Troubleshooting

## Solver not registering with aggregator
- Verify `MINER_API_KEY` is set correctly
- Check aggregator connectivity: `curl $AGGREGATOR_URL/health`
- Ensure solver host is accessible from aggregator (use `host.docker.internal` if aggregator is in Docker)
- Review logs for registration errors

## No quotes being provided
- Verify Ethereum RPC URL is configured: `ETHEREUM_RPC_URL` or `ALCHEMY_API_KEY`
- Check RPC URL is accessible and has sufficient rate limits
- Verify solver is registered and active (miner endpoints require `MINER_API_KEY`):
  - `curl -H "X-API-Key: $MINER_API_KEY" "$AGGREGATOR_URL/v1/solvers/$SOLVER_ID"`
- Review solver logs for quote request errors

## Rate limiting errors
- Configure a reliable RPC provider (Infura, Alchemy, or local node)
- Consider upgrading your RPC plan for higher rate limits
- Check `ETHEREUM_RPC_URL` is set correctly
- Monitor RPC usage and adjust if needed

## Solver not responding
- Check solver is running: `curl http://localhost:$PORT/health`
- Verify port is not in use: `lsof -i :$PORT`
- Review solver logs for errors
- Ensure solver host is correct (especially if aggregator is in Docker)

## Multiple solvers not working
- Verify ports don't conflict: `MINER_BASE_PORT + solver_index`
- Check each solver is on a different port
- Ensure all solvers are registered (requires `MINER_API_KEY`):
  - `curl -H "X-API-Key: $MINER_API_KEY" "$AGGREGATOR_URL/v1/solvers"`
- Review logs for each solver instance

## Token discovery issues
- Check RPC URL is accessible
- Verify token lists are reachable (Uniswap, CoinGecko)
- Review logs for token discovery errors
- Check `MOCK_SOLVER_TOKEN_LIMIT` if too many tokens are being discovered

## Order execution failures
- Verify wallet has sufficient balance (Bittensor mode)
- Check transaction gas limits
- Review on-chain transaction status
- Ensure token approvals are set correctly

## Port conflicts
- Check processes holding ports: `lsof -i :8000 -i :8001`
- Stop or reconfigure conflicting services
- Use different `MINER_BASE_PORT` if needed

## Aggregator connection issues
- Verify `AGGREGATOR_URL` is correct
- Check network connectivity
- Ensure aggregator is running: `curl $AGGREGATOR_URL/health`
- Note: `neurons/miner.py` currently uses fixed per-request timeouts (there is no `AGGREGATOR_TIMEOUT` setting for the miner).

## Bittensor mode issues
- Verify wallet is registered on the subnet
- Check wallet has sufficient balance
- Ensure `NETUID` matches the target subnet
- Verify `SUBTENSOR_NETWORK` is correct

## Simulation mode issues
- Verify `MINER_ID` is set
- Check generated hotkey is unique
- Review logs for hotkey generation errors

See also: [Configuration](./configuration.md), [Quickstart](./quickstart.md).

