#!/usr/bin/env bash
#
# One-click validator repair + health-gated update for Minotaur SN112.
#
# A SAFE replacement for Watchtower auto-update, AND a repair tool for a node
# that's already down/flapping. Run it any time the stack is unhappy — it is
# idempotent and safe to re-run.
#
# What it does, in order:
#   1. PREFLIGHT — verifies docker/compose, the compose file, and that the
#      MANDATORY env vars are actually set (not left at YOUR_* placeholders).
#      A missing signing key or fork-upstream RPC is the #1 cause of a stuck
#      stack, so this fails FAST with a clear list instead of a cryptic
#      "anvil is unhealthy" 20 minutes later.
#   2. SNAPSHOT the running api image (for rollback), if any.
#   3. PULL :stable for the tag-tracked services (fork-cache, validator, api) —
#      NOT the anvils, so foundry never churns underneath a healthy fork.
#   4. HEAL THE ANVILS — the forks (anvil-eth/base/btevm) are what api/validator
#      depend on with `condition: service_healthy`. A plain `docker compose up`
#      ABORTS in ~1s if any anvil is already `unhealthy` (it does not wait for a
#      stuck container to recover). So this force-recreates any missing/unhealthy
#      anvil to reset its health grace period, then waits for all three to fork
#      and go healthy (btevm cold-forks slowest). This is the step that makes the
#      difference between "repaired" and "‼️ MANUAL INTERVENTION REQUIRED".
#   5. BRING UP api/validator with `--wait` — now their anvil deps are healthy,
#      so the health-gate passes instead of fail-fasting.
#   6. On failure, roll back to the previous image if we have one; otherwise
#      print a targeted diagnosis (which anvil, its logs, the likely upstream).
#
# Usage (run from platform/validator/, or set MINOTAUR_COMPOSE_DIR):
#   ./update.sh                  # repair + update to current :stable
#   ./update.sh --no-pull        # repair only, don't pull a newer image
#   ./update.sh --skip-env-check # bypass the mandatory-env preflight (not advised)
#
# RECOMMENDED for high-stake validators: disable Watchtower
# (MINOTAUR_DISABLE_WATCHTOWER=1 in .env / drop the `autoupdate` profile) and run
# this on the subnet's publish cadence, e.g. hourly from cron:
#   0 * * * * cd /opt/minotaur/platform/validator && ./update.sh >> update.log 2>&1
#
# Exit codes: 0 = healthy · 1 = failed (rolled back to previous image if one
# existed; else node still down — see the diagnosis printed above) · 2 =
# precondition error (bad env / missing compose / docker) · 3 = rollback ALSO
# failed (node DOWN, manual intervention required).
#
set -euo pipefail

# ── config (env-overridable) ──────────────────────────────────────────────
ANVIL_WAIT="${MINOTAUR_ANVIL_WAIT:-300}"        # secs for the forks to go healthy (btevm start_period is 90s)
SERVICE_WAIT="${MINOTAUR_UPDATE_WAIT:-240}"     # secs for api/validator to go healthy
SERVICES="${MINOTAUR_UPDATE_SERVICES:-fork-cache validator api}"  # tag-tracked (pulled) services
ANVILS="${MINOTAUR_ANVILS:-anvil-eth anvil-base anvil-btevm}"
# Phase 2 split (LEADER ONLY): when the leader activates the benchmark-worker
# profile it MUST keep the worker in lockstep with the api. In its .env, set:
#   COMPOSE_PROFILES=benchmark-worker
#   MINOTAUR_UPDATE_SERVICES="fork-cache validator api benchmark-worker"
#   MINOTAUR_ANVILS="anvil-eth anvil-base anvil-btevm anvil-eth-bench anvil-base-bench anvil-btevm-bench"
#   MINOTAUR_DIGEST_PARITY_SVCS="benchmark-worker"   # asserts worker digest == api's
# Empty by default → a no-op on every follower (no worker in the stack).
DIGEST_PARITY_SVCS="${MINOTAUR_DIGEST_PARITY_SVCS:-}"
PROBE_SVC="${MINOTAUR_UPDATE_PROBE_SVC:-api}"   # snapshot the rollback image from this service
DIR="${MINOTAUR_COMPOSE_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
ENV_FILE="${MINOTAUR_ENV_FILE:-$DIR/.env}"
DO_PULL=1
SKIP_ENV_CHECK="${MINOTAUR_SKIP_ENV_CHECK:-0}"

for arg in "$@"; do
  case "$arg" in
    --no-pull) DO_PULL=0 ;;
    --skip-env-check) SKIP_ENV_CHECK=1 ;;
    -h|--help) sed -n '2,45p' "$0"; exit 0 ;;
    *) echo "unknown arg: $arg (see --help)"; exit 2 ;;
  esac
done
cd "$DIR"

log()  { printf '[update %s] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }
warn() { printf '[update %s] ⚠️  %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >&2; }
err()  { printf '[update %s] ‼️  %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >&2; }

# docker compose v2 (plugin) or v1 (standalone)
if docker compose version >/dev/null 2>&1; then DC() { docker compose "$@"; }
elif command -v docker-compose >/dev/null 2>&1; then DC() { docker-compose "$@"; }
else err "FATAL: neither 'docker compose' nor 'docker-compose' found"; exit 2; fi

# ── 0. preflight: docker + compose file ───────────────────────────────────
if ! docker info >/dev/null 2>&1; then
  err "cannot talk to the Docker daemon (is it running? are you in the docker group?)"; exit 2
fi
if [ ! -f "$DIR/docker-compose.yml" ] && [ ! -f "$DIR/compose.yml" ]; then
  err "no compose file in $DIR — run from platform/validator/ or set MINOTAUR_COMPOSE_DIR"; exit 2
fi

# ── 1. preflight: mandatory env vars ──────────────────────────────────────
# Read a var from the shell env first, then the .env file (the same file docker
# compose substitutes from). No sourcing — .env is parsed, never executed.
env_val() {
  local var="$1" v=""
  v="$(printenv "$var" 2>/dev/null || true)"
  if [ -z "$v" ] && [ -f "$ENV_FILE" ]; then
    v="$(grep -E "^[[:space:]]*${var}=" "$ENV_FILE" 2>/dev/null | tail -n1 \
         | sed -E "s/^[[:space:]]*${var}=//; s/^\"(.*)\"$/\1/; s/^'(.*)'$/\1/")" || v=""
  fi
  printf '%s' "$v"
}
is_unset_or_placeholder() {
  # empty, or still a template placeholder (YOUR_*, changeme, <...>, xxx keys)
  case "$1" in
    "" ) return 0 ;;
    *YOUR_*|*your_*|*CHANGEME*|*changeme*|*'<'*'>'*|*YOUR_ALCHEMY_KEY*) return 0 ;;
    *) return 1 ;;
  esac
}

# name|hint   — REQUIRED: an unset/placeholder value breaks the stack.
REQUIRED_ENV=(
  "VALIDATOR_PRIVATE_KEY|EVM consensus signing key — round certification is silently disabled without it. Generate: cast wallet new"
  "ADMIN_API_KEY|gates deploy/scoring routes — without it, anonymous callers can spend the relayer's gas. Generate: openssl rand -hex 32"
  "SOLVER_ROUND_INTERNAL_API_KEY|champion-consensus proposal gate — send the value to the subnet team at registration. Generate: openssl rand -hex 32"
  "WALLET_NAME|your registered Bittensor wallet name (btcli)"
  "HOTKEY_NAME|your registered Bittensor hotkey name (btcli)"
  "VALIDATOR_AXON_URL|public http://<host>:9100 URL peers use for discovery — must be reachable from the internet"
  "ETH_UPSTREAM_RPC_URL|Ethereum archive RPC the anvil-eth fork pulls from — a placeholder here = anvil-eth never goes healthy. Use your own Alchemy/Infura key"
  "BASE_UPSTREAM_RPC_URL|Base archive RPC the anvil-base fork pulls from — a placeholder here = anvil-base never goes healthy. Use your own Alchemy/Infura key"
)
# name|hint   — RECOMMENDED: works without, but a common cause of trouble.
WARN_ENV=(
  "BITTENSOR_EVM_UPSTREAM_RPC_URL|unset → anvil-btevm forks from the public lite.chain.opentensor.ai, which is rate-limited on shared IPs and is the #1 cause of anvil-btevm failing its healthcheck. Point it at your own subtensor node if you run one"
  "LEADER_API_URL|unset → this follower won't sync the app catalog and can't re-score consensus proposals. Set to https://api.minotaursubnet.com (or your leader)"
)

preflight_env() {
  [ "$SKIP_ENV_CHECK" = "1" ] && { warn "env preflight SKIPPED (--skip-env-check)"; return 0; }
  if [ ! -f "$ENV_FILE" ]; then
    err "no .env at $ENV_FILE — copy .env.example to .env and fill in your values"; exit 2
  fi
  local missing=() e name hint val m
  for e in "${REQUIRED_ENV[@]}"; do
    name="${e%%|*}"; hint="${e#*|}"; val="$(env_val "$name")"
    if is_unset_or_placeholder "$val"; then missing+=("$name — $hint"); fi
  done
  for e in "${WARN_ENV[@]}"; do
    name="${e%%|*}"; hint="${e#*|}"; val="$(env_val "$name")"
    if is_unset_or_placeholder "$val"; then warn "$name not set: $hint"; fi
  done
  if [ "${#missing[@]}" -gt 0 ]; then
    err "MANDATORY env vars are unset or still at YOUR_* placeholders in $ENV_FILE:"
    for m in "${missing[@]}"; do printf '        • %s\n' "$m" >&2; done
    err "fix these in $ENV_FILE, then re-run. (Override at your own risk: --skip-env-check)"
    exit 2
  fi
  log "✅ env preflight passed (all mandatory vars set)"
}

# ── health helpers ─────────────────────────────────────────────────────────
# One of: missing | starting | healthy | unhealthy | nohealthcheck | <state>
svc_health() {
  local svc="$1" cid state health
  cid="$(DC ps -q "$svc" 2>/dev/null || true)"
  [ -z "$cid" ] && { echo missing; return; }
  state="$(docker inspect --format '{{.State.Status}}' "$cid" 2>/dev/null || echo missing)"
  [ "$state" != "running" ] && { echo "$state"; return; }
  health="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}nohealthcheck{{end}}' "$cid" 2>/dev/null || echo running)"
  echo "$health"
}

# ── image digest parity (Phase 2 split: api and benchmark-worker MUST match) ──
# Scoring is a consensus quantity, so the split worker MUST run the SAME image
# digest as the api — a stale worker would score on old code and split the node's
# own consensus. After a successful up, assert each service in
# MINOTAUR_DIGEST_PARITY_SVCS shares PROBE_SVC's (api's) running image digest.
# Loud WARN, not a hard fail: a parity gap must be visible in update.log, but must
# not take the leader down mid-soak (the operator recreates the lagging service).
check_digest_parity() {
  [ -z "$DIGEST_PARITY_SVCS" ] && return 0
  local ref_cid ref_dig svc cid dig
  ref_cid="$(DC ps -q "$PROBE_SVC" 2>/dev/null || true)"
  if [ -z "$ref_cid" ]; then warn "digest-parity: $PROBE_SVC not running — cannot compare"; return 0; fi
  ref_dig="$(docker inspect --format '{{.Image}}' "$ref_cid" 2>/dev/null || true)"
  for svc in $DIGEST_PARITY_SVCS; do
    cid="$(DC ps -q "$svc" 2>/dev/null || true)"
    if [ -z "$cid" ]; then
      warn "digest-parity: $svc not running — split misconfigured? (is COMPOSE_PROFILES=benchmark-worker set?)"
      continue
    fi
    dig="$(docker inspect --format '{{.Image}}' "$cid" 2>/dev/null || true)"
    if [ "$dig" != "$ref_dig" ]; then
      err "DIGEST DRIFT: $svc ($dig) != $PROBE_SVC ($ref_dig) — the worker is on a DIFFERENT image than the api; scoring may diverge. Fix: DC up -d --force-recreate $svc"
    else
      log "digest-parity ✅ $svc matches $PROBE_SVC ($ref_dig)"
    fi
  done
}

diagnose_anvils() {
  err "one or more forks did not go healthy within ${ANVIL_WAIT}s. Diagnosis:"
  local a st up_env
  for a in $ANVILS; do
    st="$(svc_health "$a")"
    case "$a" in
      *eth)   up_env="ETH_UPSTREAM_RPC_URL" ;;
      *base)  up_env="BASE_UPSTREAM_RPC_URL" ;;
      *btevm) up_env="BITTENSOR_EVM_UPSTREAM_RPC_URL (defaults to the public lite endpoint)" ;;
      *)      up_env="its upstream RPC" ;;
    esac
    printf '        • %-12s : %s\n' "$a" "$st" >&2
    if [ "$st" = "unhealthy" ] || [ "$st" = "starting" ]; then
      printf '          most likely: the fork upstream is unreachable/slow — check %s\n' "$up_env" >&2
      printf '          logs:\n' >&2
      DC logs --tail 12 "$a" 2>&1 | sed 's/^/            /' >&2 || true
    fi
  done
  err "fork-cache proxies these upstreams; also inspect: DC logs --tail 40 fork-cache"
}

# ── 2. snapshot the running api image for rollback ────────────────────────
OLD_ID=""; IMAGE_REF=""
snap_cid="$(DC ps -q "$PROBE_SVC" 2>/dev/null || true)"
if [ -n "$snap_cid" ]; then
  OLD_ID="$(docker inspect --format '{{.Image}}' "$snap_cid" 2>/dev/null || true)"
  IMAGE_REF="$(docker inspect --format '{{.Config.Image}}' "$snap_cid" 2>/dev/null || true)"
  case "$IMAGE_REF" in
    *@sha256:*) IMAGE_REF="" ;;  # digest-pinned: rollback-by-retag is meaningless
  esac
fi

# ── run ────────────────────────────────────────────────────────────────────
preflight_env

if [ -n "$OLD_ID" ] && [ -n "$IMAGE_REF" ]; then
  log "current $PROBE_SVC image: $IMAGE_REF ($OLD_ID) — rollback armed"
else
  log "no healthy $PROBE_SVC image to snapshot — this is a REPAIR (rollback disabled; goal is to get healthy)"
fi

if [ "$DO_PULL" = "1" ]; then
  log "pulling :stable for: $SERVICES"
  # shellcheck disable=SC2086
  DC pull $SERVICES || warn "pull failed — continuing with the images already on disk"
fi

# ── 3. heal the forks BEFORE the health-gated app up ──────────────────────
log "ensuring fork-cache is up…"
DC up -d fork-cache >/dev/null 2>&1 || true

recreate=""
for a in $ANVILS; do
  st="$(svc_health "$a")"
  if [ "$st" = "missing" ] || [ "$st" = "unhealthy" ] || [ "$st" = "exited" ]; then
    recreate="$recreate $a"
  fi
done
if [ -n "$recreate" ]; then
  log "forks need a clean start (missing/unhealthy):$recreate — force-recreating so their health grace period resets"
  # shellcheck disable=SC2086
  DC up -d --force-recreate $recreate >/dev/null 2>&1 || true
fi

log "waiting up to ${ANVIL_WAIT}s for the forks to reach healthy (btevm cold-forks slowest)…"
# shellcheck disable=SC2086
if ! DC up -d --wait --wait-timeout "$ANVIL_WAIT" $ANVILS; then
  diagnose_anvils
  err "cannot bring up api/validator against unhealthy forks — aborting before touching them."
  exit 1
fi
log "✅ all forks healthy"

# ── 4. bring up api/validator (health-gate now satisfied) ─────────────────
log "recreating api/validator with --wait (timeout ${SERVICE_WAIT}s)…"
if DC up -d --wait --wait-timeout "$SERVICE_WAIT"; then
  check_digest_parity   # Phase 2 split: warn if benchmark-worker digest != api's
  log "✅ update/repair complete — stack healthy"
  exit 0
fi

# ── 5. unhealthy → roll back if we can, else diagnose ─────────────────────
err "api/validator did not reach healthy within ${SERVICE_WAIT}s"
if [ -z "$OLD_ID" ] || [ -z "$IMAGE_REF" ]; then
  err "no rollback target (node was already down). The forks are healthy, so this is api/validator itself —"
  err "check: DC logs --tail 60 api   ·   DC logs --tail 60 validator   (and re-run env preflight)"
  exit 1
fi
log "rolling back $PROBE_SVC to previous image $OLD_ID …"
docker tag "$OLD_ID" "$IMAGE_REF"
# shellcheck disable=SC2086
if DC up -d --wait --wait-timeout "$SERVICE_WAIT" --force-recreate $SERVICES; then
  err "↩︎ rolled back — node HEALTHY on the previous image. Investigate the new release before retrying."
  exit 1
fi
err "‼️ ROLLBACK ALSO FAILED — node is DOWN, manual intervention required"
exit 3
