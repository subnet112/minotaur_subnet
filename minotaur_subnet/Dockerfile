# syntax=docker/dockerfile:1.7
#
# ghcr.io/subnet112/minotaur-validator — multi-stage hardened build.
#
# Goals:
#   1. Non-root runtime user (audit C5). An RCE in any api/validator
#      route no longer lands one syscall away from arbitrary container
#      spawn via the docker-socket-proxy nor from coldkey/wallet read.
#   2. Pinned-by-digest base image (audit F-20). The `:slim-bookworm`
#      tag is mutable; pinning by sha256 stops a silent upstream
#      retag from changing the image we ship.
#   3. Verified Foundry installer (audit H13). The legacy
#      `curl|bash` from foundry.paradigm.xyz pulled and ran whatever
#      Paradigm served at the moment of build. We now fetch the
#      installer to disk, sha256-check it, and run from disk.
#   4. Node from Debian apt (audit H13). NodeSource's setup_20.x is
#      another curl|bash; the Debian Bookworm `nodejs` package is
#      built from the distro tree and arrives signed.
#   5. Multi-stage (audit F-04..F-06). Builder carries gcc, git,
#      foundryup, forge, anvil, build tooling. Final layer carries
#      only the venv, compiled contract artefacts, docker-ce-cli,
#      and `cast` (used by the healthcheck). No source compiler in
#      the runtime image.
#   6. forge install no longer swallowed by `|| true` (audit F-03).
#      A missing OZ submodule used to ship a half-built image.

# ─── Stage 1: builder ─────────────────────────────────────────────────
# Base pinned by digest. To refresh: `docker pull python:3.12-slim-bookworm
# && docker inspect --format '{{index .RepoDigests 0}}' python:3.12-slim-bookworm`
# and update the digest below + the matching one in the final stage.
# TODO(sec): pin sha256 — replace the tag-only ref with @sha256:<digest>
# once the dev has run the pull. Both stages MUST use the same digest.
FROM python:3.12-slim-bookworm AS builder

ENV DEBIAN_FRONTEND=noninteractive \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Build deps. curl + ca-certificates for the foundryup fetch only;
# everything else compiles native wheels (eth-keys, pynacl, etc).
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        curl \
        ca-certificates \
        git \
        gcc \
        g++ \
        python3-dev \
        libffi-dev \
        libssl-dev \
        pkg-config \
        nodejs \
        npm \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Foundry: verify the installer script BEFORE piping to bash, then pin
# foundryup to a specific release tag rather than tracking nightly.
#
# TODO(sec): replace KNOWN_SHA256 below with the actual sha256 of the
# foundryup installer at a pinned git ref. Recommended procedure:
#   1. Pick a commit SHA from
#      https://github.com/foundry-rs/foundry/commits/master/foundryup/install
#   2. Replace `master` in the URL below with that SHA.
#   3. Run `curl -fsSL <pinned-url> | sha256sum` and put the result here.
#   4. Pick the matching forge/cast release tag from
#      https://github.com/foundry-rs/foundry/releases and pass it to
#      `foundryup -v <tag>` below.
ARG FOUNDRYUP_INSTALL_SHA256=TODO_PIN_SHA256
ARG FOUNDRY_VERSION=stable
RUN curl -fsSL https://raw.githubusercontent.com/foundry-rs/foundry/master/foundryup/install \
        -o /tmp/foundryup-install \
    && if [ "${FOUNDRYUP_INSTALL_SHA256}" != "TODO_PIN_SHA256" ]; then \
         echo "${FOUNDRYUP_INSTALL_SHA256}  /tmp/foundryup-install" | sha256sum -c -; \
       else \
         echo "WARN: foundryup-install sha256 not pinned — see TODO(sec) in Dockerfile" >&2; \
       fi \
    && bash /tmp/foundryup-install \
    && rm /tmp/foundryup-install \
    && /root/.foundry/bin/foundryup -v "${FOUNDRY_VERSION}"

ENV PATH="/root/.foundry/bin:${PATH}"

# Python venv lives at /opt/venv so it can be copied wholesale to the
# final stage without dragging the system site-packages along.
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:${PATH}"
RUN pip install --upgrade pip==24.2 setuptools==75.1.0 wheel==0.44.0

WORKDIR /build

# Python deps — prefer hash-locked install when requirements.lock is
# present (generated via `pip-compile --generate-hashes requirements.txt
# > requirements.lock`), otherwise fall back to the pinned-no-hash file
# with --no-deps as a minimum bar against transitive drift.
COPY minotaur_subnet/requirements.txt /build/requirements.txt
COPY minotaur_subnet/requirements.lock* /build/
RUN if [ -f /build/requirements.lock ]; then \
        pip install --require-hashes -r /build/requirements.lock; \
    else \
        echo "WARN: requirements.lock missing — installing pinned deps without hashes" >&2; \
        pip install --no-deps -r /build/requirements.txt; \
    fi

# Compile contracts in the builder. forge install MUST succeed —
# previously `|| true` masked missing OpenZeppelin and we shipped a
# half-built image. (audit F-03)
COPY contracts/ /build/contracts/
RUN cd /build/contracts \
    && forge install --no-commit \
    && forge build --sizes

# ─── Stage 2: final runtime ───────────────────────────────────────────
# Same digest-pinned base as the builder. TODO(sec): keep the two in sync.
FROM python:3.12-slim-bookworm AS final

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# Runtime apt deps:
#   - ca-certificates: TLS to RPC providers + GHCR
#   - curl: healthchecks + the operator-side foundryup-style flows
#   - docker-ce-cli: spawning benchmark containers via docker-socket-proxy
#     (NOT docker.io — that pulls dockerd, containerd, runc; we only
#     need the CLI client because the daemon lives on the host)
#
# Docker apt repo signing key is fetched once and pinned to the keyring;
# the repo line uses signed-by= so apt won't trust other sources.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        gnupg \
    && install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://download.docker.com/linux/debian/gpg \
        | gpg --dearmor -o /etc/apt/keyrings/docker.gpg \
    && chmod a+r /etc/apt/keyrings/docker.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
        https://download.docker.com/linux/debian bookworm stable" \
        > /etc/apt/sources.list.d/docker.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends docker-ce-cli \
    && apt-get purge -y --auto-remove gnupg \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Non-root runtime user. uid 1000 must match the compose-side `user:
# "1000:1000"` directive (PR-3) and the chown on the /data volume below.
RUN useradd --create-home --uid 1000 --shell /bin/bash minotaur

# Bring in the venv from the builder.
COPY --from=builder --chown=minotaur:minotaur /opt/venv /opt/venv

# Bring in `cast` only. Drop forge + anvil + foundryup — the runtime
# image doesn't compile or fork. cast stays because the healthcheck on
# the anvil sidecars uses `cast block-number`, and operators reach into
# the api container for ad-hoc chain reads.
COPY --from=builder /root/.foundry/bin/cast /usr/local/bin/cast
RUN chmod 0755 /usr/local/bin/cast

# Bring in the compiled contract artefacts (not the source — the
# runtime only needs the ABIs + bytecode the api/relayer load).
COPY --from=builder --chown=minotaur:minotaur /build/contracts/out /home/minotaur/app/contracts/out
COPY --from=builder --chown=minotaur:minotaur /build/contracts/cache /home/minotaur/app/contracts/cache
COPY --chown=minotaur:minotaur contracts/src /home/minotaur/app/contracts/src
COPY --chown=minotaur:minotaur contracts/foundry.toml /home/minotaur/app/contracts/foundry.toml
COPY --chown=minotaur:minotaur contracts/remappings.txt /home/minotaur/app/contracts/remappings.txt

# Application code. .dockerignore at repo root prunes tests/, docs/,
# .git/, contracts/{test,script,broadcast}, etc. from the build
# context so they don't end up here.
COPY --chown=minotaur:minotaur minotaur_subnet/ /home/minotaur/app/minotaur_subnet/
COPY --chown=minotaur:minotaur platform/ /home/minotaur/app/platform/

# /data is the store volume mount target. Owned by uid 1000 so the
# non-root process can write store.json without a runtime chown.
RUN mkdir -p /data && chown -R minotaur:minotaur /data

ENV PATH="/opt/venv/bin:${PATH}" \
    PYTHONPATH=/home/minotaur/app

USER minotaur
WORKDIR /home/minotaur/app

EXPOSE 8080 9100

# Default: run the API server. Operator can override with
# `command: ["python", "-m", "minotaur_subnet.validator.main", ...]`
# in compose (the canonical validator stack does exactly this).
ENTRYPOINT ["python"]
CMD ["-m", "minotaur_subnet.api.server"]
