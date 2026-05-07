FROM python:3.12-slim-bookworm

# Install Node.js 20.x, git, Docker CLI, and Foundry
# Docker CLI is needed by the local screening/benchmark worker containers.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates git docker.io \
    && curl -L https://foundry.paradigm.xyz | bash \
    && /root/.foundry/bin/foundryup \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && apt-get clean && rm -rf /var/lib/apt/lists/*
ENV PATH="/root/.foundry/bin:${PATH}"

WORKDIR /app

# Install Python dependencies
COPY minotaur_subnet/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir bittensor

# Copy contracts and install Solidity dependencies
COPY contracts/ /app/contracts/
RUN cd /app/contracts && forge install 2>/dev/null || true

# Copy application code
COPY minotaur_subnet/ /app/minotaur_subnet/
COPY platform/ /app/platform/

# Ensure the package is importable
ENV PYTHONPATH=/app

EXPOSE 8080 8090 9100

# Default: run the API server
CMD ["python", "-m", "minotaur_subnet.api.server"]
