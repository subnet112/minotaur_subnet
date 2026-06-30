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
# Base pinned by digest (audit F-20). The :slim-bookworm tag is mutable;
# pinning by sha256 stops a silent upstream retag from changing the image
# we ship. Refresh procedure (must update BOTH stage 1 and stage 2 below
# together — they must use the same digest):
#   docker buildx imagetools inspect python:3.12-slim-bookworm \
#     --format '{{ json .Manifest.Digest }}'
# Captured 2026-05-26 against python:3.12-slim-bookworm.
FROM python:3.12-slim-bookworm@sha256:93ab4b7fa528b25124c97bcc755415e60eb671a86b4dbe0328df2fe2d1c1193d AS builder

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
# Pinned 2026-05-26:
#   - foundryup/install at foundry-rs/foundry commit efcde6c99eb (2025-11-07)
#   - sha256 of that file: e103bb7839e0c8c65263c7971d0b6bf94ffbe2e81dff89c6b3ecb6ce2d76b71c
#   - foundryup release: v1.7.1 (2026-05-08)
# Refresh procedure (security-relevant — read carefully before bumping):
#   1. Pick a NEW commit hash from
#      https://github.com/foundry-rs/foundry/commits/master/foundryup/install
#      that you've manually reviewed.
#   2. Replace FOUNDRYUP_INSTALL_COMMIT below with it.
#   3. curl the file from the pinned commit and sha256sum it; put that
#      hash in FOUNDRYUP_INSTALL_SHA256.
#   4. Pick the matching forge/cast release tag from
#      https://github.com/foundry-rs/foundry/releases and update
#      FOUNDRY_VERSION.
ARG FOUNDRYUP_INSTALL_COMMIT=efcde6c99eb23b3aa2fec3188c1808fb6273bbcb
ARG FOUNDRYUP_INSTALL_SHA256=e103bb7839e0c8c65263c7971d0b6bf94ffbe2e81dff89c6b3ecb6ce2d76b71c
ARG FOUNDRY_VERSION=v1.7.1
RUN curl -fsSL "https://raw.githubusercontent.com/foundry-rs/foundry/${FOUNDRYUP_INSTALL_COMMIT}/foundryup/install" \
        -o /tmp/foundryup-install \
    && echo "${FOUNDRYUP_INSTALL_SHA256}  /tmp/foundryup-install" | sha256sum -c - \
    && bash /tmp/foundryup-install \
    && rm /tmp/foundryup-install \
    && /root/.foundry/bin/foundryup -i "${FOUNDRY_VERSION}"
# Note: this foundryup release uses `-i / --install <version>` (the old
# `-v <version>` flag was repurposed to "print foundryup's own version
# and exit" — see foundry-rs/foundry@efcde6c). If you bump the pinned
# install commit above, re-verify the CLI flags haven't shifted again.

ENV PATH="/root/.foundry/bin:${PATH}"

# Python venv lives at /opt/venv so it can be copied wholesale to the
# final stage without dragging the system site-packages along.
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:${PATH}"
RUN pip install --upgrade pip==24.2
# Note: setuptools + wheel come from requirements.lock (pinned by hash via
# --allow-unsafe). Bootstrap-installing them here would just be downgraded
# by the lock pass below.

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
    && forge build --sizes
# Note: `forge install` is intentionally skipped — it shells out to
# `git submodule update`, but the build context excludes .git (see
# .dockerignore). The contracts/lib/ tree is already vendored at
# image-build time (forge-std, openzeppelin-contracts, v3-core,
# v3-periphery). `forge build` is the load-bearing check: it fails
# loudly if any import resolves to a missing path. (audit F-03's
# intent — "forge install must succeed" — is preserved at the
# different layer of `forge build`.)

# ─── Stage 2: final runtime ───────────────────────────────────────────
# Same digest-pinned base as the builder. Keep the two in sync — refresh
# procedure documented above the builder FROM.
FROM python:3.12-slim-bookworm@sha256:93ab4b7fa528b25124c97bcc755415e60eb671a86b4dbe0328df2fe2d1c1193d AS final

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# Runtime apt deps:
#   - ca-certificates: TLS to RPC providers + GHCR
#   - curl: healthchecks + the operator-side foundryup-style flows
#   - nodejs: real runtime dep — the JS scoring sandbox
#     (minotaur_subnet/engine/sandbox.py) spawns `node` as a subprocess
#     to run validator-side scoring of each App's JS module. Dropping it
#     here is a regression; npm is NOT needed at runtime (no `npm install`
#     happens in the running container).
#   - docker-ce-cli: spawning benchmark containers via docker-socket-proxy
#     (NOT docker.io — that pulls dockerd, containerd, runc; we only
#     need the CLI client because the daemon lives on the host)
#   - docker-buildx-plugin: REQUIRED for solver image builds (screening
#     Stage 2). Without it `docker build` falls back to the LEGACY builder,
#     whose image-layer cache hard-fails ("unable to find image sha256:…")
#     once a referenced base layer is GC'd — which silently blocked EVERY
#     fresh submission build (only pre-cached images could score). buildx
#     gives BuildKit's content-addressed cache, which has no such fragility.
#     Paired with DOCKER_BUILDKIT=1 below so `docker build` uses it by default.
#
# Docker apt repo signing key is fetched once and pinned to the keyring;
# the repo line uses signed-by= so apt won't trust other sources.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        gnupg \
        nodejs \
    && install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://download.docker.com/linux/debian/gpg \
        | gpg --dearmor -o /etc/apt/keyrings/docker.gpg \
    && chmod a+r /etc/apt/keyrings/docker.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
        https://download.docker.com/linux/debian bookworm stable" \
        > /etc/apt/sources.list.d/docker.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends docker-ce-cli docker-buildx-plugin \
    && apt-get purge -y --auto-remove gnupg \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Make `docker build` use BuildKit by default (buildx is installed above). The
# legacy builder is deprecated and its layer cache is what broke fresh builds.
ENV DOCKER_BUILDKIT=1

# Non-root runtime user. uid 1000 must match the compose-side `user:
# "1000:1000"` directive (PR-3) and the chown on the /data volume below.
RUN useradd --create-home --uid 1000 --shell /bin/bash minotaur

# Backward-compat for the wallet mount path. The canonical 2026-05-26+
# compose mounts the bittensor wallet at /home/minotaur/.bittensor/wallets
# (post-uid 1000 migration), but pre-2026-05-26 compose files mounted at
# /root/.bittensor/wallets. Symlink /home/minotaur/.bittensor →
# /root/.bittensor so the bittensor SDK's default $HOME/.bittensor lookup
# resolves to whichever path the operator's compose actually binds. Plus
# chmod /root traversable so uid 1000 can reach the legacy mount point
# without dropping the read-only mount mode.
#
# Additionally chown the SYMLINK TARGET (/root/.bittensor) to minotaur and
# pre-create an empty wallets/ subdir. Why: the bittensor SDK runs
# `Path("$HOME/.bittensor/wallets").mkdir(parents=True, exist_ok=True)` at
# IMPORT TIME (bittensor/core/settings.py). On services that don't mount a
# wallet (the api block in some legacy composes only mounts on the
# validator block), `exist_ok=True` still fails if Python has to create
# the leaf and uid 1000 lacks write on /root/.bittensor — which it does
# when the dir is root-owned 0755. Pre-creating wallets/ owned by minotaur
# means `mkdir(exist_ok=True)` is a no-op even with no host mount, so a
# plain `import bittensor` in the api process can't crash the metagraph
# sync. Wallet FILE perms on the host side still need to be
# uid-1000-readable — see platform/validator/.env.example.
RUN chmod 0755 /root \
    && mkdir -p /root/.bittensor/wallets \
    && chown -R minotaur:minotaur /root/.bittensor \
    && rm -rf /home/minotaur/.bittensor \
    && ln -s /root/.bittensor /home/minotaur/.bittensor \
    && chown -h minotaur:minotaur /home/minotaur/.bittensor

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
# Note: no separate remappings.txt — remappings are inlined in
# contracts/foundry.toml's [profile.default] table (verified via
# `forge remappings`).

# Application code. .dockerignore at repo root prunes tests/, docs/,
# .git/, contracts/{test,script,broadcast}, etc. from the build
# context so they don't end up here.
COPY --chown=minotaur:minotaur minotaur_subnet/ /home/minotaur/app/minotaur_subnet/
COPY --chown=minotaur:minotaur platform/ /home/minotaur/app/platform/

# /data is the store volume mount target. Owned by uid 1000 so the
# non-root process can write store.json without a runtime chown.
RUN mkdir -p /data && chown -R minotaur:minotaur /data

# Bake the git SHA the image was built from into an env var so the
# api + validator daemon can echo it back in /health. Operators (and us
# in incident response) can then see which version is running in each
# container without docker inspect. Defaults to "dev" for local builds
# that don't pass --build-arg; CI sets it to ${{ github.sha }}. Placed
# AFTER all heavy COPYs so a value change only invalidates this thin
# layer + the user/workdir below, not the venv / contract / app layers.
ARG MINOTAUR_IMAGE_SHA=dev
ENV MINOTAUR_IMAGE_SHA=${MINOTAUR_IMAGE_SHA}

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


# ── testnet stage ──────────────────────────────────────────────────────────
# Adds `forge` + the vendored contract libs that the local-testnet's app
# compile/deploy path needs: DeployService.compile() runs `forge build` on
# submitted app source, which requires forge and lib/{openzeppelin,uniswap,
# forge-std}. The slim `final` stage deliberately drops both. This stage is
# layered ON TOP of `final` and is selected only by the local-testnet compose
# (`target: testnet`) — the prod image stays `final` and unaffected.
FROM final AS testnet
USER root
COPY --from=builder /root/.foundry/bin/forge /usr/local/bin/forge
RUN chmod 0755 /usr/local/bin/forge
COPY --from=builder --chown=minotaur:minotaur /build/contracts/lib /home/minotaur/app/contracts/lib
USER minotaur
