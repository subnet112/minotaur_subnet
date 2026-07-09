# Updating safely

New code is promoted to the `:stable` tag by the subnet team after soak-testing
on prod. This page covers how to pick it up **without risking your node going
down on a bad recreate** — which matters most for high-stake validators.

## Why not just use Watchtower?

The `autoupdate` profile runs Watchtower, which recreates your tag-tracked
containers (`fork-cache`, `validator`, `api`) as soon as `:stable` moves. That is
convenient, but Watchtower 1.7.1 has **two limitations that can leave your node
down**:

1. **It ignores `depends_on: condition: service_healthy`.** The compose declares
   that the api/validator must wait for the three Anvil forks to be *healthy*
   (they take 60–90s to fork mainnet) before starting. Watchtower recreates the
   containers immediately, so the api can come up against Anvils that are still
   cold. On prod the api runs with `require_real_sim` on, so a sim backend that
   isn't ready yet makes it fail.
2. **It never rolls back.** If the recreated container is unhealthy, Watchtower
   moves on. Your node stays down until you notice.

Symptom: *"every time a new version ships and my node pulls it, it breaks."*

## The safe path: `update.sh`

`platform/validator/update.sh` is a drop-in replacement that fixes both:

- pulls the new image for the tag-tracked services **only** (so foundry/Anvil
  never churns underneath you),
- recreates with `docker compose up -d --wait`, which **honours the Anvil
  health-ordering** and returns non-zero if anything fails to go healthy,
- **rolls back** to the previous image automatically if the new one is unhealthy,
  so a bad release never leaves you down.

### One-off update

```bash
cd platform/validator
./update.sh
```

Exit codes: `0` updated & healthy · `1` update failed and was **rolled back**
(node healthy on the OLD image — investigate before retrying) · `2` precondition
error · `3` rollback also failed (node down, manual intervention).

### Recommended for high-stake validators: disable Watchtower + cron the updater

Turn Watchtower off (bring the stack up **without** the `autoupdate` profile, or
set `MINOTAUR_DISABLE_WATCHTOWER=1` in `.env`) and run the gated updater on the
subnet's publish cadence:

```cron
# hourly, health-gated, auto-rollback; logs to update.log
0 * * * * cd /opt/minotaur/platform/validator && ./update.sh >> update.log 2>&1
```

Host-side monitoring should alert on a non-zero exit (a `1` means you're running
the previous build and the new `:stable` needs a look before you retry).

### Tuning

| Env | Default | Meaning |
| --- | --- | --- |
| `MINOTAUR_UPDATE_WAIT` | `240` | Seconds to wait for services to reach healthy before rolling back. Raise it on slow hosts (cold Anvil forks). |
| `MINOTAUR_UPDATE_SERVICES` | `fork-cache validator api` | Tag-tracked services to pull/recreate. |
| `MINOTAUR_COMPOSE_DIR` | script dir | Where the compose file lives, if not run from `platform/validator/`. |

## Pinning to a specific build

To pin to an exact build instead of tracking `:stable`, set
`MINOTAUR_IMAGE_TAG=sha-<short_sha>` (or a full `@sha256:` digest) in `.env` and
bring the stack up without the `autoupdate` profile. With a digest pin the tag is
immutable, so `update.sh` skips its retag-rollback (roll back by changing the pin
and re-running). This is the most conservative option and is recommended for
production per the security note in the compose file.
