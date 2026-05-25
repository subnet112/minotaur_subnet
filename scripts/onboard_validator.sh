#!/usr/bin/env bash
#
# Onboard a third-party validator: add their EVM signing address to the
# on-chain ValidatorRegistry on BOTH chains (Base + BT EVM).
#
# ChampionRegistry on BT EVM delegates its validator-set authority to BT
# EVM ValidatorRegistry via constructor wiring — so writing to the two
# ValidatorRegistries is sufficient. The new validator is recognized for
# both order-consensus (Base) and champion-consensus (BT EVM via
# ChampionRegistry's isValidator() delegation) without a third write.
#
# Run by the subnet-team operator who holds the registry owner key.
# Dry-run by default; pass --execute to actually broadcast the writes.
#
# Usage:
#   scripts/onboard_validator.sh <EVM_ADDR> [--hotkey <SS58>] [--axon <URL>] [--execute]
#
# Env:
#   REGISTRY_OWNER_KEY    REQUIRED for --execute: hex private key of the
#                         registry owner (currently the deployer key).
#   BASE_RPC              Base mainnet RPC URL (defaults to a public node).
#   BTEVM_RPC             BT EVM RPC URL (defaults to the public lite endpoint).
#   VALIDATOR_REGISTRY_8453   Base ValidatorRegistry address.
#   VALIDATOR_REGISTRY_964    BT EVM ValidatorRegistry address.

set -euo pipefail

# ── Defaults pulled from docs/operator/network-reference.md ────────────────
: "${BASE_RPC:=https://base.publicnode.com}"
: "${BTEVM_RPC:=https://lite.chain.opentensor.ai}"
: "${VALIDATOR_REGISTRY_8453:=0x88a08d1105393EACE9B6f5ff678DbE508B8639aC}"
: "${VALIDATOR_REGISTRY_964:=0x0B5fE44e90515571761D86C28c4855F325EDE098}"

# ── CLI parsing ───────────────────────────────────────────────────────────
NEW_VALIDATOR=""
HOTKEY=""
AXON_URL=""
EXECUTE=0

usage() {
  sed -n '2,28p' "$0" | sed 's/^# \{0,1\}//' >&2
  exit 1
}

while [ $# -gt 0 ]; do
  case "$1" in
    --hotkey) HOTKEY="$2"; shift 2 ;;
    --axon)   AXON_URL="$2"; shift 2 ;;
    --execute) EXECUTE=1; shift ;;
    -h|--help) usage ;;
    0x*) NEW_VALIDATOR="$1"; shift ;;
    *) echo "ERR: unknown arg: $1" >&2; usage ;;
  esac
done

[ -z "$NEW_VALIDATOR" ] && { echo "ERR: validator EVM address required" >&2; usage; }

# Checksum the address before doing anything else — web3.py + cast both
# reject non-checksummed addresses in some code paths.
NEW_VALIDATOR=$(cast to-check-sum-address "$NEW_VALIDATOR")
echo "Onboarding validator:"
echo "  EVM addr: $NEW_VALIDATOR"
[ -n "$HOTKEY" ]   && echo "  Hotkey:   $HOTKEY"
[ -n "$AXON_URL" ] && echo "  Axon URL: $AXON_URL"
echo

if [ "$EXECUTE" -eq 1 ] && [ -z "${REGISTRY_OWNER_KEY:-}" ]; then
  echo "ERR: --execute requires REGISTRY_OWNER_KEY env to be set" >&2
  exit 1
fi

# ── Per-chain processing ──────────────────────────────────────────────────
process_chain() {
  local chain_name="$1"
  local rpc_url="$2"
  local registry="$3"

  echo "── $chain_name (ValidatorRegistry $registry) ──"

  # Refuse to operate on a contract that doesn't expose quorumBps — that's
  # the signature of the post-refactor ValidatorRegistry. Pre-refactor
  # contracts revert here and we don't want to write to those.
  if ! cast call "$registry" 'quorumBps()(uint256)' --rpc-url "$rpc_url" >/dev/null 2>&1; then
    echo "  SKIP: quorumBps() reverted — likely a pre-refactor contract or wrong address"
    return
  fi

  local current_raw
  current_raw=$(cast call "$registry" 'getValidators()(address[])' --rpc-url "$rpc_url")
  # cast returns: [0xAddr1, 0xAddr2, ...]
  # Strip brackets, split on comma, trim whitespace, lowercase for comparison.
  local current
  current=$(printf '%s' "$current_raw" | tr -d '[]' | tr ',' '\n' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//' | grep -v '^$' || true)

  if printf '%s\n' "$current" | grep -qi "^$(printf '%s' "$NEW_VALIDATOR" | tr 'A-Z' 'a-z')$"; then
    echo "  Already registered — no write needed"
    return
  fi

  # Build the new sorted set. Sort case-insensitively but preserve the
  # checksummed form of each address (cast / web3 will accept either, but
  # checksummed is the convention).
  local combined
  combined=$( (printf '%s\n' "$current"; printf '%s\n' "$NEW_VALIDATOR") | grep -v '^$' )
  local sorted
  # awk dedup (case-insensitive) + sort ascending lowercase, preserving
  # the first-seen casing.
  sorted=$(printf '%s\n' "$combined" | awk 'BEGIN{IGNORECASE=1} !seen[tolower($0)]++' | sort -f)

  local new_array
  new_array="["$(printf '%s\n' "$sorted" | paste -sd, -)"]"

  echo "  Current set ($(printf '%s\n' "$current" | wc -l) validators):"
  printf '    %s\n' $current
  echo "  New set ($(printf '%s\n' "$sorted" | wc -l) validators):"
  printf '    %s\n' $sorted

  if [ "$EXECUTE" -eq 0 ]; then
    echo "  DRY-RUN: would cast send updateValidators($new_array)"
    return
  fi

  echo "  Sending updateValidators(...) tx ..."
  local tx
  tx=$(cast send "$registry" 'updateValidators(address[])' "$new_array" \
        --rpc-url "$rpc_url" \
        --private-key "$REGISTRY_OWNER_KEY" \
        --json | python3 -c 'import sys,json; print(json.load(sys.stdin).get("transactionHash",""))' 2>/dev/null || echo "")
  if [ -z "$tx" ]; then
    echo "  ERR: tx submission failed" >&2
    exit 1
  fi
  echo "  Tx: $tx"

  # Verify isValidator returned true
  local is_v
  is_v=$(cast call "$registry" 'isValidator(address)(bool)' "$NEW_VALIDATOR" --rpc-url "$rpc_url")
  if [ "$is_v" != "true" ]; then
    echo "  ERR: isValidator($NEW_VALIDATOR) returned $is_v after tx — investigate" >&2
    exit 1
  fi
  echo "  Verified: isValidator($NEW_VALIDATOR) = true"
}

process_chain "Base"   "$BASE_RPC"  "$VALIDATOR_REGISTRY_8453"
echo
process_chain "BT EVM" "$BTEVM_RPC" "$VALIDATOR_REGISTRY_964"
echo

if [ "$EXECUTE" -eq 0 ]; then
  echo "Dry-run complete. Re-run with --execute (and REGISTRY_OWNER_KEY set) to broadcast."
else
  echo "Onboarding complete on both chains. Validator $NEW_VALIDATOR is registered."
  echo
  echo "Off-chain notes for our records (NOT written on-chain):"
  [ -n "$HOTKEY" ]   && echo "  Hotkey:   $HOTKEY"
  [ -n "$AXON_URL" ] && echo "  Axon URL: $AXON_URL"
  echo
  echo "Within ~60s, our validators' ProtocolConfig refresh loop will pick up the"
  echo "new entry, probe http://<axon>:9100/identity, verify the EIP-712 binding,"
  echo "and start including the new validator in proposals + champion-consensus."
fi
