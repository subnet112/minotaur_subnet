"""Round-entry rotation: fair slate selection across rounds (LRU, not first-come).

A round benches at most ``SOLVER_ROUND_MAX_SUBMISSIONS`` submissions, but with
rotation the intake no longer turns miners away once that many have arrived
(the old first-come behaviour). Every submission accepted during the OPEN
window is a slate CANDIDATE; at close the leader selects the miners that were
benchmarked LONGEST AGO (never-benched first) and rejects the overflow with an
explicit resubmit-next-round reason. A skipped miner's rotation seniority keeps
growing, so selection is starvation-free by construction: with M contending
miners and N slots, every miner is benched at least once every ceil(M/N) rounds.

Ties (equal seniority — e.g. two never-benched miners) break by
``sha256(hotkey:round_id)``, so the order reshuffles every round and anyone can
recompute it from public data — no alphabetical or arrival-time advantage.

The ledger is LEADER-LOCAL operator state, the same category as the intake caps
(admission control, not a fleet-consensus parameter): followers simply mirror
whatever slate the leader closed with via the close broadcast's submission
snapshot. Losing the ledger degrades gracefully — everyone ties at
"never benched" and the salted-hash shuffle decides until history rebuilds.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
import threading
import time
from collections.abc import Iterable, Mapping
from typing import Any

logger = logging.getLogger(__name__)

# Statuses OUT of the running for slate selection: already-rejected submissions
# (screening fail etc.) don't occupy a slot; ADOPTED can't occur pre-close;
# WAITLISTED is a prior rotation pass's decision (re-running must not re-process
# it).
_TERMINAL_STATUSES = ("rejected", "adopted", "waitlisted")


def _status_value(sub: Any) -> str:
    st = getattr(sub, "status", None)
    return str(getattr(st, "value", None) or st or "")


def is_terminal_status(sub: Any) -> bool:
    """Is this submission OUT of the running for its round (rejected /
    adopted / waitlisted)?

    The single shared definition of round-terminality — the same
    ``_TERMINAL_STATUSES`` that slate selection and the decision-window
    autoscale use (see :func:`benchable_candidate_count` for the #620 incident
    that taught the one-definition discipline). The screening pipeline's
    re-queue guard MUST use this too: it used to check only REJECTED while
    rotation had switched to parking overflow as WAITLISTED, so a
    late-finishing screening silently overwrote a terminal waitlist back to
    BENCHMARKING and busted the slate cap.
    """
    return _status_value(sub) in _TERMINAL_STATUSES


def benchable_candidate_count(subs: Iterable[Any]) -> int:
    """How many of ``subs`` a rotation pass would consider — i.e. how many get
    BENCHED when rotation is disabled (``slots <= 0``) or fails.

    Shares ``_TERMINAL_STATUSES`` with :func:`apply_rotation_slate` on purpose:
    the decision-window autoscale used to keep its OWN copy of this rule as
    "status != rejected", and when #620 parked rotation's overflow in
    ``waitlisted`` instead of ``rejected`` the two silently diverged — never-benched
    submissions inflated the window until activation outlived the champion
    approval and certify() reverted "Expired". One definition, one place.
    """
    return len([s for s in subs if _status_value(s) not in _TERMINAL_STATUSES])


def rotation_ledger_path() -> str:
    """Path of the leader-local rotation ledger.

    ``SOLVER_ROTATION_LEDGER_PATH`` wins; otherwise the ledger lives next to
    the round store (``SOLVER_ROUND_STORE_PATH``) so it lands on the same
    persistent volume (/data in production, per #430). Shared by the api's
    close-time rotation and the benchmark worker's slate-width belt so both
    read the SAME ledger.
    """
    explicit = os.environ.get("SOLVER_ROTATION_LEDGER_PATH", "").strip()
    if explicit:
        return explicit
    round_store_path = os.environ.get("SOLVER_ROUND_STORE_PATH", "").strip()
    base = os.path.dirname(round_store_path) if round_store_path else "."
    return os.path.join(base or ".", "solver_rotation.json")


def absence_reset_seconds() -> float:
    """Absence window after which accrued seniority is forfeited (seconds).

    ``SOLVER_ROTATION_ABSENCE_RESET_SECONDS``; code default 4 days, ``0``
    disables. Seniority must be EARNED BY PRESENCE: a miner idle 24 days used
    to re-enter at the FRONT of the queue (its wait clock kept aging while it
    was gone), out-senioring everyone actively rotating. With this window, an
    identity whose last submission activity is older than the threshold
    re-enters as a NEWCOMER (clock = return time) — the same junior-ing the
    fresh-mint rule applies, extended to lapsed identities. The discriminator
    is ACTIVITY, not clock age: a continuously-submitting miner merely starved
    by capacity (233h waits were real under the 3-seat slate) keeps its full
    seniority. Default lives in code so a leader failover keeps the guard.
    """
    raw = os.environ.get("SOLVER_ROTATION_ABSENCE_RESET_SECONDS", "").strip()
    try:
        return float(raw) if raw else 4 * 86400.0
    except ValueError:
        return 4 * 86400.0


def fold_reset(
    benched: dict[str, float],
    reset: dict[str, float],
) -> dict[str, float]:
    """Fold absence-reset stamps into the benched map (max-merge) — PURE.

    A reset acts like a phantom bench at the return time: :func:`wait_ts`
    prefers the benched anchor when present, so max-merging the reset there
    makes the returner exactly as junior as someone benched at that moment —
    without corrupting the honest on-disk ``benched`` map (``last_benched_at``
    display stays real). Works at both hotkey level and after the MAX actor
    aggregation (:func:`actor.actor_last_selected` preserves the fold).
    """
    if not reset:
        return benched
    out = dict(benched)
    for k, ts in reset.items():
        if ts > out.get(k, 0.0):
            out[k] = ts
    return out


def actor_evidence_map(
    active: dict[str, float],
    benched: dict[str, float],
    seen: dict[str, float],
    actor_of: Any = None,
) -> dict[str, float]:
    """Last evidence of life per actor — PURE, shared by the absence-reset
    detection and the queue endpoint's display so the two can never drift.

    Per hotkey: submission activity where known, else any ledger trace
    (bench / first-seen). Per actor: MAX over ALL its hotkeys — a fleet is
    absent only if all its identities were (one active sibling keeps the
    whole actor's clock alive, consistent with shared actor seniority).
    """
    out: dict[str, float] = {}
    for hk in set(active) | set(benched) | set(seen):
        ev = active.get(hk, 0.0) or max(benched.get(hk, 0.0), seen.get(hk, 0.0))
        actor = (actor_of(hk) if actor_of is not None else hk) or hk
        if ev > out.get(actor, 0.0):
            out[actor] = ev
    return out


def wait_ts(
    key: str,
    benched: dict[str, float],
    seen: dict[str, float],
    now: float,
) -> float:
    """The seniority timestamp for a hotkey OR actor: LOWER sorts first (gets a
    slot), so this is "waiting since when".

      * benched before  -> its last-benched ts (recently benched = junior).
      * never benched    -> its first-seen ts (long-waiting newcomer = senior;
                            fresh mint's first-seen ~= now = junior).
      * never seen yet   -> ``now`` (brand-new this round = most junior).

    This is the whole anti-mint idea: a fresh identity can't out-senior a miner
    who has genuinely been waiting, because a fresh identity's clock starts now.
    """
    if key in benched:
        return benched[key]
    if key in seen:
        return seen[key]
    return now


def rotation_sort_key(
    hotkey: str,
    round_id: str,
    benched: dict[str, float],
    seen: dict[str, float] | None = None,
    now: float | None = None,
) -> tuple[float, str]:
    """(wait_ts, salted tie-break) — lower sorts first. See :func:`wait_ts`.

    ``seen``/``now`` default to empty/0.0 for the legacy call shape; with them
    a never-benched hotkey ranks by first-seen age instead of jumping the queue.
    """
    return (
        wait_ts(hotkey, benched, seen or {}, now if now is not None else 0.0),
        hashlib.sha256(f"{hotkey}:{round_id}".encode()).hexdigest(),
    )


def actor_rotation_sort_key(
    hotkey: str,
    round_id: str,
    actor_benched: dict[str, float],
    actor_of: Any,
    actor_seen: dict[str, float] | None = None,
    now: float | None = None,
) -> tuple[float, str, str]:
    """Actor-keyed variant of :func:`rotation_sort_key` (see harness/actor.py).

    Seniority is the ACTOR's :func:`wait_ts` — its last bench (max over its
    hotkeys, ``actor_benched``) or, if it never benched, its first-seen (min
    over its hotkeys, ``actor_seen``); a brand-new actor defaults to ``now``.
    So a fleet's hotkeys share one seniority and one per-round shuffle position,
    and a freshly-minted coldkey/owner can't out-senior a genuine waiter. The
    hotkey-salted third element only orders submissions WITHIN one actor.
    """
    actor = actor_of(hotkey or "") or (hotkey or "")
    return (
        wait_ts(actor, actor_benched, actor_seen or {}, now if now is not None else 0.0),
        hashlib.sha256(f"{actor}:{round_id}".encode()).hexdigest(),
        hashlib.sha256(f"{hotkey}:{round_id}".encode()).hexdigest(),
    )


def select_rotation_slate(
    candidates: list[Any],
    slots: int,
    last_selected: dict[str, float],
    round_id: str,
    actor_of: Any = None,
    seen: dict[str, float] | None = None,
    now: float | None = None,
    structural_collapse: bool = False,
) -> tuple[list[Any], list[Any]]:
    """PURE: split candidates into (selected, skipped) by wait-time order.

    Seniority is :func:`wait_ts` — last-benched, or first-seen for a
    never-benched identity (``seen``/``now``), so a fresh mint sits at the back
    instead of jumping the queue. With ``actor_of`` (hotkey → actor, see
    harness/actor.py) the key is actor-aggregated and selection soft-dedups per
    actor: the first pass seats at most ONE submission per actor — a fleet
    rotating N hotkeys holds one seat, not N — and only when fewer distinct
    actors than slots contend do an actor's further submissions fill the
    leftover seats. ``skipped`` stays in seniority order.

    ``structural_collapse`` (enforce mode) adds an orthogonal cut: seat at most
    ONE submission per structural fingerprint too, so a fleet of DISTINCT actors
    (coldkeys) running structurally-identical code (salted constants) — which
    the per-actor dedup cannot see — holds one seat, not N. Freed slots backfill
    from the overflow in seniority order but NEVER re-seat an already-benched
    fingerprint (else the wide fleet just refills the slots its collapse freed);
    a legit repeat-actor with distinct code can. This changes the benched slate
    -> the pack hash, so it MUST be promoted fleet-uniform. Fingerprint-less
    submissions never collapse.
    """
    slots = max(0, int(slots))
    seen = seen or {}
    now = now if now is not None else time.time()
    if actor_of is None:
        ordered = sorted(
            candidates,
            key=lambda s: rotation_sort_key(
                getattr(s, "hotkey", "") or "", round_id, last_selected, seen, now,
            ),
        )
        return ordered[:slots], ordered[slots:]

    from minotaur_subnet.harness.actor import actor_first_seen, actor_last_selected

    actor_last = actor_last_selected(last_selected, actor_of)
    actor_seen = actor_first_seen(seen, actor_of)
    ordered = sorted(
        candidates,
        key=lambda s: actor_rotation_sort_key(
            getattr(s, "hotkey", "") or "", round_id, actor_last, actor_of,
            actor_seen, now,
        ),
    )
    selected: list[Any] = []
    overflow: list[Any] = []
    seated_actors: set[str] = set()
    seated_structs: set[str] = set()

    def _struct(sub: Any) -> str | None:
        if not structural_collapse:
            return None
        return getattr(sub, "structural_fingerprint", None) or None

    for sub in ordered:
        hk = getattr(sub, "hotkey", "") or ""
        actor = actor_of(hk) or hk
        sfp = _struct(sub)
        blocked = actor in seated_actors or (sfp is not None and sfp in seated_structs)
        if len(selected) < slots and not blocked:
            selected.append(sub)
            seated_actors.add(actor)
            if sfp is not None:
                seated_structs.add(sfp)
        else:
            overflow.append(sub)
    # Fewer distinct actors than slots: fill from the overflow in seniority
    # order (repeat actors) rather than waste bench capacity. Under
    # structural_collapse, skip any overflow whose fingerprint is already
    # seated so the freed slots go to distinct code, not the fleet's spares.
    i = 0
    while len(selected) < slots and i < len(overflow):
        sfp = _struct(overflow[i])
        if sfp is not None and sfp in seated_structs:
            i += 1
            continue
        sub = overflow.pop(i)
        selected.append(sub)
        if sfp is not None:
            seated_structs.add(sfp)
    return selected, overflow


def structural_dedup_clusters(
    subs: list[Any],
    actor_of: Any = None,
) -> list[list[Any]]:
    """Group submissions that share a structural fingerprint ACROSS DISTINCT
    actors — the sybil signature (one codebase, N coldkeys, salted constants).

    Returns one list per offending fingerprint, each holding the ≥2
    submissions that share it AND belong to ≥2 distinct actors, sorted so the
    caller can keep a stable representative. Submissions with no structural
    fingerprint (unparseable / pre-metric) are ignored — never grouped, so a
    missing value can't manufacture a false cluster.

    PURE + observe-safe: does not mutate or select. ``select_rotation_slate``
    already soft-dedups per actor; this is the orthogonal cut that a fleet of
    DISTINCT coldkeys running identical code slips through. Phase 0 only
    reports these; arming (collapsing them to one slot) is gated and MUST be
    promoted fleet-uniform because it changes the benched slate → the pack
    hash.
    """
    by_fp: dict[str, list[Any]] = {}
    for s in subs:
        fp = getattr(s, "structural_fingerprint", None)
        if fp:
            by_fp.setdefault(fp, []).append(s)

    clusters: list[list[Any]] = []
    for fp, group in by_fp.items():
        if len(group) < 2:
            continue
        actors = {
            (actor_of(getattr(g, "hotkey", "") or "") if actor_of else None)
            or (getattr(g, "hotkey", "") or "")
            for g in group
        }
        if len(actors) >= 2:
            clusters.append(list(group))
    return clusters


class RotationLedger:
    """Per-hotkey seniority timestamps with atomic JSON persistence.

    Two maps, both ``{hotkey: unix_ts}``:

      * ``benched`` — when the hotkey last occupied a bench slot (``mark_selected``).
      * ``seen``    — when the hotkey FIRST appeared (``mark_seen``): anchored
                      at its earliest submission ``created_at``, min-write.

    Seniority (see :func:`wait_ts`) is "how long since you last benched, or —
    if you never have — since you first appeared." A never-benched hotkey is
    therefore ranked by its first-seen age, NOT handed instant most-senior
    status, so minting a fresh identity buys the back of the queue, not the
    front. This replaced the never-benched=0.0 rule + the build-budget's
    newcomer pool: one wait-time LRU, no reserved newcomer share to farm.

    ``seen`` is anchored to submission ``created_at`` (server-assigned at
    intake), NOT to "the time a submission survived a rotation pass": a
    submission parked terminal pre-close (e.g. by the build-budget flush) still
    anchors its hotkey's clock, so losing the build race can never restart an
    honest waiter's seniority (the round-N starvation bug). Min-write: a later
    stamp can only move an entry EARLIER (toward the true first appearance,
    e.g. store-history backfill at ledger-v2 migration), never later — so
    re-submitting can neither refresh nor game the clock.

    On-disk format is ``{"benched": {...}, "seen": {...}}``; a legacy flat
    ``{hotkey: ts}`` file loads as ``benched`` (its hotkeys also seed ``seen``
    at their benched ts, so pre-existing miners aren't treated as brand-new).
    Never-benched miners have no flat-file entry at all — their first-seen is
    reconstructed from submission-store history (earliest ``created_at``) by
    the first post-upgrade rotation pass, so days of pre-upgrade waiting keep
    their place in the queue. Single-writer (the leader's round coordinator);
    best-effort — a lost write only delays fairness by one round, never
    corrupts one.
    """

    def __init__(self, path: str) -> None:
        self._path = str(path)

    def _load_raw(
        self,
    ) -> tuple[dict[str, float], dict[str, float], dict[str, float], dict[str, float]]:
        """(benched, seen, active, reset) — v3 on-disk shape.

        ``active`` = last submission activity per hotkey (max-write, stamped
        every rotation pass); ``reset`` = absence-reset stamps (max-write, the
        "your clock restarted here" record — folded into benched at READ time
        via :func:`fold_reset`, never mutating the honest bench history).
        v2 files ({benched, seen}) load with empty active/reset; legacy flat
        files load as before.
        """
        try:
            with open(self._path) as f:
                raw = json.load(f)
        except FileNotFoundError:
            return {}, {}, {}, {}
        except Exception:
            logger.warning(
                "rotation ledger unreadable (%s) — treating all miners as never-benched",
                self._path, exc_info=True,
            )
            return {}, {}, {}, {}
        if not isinstance(raw, dict):
            return {}, {}, {}, {}

        def _floats(d: Any) -> dict[str, float]:
            out: dict[str, float] = {}
            if isinstance(d, dict):
                for k, v in d.items():
                    try:
                        out[str(k)] = float(v)
                    except (TypeError, ValueError):
                        continue
            return out

        if isinstance(raw.get("benched"), dict) or isinstance(raw.get("seen"), dict):
            return (
                _floats(raw.get("benched")),
                _floats(raw.get("seen")),
                _floats(raw.get("active")),
                _floats(raw.get("reset")),
            )
        # Legacy flat file: {hotkey: last_benched_ts}. Seed `seen` from it so
        # miners with history aren't demoted to brand-new on the first v2 write.
        benched = _floats(raw)
        return benched, dict(benched), {}, {}

    def load(self) -> dict[str, float]:
        """The benched map ``{hotkey: last_selected_ts}`` (name kept for the
        many existing call sites)."""
        return self._load_raw()[0]

    def load_seen(self) -> dict[str, float]:
        """The first-seen map ``{hotkey: first_seen_ts}``."""
        return self._load_raw()[1]

    def load_active(self) -> dict[str, float]:
        """The activity map ``{hotkey: last_submission_created_at}``."""
        return self._load_raw()[2]

    def load_reset(self) -> dict[str, float]:
        """The absence-reset map ``{hotkey: clock_restarted_at}``."""
        return self._load_raw()[3]

    def _persist(
        self,
        benched: dict[str, float],
        seen: dict[str, float],
        active: dict[str, float],
        reset: dict[str, float],
    ) -> None:
        tmp = None
        try:
            fd, tmp = tempfile.mkstemp(
                dir=os.path.dirname(self._path) or ".", prefix=".rotation-",
            )
            with os.fdopen(fd, "w") as f:
                json.dump(
                    {"benched": benched, "seen": seen, "active": active, "reset": reset},
                    f,
                )
            os.replace(tmp, self._path)
            tmp = None
        except Exception:
            logger.warning("rotation ledger write failed (%s)", self._path, exc_info=True)
        finally:
            if tmp:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass

    def mark_selected(self, hotkeys: list[str], ts: float) -> None:
        benched, seen, active, reset = self._load_raw()
        for hk in hotkeys:
            if hk:
                benched[hk] = float(ts)
                seen.setdefault(hk, float(ts))  # a benched hotkey has been seen
        self._persist(benched, seen, active, reset)

    def mark_seen(self, seen_at: Mapping[str, float]) -> None:
        """Anchor first-seen for ``{hotkey: earliest created_at}`` — min-write.

        A new entry is stamped as given; an existing entry only moves EARLIER
        (``min``), never later — so a hotkey's clock is pinned to its true
        first appearance: re-submission can't refresh it, and a store-history
        backfill (migration) can only restore seniority, not shrink it.
        """
        benched, seen, active, reset = self._load_raw()
        changed = False
        for hk, ts in seen_at.items():
            if not hk:
                continue
            ts = float(ts)
            cur = seen.get(hk)
            if cur is None or ts < cur:
                seen[hk] = ts
                changed = True
        if changed:
            self._persist(benched, seen, active, reset)

    def mark_active(self, active_at: Mapping[str, float]) -> None:
        """Record submission activity ``{hotkey: created_at}`` — max-write.

        The activity anchor the absence rule measures gaps against. Max-write:
        activity can only move FORWARD; a stale backfill can never shrink a
        fresher record.
        """
        benched, seen, active, reset = self._load_raw()
        changed = False
        for hk, ts in active_at.items():
            if not hk:
                continue
            ts = float(ts)
            if ts > active.get(hk, 0.0):
                active[hk] = ts
                changed = True
        if changed:
            self._persist(benched, seen, active, reset)

    def mark_reset(self, hotkeys: Iterable[str], ts: float) -> None:
        """Stamp absence resets ``hotkey -> ts`` — max-write.

        Persisted so the demotion STICKS: without it a returner would be
        junior for exactly one round and then jump back to the front on its
        stale bench anchor next round.
        """
        benched, seen, active, reset = self._load_raw()
        changed = False
        for hk in hotkeys:
            if hk and float(ts) > reset.get(hk, 0.0):
                reset[hk] = float(ts)
                changed = True
        if changed:
            self._persist(benched, seen, active, reset)


def _notify_skipped_in_background(
    notify: Any,
    items: list[tuple[Any, str | None]],
    reason: str,
    round_id: str,
) -> threading.Thread:
    """Fire the per-submission not-selected notifications OFF the close path.

    Each ``notify(sub, reason, repo_token)`` posts a GitHub PR comment —
    seconds of blocking network per private submission. Run serially inline
    (the pre-fix behaviour), a 20-candidate round freezes the event loop for a
    minute+: /health goes dark, uvicorn's SIGTERM handler can never run, and a
    container stop escalates to SIGKILL mid-close (observed 2026-07-07,
    round-e29724243-n1). A daemon thread keeps the feedback best-effort without
    holding the round close hostage; a crash mid-thread only loses PR comments,
    never rejects (those already landed in phase 1).
    """
    def _run() -> None:
        posted = 0
        for sub, token in items:
            try:
                notify(sub, reason, token)
                posted += 1
            except Exception:
                logger.warning(
                    "rotation notify failed for %s (ignored)",
                    getattr(sub, "submission_id", "?"), exc_info=True,
                )
        logger.info(
            "rotation notify for %s: %d/%d not-selected comments attempted",
            round_id, posted, len(items),
        )

    thread = threading.Thread(
        target=_run, name=f"rotation-notify-{round_id}", daemon=True,
    )
    thread.start()
    return thread


def apply_rotation_slate(
    sub_store: Any,
    round_id: str,
    slots: int,
    ledger: RotationLedger,
    now: float | None = None,
    notify: Any = None,
) -> dict[str, Any]:
    """Select the round's benched slate by rotation and reject the overflow.

    Runs on the LEADER at round close, before the close snapshot is built, so
    followers mirror the post-rotation submission set. Rotation applies to ALL
    live submissions regardless of screening/benchmark progress — fairness must
    not depend on who screened fastest, or the arrival race the rotation exists
    to remove comes straight back through the side door.

    ``slots <= 0`` disables rotation (matches the cap's 0-=-unlimited
    convention). Selected miners' ledger entries advance even when the round is
    uncontested, so seniority always reflects the last actual bench.

    TRUNCATION-PROOF DESIGN (two phases). The old shape interleaved a slow
    network call per skipped submission (notify → GitHub PR comment, seconds
    each) with its store reject; killing the process mid-sweep abandoned the
    tail of the rejects and the un-rejected survivors were benched, busting the
    slate width (2026-07-07: 12 of 19 rejects landed, 10 scored on 3 slots).
    Now:

      Phase 1 — REJECT (fast, local-only, no network): for every skipped
      submission, capture its private-repo token (reject purges it, and the PR
      comment needs it), then ``store.reject``. Per-submission failures are
      contained; a ``CancelledError``/``BaseException`` mid-sweep still lands
      every remaining reject via a tight store-only loop before re-raising.

      Phase 2 — NOTIFY (best-effort, background thread): post the
      not-selected PR comments with the tokens retained from phase 1, off the
      close path, so the event loop is never blocked and a slow/failing GitHub
      never delays or truncates anything.

    ``notify`` (optional) is called as ``notify(submission, reason,
    repo_token)`` where ``repo_token`` is the token captured BEFORE the
    terminal reject purged it (None for public submissions).
    """
    if slots <= 0:
        return {"applied": False, "reason": "rotation disabled (slots <= 0)"}
    from minotaur_subnet.harness.actor import distinct_actor_count, snapshot_resolver

    subs = sub_store.list_by_round(round_id)
    candidates = [s for s in subs if _status_value(s) not in _TERMINAL_STATUSES]
    # None (kill-switch, or no coldkey data yet) => the legacy per-hotkey
    # selection below (still wait-time ordered, just not actor-aggregated).
    actor_of = snapshot_resolver()
    now_ts = time.time() if now is None else now
    # Record first-seen BEFORE selecting, so a never-benched identity ages from
    # its first appearance and a fresh mint sorts junior. Anchored at each
    # submission's server-assigned ``created_at`` and swept over ALL of the
    # round's submissions — terminal ones included: the build-budget flush has
    # already parked its unbuilt waiters as WAITLISTED by the time this runs,
    # and skipping them would restart their wait clock every round they lose
    # the build race (starving an honest newcomer forever). Enriched with the
    # store's all-time earliest created_at per hotkey (when the store offers
    # it), which also auto-migrates never-benched miners off the legacy flat
    # ledger: their pre-upgrade waiting is reconstructed from submission
    # history instead of being reset to "now". mark_seen is min-write, so none
    # of this can ever move a clock later.
    earliest: dict[str, float] = {}
    for s in subs:
        hk = getattr(s, "hotkey", "") or ""
        if not hk:
            continue
        try:
            created = float(getattr(s, "created_at", 0.0) or 0.0)
        except (TypeError, ValueError):
            created = 0.0
        created = created or now_ts  # never stamp 0.0 (= instant max seniority)
        if hk not in earliest or created < earliest[hk]:
            earliest[hk] = created
    history = getattr(sub_store, "earliest_created_at_by_hotkey", None)
    if callable(history):
        try:
            for hk, ts in (history() or {}).items():
                if hk in earliest and ts and float(ts) < earliest[hk]:
                    earliest[hk] = float(ts)
        except Exception:
            logger.warning(
                "rotation: store history for first-seen anchoring failed "
                "(round-scope created_at used)", exc_info=True,
            )
    ledger.mark_seen(earliest)

    # ── Absence reset: seniority is earned by presence ───────────────────────
    # An identity whose last submission activity is older than the window
    # re-enters as a NEWCOMER: without this, a miner idle for weeks kept aging
    # its wait clock and re-entered at the FRONT of the queue, out-senioring
    # everyone actively rotating (observed 2026-07-26: 24d-idle at rank 1).
    # Keyed on ACTIVITY, never clock age — a continuously-submitting miner
    # merely starved by capacity keeps full seniority. Detection runs BEFORE
    # this round's activity stamp (a returner's fresh submission must not mask
    # the gap it returned from) and the reset is PERSISTED (mark_reset) so the
    # demotion sticks beyond the return round. Actor-level: a fleet is absent
    # only if ALL its identities were (one active sibling keeps the whole
    # actor's clock alive — consistent with shared actor seniority).
    threshold = absence_reset_seconds()
    if threshold > 0 and candidates:
        active = ledger.load_active()
        # Backfill prior activity from store history (excluding THIS round) so
        # the first post-upgrade pass never mistakes a starved-but-active miner
        # for a returning absentee. Best-effort, max-write semantics.
        history_latest = getattr(sub_store, "latest_created_at_by_hotkey", None)
        if callable(history_latest):
            try:
                for hk, ts in (history_latest(exclude_round_id=round_id) or {}).items():
                    if ts and float(ts) > active.get(hk, 0.0):
                        active[hk] = float(ts)
            except Exception:
                logger.warning(
                    "rotation: store history for activity backfill failed "
                    "(ledger activity only)", exc_info=True,
                )
        actor_evidence = actor_evidence_map(
            active, ledger.load(), ledger.load_seen(), actor_of,
        )

        def _actor(hk: str) -> str:
            return (actor_of(hk) if actor_of is not None else hk) or hk

        to_reset: list[str] = []
        lapsed: set[str] = set()
        for s in candidates:
            hk = getattr(s, "hotkey", "") or ""
            if not hk:
                continue
            a = _actor(hk)
            # Unknown actor (no trace at all) => genuinely new => already a
            # newcomer via first-seen; no reset needed.
            evidence = actor_evidence.get(a, now_ts)
            if now_ts - evidence > threshold:
                to_reset.append(hk)
                lapsed.add(a)
        if to_reset:
            ledger.mark_reset(to_reset, now_ts)
            logger.info(
                "rotation %s: absence reset for %d hotkey(s) / %d actor(s) — "
                "last activity > %.0fs ago; re-entering as newcomers",
                round_id, len(to_reset), len(lapsed), threshold,
            )
    # Stamp this round's activity AFTER detection (see above). Swept over ALL
    # of the round's submissions, terminal included — same rationale as
    # mark_seen: losing the build race is still presence.
    activity: dict[str, float] = {}
    for s in subs:
        hk = getattr(s, "hotkey", "") or ""
        if not hk:
            continue
        try:
            created = float(getattr(s, "created_at", 0.0) or 0.0)
        except (TypeError, ValueError):
            created = 0.0
        created = created or now_ts
        if created > activity.get(hk, 0.0):
            activity[hk] = created
    ledger.mark_active(activity)

    # Structural-dedup mode (env-gated, default OFF). In ``enforce`` the slate
    # collapses cross-actor structural clusters to one slot; ``observe`` only
    # logs. Read before selection so ``enforce`` feeds select_rotation_slate.
    from minotaur_subnet.harness.structural_fingerprint import structural_dedup_mode
    _dedup_mode = structural_dedup_mode()
    selected, skipped = select_rotation_slate(
        candidates, slots, fold_reset(ledger.load(), ledger.load_reset()),
        round_id, actor_of=actor_of, seen=ledger.load_seen(), now=now_ts,
        structural_collapse=(_dedup_mode == "enforce"),
    )
    if actor_of is not None:
        n_actors = distinct_actor_count(
            (getattr(s, "hotkey", "") or "" for s in candidates), actor_of,
        )
        logger.info(
            "rotation %s: %d candidate(s) from %d actor(s) -> %d selected "
            "(actor-keyed slate, map=%s)",
            round_id, len(candidates), n_actors, len(selected), actor_of.source,
        )

    # Structural-dedup (env-gated, default OFF): cross-actor clusters where
    # DISTINCT coldkeys run structurally-identical code (salted constants) —
    # the sybil that actor-keying alone can't collapse. Clusters are computed
    # over the whole candidate pool (not just the slate) for full-fleet
    # visibility; in ``enforce`` the slate was already collapsed above (<=1 per
    # fingerprint), so this only reports what happened. ``enforce`` changes the
    # benched slate -> the pack hash and must be promoted fleet-uniform.
    if _dedup_mode != "off":
        try:
            selected_ids = {id(s) for s in selected}
            clusters = structural_dedup_clusters(candidates, actor_of)
            for c in clusters:
                fp = getattr(c[0], "structural_fingerprint", "") or ""
                in_slate = sum(1 for s in c if id(s) in selected_ids)
                logger.warning(
                    "[structural-dedup %s] %s: %d submissions across distinct "
                    "actors share structural fingerprint %s — likely one sybil"
                    "%s: %s",
                    _dedup_mode.upper(), round_id, len(c), fp[:16],
                    (" (enforced: %d seated, %d excluded from slate)"
                     % (in_slate, len(c) - in_slate))
                    if _dedup_mode == "enforce"
                    else (" (%d currently in slate; would collapse to 1 when armed)"
                          % in_slate),
                    ", ".join(
                        f"{getattr(s, 'submission_id', '?')}"
                        f"(hk={(getattr(s, 'hotkey', '') or '')[:10]})"
                        for s in c
                    ),
                )
        except Exception:
            logger.debug("structural-dedup observe failed for %s", round_id, exc_info=True)

    # ── Structural CO-OCCURRENCE evidence (the automatic operator merge) ────
    # WHO shipped identical structure TOGETHER this round, recorded per actor
    # pair. The collapse above is per-round and per-fingerprint, so a ring that
    # re-rolls its fingerprint every round pays nothing for being caught; this
    # ledger keys on the ACTORS instead, which cost a registration burn each.
    # Swept over ALL of the round's submissions (not just the candidates): a
    # ring member parked by the build budget is still evidence of who submits
    # together. Merges take effect from the NEXT selection pass — this one
    # already froze its resolver. Best-effort: never blocks a close.
    from minotaur_subnet.harness.actor import (
        record_structural_coclusters,
        structural_merge_min_rounds,
        structural_merge_mode,
    )

    _merge_mode = structural_merge_mode()
    if _merge_mode != "off" and actor_of is not None:
        try:
            # Grouped by fingerprint over HOTKEYS, deliberately NOT through
            # structural_dedup_clusters: that helper needs >=2 distinct ACTORS,
            # which an already-merged ring no longer has — it would stop
            # refreshing its own evidence and the merge would lapse on the TTL.
            # record_structural_coclusters folds hotkeys to their pre-merge
            # identity and drops single-identity groups.
            _by_fp: dict[str, set[str]] = {}
            for s in subs:
                fp = getattr(s, "structural_fingerprint", None)
                hk = getattr(s, "hotkey", "") or ""
                if fp and hk:
                    _by_fp.setdefault(fp, set()).add(hk)
            _clusters = [hks for hks in _by_fp.values() if len(hks) >= 2]
            for group in record_structural_coclusters(round_id, _clusters):
                logger.warning(
                    "[structural-merge %s] %s: %d actors co-shipped identical "
                    "structure in >=%d rounds — one operator%s: %s",
                    _merge_mode.upper(), round_id, len(group),
                    structural_merge_min_rounds(),
                    (" (merged from the next pass: one queue seat, one build "
                     "unit, one submission per round)")
                    if _merge_mode == "enforce"
                    else " (would merge when armed; selection unchanged)",
                    ", ".join(sorted(a[:12] for a in group)),
                )
        except Exception:
            logger.warning(
                "structural co-occurrence pass failed for %s (ignored)",
                round_id, exc_info=True,
            )
    reject_reason = (
        f"not selected for {round_id} (rotation: "
        f"{len(candidates)} candidates, {slots} slots) — resubmit "
        f"next round; miners benched longest ago go first"
    )
    n_skipped = len(skipped)

    # skipped is in seniority order (best next-round priority FIRST), so the
    # 1-based index is the waitlist position. WAITLIST (not reject): not being
    # selected is a no-fault outcome that keeps next-round seniority. Falls back
    # to reject for stores without the method (older/test doubles).
    _waitlist = getattr(sub_store, "waitlist", None)

    def _park(sub: Any, position: int) -> None:
        if callable(_waitlist):
            _waitlist(
                sub.submission_id, reject_reason,
                outcome_code="rotation_not_selected",
                position=position, contenders=len(candidates), slots=slots,
            )
        else:
            sub_store.reject(sub.submission_id, reject_reason)

    # ── Phase 1: park every skipped submission (fast, no network) ────────────
    to_notify: list[tuple[Any, str | None]] = []
    get_token = getattr(sub_store, "get_repo_token", None)
    done = 0
    try:
        for idx, sub in enumerate(skipped):
            token = None
            if notify is not None and callable(get_token):
                try:
                    # Captured BEFORE the terminal transition purges the private
                    # token, which the phase-2 PR comment needs.
                    token = get_token(sub.submission_id)
                except Exception:
                    logger.warning(
                        "rotation token capture failed for %s (comment may "
                        "not post; waitlist unaffected)",
                        getattr(sub, "submission_id", "?"), exc_info=True,
                    )
            try:
                _park(sub, idx + 1)
            except Exception:
                logger.warning(
                    "rotation waitlist failed for %s (ignored)",
                    getattr(sub, "submission_id", "?"), exc_info=True,
                )
            done += 1
            if notify is not None:
                to_notify.append((sub, token))
    except BaseException:
        # Cancellation / interpreter teardown mid-sweep: parking the skipped set
        # is the round's INTEGRITY (an un-parked survivor gets benched and busts
        # the slate width) — finish the rest with a tight store-only loop (no
        # token capture, no notify bookkeeping) before re-raising.
        for idx in range(done, n_skipped):
            sub = skipped[idx]
            try:
                _park(sub, idx + 1)
            except BaseException:  # noqa: BLE001 — best-effort cleanup path
                pass
        raise
    if selected:
        ledger.mark_selected(
            [getattr(s, "hotkey", "") or "" for s in selected],
            time.time() if now is None else now,
        )
    # ── Phase 2: best-effort miner feedback, off the close path ─────────────
    notify_thread = (
        _notify_skipped_in_background(notify, to_notify, reject_reason, round_id)
        if (notify is not None and to_notify)
        else None
    )
    return {
        "applied": True,
        "candidates": len(candidates),
        "slots": slots,
        "selected": [getattr(s, "submission_id", "?") for s in selected],
        "skipped": [getattr(s, "submission_id", "?") for s in skipped],
        "notify_thread": notify_thread,
    }
