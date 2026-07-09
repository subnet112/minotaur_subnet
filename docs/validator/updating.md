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

`platform/validator/update.sh` is a drop-in replacement AND a one-click repair
tool — run it any time the stack is unhappy; it is idempotent and safe to re-run.
It:

- **preflights the mandatory env vars** — a signing key or fork-upstream RPC left
  at a `YOUR_*` placeholder is the #1 cause of a stuck stack, so it fails fast with
  a clear list instead of a cryptic "anvil is unhealthy" 20 minutes later,
- pulls the new image for the tag-tracked services **only** (so foundry/Anvil
  never churns underneath a healthy fork),
- **heals the Anvil forks first** — the forks are what `api`/`validator` depend on
  with `condition: service_healthy`, and a plain `docker compose up` aborts in ~1s
  if a fork is *already* unhealthy (it won't wait for a stuck container to
  recover). So it force-recreates any missing/unhealthy fork to reset its health
  grace period and waits for all three to cold-fork and go healthy (btevm is
  slowest) — **before** bringing up `api`/`validator`,
- **rolls back** to the previous image automatically if the new one is unhealthy;
  and if the node was already fully down (nothing to roll back to), it prints a
  targeted diagnosis (which fork, its logs, the likely upstream) instead of just
  giving up.

### One-off update / repair

```bash
cd platform/validator
./update.sh                  # repair + update to current :stable
./update.sh --no-pull        # repair only, don't pull a newer image
./update.sh --skip-env-check # bypass the mandatory-env preflight (not advised)
```

Exit codes: `0` healthy · `1` failed — rolled back to the previous image if one
existed (investigate before retrying), else the node is still down and a diagnosis
was printed · `2` precondition error (bad/placeholder env, missing compose, Docker
unreachable) · `3` rollback also failed (node down, manual intervention).

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
| `MINOTAUR_ANVIL_WAIT` | `300` | Seconds to wait for the Anvil forks to go healthy. Raise it if btevm cold-forks slowly (shared-IP / public upstream). |
| `MINOTAUR_UPDATE_WAIT` | `240` | Seconds to wait for `api`/`validator` to reach healthy before rolling back. |
| `MINOTAUR_UPDATE_SERVICES` | `fork-cache validator api` | Tag-tracked services to pull/recreate. |
| `MINOTAUR_SKIP_ENV_CHECK` | `0` | `1` (or `--skip-env-check`) bypasses the mandatory-env preflight. Not advised. |
| `MINOTAUR_ENV_FILE` | `<dir>/.env` | Path to the env file the preflight reads. |
| `MINOTAUR_COMPOSE_DIR` | script dir | Where the compose file lives, if not run from `platform/validator/`. |

The env preflight requires these to be set to real values (not `YOUR_*`
placeholders): `VALIDATOR_PRIVATE_KEY`, `ADMIN_API_KEY`,
`SOLVER_ROUND_INTERNAL_API_KEY`, `WALLET_NAME`, `HOTKEY_NAME`,
`VALIDATOR_AXON_URL`, `ETH_UPSTREAM_RPC_URL`, `BASE_UPSTREAM_RPC_URL`. It *warns*
(but continues) when `BITTENSOR_EVM_UPSTREAM_RPC_URL` or `LEADER_API_URL` are
unset.

## Pinning to a specific build

To pin to an exact build instead of tracking `:stable`, set
`MINOTAUR_IMAGE_TAG=sha-<short_sha>` (or a full `@sha256:` digest) in `.env` and
bring the stack up without the `autoupdate` profile. With a digest pin the tag is
immutable, so `update.sh` skips its retag-rollback (roll back by changing the pin
and re-running). This is the most conservative option and is recommended for
production per the security note in the compose file.
