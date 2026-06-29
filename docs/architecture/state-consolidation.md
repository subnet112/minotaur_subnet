# State Consolidation — one champion, one owner

> Retires the **"silent‑None / two‑copy drift"** bug class. Authored after the same bug
> shape appeared **four times in one day** (#430, #444, #446, the follower‑adopt gate).
> When you fix one bug shape four times, the bugs aren't the problem — the architecture is.

## 1. The signal

Every one of those bugs was the same sentence:

> *A store/dependency silently became `None` because a config flag / env var / CLI arg
> gated it — and two processes (the API and the validator) each kept a private,
> file‑backed copy of the same state that drifted.*

- **#430** — `RoundStore`/`SubmissionStore` ran in‑memory unless an env was set → wiped on restart.
- **#444** — `EpochManager._sub_store = None` because `sub_store` was gated behind `ENABLE_BENCHMARK_WORKER` → `activate_certified_round` raised.
- **#446** — the validator's `_champion_round_store = None` because its path fallback was gated on `--store-path` → **100 % burn even after the API adopted the champion**.

The live symptom that cost emissions: the API adopted correctly and `GET /v1/solver/champion` returned the right champion the **entire time**, but the validator — the process that calls `set_weights` — never read that endpoint. It read its **own** `RoundStore` copy (`_local_champion_hotkey`), which was `None`, and silently burned.

## 2. Diagnosis: how many answers to "who is the champion?"

There were **six** independently‑writable representations, reconciled only by best‑effort side channels:

| # | Representation | Location |
|---|---|---|
| 1 | API `RoundStore` snapshot (`solver_rounds.json`) | `harness/round_store.py` |
| 2 | API `SubmissionStore` `ADOPTED` row | `harness/submission_store.py` |
| 3 | API `EpochManager._champion` (in‑RAM) | `epoch/manager.py` |
| 4 | **Validator's own `RoundStore` copy (or `None`)** | `validator/main.py` |
| 5 | Validator `_queued_weights_mapping` (HTTP push) | `validator/main.py` |
| 6 | On‑chain `ChampionRegistry` (merge‑gate only) | `contracts/.../ChampionRegistry.sol` |

The weight path read **#4** — a second in‑memory copy of `solver_rounds.json` in a different container — and reconciled with the authoritative **#1** only through mtime‑polled file reload, which requires both containers to mount the same `/data` volume. The third‑party validator compose **doesn't even mount `/data`** — so the IPC channel the design depended on wasn't wired at all.

## 3. The bug class, named

**"Flag‑gated `None` + two‑copy file‑IPC drift."** Config flags gated the *existence* of a dependency (not just a feature), and the same fact lived in N stores with N path rules, coordinated by files with independent in‑RAM caches. Both failed **silently** — single‑warning‑then‑degrade‑forever, invisible to `/health` (a 100 %‑burning validator reported `source: burn_fallback, result: ok`, distinguishable only by a buried `uids_attempted: 1` vs `2`).

## 4. Target architecture

One authority per fact; consumers **read** it, never copy it.

```
   adopts, persists ──▶  API process: the ONE RoundStore (champion of record)
                         consensus / adoption / GET /v1/solver/champion (authority)
                                 │ HTTP (co-located, localhost-class)
   set_weights ◀── Validator process: THIN emitter, holds NO champion state.
                   Resolves the champion from its OWN co-located API + a bounded
                   last-known-good memo. Never chain, never the public leader.
```

**Rejected: merging API + validator into one process.** It would give the cleanest single in‑RAM champion with zero IPC, but it puts the heavy benchmark worker + anvil sims + FastAPI in the **same restart/crash domain** as the one process that must never stop calling `set_weights`. On a piecemeal‑updated consensus fleet, fault isolation beats IPC elegance.

## 5. The four invariants (the defense)

The rules that, in place, would have prevented **every** bug this session:

1. **One source of truth per fact; consumers read, never copy.**
2. **Config flags gate *workers*, never the *existence* of a store/dependency.**
3. **Fail loud, not silent `None`** — a missing required dependency is loud (a crash or a red `/health`), never a quiet in‑memory/burn.
4. **Every fallback is observable** — a once‑per‑transition log **and** a `/health` field. No "single warning then silence."

## 6. What this refactor changes

A clean cut — legacy paths removed, no opt‑in flags, public endpoints preserved.

**Validator → thin champion consumer** (`validator/main.py`, `validator/champion_client.py`)
- New `ChampionResolver` reads the champion from THIS node's co‑located API (`GET /v1/solver/champion`, default `http://api:8080`) with a bounded **last‑known‑good memo** so a transient API restart never flips a standing champion to 100 % burn. No wall‑clock inside (caller passes monotonic `now`); the single network seam is isolated for testing.
- `_local_champion_hotkey` is now a thin `await self._champion_resolver.resolve(...)` — **HTTP‑only**.
- **Unresolved ≠ no champion (the safety invariant).** The epoch loop burns ONLY on a *definitive* no‑champion (`source 'api'`, hotkey null). When the API is unreachable (`source 'none'` — a cold memo right after a watchtower **co‑restart** of api+validator, or an outage past the memo TTL), the validator **SKIPs** the emit: its prior on‑chain champion weights persist until the API answers, and it retries each tick. Burning out of *ignorance* would collapse the standing champion's emission for a full ~1300 s commit‑reveal window (and on the lead, network‑wide via the Yuma median). The resolver also **never memoizes a `None`**, so a transient no‑champion read can't poison the last‑known‑good into a sticky burn. `/health` exposes `emission_mode ∈ {champion, burn, hold}`. This restores the old local‑file read's "instant at startup + tolerant of arbitrarily long API outages" robustness **without** holding any champion state.
- **Deleted:** `_champion_round_store`, the `round_store_path` resolution + its `--store-path` fallback, and the `CHAMPION_SOURCE` mode flag. The validator holds **no champion state**, so there is no second copy left to drift to `None`/stale (invariant 1). This is the change that retires #446 and the entire drift class.

**API → stores gate the worker, not themselves** (`api/startup.py`, already in via #444)
- `sub_store = submissions.get_store()` is built **unconditionally**; only the `BenchmarkWorker` loop stays behind `ENABLE_BENCHMARK_WORKER` (invariant 2). `activate_certified_round` no longer raises on followers.

**Observability** (`validator/main.py` `/health`)
- `/health` now reports `champion_source` (`api`/`memo`/`none`), `champion_hotkey`, and `emission_mode` (`champion`/`burn`) — so a validator burning a **real** champion can no longer present as healthy (invariant 4).

**Deployment** (`platform/validator/docker-compose.yml`)
- `CHAMPION_API_URL=http://api:8080` on the validator; the validator no longer needs `--store-path`/`SOLVER_ROUND_STORE_PATH`/the `store-data:/data` mount for the champion (the file‑IPC is gone). The API remains the single owner of `/data`.

**Preserved:** `GET /v1/solver/champion` and every public/internal endpoint; the validator's `AppIntentStore` (app catalog) is untouched.

## 7. Hardening (also in this refactor)

Same trajectory, all three landed here:

- **Fail‑loud durable state (invariant 3).** `state._resolve_persist_path` **raises** when a required durable path is unresolvable, instead of silently returning `None` (the #430 in‑memory wipe). The production api entrypoint (`api/server.py:main`) calls `require_durable_state()`, so a mis‑volumed node crashes at boot with a clear message naming the env/volume to fix; tests and dev stay in‑memory‑safe (off by default — never an env flag).
- **In‑process read precedence clarified (not rewritten).** `_get_incumbent_snapshot` was *already* RoundStore‑authoritative — the `SubmissionStore` `ADOPTED` row and in‑RAM `_champion` are cold‑boot REPAIR tiers, not competing sources, so the `_hot_swap` dual‑write can never surface a *wrong* champion (at worst the right one from a repair tier). Documented in place; the consensus write path is deliberately left untouched.
- **`AppIntentStore` path unified.** The validator now resolves the app catalog the same way the API does (`APP_INTENTS_STORE_PATH` env, `--store-path` fallback), so a node's api + validator never resolve the shared SQLite store differently.

## 8. What good looks like afterward

- "Who is the champion?" has **one** answer (the API's `RoundStore`), read over **one** channel (`GET /v1/solver/champion`). The validator keeps no champion state.
- A burning‑a‑real‑champion validator shows `emission_mode: burn` on `/health` — **observable**, not silent.
- A future debug checks **one** endpoint, not five (API, validator, metagraph, env, mounts).
- No config flag can null a dependency; flags gate workers only.
