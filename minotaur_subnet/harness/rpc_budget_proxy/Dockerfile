# Deterministic JSON-RPC counting/budget proxy — trusted validator sidecar.
#
# Minimal image: just python + aiohttp. It runs the proxy module, which sits
# between an untrusted benchmark solver and the Anvil fork(s) and meters the
# solver's RPC work against a fleet-uniform integer budget.
#
# Build (from repo root, so the package path resolves):
#   docker build -f minotaur_subnet/harness/rpc_budget_proxy/Dockerfile \
#       -t minotaur-rpc-budget-proxy .
#
# Run (example, observe mode, two upstreams):
#   docker run -p 8645:8645 \
#       -e UPSTREAMS='{"eth":"http://anvil-eth:8545","base":"http://anvil-base:8545"}' \
#       -e BUDGET_PROXY_MODE=observe \
#       minotaur-rpc-budget-proxy
FROM python:3.12-slim

# Pin aiohttp to the repo's consensus-pinned version for reproducible builds.
RUN pip install --no-cache-dir aiohttp==3.13.3

WORKDIR /app

# Only what this sidecar needs. The proxy imports just this subpackage plus the
# stdlib-only shared backoff primitive (minotaur_subnet.rpc_backoff — no web3 /
# no other repo deps), so the image stays tiny. A stub package marker makes the
# module path `minotaur_subnet.harness.rpc_budget_proxy` resolve.
COPY minotaur_subnet/harness/rpc_budget_proxy/ /app/minotaur_subnet/harness/rpc_budget_proxy/
COPY minotaur_subnet/rpc_backoff.py /app/minotaur_subnet/rpc_backoff.py
RUN mkdir -p /app/minotaur_subnet/harness \
    && touch /app/minotaur_subnet/__init__.py /app/minotaur_subnet/harness/__init__.py

# Data-plane port (faces the untrusted solver). The control plane is on the
# same port here; isolate networks at the orchestration layer.
EXPOSE 8645

ENV LISTEN_HOST=0.0.0.0 \
    LISTEN_PORT=8645 \
    BUDGET_PROXY_MODE=observe

ENTRYPOINT ["python", "-m", "minotaur_subnet.harness.rpc_budget_proxy.proxy"]
