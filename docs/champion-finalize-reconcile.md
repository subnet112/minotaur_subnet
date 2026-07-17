# Champion finalize: defer-not-abort + reconcile

## The incident (2026-07-17)

Challenger `sub_89b7f6f8cae6` legitimately won and certified `round-e29737890`.
Finalization (relayer `_finalize_core` → `on_champion_adopted_pr`: on-chain attest
then squash-merge) **ran on the relayer** and the PR **merged** (`e0d68e1`). But the
08:00 `update.sh` cycle had just recreated the stack, and the leader's api came up
~21 s before the `relayer` hostname was DNS-resolvable. The leader's
`on_champion_adopted_via_relayer` POST therefore failed with `NameResolutionError`,
the `#326` merge-gate **aborted the round** (`merge_failed:relayer_unreachable`), and
the merge was **orphaned on `main`** while the throne/emissions stayed with the old
champion (aurora-router). A permanent split, healed manually by reverting the merge.

**Root cause:** the merge-gate treated an *unknown* finalize outcome (the leader
couldn't reach/parse the relayer) identically to a *definitive* refusal — it aborted
a round whose finalize may have already landed, with no resume and no reconcile.

## Design (chosen: keep attest→merge, add resume/reconcile)

The on-chain attest is deliberately the **prerequisite + sole authority** for the
merge (`merge_miner_pr_when_certified` refuses unless an on-chain quorum cert binds
the head), so the order stays **attest → merge**. We do **not** invert it. Instead:

1. **Defer, don't abort, on an UNKNOWN outcome.** `on_champion_adopted_via_relayer`
   already tags leader-can't-reach/parse failures with **`stage="client"`**
   (`relayer_unreachable`, `relayer_http_*`, `relayer_bad_reply`), distinct from the
   relayer's definitive refusals (`stage ∈ {validation, attest, merge, internal}`).
   On `stage="client"` the merge-gate now **leaves the round CERTIFIED** and returns
   `deferred=True` instead of aborting. The coordinator re-drives activation on a
   later tick; the finalize is idempotent (already-attested / already-merged →
   success, drifted PR → publish-certified-tree), so the retry **completes**. Bounded
   by `decision_deadline_epoch`: past it, abort (and reconcile any orphaned merge).
2. **Truth-based adopt / reconciler** (follow-up): a sweep over recently-certified
   rounds that reconciles against ground truth — `head attested on-chain?` +
   `head on main?` — and heals **both** directions: an attested+merged win the leader
   missed → **complete** the adoption; a merge that landed with no valid attestation
   (or that won't adopt by deadline) → **auto-revert** it.
3. **Relayer health-gate + startup ordering** (follow-up): the leader probes the
   relayer before finalizing and retries transient unreachability with backoff; and
   `update.sh` orders the api after the relayer is healthy so a boot race can't
   trigger a half-finalize.

This keeps the security model (on-chain cert = merge authority) intact, fixes the
actual failure (lost reply + blind re-attempt), and still guarantees `main` is never
left ahead of the throne (the reconciler reverts orphaned merges).

## In this PR (Part 1 — the core, incident-preventing change)

- `epoch/manager.py` `activate_certified_round`: capture the callback `stage`;
  on `stage="client"` within `decision_deadline_epoch`, **defer** (`deferred=True`,
  round stays CERTIFIED) instead of aborting; past the deadline, abort as before.
- `api/startup.py` `_maybe_activate_certified_round`: return `False` on
  `deferred` so the loop does **not** `open_next_round` and re-drives on a later tick.
- Tests: defer-on-unknown, abort-on-definitive-refusal, abort-on-unknown-past-deadline.

Part 1 alone would have prevented the 2026-07-17 split: `relayer_unreachable`
(`stage="client"`) now defers, and the coordinator completes the adoption once the
relayer is reachable — well within the ~30-min `decision_deadline` window.

## In this PR (Part 2 — reconciler + auto-revert)

`minotaur_subnet/relayer/champion_reconcile.py`:

- `classify_main_reconcile(main_tree, adopted_tree, onchain_throne_is_adopted)` — the
  pure decision. **REVERT only** when `main` drifted from the adopted champion **and**
  the on-chain throne is still the adopted champion (the merge never took the throne =
  orphan). A moved throne → **ALERT** (a real win the leader hasn't adopted — never
  auto-reverted). Missing/ambiguous input → NOOP/ALERT. Exhaustively unit-tested.
- `revert_main_to_tree(...)` — the auto-revert: a forward commit whose tree == the
  adopted champion's tree, parented on the drifted head (restores content regardless of
  drift depth, no history rewrite). FAIL-CLOSED on any write error.
- `onchain_throne_is_adopted(commit, round)` — the throne signal via `_onchain_cert_binds`;
  **False on any read error** so a bad read can never force a revert.
- `reconcile_champion_main(...)` / `run_reconcile_pass(...)` — orchestration + `dry_run`.
- `api/startup.py`: a leader-only sweep loop, **OBSERVE-ONLY by default** (detect + log,
  no write). Arm the auto-revert with `CHAMPION_RECONCILE_ENFORCE=1` after soak. Kill:
  `CHAMPION_MAIN_RECONCILE=0`; cadence `CHAMPION_MAIN_RECONCILE_SECONDS` (default 300s).

Together with Part 1, a half-completed finalization now (a) defers + completes when the
relayer returns, and (b) if it still stranded an orphaned merge, the sweep reverts `main`
back to the adopted champion — automating exactly the 2026-07-17 manual heal.

## In this PR (Part 3a — relayer health-gate)

`on_champion_adopted_via_relayer` (`solver_repo.py`) now **probes the relayer's
`/health` before POSTing the finalize** (`_relayer_ready`). If the relayer isn't ready
— the exact window the 08:00 `update.sh` recreate opened, where the api came up before
the relayer's DNS/port — it returns `stage="client"` so the merge-gate **DEFERS** (Part
1) instead of aborting, and it **never POSTs** a finalize the relayer might half-apply.
Single fast probe (no in-line sleep — the coordinator's re-drive cadence is the retry,
so the event loop is never blocked). Kill switch `RELAYER_HEALTH_GATE=0`. Tests in
`test_champion_adopted_via_relayer.py`.

## Why the ordering fix (3b) is *not* viable — health-gate supersedes it

Investigated on the leader: **`relayer.depends_on: [api]`** — the relayer is
*designed* to start **after** the api. Adding `api.depends_on: relayer` is therefore a
**dependency cycle** (`docker compose config` → "dependency cycle detected: api ->
relayer -> api"), and reordering `update.sh`'s service list just fights that dependency.
So compose/`update.sh` ordering **cannot** guarantee relayer-before-api. This is
precisely why the api can finalize before the relayer is ready — and it's exactly what
the Part-3a health-gate handles by deferring. **The health-gate is the correct and
sufficient fix; the ordering follow-up is closed as not-viable.**

## Follow-ups (tracked)

- [x] Reconciler sweep + auto-revert (Part 2 — observe-default).
- [x] Relayer health-gate before finalize (Part 3a).
- [x] ~~`update.sh`/compose ordering~~ — closed: blocked by the `relayer → api` dependency cycle; the health-gate supersedes it.
- [ ] Durable finalize journal for cross-restart resume (belt-and-suspenders to the deadline-bounded retry).
- [ ] Soak the reconciler in observe, validate the drift/throne signal, then arm `CHAMPION_RECONCILE_ENFORCE=1`.
