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

THE SYBIL TRAP, and the two-pool answer. Rotation seniority treats
never-benched identities as MOST senior (rotation_sort_key: missing ledger
entry = timestamp 0.0) — correct for the bench slate's starvation-freedom, but
a build budget allocated purely by that rule hands all 8 builds to fresh
sybils every round: minting a new hotkey out-seniors every proven miner. So
the budget is split into two pools by ``SOLVER_BUILD_PROVEN_SHARE`` (default
0.75 — 6 of 8 units):

  PROVEN   — hotkeys with a rotation-ledger entry (at least one completed past
             bench), ordered LRU (benched longest ago first), salted-hash
             tie-break: the rotation rule, applied to the identities it was
             designed for.
  NEWCOMER — never-benched hotkeys, ordered purely by the salted
             ``sha256(hotkey:round_id)`` hash — a deterministic, publicly
             recomputable per-round lottery with no arrival/alphabetical bias.

Trade-off, stated honestly: under a flood of S never-benched sybils an honest
newcomer's per-round build probability is ~2/S — degraded to probabilistic,
never zero (fresh lottery every round, so no permanent starvation), with
expected entry in ~S/2 rounds. Sybils can no longer starve proven miners; the
residual attack (buying lottery tickets by minting identities) is priced in
registration cost, which is min_burn's job, not the scheduler's. A sybil that
wins and gets BENCHED does become "proven", but that promotion is bounded by
the newcomer share per round and still costs registration + the per-owner and
fingerprint caps upstream. Losing the ledger degrades exactly as rotation
documents: everyone ties at "never benched" and the per-round lottery decides
until history rebuilds.

SPILLOVER. A pool with no waiters donates its units so quiet rounds waste no
capacity — but asymmetrically:
  * proven → newcomer units: IMMEDIATE (a proven miner consuming an idle
    newcomer unit costs newcomers nothing they were contending for).
  * newcomer → proven units: only after ``SOLVER_BUILD_NEWCOMER_SPILL_AFTER``
    (default 0.5) of the round's open window has elapsed. Without this delay
    the live flood pattern — bots submitting the instant the round opens,
    before any proven miner has arrived — would drain the proven pool through
    the "no proven waiter exists" rule and partially reintroduce the
    arrival-order advantage this gate exists to remove.

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

from minotaur_subnet.harness.actor import actor_last_selected, get_actor_resolver
from minotaur_subnet.harness.rotation import (
    RotationLedger,
    actor_rotation_sort_key,
    rotation_ledger_path,
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


def proven_share() -> float:
    """Fraction of the build budget reserved for PROVEN miners (ledger entry =
    at least one completed past bench). Default 0.75 → 6 of 8 units proven,
    2 newcomer. Clamped to [0, 1]. See the module docstring for why a share of
    the budget must be held back from the never-benched-is-most-senior rule.
    """
    raw = os.environ.get("SOLVER_BUILD_PROVEN_SHARE", "0.75").strip()
    try:
        val = float(raw)
    except ValueError:
        val = 0.75
    return min(1.0, max(0.0, val))


def newcomer_spill_after_fraction() -> float:
    """Fraction of the round's OPEN window that must elapse before a NEWCOMER
    waiter may consume an idle PROVEN unit (default 0.5). The delay closes the
    open-instant hole: bots submitting at round open, before any proven miner
    arrives, must not drain the proven pool via the no-proven-waiter spill
    rule. Proven → newcomer spill is immediate (see module docstring).
    <= 0 disables the delay; >= 1 disables newcomer→proven spill entirely.
    """
    raw = os.environ.get("SOLVER_BUILD_NEWCOMER_SPILL_AFTER", "0.5").strip()
    try:
        return float(raw)
    except ValueError:
        return 0.5


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
    # actor_rotation_sort_key(hotkey, round_id, actor_last, actor_of):
    # (actor_last_selected_ts, actor-salted sha256, hotkey-salted sha256).
    # For proven actors the timestamp orders LRU-first; for newcomers every
    # timestamp is 0.0 so the ACTOR-salted hash is a pure per-round lottery —
    # one ticket per actor, however many hotkeys it registers (see
    # harness/actor.py; the identity resolver reproduces the old per-hotkey
    # ordering exactly).
    key: tuple[float, str, str]
    proven: bool
    actor: str = ""
    event: asyncio.Event = field(default_factory=asyncio.Event)
    outcome: BuildGrant | None = None


@dataclass
class _RoundGateState:
    round_id: str
    budget: int
    proven_units: int
    newcomer_units: int
    opened_at: float
    open_seconds: float
    spill_after_fraction: float
    ledger: dict[str, float]
    # Actor view, snapshotted with the ledger at ensure_round so one round's
    # dispatch is internally consistent even across a mid-round metagraph
    # re-sync (next round picks up the new map).
    actor_of: Any = None
    actor_last: dict[str, float] = field(default_factory=dict)
    charged_actors: set[str] = field(default_factory=set)
    charged_ids: set[str] = field(default_factory=set)
    proven_charged: int = 0
    newcomer_charged: int = 0
    in_flight: set[str] = field(default_factory=set)
    waiters: list[_Waiter] = field(default_factory=list)
    flushed: bool = False

    @property
    def total_charged(self) -> int:
        return self.proven_charged + self.newcomer_charged


class BuildBudgetGate:
    """The per-round build-budget dispatcher. One instance per process (see
    :func:`get_build_budget_gate`); all methods must run on the event loop
    thread (they do today: pipeline tasks, and the leader's close path).
    """

    def __init__(
        self,
        *,
        ledger_loader: Callable[[], dict[str, float]] | None = None,
        now: Callable[[], float] = time.time,
    ) -> None:
        self._rounds: dict[str, _RoundGateState] = {}
        self._now = now
        self._ledger_loader = ledger_loader or (
            lambda: RotationLedger(rotation_ledger_path()).load()
        )

    # ── round lifecycle ──────────────────────────────────────────────────

    def needs_round(self, round_id: str) -> bool:
        return round_id not in self._rounds

    def ensure_round(
        self,
        round_id: str,
        *,
        opened_at: float,
        open_seconds: float,
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
        share = proven_share()
        if budget > 0:
            proven_units = min(budget, max(0, round(budget * share)))
        else:
            proven_units = 0
        ledger = self._ledger_loader()
        actor_of = get_actor_resolver()
        state = _RoundGateState(
            round_id=round_id,
            budget=budget,
            proven_units=proven_units,
            newcomer_units=max(0, budget - proven_units),
            opened_at=float(opened_at or 0.0),
            open_seconds=float(open_seconds or 0.0),
            spill_after_fraction=newcomer_spill_after_fraction(),
            ledger=ledger,
            actor_of=actor_of,
            actor_last=actor_last_selected(ledger, actor_of),
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
    def _is_proven(state: _RoundGateState, hotkey: str) -> bool:
        # A ledger entry means "completed at least one past bench" — the
        # rotation ledger only advances for SELECTED slate members
        # (rotation.apply_rotation_slate → mark_selected). Actor-keyed: ANY of
        # the actor's hotkeys having benched makes the whole actor proven, so
        # a fleet's freshly-minted sibling hotkey can no longer re-enter the
        # newcomer lottery its coldkey already graduated from.
        actor = BuildBudgetGate._actor(state, hotkey)
        return float(state.actor_last.get(actor, 0.0)) > 0.0

    @staticmethod
    def _charge_unit(state: _RoundGateState, submission_id: str, hotkey: str) -> None:
        """Record one spent budget unit for ``submission_id`` (idempotence is
        the caller's job via ``charged_ids``)."""
        state.charged_ids.add(submission_id)
        state.charged_actors.add(BuildBudgetGate._actor(state, hotkey))
        if BuildBudgetGate._is_proven(state, hotkey):
            state.proven_charged += 1
        else:
            state.newcomer_charged += 1

    def _spill_to_proven_open(self, state: _RoundGateState) -> bool:
        """May a newcomer consume an idle proven unit yet? (Amendment: only
        after a configured fraction of the open window, so open-instant bots
        can't drain the proven pool before proven miners arrive.)"""
        frac = state.spill_after_fraction
        if frac <= 0:
            return True
        if state.open_seconds <= 0 or state.opened_at <= 0:
            # No window information — fail toward fairness for newcomers
            # (spill allowed) only when the delay is disabled; otherwise hold
            # the proven reserve for the whole round.
            return False
        return (self._now() - state.opened_at) >= frac * state.open_seconds

    def _pick_next(self, state: _RoundGateState) -> tuple[_Waiter, str] | None:
        """Choose the next waiter to grant and the POOL whose unit it spends.

        Largest-remainder interleave between the pools (with 6/2 units the
        grant pattern is P P P N P P P N), each pool internally ordered by
        the actor-keyed rotation sort; asymmetric spillover per the module
        docstring. Soft per-actor dedup: waiters whose ACTOR already spent a
        unit this round sort behind every fresh actor in their pool — a fleet
        only collects a second unit when no other actor is waiting, so budget
        is never wasted but never multiplied by hotkey count either.
        """
        def _pool(waiters: Iterable[_Waiter]) -> list[_Waiter]:
            return sorted(
                waiters, key=lambda w: (w.actor in state.charged_actors, w.key),
            )

        proven_w = _pool(w for w in state.waiters if w.proven)
        newcomer_w = _pool(w for w in state.waiters if not w.proven)
        proven_left = state.proven_units - state.proven_charged
        newcomer_left = state.newcomer_units - state.newcomer_charged

        # Pool preference: serve the pool at-or-below its proportional pace
        # (cross-multiplied to avoid division; ties prefer proven).
        prefer_proven = (
            state.proven_charged * state.newcomer_units
            <= state.newcomer_charged * state.proven_units
        )
        order = ("proven", "newcomer") if prefer_proven else ("newcomer", "proven")
        for pool in order:
            if pool == "proven" and proven_w and proven_left > 0:
                return proven_w[0], "proven"
            if pool == "newcomer" and newcomer_w and newcomer_left > 0:
                return newcomer_w[0], "newcomer"
        # Spillover — only when the unit's own constituency has NO waiter:
        # proven → newcomer units immediately…
        if proven_w and not newcomer_w and newcomer_left > 0:
            return proven_w[0], "newcomer"
        # …newcomer → proven units only after the open-window delay.
        if (
            newcomer_w
            and not proven_w
            and proven_left > 0
            and self._spill_to_proven_open(state)
        ):
            return newcomer_w[0], "proven"
        return None

    # ── dispatch machinery ───────────────────────────────────────────────

    def _grant(self, state: _RoundGateState, waiter: _Waiter, pool: str) -> None:
        state.waiters.remove(waiter)
        state.charged_ids.add(waiter.submission_id)
        state.charged_actors.add(waiter.actor)
        if pool == "proven":
            state.proven_charged += 1
        else:
            state.newcomer_charged += 1
        state.in_flight.add(waiter.submission_id)
        waiter.outcome = BuildGrant(
            granted=True, charged=True,
            reason=f"unit {state.total_charged}/{state.budget} ({pool} pool)",
        )
        waiter.event.set()
        logger.info(
            "[build-budget] %s: granted build to %s (hotkey=%s pool=%s "
            "proven=%d/%d newcomer=%d/%d in_flight=%d waiting=%d)",
            state.round_id, waiter.submission_id, waiter.hotkey[:12], pool,
            state.proven_charged, state.proven_units,
            state.newcomer_charged, state.newcomer_units,
            len(state.in_flight), len(state.waiters),
        )

    def _dispatch(self, state: _RoundGateState) -> None:
        if state.flushed:
            return
        concurrency = _dispatch_concurrency()
        while (
            state.waiters
            and len(state.in_flight) < concurrency
            and state.total_charged < state.budget
        ):
            pick = self._pick_next(state)
            if pick is None:
                return
            self._grant(state, *pick)

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
            self.ensure_round(round_id, opened_at=0.0, open_seconds=0.0)
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
            key=actor_rotation_sort_key(
                hotkey or "", round_id, state.actor_last,
                state.actor_of or (lambda hk: hk),
            ),
            proven=self._is_proven(state, hotkey or ""),
            actor=self._actor(state, hotkey or ""),
        )
        state.waiters.append(waiter)
        self._dispatch(state)
        while waiter.outcome is None:
            try:
                await asyncio.wait_for(waiter.event.wait(), timeout=_WAIT_POLL_SECONDS)
            except asyncio.TimeoutError:
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
        ordered = sorted(state.waiters, key=lambda w: (not w.proven, w.key))
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
                    f"({state.total_charged}/{state.budget} builds dispatched "
                    f"by seniority) — waitlisted, seniority retained"
                ),
            )
            waiter.event.set()
            parked.append(waiter.submission_id)
        state.waiters.clear()
        logger.info(
            "[build-budget] %s: flush parked %d waiter(s) at close "
            "(proven=%d/%d newcomer=%d/%d units spent)",
            round_id, len(parked),
            state.proven_charged, state.proven_units,
            state.newcomer_charged, state.newcomer_units,
        )
        return parked

    def snapshot(self, round_id: str) -> dict[str, Any] | None:
        """Observability: the round's budget accounting (for logs/tests)."""
        state = self._rounds.get(round_id)
        if state is None:
            return None
        return {
            "budget": state.budget,
            "proven_units": state.proven_units,
            "newcomer_units": state.newcomer_units,
            "proven_charged": state.proven_charged,
            "newcomer_charged": state.newcomer_charged,
            "charged": sorted(state.charged_ids),
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
