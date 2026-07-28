# Solver SDK v2 — migrating a published interface without breaking miners

**Status:** proposal
**Prompted by:** the reverted de-DEX-ification batch (#1172)

---

## 1. The incident this exists to prevent

`MarketSnapshot.pool_states` and `dex_config` are Uniswap-shaped fields on the
miner-facing SDK type. Nothing in the platform read them; neither reference
solver used them; the SDK's own docstring tells solvers to prefer RPC. On that
evidence they were removed (#1161).

The **live champion** reads `snapshot.pool_states` with unguarded attribute
access:

```
king_base.py:2926   pool_states = (snapshot.pool_states if snapshot and snapshot.pool_states else {}) or {}
king_base.py:4048   pool_states = (snapshot.pool_states if snapshot and snapshot.pool_states else {}) or {}
solver.py:401       ps = getattr(snapshot, "pool_states", None)      # the only safe one
```

`snapshot and snapshot.pool_states` touches the attribute to evaluate the
guard, so the removal turns it into an `AttributeError` inside plan
generation — every scored scenario fails, for the incumbent, on the hour the
leader auto-updates. The whole batch was reverted.

### The reasoning error, stated plainly

The audit asked "does anything read this field?" and searched *this repo*.
That answers a different question. Solver code lives in miners' repos, is
forked-and-improved from a base we publish, and is invisible to grep here. An
in-repo search returning nothing is not evidence of no consumers — it is
evidence of no consumers *we can see*, which is the smaller half.

Worse, an earlier instinct that removal "breaks every existing solver" was
retracted as "unverified and overstated" on the strength of that same
incomplete search. The instinct was right; the retraction was the mistake.

---

## 2. What counts as published interface

Anything a solver container can reach. Concretely:

| Surface | Where | Reachable how |
|---|---|---|
| `MarketSnapshot` fields | `sdk/intent_solver.py` | attribute access in `generate_plan` |
| `IntentState` fields, incl. `typed_context` | `shared/types.py`, `v3/contexts.py` | attribute access |
| `IntentSolver` ABC method signatures | `sdk/intent_solver.py` | subclassing |
| `SolverMetadata` | `sdk/intent_solver.py` | constructed by every solver |
| The harness IPC wire shape | `harness/protocol.py` | JSON across the container boundary |
| `initialize(config)` keys, e.g. `rpc_urls` | orchestrator | dict access |

Removing or renaming anything in that table is a breaking change to an
interface with an installed base we do not control. Adding to it is safe.

> The reverted batch also deleted `SwapIntentContext` / `TwapIntentContext` /
> `RebalanceIntentContext` (#1163), which are equally reachable. The champion
> was checked for `pool_states` and **not** for those, so whether #1163 was
> independently safe is still unknown — it must be measured the same way
> before it returns.

---

## 3. The rule

**Additive now, deprecate loudly, retire on evidence.**

### Phase A — add, never remove

Ship the new shape alongside the old. For the snapshot that means:

- add `app_data: dict[str, Any]` (app-namespaced, platform never interprets);
- **stop populating** `pool_states` / `dex_config` / `prices` — delete the
  Uniswap pool queries, the monitored-pool table, the ABI, `_derive_prices`
  and the synthetic pool generator, which is where essentially all of #1161's
  value was;
- **keep the fields**, defaulting to `{}`.

A solver reading `snapshot.pool_states` gets `{}`, its `else` branch fires,
and it falls through to the RPC path the SDK already recommends. The platform
stops modelling a DEX on day one; nothing breaks.

The same shape applies elsewhere: keep the archetype context classes as thin
aliases of the base while `build_typed_context` stops producing them.

### Phase B — deprecate with a signal miners actually receive

1. `DeprecationWarning` on access (a `__getattr__` shim on the dataclass, so
   reading an emptied field is visible in solver logs).
2. The miner agent prompt and submission spec state the replacement.
3. A dated retirement target, announced once, not implied.

### Phase C — retire on measured evidence, not on a timer

The gate is **zero live solvers touching the field**, and it is checkable —
the champion's image is on the leader:

```bash
docker run --rm --entrypoint sh --network none <solver-image> \
  -c "grep -rn 'pool_states\|dex_config' /app --include=*.py"
```

This is exactly the check that caught the incident, run before the change
rather than after. It generalises to every submission in the current slate,
not just the champion — see §5.

Retirement is its own promote, with its own revert plan.

---

## 4. Why not a versioned `IntentSolver` base class

The obvious alternative — ship `IntentSolverV2` and let miners subclass the
new one — is worse here:

- it forks the benchmark harness (two ABCs to load, score and sandbox);
- it splits the champion contest across incompatible bases, and the relative
  adoption rule compares per-order across ONE corpus;
- it does not actually help, because the breaking surface is `MarketSnapshot`
  data, which both bases would hand to solvers identically.

The field-level lifecycle above gets the same outcome with no fork. Reserve a
genuine v2 base class for a change to the **method contract** (a new required
method, or a changed `generate_plan` signature), where additive evolution
genuinely cannot express it.

---

## 5. Tooling to build first

`tools/solver_surface_audit.py` — read-only, mirrors the two replay harnesses
already in `tools/`:

- enumerate the current slate's solver images from the submission store;
- grep each for a candidate symbol (`pool_states`, `SwapIntentContext`, …);
- report which submissions reference it, with file and line;
- exit non-zero if any do.

Wire it in as a **pre-merge gate for any change to the §2 table**. The
principle the two replay harnesses established — measure against the live
system before changing behaviour under it — was applied to corpus dedup and to
token seeding, and simply not applied to the SDK surface. This closes that.

---

## 6. Re-landing the reverted work under this rule

| Reverted | Re-land as |
|---|---|
| #1161 MarketSnapshot | Phase A: keep fields empty, delete the Uniswap machinery + synthetic pool generator. ~90% of the value, zero break. |
| #1163 v3 archetypes | Audit the slate for the context classes first. If referenced, keep them as aliases of the base while `build_typed_context` stops emitting them. |
| #1166 token seeding | Platform-internal — no solver surface. Re-land as-is. |
| #1168 quote allowlist | Platform-internal — no solver surface. Re-land as-is; still a pack-hash move needing a fleet-uniform promote. |
| #1141 quality metric | Platform-internal, plus the unresolved §6 mutability question from `app-quality-metric.md`. |

#1166 and #1168 were reverted only because they sat on top of the breaking
commits in one batch, not because anything was found wrong with them.

---

## 7. Process changes

1. **Grep the slate, not the repo**, before removing anything in §2.
2. **A repo-wide search returning nothing is not evidence of no consumers** —
   say which populations were searched and which were not.
3. **Batch by blast radius, not by theme.** Five PRs merged together meant one
   breaking change forced four safe ones to be reverted with it.
4. **Treat a retracted safety warning as a claim needing its own evidence.**
   Downgrading a risk is a load-bearing assertion, not a neutral edit.
