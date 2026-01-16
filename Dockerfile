FROM python:3.12-slim-bookworm

# Keep Python predictable in containers
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app

WORKDIR /app

# System deps:
# - docker.io provides docker CLI (required by validator OrderSimulator when using host docker socket)
# - git/curl for optional workflows and debugging
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates \
      curl \
      git \
      docker.io \
    && rm -rf /var/lib/apt/lists/*

# Install python deps first for better layer caching
COPY requirements.txt /app/requirements.txt
RUN python -m pip install -r /app/requirements.txt

# Copy repo
COPY . /app

# Entrypoint that can start miner or validator
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Common ports (miner starts solvers on MINER_BASE_PORT + index)
EXPOSE 8000 8001 8002 4100 4000

ENTRYPOINT ["/entrypoint.sh"]
CMD ["help"]




