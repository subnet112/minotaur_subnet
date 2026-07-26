"""Per-round BUILD budget: seniority-dispatched stage-2 admission (leader-local).

WHY. Screening stage 2 is a docker build (2 CPUs, 1-3 min, GHCR push) and until
now the leader admitted an UNBOUNDED number of them per round: the 2026-07-16
build flood ran 63 builds/hour from fresh sybil identities (1200 images, 45GB
of disk on the leader). The obvious counter-measure — the old
``SOLVER_ROUND_INTAKE_MAX`` first-come intake 409 — was rejected by the
operator: open-instant bots fill every slot at round open and lock slower
legitimate miners out, re-introducing exactly the arrival-order race the
rotation slate exists to remove (see harness/rotation.py).

THIS gate caps BUILDS, not intake. Intake stays unbounded (per-identity caps
aside) and every accepted submission still gets its cheap stage-1 verdict
immediately; the expensive stage-2 build is dispensed from a per-round budget
of ``SOLVER_ROUND_INTAKE_MAX`` units (repurposed; default 8 in code, 0 =
unlimited) allocated by ROTATION SENIORITY, not arrival order. Submissions
that never win a unit are WAITLISTED no-fault at close with full next-round
seniority — never a terminal reject for flow control.

HOW dispatch works. Grants are paced against real build capacity
(``SCREENING_BUILD_CONCURRENCY``, the same bound as screening's stage-2
semaphore): at most that many granted builds are in flight, and each time one
finishes the gate hands the next unit to the highest-priority WAITER. Pacing
is the fairness mechanism — if the gate granted all 8 units to the first 8
arrivals, arrival order would decide again. With pacing, a senior miner who
submits two minutes after a bot flood loses only the builds that PHYSICALLY
started before it arrived (~1 per 1-3 min), and outranks every queued bot for
the next unit. Budget-winners keep today's near-immediate build feedback.

HOLDBACK (late-senior protection). Pacing orders waiters who are PRESENT, but
when builds finish in seconds (warm docker caches on unchanged re-submissions)
the whole budget can be spent on arrival order minutes after open — before a
slower-submitting senior miner's pipeline has even pushed (observed 2026-07-26:
the most senior waiter, ~40h since last bench, arrived 8 min after open and
found 8/8 units gone to a 48-second bot flood, twice in a row). So the last
``SOLVER_BUILD_HOLDBACK_UNITS`` units (default 3; 0 disables) are not
dispensable until ``SOLVER_BUILD_HOLDBACK_SECONDS`` (default 600) after the
round's gate state is created (~= the first submission's stage-2 entry): the
flood spends the immediate units, and anyone arriving within the window gets
ranked by seniority for the held-back remainder. A quiet round loses nothing —
the units dispense the moment the window lapses (waiters re-dispatch on their
poll tick, so no grant/release event is needed) and post-close stragglers keep
the full budget exactly as before.

ONE WAIT-TIME QUEUE (no pools). Units are handed to waiters by
``rotation.wait_ts`` seniority — "waiting since when": a hotkey's (actor's) last
bench, or, if it never benched, its FIRST-SEEN time. A freshly-minted identity's
clock starts NOW, so it sorts junior and cannot out-senior a miner who has
genuinely been waiting. That single change retires the old two-pool /
newcomer-lottery / spillover apparatus: those existed only because the previous
rule treated never-benched as MOST senior (ts 0.0), which handed fresh sybils
the front of the queue and forced a reserved "proven" share to defend against
it. With first-seen aging there is nothing to defend — minting buys the back of
the line — so the gate is just: order all waiters by (fresh-actor-first,
wait-time), grant while budget and pacing allow. Soft per-actor dedup keeps a
fleet from taking a second unit while any other actor waits; leftover budget in
a quiet round still fills. Starvation-free: an honest newcomer ages toward the
front as others bench and reset, benching in its fair turn.

RESTART SAFETY (single-charge). The gate is in-process state; a restart wipes
it while ``resume_stranded_screenings`` re-kicks pipelines from scratch. On
first touch of a round the gate rebuilds its charged set from the store —
every submission with a prior stage-2 attempt (status at/past the build, or a
recorded stage-2 result) is counted exactly once — and such a submission
passes the gate WITHOUT consuming a new unit, so a restart can neither
double-charge the round nor grant 8 fresh builds on top of 8 pre-restart ones.

SCOPE. Leader-local only, same category as the intake caps: admission control
at the only submissions ingress. No consensus or wire-format change, no new
fields on synced payloads; followers (who take no submissions) never see it.
Waitlisted-by-budget submissions ride the existing WAITLISTED status through
the close snapshot. Composition: parked waiters are terminal BEFORE
apply_rotation_slate computes candidacy (flush_round is called at the top of
apply_round_rotation), so the #797 decision-window autoscale, the slate-of-3,
and the Phase-2 worker's benched-slate belt all see one consistent picture
through the shared rotation terminal-status rule.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from minotaur_subnet.harness.actor import (
    actor_first_seen,
    actor_last_selected,
    snapshot_resolver,
)
from minotaur_subnet.harness.rotation import (
    RotationLedger,
    actor_rotation_sort_key,
    rotation_ledger_path,
    rotation_sort_key,
)

logger = logging.getLogger(__name__)

# How often a parked waiter re-checks its round's status while waiting for a
# grant. The normal wake-ups are event-driven (grant / close-time flush); this
# poll only catches a round closed WITHOUT the rotation flush (e.g. the manual
# /solver/round/close endpoint, or an abort) so no coroutine waits forever.
_WAIT_POLL_SECONDS = 15.0

# Bound the per-round state map. Rounds cadence ~10-30 min and state is tiny,
# but an unbounded dict is still a leak; keep enough history that a late
# straggler from the previous round (slow clone finishing post-close) still
# finds its round's flushed/charged state instead of re-bootstrapping.
_MAX_TRACKED_ROUNDS = 8


def round_build_budget() -> int:
    """Per-round BUILD budget across all miners — ``SOLVER_ROUND_INTAKE_MAX``.

    Repurposed from the rejected first-come intake 409 (same env var so
    existing operator config keeps meaning "how many submissions may cost the
    leader real work per round"): the value now caps stage-2 DOCKER BUILDS,
    dispensed by rotation seniority at the stage-2 entry instead of turning
    miners away at the gateway. Default 8 IN CODE (the subnet owner's cap for
    the 2026-07-16 build flood); 0 = unlimited (pre-flood behaviour).
    Operator-local admission control, like the other submission caps.
    """
    raw = os.environ.get("SOLVER_ROUND_INTAKE_MAX", "8").strip()
    try:
        return int(raw)
    except ValueError:
        return 8


def holdback_units() -> int:
    """How many budget units are reserved for the holdback window —
    ``SOLVER_BUILD_HOLDBACK_UNITS``, default 3 in code, 0 = no holdback
    (kill-switch: exact pre-holdback dispatch). Values above the round budget
    simply hold the whole budget until the window lapses.
    """
    raw = os.environ.get("SOLVER_BUILD_HOLDBACK_UNITS", "3").strip() or "3"
    try:
        return max(0, int(raw))
    except ValueError:
        return 3


def holdback_seconds() -> float:
    """How long after gate creation the held-back units stay reserved —
    ``SOLVER_BUILD_HOLDBACK_SECONDS``, default 600. Anchored at ensure_round
    (the first stage-2 entry of the round, seconds after open under a flood);
    in a quiet round the anchor drifts later, which is harmless — a quiet
    round has budget to spare either way.
    """
    raw = os.environ.get("SOLVER_BUILD_HOLDBACK_SECONDS", "600").strip() or "600"
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 600.0


def _dispatch_concurrency() -> int:
    """Grant pacing width — mirrors screening's stage-2 build semaphore
    (``SCREENING_BUILD_CONCURRENCY``, default 1) so granted builds never queue
    at the semaphore in arrival order: the gate IS the queue, and it is
    priority-ordered. Raising the semaphore without restarting keeps the two
    reads consistent because both read the env lazily.
    """
    raw = os.environ.get("SCREENING_BUILD_CONCURRENCY", "1").strip() or "1"
    try:
        return max(1, int(raw))
    except ValueError:
        return 1


@dataclass
class BuildGrant:
    """Outcome of a gate acquire.

    ``granted``  — proceed to stage 2.
    ``charged``  — this grant consumed a fresh budget unit (False for the
                   0=unlimited pass-through and for restart re-dispatch of a
                   submission whose unit was already spent).
    ``parked``   — the DENIAL was already recorded in the store by the
                   close-time flush (waitlisted, no-fault); the caller must
                   not park it again.
    """

    granted: bool
    charged: bool = False
    parked: bool = False
    reason: str = ""


@dataclass
class _Waiter:
    submission_id: str
    hotkey: str
    # The wait-time sort key (rotation.wait_ts): (seniority, actor-salt,
    # hotkey-salt) actor-keyed, or (seniority, hotkey-salt) legacy. Seniority is
    # last-benched, or first-seen for a never-benched identity — so a fresh
    # mint sorts junior, not senior. Snapshotted at ensure_round so a round's
    # ordering is stable across a mid-round metagraph re-sync.
    key: tuple[float, str, str] | tuple[float, str]
    actor: str = ""
    event: asyncio.Event = field(default_factory=asyncio.Event)
    outcome: BuildGrant | None = None


@dataclass
class _RoundGateState:
    round_id: str
    budget: int
    ledger: dict[str, float]
    # Actor view + seniority maps, snapshotted with the ledger at ensure_round.
    # actor_of is a FROZEN ActorResolver (harness/actor.py) or None (legacy
    # per-hotkey). actor_last/actor_seen are the benched/first-seen timestamps
    # aggregated per actor for wait_ts; now_at anchors a brand-new actor's clock.
    actor_of: Any = None
    actor_last: dict[str, float] = field(default_factory=dict)
    actor_seen: dict[str, float] = field(default_factory=dict)
    now_at: float = 0.0
    charged_actors: set[str] = field(default_factory=set)
    charged_ids: set[str] = field(default_factory=set)
    charged: int = 0
    in_flight: set[str] = field(default_factory=set)
    waiters: list[_Waiter] = field(default_factory=list)
    flushed: bool = False
    holdback_lapse_logged: bool = False

    @property
    def total_charged(self) -> int:
        return self.charged


class BuildBudgetGate:
    """The per-round build-budget dispatcher. One instance per process (see
    :func:`get_build_budget_gate`); all methods must run on the event loop
    thread (they do today: pipeline tasks, and the leader's close path).
    """

    def __init__(
        self,
        *,
        ledger_loader: Callable[[], dict[str, float]] | None = None,
        seen_loader: Callable[[], dict[str, float]] | None = None,
        now: Callable[[], float] = time.time,
    ) -> None:
        self._rounds: dict[str, _RoundGateState] = {}
        self._now = now
        self._ledger_loader = ledger_loader or (
            lambda: RotationLedger(rotation_ledger_path()).load()
        )
        self._seen_loader = seen_loader or (
            lambda: RotationLedger(rotation_ledger_path()).load_seen()
        )

    # ── round lifecycle ──────────────────────────────────────────────────

    def needs_round(self, round_id: str) -> bool:
        return round_id not in self._rounds

    def ensure_round(
        self,
        round_id: str,
        *,
        prior_attempts: list[tuple[str, str]] | None = None,
    ) -> None:
        """Create (idempotently) the gate state for a round.

        ``prior_attempts`` is the restart-rebuild input: ``(submission_id,
        hotkey)`` pairs whose stage-2 build already started in an earlier
        process life. Each is charged EXACTLY ONCE against its pool here, and
        acquire() later grants them for free — a restart never double-charges
        the round or resets the budget (see module docstring, RESTART SAFETY).
        """
        if round_id in self._rounds:
            return
        budget = round_build_budget()
        ledger = self._ledger_loader()
        seen = self._seen_loader()
        actor_of = snapshot_resolver()
        if actor_of is not None:
            actor_last = actor_last_selected(ledger, actor_of)
            actor_seen = actor_first_seen(seen, actor_of)
        else:  # legacy per-hotkey: identity aggregation
            actor_last = dict(ledger)
            actor_seen = dict(seen)
        state = _RoundGateState(
            round_id=round_id,
            budget=budget,
            ledger=ledger,
            actor_of=actor_of,
            actor_last=actor_last,
            actor_seen=actor_seen,
            now_at=self._now(),
        )
        for sid, hotkey in prior_attempts or []:
            if sid in state.charged_ids:
                continue
            self._charge_unit(state, sid, hotkey)
        self._rounds[round_id] = state
        if prior_attempts:
            logger.info(
                "[build-budget] %s: rebuilt %d prior build charge(s) from the "
                "store (restart) — %d/%d units already spent",
                round_id, len(state.charged_ids), state.total_charged,
                state.budget,
            )
        self._prune()

    def _prune(self) -> None:
        """Drop the oldest idle round states beyond the retention bound."""
        while len(self._rounds) > _MAX_TRACKED_ROUNDS:
            for rid, st in self._rounds.items():
                if not st.waiters and not st.in_flight:
                    del self._rounds[rid]
                    break
            else:
                return  # everything old still has activity — keep it all

    # ── classification / ordering ────────────────────────────────────────

    @staticmethod
    def _actor(state: _RoundGateState, hotkey: str) -> str:
        hotkey = hotkey or ""
        if state.actor_of is None:
            return hotkey
        return state.actor_of(hotkey) or hotkey

    @staticmethod
    def _charge_unit(state: _RoundGateState, submission_id: str, hotkey: str) -> None:
        """Record one spent budget unit for ``submission_id`` (idempotence is
        the caller's job via ``charged_ids``)."""
        state.charged_ids.add(submission_id)
        state.charged_actors.add(BuildBudgetGate._actor(state, hotkey))
        state.charged += 1

    def _pick_next(self, state: _RoundGateState) -> _Waiter | None:
        """The next waiter to grant — one wait-time queue, no pools.

        Order: fresh-actor first (an actor that has NOT yet spent a unit this
        round sorts ahead of one that has — soft per-actor dedup, so a fleet
        never multiplies its build share by hotkey count), then by wait-time
        seniority (``_Waiter.key``: last-benched, or first-seen for a
        never-benched identity — a fresh mint is junior). Budget never wasted:
        once every fresh actor is served, repeat actors fill the remainder.
        """
        if not state.waiters:
            return None
        if state.actor_of is None:  # legacy: pure wait-time order
            return min(state.waiters, key=lambda w: w.key)
        return min(
            state.waiters,
            key=lambda w: (w.actor in state.charged_actors, w.key),
        )

    # ── dispatch machinery ───────────────────────────────────────────────

    def _grant(self, state: _RoundGateState, waiter: _Waiter) -> None:
        state.waiters.remove(waiter)
        state.charged_ids.add(waiter.submission_id)
        state.charged_actors.add(waiter.actor)
        state.charged += 1
        state.in_flight.add(waiter.submission_id)
        waiter.outcome = BuildGrant(
            granted=True, charged=True,
            reason=f"unit {state.charged}/{state.budget}",
        )
        waiter.event.set()
        logger.info(
            "[build-budget] %s: granted build to %s (hotkey=%s actor=%s "
            "spent=%d/%d in_flight=%d waiting=%d)",
            state.round_id, waiter.submission_id, waiter.hotkey[:12],
            waiter.actor[:12], state.charged, state.budget,
            len(state.in_flight), len(state.waiters),
        )

    def _dispensable_budget(self, state: _RoundGateState) -> int:
        """The budget _dispatch may spend RIGHT NOW: the full budget once the
        holdback window has lapsed (or with holdback disabled), else the
        budget minus the held-back units. Only pre-flush dispatch is bounded
        by this — the post-close straggler grant in acquire() keeps the full
        budget on purpose (the window is long past by any real close).
        """
        held = holdback_units()
        if held <= 0:
            return state.budget
        if self._now() - state.now_at < holdback_seconds():
            return max(0, state.budget - held)
        if not state.holdback_lapse_logged:
            state.holdback_lapse_logged = True
            if state.charged >= state.budget - held:
                logger.info(
                    "[build-budget] %s: holdback window lapsed — %d held "
                    "unit(s) now dispensable (%d/%d spent, %d waiting)",
                    state.round_id, min(held, state.budget),
                    state.charged, state.budget, len(state.waiters),
                )
        return state.budget

    def _dispatch(self, state: _RoundGateState) -> None:
        if state.flushed:
            return
        concurrency = _dispatch_concurrency()
        while (
            state.waiters
            and len(state.in_flight) < concurrency
            and state.charged < self._dispensable_budget(state)
        ):
            pick = self._pick_next(state)
            if pick is None:
                return
            self._grant(state, pick)

    # ── public API ───────────────────────────────────────────────────────

    async def acquire(
        self,
        *,
        submission_id: str,
        hotkey: str,
        round_id: str,
        prior_attempt: bool = False,
        round_is_open: Callable[[], bool] | None = None,
    ) -> BuildGrant:
        """Ask for a build unit; may WAIT (until grant, or the close-time
        flush, or a detected round close). ``prior_attempt`` marks a restart
        re-dispatch whose unit was already spent — it passes free, exactly
        once. Callers must ensure_round() first and must release() after the
        build whenever this returns granted.
        """
        state = self._rounds.get(round_id)
        if state is None:
            # Defensive: callers ensure_round() first; a bare acquire must
            # still never crash a pipeline. Bootstrap with no history.
            self.ensure_round(round_id)
            state = self._rounds[round_id]

        # 0 = unlimited: transparent pass-through, today's behaviour exactly
        # (the stage-2 semaphore alone bounds concurrency).
        if state.budget <= 0:
            return BuildGrant(granted=True, charged=False, reason="budget unlimited")

        # Restart re-dispatch: the unit was spent in a previous process life
        # (or earlier in this one) — pass free, and count exactly once.
        if prior_attempt or submission_id in state.charged_ids:
            if submission_id not in state.charged_ids:
                self._charge_unit(state, submission_id, hotkey)
            state.in_flight.add(submission_id)
            return BuildGrant(
                granted=True, charged=False,
                reason="prior build attempt — unit already spent",
            )

        closed = state.flushed or (round_is_open is not None and not round_is_open())
        if closed:
            # Post-close stragglers (slow clone/stage-1 that only reached the
            # gate after the flush): grant while budget remains — this is
            # exactly the pre-gate semantics for slow screeners, still bounded
            # by the round's budget — otherwise deny (caller parks no-fault).
            if state.total_charged < state.budget:
                self._charge_unit(state, submission_id, hotkey)
                state.in_flight.add(submission_id)
                return BuildGrant(
                    granted=True, charged=True,
                    reason="post-close grant (budget remained)",
                )
            return BuildGrant(
                granted=False,
                reason=(
                    f"round {round_id} closed with its build budget "
                    f"({state.budget}) fully spent"
                ),
            )

        waiter = _Waiter(
            submission_id=submission_id,
            hotkey=hotkey or "",
            key=(
                actor_rotation_sort_key(
                    hotkey or "", round_id, state.actor_last, state.actor_of,
                    state.actor_seen, state.now_at,
                )
                if state.actor_of is not None
                else rotation_sort_key(
                    hotkey or "", round_id, state.ledger, state.actor_seen,
                    state.now_at,
                )
            ),
            actor=self._actor(state, hotkey or ""),
        )
        state.waiters.append(waiter)
        self._dispatch(state)
        while waiter.outcome is None:
            try:
                await asyncio.wait_for(waiter.event.wait(), timeout=_WAIT_POLL_SECONDS)
            except asyncio.TimeoutError:
                # Held-back units dispense on a clock, not an event: with no
                # grant/release traffic after the holdback window lapses,
                # somebody has to run dispatch — the poll tick is that
                # somebody (worst case one poll interval of extra latency).
                self._dispatch(state)
                if waiter.outcome is not None:
                    return waiter.outcome
                # Round closed without a rotation flush (manual close / abort)
                # — self-evict so the coroutine never waits forever. The
                # caller parks the submission no-fault.
                if (
                    round_is_open is not None
                    and not round_is_open()
                    and not state.flushed
                    and waiter.outcome is None
                ):
                    try:
                        state.waiters.remove(waiter)
                    except ValueError:
                        pass
                    return BuildGrant(
                        granted=False,
                        reason=(
                            f"round {round_id} closed before a build unit "
                            "freed (no flush observed)"
                        ),
                    )
        return waiter.outcome

    def release(self, round_id: str, submission_id: str) -> None:
        """A granted build finished (pass OR fail): free the pacing slot and
        dispatch the next waiter by priority. The budget charge stays spent —
        a failed build was still a build (that is what the 8-vs-3 headroom is
        for). Idempotent; unknown rounds/ids are ignored.
        """
        state = self._rounds.get(round_id)
        if state is None:
            return
        state.in_flight.discard(submission_id)
        self._dispatch(state)

    def flush_round(
        self,
        round_id: str,
        park: Callable[[_Waiter, int, int], None] | None = None,
    ) -> list[str]:
        """Close-time flush: mark the round's gate closed and park every
        still-waiting submission via ``park(waiter, position, contenders)``.

        Called by apply_round_rotation BEFORE apply_rotation_slate computes
        candidacy, so never-built waiters are already terminal (WAITLISTED,
        no-fault, seniority retained) and can neither be "selected" onto a
        slate they have no image for nor inflate the #797 decision-window
        autoscale. Parking happens synchronously HERE (the woken coroutines
        only resume after the close path yields to the loop — too late for
        the rotation pass). Waiters are parked in priority order so the
        recorded waitlist position is meaningful.
        """
        state = self._rounds.get(round_id)
        if state is None:
            return []
        already_flushed = state.flushed
        state.flushed = True
        if not state.waiters:
            return []
        if already_flushed:
            # Re-entrant close tick: waiters that raced in post-flush are
            # denied by acquire(); nothing should be queued, but be safe.
            logger.warning(
                "[build-budget] %s: flush re-entered with %d waiter(s)",
                round_id, len(state.waiters),
            )
        ordered = sorted(state.waiters, key=lambda w: w.key)
        contenders = len(ordered)
        parked: list[str] = []
        for idx, waiter in enumerate(ordered):
            parked_ok = False
            if park is not None:
                try:
                    park(waiter, idx + 1, contenders)
                    parked_ok = True
                except Exception:
                    logger.warning(
                        "[build-budget] parking %s failed (pipeline will park "
                        "it as fallback)", waiter.submission_id, exc_info=True,
                    )
            # parked=False when the store write failed → the woken pipeline
            # coroutine parks it instead (belt and braces, never double-park).
            waiter.outcome = BuildGrant(
                granted=False, parked=parked_ok,
                reason=(
                    f"build budget for {round_id} exhausted "
                    f"({state.charged}/{state.budget} builds dispatched by "
                    f"seniority) — waitlisted, seniority retained"
                ),
            )
            waiter.event.set()
            parked.append(waiter.submission_id)
        state.waiters.clear()
        logger.info(
            "[build-budget] %s: flush parked %d waiter(s) at close "
            "(%d/%d units spent)",
            round_id, len(parked), state.charged, state.budget,
        )
        return parked

    def snapshot(self, round_id: str) -> dict[str, Any] | None:
        """Observability: the round's budget accounting (for logs/tests)."""
        state = self._rounds.get(round_id)
        if state is None:
            return None
        return {
            "budget": state.budget,
            "dispensable_budget": self._dispensable_budget(state),
            "charged_count": state.charged,
            "charged": sorted(state.charged_ids),
            "charged_actors": sorted(state.charged_actors),
            "in_flight": sorted(state.in_flight),
            "waiting": [w.submission_id for w in state.waiters],
            "flushed": state.flushed,
        }


_gate: BuildBudgetGate | None = None


def get_build_budget_gate() -> BuildBudgetGate:
    """Process-wide gate singleton (leader api process only in practice)."""
    global _gate
    if _gate is None:
        _gate = BuildBudgetGate()
    return _gate


def set_build_budget_gate(gate: BuildBudgetGate | None) -> None:
    """Test seam: swap/reset the singleton."""
    global _gate
    _gate = gate
