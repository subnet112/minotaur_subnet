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

## Follow-ups (tracked)

- [ ] Reconciler sweep (complete missed win / auto-revert orphaned or uncertified merge).
- [ ] `_revert_champion_merge` compensation helper (built on `_publish_certified_tree_to_canonical`).
- [ ] Leader-side relayer health-gate + bounded retry in `on_champion_adopted_via_relayer`.
- [ ] `update.sh` / compose ordering: api finalizes only after the relayer is healthy.
- [ ] Durable finalize journal for cross-restart resume (belt-and-suspenders to the deadline-bounded retry).
