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
SERVICES="${MINOTAUR_UPDATE_SERVICES:-fork-cache validator api relayer}"  # tag-tracked (pulled) services
ANVILS="${MINOTAUR_ANVILS:-anvil-eth anvil-base anvil-btevm}"
PROBE_SVC="${MINOTAUR_UPDATE_PROBE_SVC:-api}"   # snapshot the rollback image from this service
DIR="${MINOTAUR_COMPOSE_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
ENV_FILE="${MINOTAUR_ENV_FILE:-$DIR/.env}"
DO_PULL=1
SKIP_ENV_CHECK="${MINOTAUR_SKIP_ENV_CHECK:-0}"
# ── round-drain (LEAD-ADAPTED): don't recreate the round coordinator MID-ROUND.
# The hourly recreate straddling a round's scoring/finalization window has caused
# both an orphaned champion merge (relayer race) and stranded benchmark reports
# (in-flight scores reaped). When a new coordinator image is staged, wait for the
# round to leave its ~2-min hot window before recreating. Bounded so an update
# never blocks forever; in-app defer/health-gate still backstops any residual race.
ROUND_DRAIN="${MINOTAUR_ROUND_DRAIN:-1}"          # 1 = drain before a coordinator recreate; 0 = old behaviour
DRAIN_MAX_WAIT="${MINOTAUR_DRAIN_MAX_WAIT:-600}"  # hard cap (secs) — proceed anyway past this
DRAIN_POLL="${MINOTAUR_DRAIN_POLL:-20}"           # secs between round-status polls

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
# LEAD-ADAPTED (not the shared template): route every compose call through the
# lead's dc.sh so the production compose + BOTH env files (.env.production + .env.keys)
# are always loaded. This is what makes :latest (develop) get pulled and the :? key
# guards resolve. Followers keep using the pristine platform/validator/update.sh.
DC() { "$DIR/dc.sh" "$@"; }

# ── 0. preflight: docker + compose file ───────────────────────────────────
if ! docker info >/dev/null 2>&1; then
  err "cannot talk to the Docker daemon (is it running? are you in the docker group?)"; exit 2
fi
if [ ! -f "$DIR/docker-compose.production.yml" ] || [ ! -x "$DIR/dc.sh" ]; then
  err "lead-adapted update.sh expects docker-compose.production.yml + dc.sh in $DIR"; exit 2
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

# ── round-drain helpers ─────────────────────────────────────────────────────
# Current solver round status from the benchmark-worker's (or api's) round store.
# Prints the lowercase status, or none|error. Never fails the run.
round_status() {
  local cid out
  cid="$(DC ps -q benchmark-worker 2>/dev/null || true)"
  [ -z "$cid" ] && cid="$(DC ps -q api 2>/dev/null || true)"
  [ -z "$cid" ] && { echo none; return; }
  out="$(docker exec "$cid" python3 -c 'import json
try:
    d = json.load(open("/data/solver_rounds.json"))
    crid = d.get("current_round_id")
    r = (d.get("rounds") or {}).get(crid) or {}
    print((r.get("status") or "none").lower())
except FileNotFoundError:
    print("none")
except Exception:
    print("error")' 2>/dev/null || true)"
  echo "${out:-error}"
}

# True (0) if a freshly-pulled image for a coordinator service differs from what
# is running — i.e. `up` WILL recreate it, so a mid-round recreate is at stake.
# If nothing will recreate, we skip the drain entirely (no needless waiting).
recreate_pending() {
  local svc cid running tag tagimg
  for svc in $SERVICES; do
    case "$svc" in api|relayer|benchmark-worker) ;; *) continue ;; esac
    cid="$(DC ps -q "$svc" 2>/dev/null || true)"
    [ -z "$cid" ] && return 0                       # not running → up will (re)create it
    running="$(docker inspect --format '{{.Image}}' "$cid" 2>/dev/null || true)"
    tag="$(docker inspect --format '{{.Config.Image}}' "$cid" 2>/dev/null || true)"
    case "$tag" in *@sha256:*) continue ;; esac     # digest-pinned → tag compare meaningless
    tagimg="$(docker image inspect --format '{{.Id}}' "$tag" 2>/dev/null || true)"
    [ -n "$tagimg" ] && [ -n "$running" ] && [ "$running" != "$tagimg" ] && return 0
  done
  return 1
}

# Wait until the round is in a state where recreating the coordinator won't
# orphan an in-flight benchmark or split a champion finalization. The hot window
# is CLOSED→REPLAYING→SHADOWING→CERTIFYING→CERTIFIED (~2 min/round); OPEN
# (collecting, ~20 min runway) and terminal/none/error are safe. DRAIN_MAX_WAIT
# caps the wait so the hourly update never blocks forever.
drain_for_safe_round() {
  [ "$ROUND_DRAIN" = "1" ] || { log "round-drain disabled (MINOTAUR_ROUND_DRAIN=0) — recreating immediately"; return 0; }
  local waited=0 st
  while [ "$waited" -lt "$DRAIN_MAX_WAIT" ]; do
    st="$(round_status)"
    case "$st" in
      closed|replaying|shadowing|certifying|certified)
        log "round-drain: status=$st (scoring/finalizing) — waiting for a safe window (${waited}/${DRAIN_MAX_WAIT}s)"
        sleep "$DRAIN_POLL"; waited=$((waited + DRAIN_POLL)) ;;
      *)
        if [ "$waited" -gt 0 ]; then log "round-drain: status=$st — safe after ${waited}s, proceeding"
        else log "round-drain: status=$st — safe, proceeding"; fi
        return 0 ;;
    esac
  done
  warn "round-drain: still '$st' after ${DRAIN_MAX_WAIT}s cap — proceeding with recreate (in-app defer/health-gate backstops any race)"
  return 0
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

# ── 3.5 drain: don't recreate the coordinator MID-ROUND ───────────────────
# Only matters when a new image is actually staged (otherwise `up` is a no-op).
if recreate_pending; then
  log "new coordinator image staged — checking the round window before recreating"
  drain_for_safe_round
else
  log "no coordinator image change — nothing to recreate, skipping round-drain"
fi

# ── 4. bring up api/validator (health-gate now satisfied) ─────────────────
log "recreating api/validator with --wait (timeout ${SERVICE_WAIT}s)…"
# shellcheck disable=SC2086 — intentional word-split; LEAD: scope to managed services
# ($SERVICES) so the one-shot 'seed' container (which exits) does not make --wait fail.
if DC up -d --wait --wait-timeout "$SERVICE_WAIT" $SERVICES; then
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
