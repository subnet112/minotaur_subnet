#!/usr/bin/env bash
#
# Post-boot validator health check. Run from anywhere after
# `docker compose up -d` has finished:
#
#   bash scripts/check_validator.sh                            # default localhost
#   bash scripts/check_validator.sh http://my-host:9100 http://my-host:8080
#
# Verifies the things that have to be true before you open the
# onboarding issue:
#
#   - validator daemon /health: ok + block_loop_running=true
#   - api service /health: ok + champion_consensus.enabled=true
#   - validator /consensus/info: quorum_bps > 0 (on-chain read worked)
#   - validator /identity: returns signed EIP-712 (wallet loaded)
#   - api /identity: same
#   - both /identity payloads agree on axon_url
#
# Output goes into the onboarding issue's "Anything else?" field.

set -uo pipefail

VALIDATOR_URL="${1:-http://localhost:9100}"
API_URL="${2:-http://localhost:8080}"

GREEN='\033[0;32m'; RED='\033[0;31m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
ok()   { printf "${GREEN}✓${NC} %s\n" "$*"; }
warn() { printf "${YELLOW}!${NC} %s\n" "$*"; }
fail() { printf "${RED}✗${NC} %s\n" "$*"; FAILS=$((FAILS+1)); }
note() { printf "${CYAN}•${NC} %s\n" "$*"; }

FAILS=0
TMP=$(mktemp)
trap 'rm -f "$TMP"' EXIT

for tool in curl jq; do
  if ! command -v "$tool" >/dev/null 2>&1; then
    printf "${RED}✗${NC} Missing tool: %s (install with: sudo apt-get install -y %s)\n" "$tool" "$tool" >&2
    exit 2
  fi
done

# probe URL → writes body to $TMP, prints HTTP code (or "000" if no response)
probe() {
  curl -sS -o "$TMP" -w "%{http_code}" --max-time 10 "$1" 2>/dev/null
}

echo "── Minotaur Subnet 112 validator health check ──"
note "Validator daemon: $VALIDATOR_URL"
note "API service:      $API_URL"
echo

# ── 1. Validator /health ─────────────────────────────────────────────
code=$(probe "$VALIDATOR_URL/health")
if [ "$code" = "200" ] && jq -e '.status == "ok"' "$TMP" >/dev/null 2>&1; then
  block_loop=$(jq -r '.block_loop_running // false' "$TMP")
  if [ "$block_loop" = "true" ]; then
    ok "validator /health — ok, block_loop_running=true"
  else
    fail "validator /health — ok but block_loop_running=false (BlockLoop didn't start)"
  fi
else
  fail "validator /health — HTTP $code (expected 200)"
fi

# ── 2. API /health ───────────────────────────────────────────────────
code=$(probe "$API_URL/health")
if [ "$code" = "200" ] && jq -e '.status == "ok"' "$TMP" >/dev/null 2>&1; then
  champion_enabled=$(jq -r '.champion_consensus.enabled // false' "$TMP")
  champion_count=$(jq -r '.champion_consensus.validator_count // 0' "$TMP")
  champion_quorum=$(jq -r '.champion_consensus.quorum_required // 0' "$TMP")
  if [ "$champion_enabled" = "true" ]; then
    ok "api /health — ok, champion_consensus enabled (validators=$champion_count quorum=$champion_quorum)"
  else
    fail "api /health — ok but champion_consensus.enabled=false (check VALIDATOR_PRIVATE_KEY)"
  fi
else
  fail "api /health — HTTP $code (expected 200)"
fi

# ── 3. validator /consensus/info ─────────────────────────────────────
EVM_ADDR=""
code=$(probe "$VALIDATOR_URL/consensus/info")
if [ "$code" = "200" ]; then
  quorum_bps=$(jq -r '.quorum_bps // 0' "$TMP")
  EVM_ADDR=$(jq -r '.validator_id // ""' "$TMP")
  if [ "$quorum_bps" -gt 0 ] 2>/dev/null; then
    ok "validator /consensus/info — quorum_bps=$quorum_bps loaded from on-chain ValidatorRegistry"
  else
    fail "validator /consensus/info — quorum_bps=$quorum_bps (on-chain read failed; check VALIDATOR_REGISTRY_* envs)"
  fi
  ok "Your EVM signing address (for the onboarding issue): $EVM_ADDR"
else
  fail "validator /consensus/info — HTTP $code"
fi

# ── 4 + 5. /identity on both ports ───────────────────────────────────
# Function writes its result into the global GOT_AXON. Running without
# command-substitution so FAILS increments propagate to the parent
# (subshells would discard them and miscount the summary).
GOT_AXON=""
check_identity() {
  local label="$1" url="$2"
  GOT_AXON=""
  local code=$(probe "$url/identity")
  if [ "$code" = "200" ] && jq -e '.signature' "$TMP" >/dev/null 2>&1; then
    local axon hotkey
    axon=$(jq -r '.axon_url' "$TMP")
    hotkey=$(jq -r '.hotkey' "$TMP")
    ok "$label /identity — signed payload (axon=$axon hotkey=${hotkey:0:12}...)"
    GOT_AXON="$axon"
  elif [ "$code" = "503" ]; then
    local detail
    detail=$(jq -r '.detail // .error // "(unknown)"' "$TMP" 2>/dev/null)
    fail "$label /identity — 503: $detail"
    case "$detail" in
      *hotkey*) warn "  → WALLET_NAME / HOTKEY_NAME env not loading the wallet. Verify ~/.bittensor/wallets/<WALLET_NAME>/hotkeys/<HOTKEY_NAME> exists on the host and the wallet directory is mounted into the container." ;;
      *AXON_URL*) warn "  → VALIDATOR_AXON_URL env not set in .env." ;;
      *Consensus*) warn "  → VALIDATOR_PRIVATE_KEY env not set in .env." ;;
    esac
  else
    fail "$label /identity — HTTP $code (expected 200 or 503)"
  fi
}

check_identity "validator" "$VALIDATOR_URL"
AXON_V="$GOT_AXON"

check_identity "api      " "$API_URL"
AXON_A="$GOT_AXON"

# ── 6. axon_url env consistency ──────────────────────────────────────
if [ -n "$AXON_V" ] && [ -n "$AXON_A" ]; then
  if [ "$AXON_V" = "$AXON_A" ]; then
    ok "axon_url consistent across validator + api ports"
  else
    fail "validator /identity axon_url ($AXON_V) doesn't match api /identity axon_url ($AXON_A)"
    warn "  → both should be the same VALIDATOR_AXON_URL env value"
  fi
fi

# ── Summary ──────────────────────────────────────────────────────────
echo
if [ $FAILS -eq 0 ]; then
  ok "All checks passed."
  echo
  note "You're ready to open the onboarding issue:"
  note "  https://github.com/subnet112/minotaur_subnet/issues/new?template=onboard-validator.yml"
  echo
  note "Paste the above output into the issue's 'Anything else?' field,"
  note "along with these required fields:"
  note "  EVM signing address: $EVM_ADDR"
  note "  Public axon URL:     ${AXON_V:-<paste yours>}"
  note "  Bittensor hotkey:    <from \`btcli wallet inspect --wallet.name <name>\`>"
  exit 0
else
  printf "${RED}%d check(s) failed.${NC} Fix the issues above before opening the onboarding issue.\n" "$FAILS"
  echo
  note "Common fixes:"
  note "  - Wallet not loading → confirm ~/.bittensor/wallets/\$WALLET_NAME/hotkeys/\$HOTKEY_NAME exists"
  note "  - quorum_bps=0 → confirm VALIDATOR_REGISTRY_* envs match docs/operator/network-reference.md"
  note "  - VALIDATOR_AXON_URL empty → fill it in .env with your public http://<host>:9100"
  note "  - docker compose ps shows unhealthy containers → docker compose logs <service>"
  exit 1
fi
