#!/usr/bin/env bash
#
# Install the three host-level tools a Minotaur Subnet 112 validator needs:
#
#   - Docker engine + compose plugin (≥25 for Watchtower compatibility)
#   - Foundry (cast / forge / anvil) — used to generate the EVM signing key
#     and to read on-chain state (cast call) during verification
#   - Bittensor CLI (btcli) — used to register a hotkey on subnet 112
#
# Idempotent: any tool already present is left untouched and the existing
# version is reported. Safe to re-run.
#
# Target: Ubuntu 22.04/24.04 and Debian 12+. For other distros, install
# the three tools manually using the vendor docs:
#   - Docker:    https://docs.docker.com/engine/install/
#   - Foundry:   https://book.getfoundry.sh/getting-started/installation
#   - btcli:     `pip install bittensor`
#
# Curl-pipe-bash entry point:
#   curl -fsSL https://raw.githubusercontent.com/subnet112/minotaur_subnet/main/scripts/install_prereqs.sh | bash

set -euo pipefail

GREEN='\033[0;32m'; RED='\033[0;31m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
ok()   { printf "${GREEN}✓${NC} %s\n" "$*"; }
note() { printf "${CYAN}•${NC} %s\n" "$*"; }
warn() { printf "${YELLOW}!${NC} %s\n" "$*"; }
err()  { printf "${RED}✗${NC} %s\n" "$*" >&2; }

if [ "$(id -u)" -eq 0 ]; then
  warn "Running as root. The Docker install will work, but the docker group"
  warn "membership for non-root usage won't apply. Prefer running as a normal"
  warn "user with sudo access."
fi

# Need sudo for the system installs. Verify it works before doing real work.
if ! sudo -n true 2>/dev/null; then
  note "This script uses sudo for system installs. You may be prompted for your password."
  sudo -v
fi

# Probe distro — abort early on anything unfamiliar.
if [ ! -r /etc/os-release ]; then
  err "/etc/os-release missing — can't detect distro. Install Docker + Foundry + btcli manually."
  exit 1
fi
. /etc/os-release
case "${ID:-}" in
  ubuntu|debian) ;;
  *) err "Unsupported distro: ${ID:-unknown}. This installer covers ubuntu/debian only."; exit 1 ;;
esac
note "Detected: ${PRETTY_NAME:-$ID}"
echo

# ── 1. Docker engine + compose plugin ────────────────────────────────
if command -v docker >/dev/null 2>&1; then
  ok "Docker already installed: $(docker --version)"
else
  note "Installing Docker via the official convenience script..."
  curl -fsSL https://get.docker.com | sudo sh
  if [ "$(id -u)" -ne 0 ]; then
    sudo usermod -aG docker "$USER"
    warn "Added $USER to the docker group. LOG OUT + LOG BACK IN before running compose,"
    warn "or run subsequent docker commands with sudo."
  fi
  ok "Docker installed: $(docker --version)"
fi

if docker compose version >/dev/null 2>&1; then
  ok "Docker Compose plugin already present: $(docker compose version --short 2>/dev/null || docker compose version | head -1)"
else
  note "Installing Docker Compose plugin..."
  sudo apt-get update -qq
  sudo apt-get install -y --no-install-recommends docker-compose-plugin
  ok "Docker Compose plugin installed"
fi
echo

# ── 2. Foundry ───────────────────────────────────────────────────────
if command -v cast >/dev/null 2>&1; then
  ok "Foundry already installed: $(cast --version 2>&1 | head -1)"
else
  note "Installing Foundry (cast / forge / anvil)..."
  curl -L https://foundry.paradigm.xyz | bash
  # foundryup is dropped into $HOME/.foundry/bin by the installer above;
  # it isn't on PATH yet so call it by full path.
  "$HOME/.foundry/bin/foundryup"
  if [ -d "$HOME/.foundry/bin" ] && ! echo ":$PATH:" | grep -q ":$HOME/.foundry/bin:"; then
    warn "Foundry binaries live in $HOME/.foundry/bin which is not in your PATH."
    warn "Add this line to your shell rc (~/.bashrc or ~/.zshrc):"
    warn "  export PATH=\"\$HOME/.foundry/bin:\$PATH\""
  fi
  ok "Foundry installed"
fi
echo

# ── 3. Bittensor CLI ─────────────────────────────────────────────────
# btcli needs Python 3.10+ and pip. Install both if missing.
if ! command -v python3 >/dev/null 2>&1; then
  note "Installing Python 3 + pip + venv..."
  sudo apt-get install -y --no-install-recommends python3 python3-pip python3-venv
fi

if command -v btcli >/dev/null 2>&1; then
  ok "btcli already installed: $(btcli --version 2>&1 | head -1 || echo 'present')"
else
  note "Installing Bittensor CLI via pip..."
  # Prefer --user so we don't fight the system Python and don't need sudo.
  python3 -m pip install --user --quiet --upgrade pip
  python3 -m pip install --user --quiet bittensor
  if [ -d "$HOME/.local/bin" ] && ! echo ":$PATH:" | grep -q ":$HOME/.local/bin:"; then
    warn "btcli landed in $HOME/.local/bin which is not in your PATH."
    warn "Add this line to your shell rc (~/.bashrc or ~/.zshrc):"
    warn "  export PATH=\"\$HOME/.local/bin:\$PATH\""
  fi
  ok "btcli installed"
fi
echo

# ── Summary ──────────────────────────────────────────────────────────
ok "All prerequisites installed."
echo
note "Next steps from the quickstart:"
echo "  3. Register a hotkey on subnet 112:"
echo "       btcli wallet new_coldkey --wallet.name my-validator"
echo "       btcli wallet new_hotkey  --wallet.name my-validator --wallet.hotkey my-hotkey"
echo "       btcli subnet register   --netuid 112 --subtensor.network finney \\"
echo "                                --wallet.name my-validator --wallet.hotkey my-hotkey"
echo "  4. Generate an EVM signing key (no funds needed):"
echo "       cast wallet new"
echo "  5. cd minotaur_subnet/platform/validator && cp .env.example .env && \$EDITOR .env"
echo "  6. docker compose --profile autoupdate up -d"
echo "  7. bash scripts/check_validator.sh"
echo
note "Full walkthrough: docs/validator/quickstart.md"
