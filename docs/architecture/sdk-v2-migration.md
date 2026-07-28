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
access (the line the incident was predicted from):

```
king_base.py:2926   pool_states = (snapshot.pool_states if snapshot and snapshot.pool_states else {}) or {}
king_base.py:4048   pool_states = (snapshot.pool_states if snapshot and snapshot.pool_states else {}) or {}
solver.py:401       ps = getattr(snapshot, "pool_states", None)      # explicitly tolerant
```

### What the removal actually does — solvers VENDOR the SDK

`/app/minotaur_subnet` inside a solver image is the miner's OWN copy.
`import minotaur_subnet` in the container resolves there, never to the
validator's installed package, and the container runs its own
`harness/runner.py` -> `dict_to_snapshot` over JSON on stdin. Verified on the
live images (#1175).

That determines the blast radius of any removal:

| Kind | Effect of removing it from this repo |
|---|---|
| **SDK symbol** (class, helper) | the vendored copy is untouched; the solver keeps importing its own. Diverges only when the miner re-vendors. |
| **Wire field** (a serialised `MarketSnapshot` / `IntentState` attribute) | we stop SENDING the key; the vendored `dict_to_snapshot` defaults it, so `snapshot.pool_states` yields `{}` and **does not raise** |
| **Protocol shape** (renaming a JSON key the vendored runner requires) | the only class that genuinely crashes an existing solver |

So #1161 would **not** have raised `AttributeError`. The champion's guard
would have gone falsy and it would have fallen through to its RPC branch. The
batch was reverted on the belief that it would crash the incumbent; that
belief was wrong.

The revert still stands, for a different and less dramatic reason: **silent
divergence**. An incumbent's fallback path goes dead, its scores move, and
nothing errors anywhere — harder to attribute after the fact than a crash,
not easier.

### The reasoning error, stated plainly

The audit asked "does anything read this field?" and searched *this repo*.
That answers a different question. Solver code lives in miners' repos, is
forked-and-improved from a base we publish, and is invisible to grep here. An
in-repo search returning nothing is not evidence of no consumers — it is
evidence of no consumers *we can see*, which is the smaller half.

The deeper error is that the same claim was then asserted in BOTH directions
from evidence that could not reach it: first "this breaks every existing
solver", then a retraction of that as "unverified and overstated", then an
urgent revert on a predicted `AttributeError` that vendoring makes impossible.
Three positions, none measured. The fix is not to hold the opposite belief
more firmly — it is to stop asserting miner-side behaviour without reading
miner-side code, which is what `tools/solver_surface_audit.py` now does.

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
> `RebalanceIntentContext` (#1163). That question is now **answered** rather
> than assumed: `solver_surface_audit --preset v3-contexts` finds 20
> dependencies across the live images, including
> `from minotaur_subnet.v3.contexts import SwapIntentContext` and an
> `isinstance()` on it. Under vendoring it would not have crashed, but the
> isinstance branch would have silently stopped matching — so #1163 was not
> independently safe either.

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

Note carefully what this does and does not buy, because the naive version of
the argument is wrong under vendoring:

- It does **not** avoid an `AttributeError` today. A vendored solver defaults
  the field either way, so "removed" and "present but empty" look identical
  to it right now.
- It **does** avoid a delayed, self-inflicted break on RE-VENDOR. Once the
  field is gone from our SDK, the next miner to re-vendor gets a dataclass
  without it, and their existing `snapshot.pool_states` line starts raising —
  at a moment of their choosing, in their image, looking like our fault.
  Keeping the field deprecated-but-present means a re-vendor stays safe and
  gives them a window to migrate.
- Both variants change behaviour silently (empty pool state). That is
  unavoidable if the platform is to stop modelling a DEX, and is exactly what
  the deprecation signal in Phase B is for.

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

This is the check that answered the question, run before the change
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

`tools/solver_surface_audit.py` — **built and landed in #1175**. Read-only,
mirrors the two replay harnesses already in `tools/`:

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
| #1161 MarketSnapshot | Phase A: keep fields empty, delete the Uniswap machinery + synthetic pool generator. ~90% of the value, no re-vendor landmine. |
| #1163 v3 archetypes | Audited (#1175): 20 live dependencies. Keep the classes as aliases of the base while `build_typed_context` stops emitting them. |
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
