# Break-glass: force the live solver image (`FORCE_SOLVER_IMAGE`)

When the **active champion's code is broken** (the live solver produces bad plans,
crashes, or stops filling orders), an operator can pin THIS node's live
order-processing solver to a specific known-good image to restore functionality —
without waiting on re-adoption or fighting champion resolution.

## What it does

Set `FORCE_SOLVER_IMAGE` to an image ref and restart. The live solver boots from
that exact image, and champion hot-swaps are **suppressed** (a broken champion
can't reactivate over the forced image) until you clear the env.

- Accepts a digest ref `ghcr.io/subnet112/minotaur-solver@sha256:<64hex>` (preferred —
  exact bytes) **or** a tag `ghcr.io/subnet112/minotaur-solver:<tag>`.
- Wins over `GENESIS_SOLVER_IMAGE`.
- **Operator-local, not consensus.** The live solver only generates *plans* on the
  leader; followers re-simulate the plan (not the solver). Champion-of-record,
  weights, and benchmarking/adoption are unaffected — only what code generates
  plans changes. So setting it on your node(s) is safe and does not split the fleet.

## Recover

```bash
# 1. Pick a known-good image (a previous :sha-<short> tag or an @sha256 digest).
#    e.g. inspect what the genesis/previous champion ran, or a prior solver build.

# 2. Set the env on the affected validator(s) and restart the api/validator.
#    (production compose, on the box)
FORCE_SOLVER_IMAGE="ghcr.io/subnet112/minotaur-solver@sha256:<digest>"
docker compose --env-file .env.production --env-file .env.keys \
  -f docker-compose.production.yml up -d --no-deps api

# 3. Confirm the override is active:
curl -s http://localhost:8080/health | jq '.forced_solver_image, .live_solver_running'
#   -> "ghcr.io/subnet112/minotaur-solver@sha256:<digest>"   true
#    The api log shows: "FORCE_SOLVER_IMAGE override ACTIVE — live solver pinned to ..."
```

## Resume normal operation

```bash
# Unset FORCE_SOLVER_IMAGE (remove it from the env file) and restart.
# /health.forced_solver_image returns null; champion/genesis resolution resumes.
```

## Notes

- Applied on **restart** (matches how other operator env changes are rolled out).
  There is no live (no-restart) flip — that is a possible future admin endpoint.
- A crashed forced solver **respawns from the same forced image** automatically
  (the runtime was created with that `image_ref`).
- This is distinct from `DISABLE_CHAMPION_ADOPTION` (which freezes *adoption*, but
  keeps running whatever champion is already live) and from the champion **revert**
  (one-step rollback to the previous champion). `FORCE_SOLVER_IMAGE` pins the
  *runtime* to an arbitrary image you choose, irrespective of the champion record.
