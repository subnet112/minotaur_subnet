# The "anvil binary" for Bittensor: a Chopsticks fork of subtensor + the
# anvil-dialect JSON-RPC sidecar the Python SubtensorSimulator drives.
# Build context = repo root:  docker build -f tools/chopsticks-sim/Dockerfile .
FROM node:22-slim

WORKDIR /app
# deps first for layer caching (installs @acala-network/chopsticks + @polkadot/*)
COPY tools/chopsticks-sim/package.json ./package.json
RUN npm install --omit=dev --no-audit --no-fund

# app code
COPY tools/chopsticks-sim/chopsticks_anvil.mjs ./
COPY tools/chopsticks-sim/chopsticks_rpc_server.mjs ./
COPY tools/chopsticks-sim/StakeMeter.deployed.hex ./

ENV PORT=8545 CK_INNER_PORT=8100
EXPOSE 8545
CMD ["node", "chopsticks_rpc_server.mjs"]
