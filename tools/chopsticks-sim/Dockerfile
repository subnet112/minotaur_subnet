# The "anvil binary" for Bittensor: a Chopsticks fork of subtensor + the
# anvil-dialect JSON-RPC sidecar the Python SubtensorSimulator drives.
# Build context = repo root:  docker build -f tools/chopsticks-sim/Dockerfile .
#
# TRIXIE, not bookworm. `sqlite3` (required for chopsticks' --db fork cache, see
# package.json) ships a PREBUILT native binding linked against GLIBC_2.38, and
# node:22-slim is Debian bookworm at glibc 2.36 — the module installs cleanly and
# then dies at `require` with ERR_DLOPEN_FAILED, so the image builds green and
# fails only at runtime, only when CK_DB is set. node:24-slim is bookworm too;
# trixie carries glibc 2.41. Verified: `node -e "require('sqlite3')"` loads here.
FROM node:22-trixie-slim

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
