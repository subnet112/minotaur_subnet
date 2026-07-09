#!/usr/bin/env bash
#
# Health-gated validator update, with automatic rollback.
#
# A SAFE replacement for Watchtower auto-update. When :stable moves, Watchtower
# recreates the tag-tracked containers immediately — it IGNORES
# `depends_on: condition: service_healthy`, so the api/validator can start
# against anvil forks that are still cold-forking mainnet (the api runs with
# require_real_sim on prod, so it fails), and Watchtower NEVER rolls back a
# broken update — the node is left down until a human notices.
#
# This script instead:
#   1. snapshots the currently-running image (for rollback),
#   2. pulls the new image for the tag-tracked services only (fork-cache,
#      validator, api — NOT the anvils, so foundry never churns underneath you),
#   3. recreates with `docker compose up -d --wait`, which DOES honour the anvil
#      health-ordering and returns non-zero if anything fails to go healthy,
#   4. on failure, retags the previous image back and restores it — so a bad
#      release never leaves the validator down.
#
# Usage (run from platform/validator/, or set MINOTAUR_COMPOSE_DIR):
#   ./update.sh
#
# RECOMMENDED for high-stake validators: disable Watchtower (set
# MINOTAUR_DISABLE_WATCHTOWER=1 in .env / drop the `autoupdate` profile) and run
# this on the subnet's publish cadence, e.g. hourly from cron:
#   0 * * * * cd /opt/minotaur/platform/validator && ./update.sh >> update.log 2>&1
#
# Exit codes: 0 = updated & healthy · 1 = update failed, rolled back to previous
# (node healthy on OLD image — investigate before retrying) · 2 = precondition
# error · 3 = rollback ALSO failed (node DOWN, manual intervention required).
#
set -euo pipefail

# ── config (env-overridable) ──────────────────────────────────────────────
WAIT_TIMEOUT="${MINOTAUR_UPDATE_WAIT:-240}"                   # secs to reach healthy
SERVICES="${MINOTAUR_UPDATE_SERVICES:-fork-cache validator api}"  # tag-tracked services
PROBE_SVC="${MINOTAUR_UPDATE_PROBE_SVC:-api}"                 # snapshot the image from this one
DIR="${MINOTAUR_COMPOSE_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
cd "$DIR"

log() { printf '[update %s] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }

# docker compose v2 (plugin) or v1 (standalone)
if docker compose version >/dev/null 2>&1; then DC() { docker compose "$@"; }
elif command -v docker-compose >/dev/null 2>&1; then DC() { docker-compose "$@"; }
else log "FATAL: neither 'docker compose' nor 'docker-compose' found"; exit 2; fi

# ── 1. snapshot the running image for rollback ────────────────────────────
OLD_ID=""; IMAGE_REF=""
cid="$(DC ps -q "$PROBE_SVC" 2>/dev/null || true)"
if [ -n "$cid" ]; then
  OLD_ID="$(docker inspect --format '{{.Image}}' "$cid")"
  IMAGE_REF="$(docker inspect --format '{{.Config.Image}}' "$cid")"
  log "current $PROBE_SVC image: $IMAGE_REF ($OLD_ID)"
  case "$IMAGE_REF" in
    *@sha256:*)
      # Operator pinned by digest — the tag is immutable and auto-update is off.
      # A retag-rollback would be meaningless; roll back by editing the pin.
      log "image is digest-pinned; rollback-by-retag disabled (change MINOTAUR_IMAGE_TAG to roll back)"
      IMAGE_REF="" ;;
  esac
else
  log "no running $PROBE_SVC container (first start) — rollback disabled for this run"
fi

# ── 2. pull the new image (tag-tracked services only) ─────────────────────
log "pulling: $SERVICES"
# shellcheck disable=SC2086  # SERVICES is a deliberate multi-word service list
DC pull $SERVICES

# ── 3. recreate with health-gating + dependency ordering ──────────────────
#    up --wait honours depends_on:service_healthy (anvils go healthy BEFORE
#    api/validator start) and returns non-zero if any service stays unhealthy —
#    exactly the two guarantees Watchtower does not give.
log "recreating with --wait (timeout ${WAIT_TIMEOUT}s)…"
if DC up -d --wait --wait-timeout "$WAIT_TIMEOUT"; then
  log "✅ update healthy"
  exit 0
fi

# ── 4. unhealthy → roll back ──────────────────────────────────────────────
log "❌ services did not reach healthy within ${WAIT_TIMEOUT}s"
if [ -z "$OLD_ID" ] || [ -z "$IMAGE_REF" ]; then
  log "‼️ no rollback target — node may be DOWN, MANUAL INTERVENTION REQUIRED"
  exit 1
fi
log "rolling back to previous image $OLD_ID …"
docker tag "$OLD_ID" "$IMAGE_REF"
# shellcheck disable=SC2086  # SERVICES is a deliberate multi-word service list
if DC up -d --wait --wait-timeout "$WAIT_TIMEOUT" --force-recreate $SERVICES; then
  log "↩︎ rolled back — node HEALTHY on the previous image. Investigate the new release before retrying."
  exit 1
fi
log "‼️ ROLLBACK ALSO FAILED — node is DOWN, manual intervention required"
exit 3
