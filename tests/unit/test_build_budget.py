"""Per-round build-budget gate (harness/build_budget.py) — allocation logic.

The gate exists because the 2026-07-16 build flood showed intake-driven builds
are uncapped (63/hour from sybil identities), and the rejected alternative — a
first-come intake 409 — hands every slot to open-instant bots. These tests pin
the rules that replace it:

  * SOLVER_ROUND_INTAKE_MAX default 8 IN CODE, 0 = unlimited pass-through;
  * grants paced against build capacity and dispatched by rotation seniority,
    NOT arrival order;
  * the sybil trap (never-benched = most senior) answered by the two-pool
    split: proven miners get their reserved share even when sybils flood the
    queue first, newcomers keep a reserved lottery share (never zero entry);
  * asymmetric spillover (proven→newcomer immediate; newcomer→proven only
    after the open-window delay — the open-instant hole);
  * close-time flush parks waiters NO-FAULT (waitlist, seniority retained);
  * restart rebuild charges each prior build attempt exactly once.
"""
from __future__ import annotations

import asyncio

import pytest

from minotaur_subnet.harness import build_budget as bb
from minotaur_subnet.harness.build_budget import BuildBudgetGate
from minotaur_subnet.harness.rotation import benchable_candidate_count
from minotaur_subnet.harness.submission_store import (
    OUTCOME_BUILD_BUDGET,
    SubmissionStatus,
    SubmissionStore,
)

ROUND = "round-e77-n1"

# A ledger with three PROVEN miners: hk-old benched longest ago (most senior).
PROVEN_LEDGER = {"hk-old": 100.0, "hk-mid": 200.0, "hk-new": 300.0}


def _gate(ledger=None, now=None, budget=None, share=None, spill=None,
          concurrency=None, monkeypatch=None, opened_at=0.0, open_seconds=0.0):
    """Build a gate + round with explicit env, injectable clock/ledger."""
    assert monkeypatch is not None
    if budget is None:
        monkeypatch.delenv("SOLVER_ROUND_INTAKE_MAX", raising=False)
    else:
        monkeypatch.setenv("SOLVER_ROUND_INTAKE_MAX", str(budget))
    if share is None:
        monkeypatch.delenv("SOLVER_BUILD_PROVEN_SHARE", raising=False)
    else:
        monkeypatch.setenv("SOLVER_BUILD_PROVEN_SHARE", str(share))
    if spill is None:
        monkeypatch.delenv("SOLVER_BUILD_NEWCOMER_SPILL_AFTER", raising=False)
    else:
        monkeypatch.setenv("SOLVER_BUILD_NEWCOMER_SPILL_AFTER", str(spill))
    if concurrency is None:
        monkeypatch.delenv("SCREENING_BUILD_CONCURRENCY", raising=False)
    else:
        monkeypatch.setenv("SCREENING_BUILD_CONCURRENCY", str(concurrency))
    clock = {"t": 1000.0}
    if now is not None:
        clock["t"] = now
    gate = BuildBudgetGate(
        ledger_loader=lambda: dict(ledger or {}),
        now=lambda: clock["t"],
    )
    gate._clock = clock  # test handle to advance time
    gate.ensure_round(ROUND, opened_at=opened_at, open_seconds=open_seconds)
    return gate


async def _acquire(gate, sid, hotkey, *, prior=False, open_=True):
    return await gate.acquire(
        submission_id=sid, hotkey=hotkey, round_id=ROUND,
        prior_attempt=prior, round_is_open=lambda: open_,
    )


def _spawn(gate, sid, hotkey):
    """Start an acquire as a task (a pipeline waiting at the gate)."""
    return asyncio.get_running_loop().create_task(_acquire(gate, sid, hotkey))


async def _settle(n: int = 4):
    for _ in range(n):
        await asyncio.sleep(0)


# ── env defaults ─────────────────────────────────────────────────────────────


def test_budget_default_is_8_in_code(monkeypatch):
    monkeypatch.delenv("SOLVER_ROUND_INTAKE_MAX", raising=False)
    assert bb.round_build_budget() == 8


def test_budget_env_override_and_unlimited(monkeypatch):
    monkeypatch.setenv("SOLVER_ROUND_INTAKE_MAX", "3")
    assert bb.round_build_budget() == 3
    monkeypatch.setenv("SOLVER_ROUND_INTAKE_MAX", "0")
    assert bb.round_build_budget() == 0
    monkeypatch.setenv("SOLVER_ROUND_INTAKE_MAX", "garbage")
    assert bb.round_build_budget() == 8


def test_pool_split_default_6_proven_2_newcomer(monkeypatch):
    gate = _gate(monkeypatch=monkeypatch)
    snap = gate.snapshot(ROUND)
    assert snap["budget"] == 8
    assert snap["proven_units"] == 6
    assert snap["newcomer_units"] == 2


# ── 0 = unlimited pass-through ───────────────────────────────────────────────


def test_unlimited_budget_is_transparent(monkeypatch):
    async def main():
        gate = _gate(budget=0, concurrency=1, monkeypatch=monkeypatch)
        # Way past any pacing/pool bound; every acquire returns immediately.
        for i in range(20):
            grant = await _acquire(gate, f"s{i}", f"hk{i}")
            assert grant.granted and not grant.charged
        snap = gate.snapshot(ROUND)
        assert snap["proven_charged"] == 0 and snap["newcomer_charged"] == 0
    asyncio.run(main())


# ── pacing + seniority dispatch (the anti-arrival-order core) ────────────────


def test_first_arrival_grants_immediately(monkeypatch):
    async def main():
        gate = _gate(ledger=PROVEN_LEDGER, concurrency=1, monkeypatch=monkeypatch)
        grant = await _acquire(gate, "s-bot", "sybil-1")  # a bot, but capacity idle
        assert grant.granted and grant.charged
    asyncio.run(main())


def test_seniority_beats_arrival_order_among_waiters(monkeypatch):
    """A proven miner arriving AFTER two bots outranks both for the next unit
    — arrival-order deviation is bounded to builds already physically started."""
    async def main():
        gate = _gate(ledger=PROVEN_LEDGER, concurrency=1, monkeypatch=monkeypatch)
        first = await _acquire(gate, "s-bot0", "sybil-0")   # capacity idle → builds
        assert first.granted
        t_bot1 = _spawn(gate, "s-bot1", "sybil-1")          # arrives 1st, waits
        t_bot2 = _spawn(gate, "s-bot2", "sybil-2")          # arrives 2nd, waits
        await _settle()
        t_old = _spawn(gate, "s-old", "hk-old")             # proven, arrives LAST
        await _settle()
        assert not t_old.done() and not t_bot1.done()
        gate.release(ROUND, "s-bot0")                       # build finished
        await _settle()
        assert t_old.done() and t_old.result().granted      # seniority won
        assert not t_bot1.done() and not t_bot2.done()
        # cleanup: flush the rest
        gate.flush_round(ROUND)
        await _settle()
        assert not t_bot1.result().granted
        assert not t_bot2.result().granted
    asyncio.run(main())


def test_proven_pool_orders_lru_first(monkeypatch):
    async def main():
        gate = _gate(ledger=PROVEN_LEDGER, concurrency=1, monkeypatch=monkeypatch)
        blocker = await _acquire(gate, "s-x", "sybil-x")    # occupy capacity
        assert blocker.granted
        t_new = _spawn(gate, "s-new", "hk-new")             # benched most recently
        t_old = _spawn(gate, "s-old", "hk-old")             # benched longest ago
        t_mid = _spawn(gate, "s-mid", "hk-mid")
        await _settle()
        gate.release(ROUND, "s-x")
        await _settle()
        assert t_old.done() and t_old.result().granted      # LRU first
        assert not t_mid.done() and not t_new.done()
        gate.release(ROUND, "s-old")
        await _settle()
        assert t_mid.done() and not t_new.done()
        gate.release(ROUND, "s-mid")
        await _settle()
        assert t_new.done()
    asyncio.run(main())


# ── the sybil trap: two-pool split ───────────────────────────────────────────


def test_sybil_flood_cannot_starve_proven_miners(monkeypatch):
    """THE trap: never-benched = rotation-most-senior, so a pure-LRU budget
    hands all 8 builds to fresh bots. The two-pool split holds 6 units for
    proven miners even when 10 sybils flood the queue FIRST — and newcomers
    keep their reserved 2 (honest-newcomer entry never hits zero)."""
    async def main():
        # opened_at/open window set so the newcomer→proven spill is still
        # CLOSED (0.5 * 300s not yet elapsed) — the flood happens at open.
        gate = _gate(ledger=PROVEN_LEDGER | {"hk-p4": 400.0, "hk-p5": 500.0,
                                             "hk-p6": 600.0},
                     concurrency=8, monkeypatch=monkeypatch,
                     opened_at=1000.0, open_seconds=300.0)
        sybils = [_spawn(gate, f"s-syb{i}", f"sybil-{i}") for i in range(10)]
        await _settle()
        # Only the 2 newcomer units went to the flood; 6 proven units held.
        snap = gate.snapshot(ROUND)
        assert snap["newcomer_charged"] == 2
        assert snap["proven_charged"] == 0
        proven_tasks = [
            _spawn(gate, f"s-p{i}", hk)
            for i, hk in enumerate(["hk-old", "hk-mid", "hk-new",
                                    "hk-p4", "hk-p5", "hk-p6"])
        ]
        await _settle()
        assert all(t.done() and t.result().granted for t in proven_tasks)
        granted_sybils = [t for t in sybils if t.done() and t.result().granted]
        assert len(granted_sybils) == 2
        gate.flush_round(ROUND)
        await _settle()
        assert sum(1 for t in sybils if t.result().granted) == 2
    asyncio.run(main())


def test_newcomer_order_is_salted_lottery_not_arrival(monkeypatch):
    """Among newcomers the salted sha256(hotkey:round_id) hash decides —
    deterministic, publicly recomputable, reshuffled per round."""
    from minotaur_subnet.harness.rotation import rotation_sort_key

    async def main():
        gate = _gate(concurrency=1, monkeypatch=monkeypatch)
        blocker = await _acquire(gate, "s-x", "hk-x")
        assert blocker.granted
        hotkeys = ["hk-a", "hk-b", "hk-c"]
        # Submit in REVERSE lottery order to prove arrival doesn't matter.
        lottery = sorted(hotkeys, key=lambda hk: rotation_sort_key(hk, ROUND, {}))
        tasks = {hk: _spawn(gate, f"s-{hk}", hk) for hk in reversed(lottery)}
        await _settle()
        gate.release(ROUND, "s-x")
        await _settle()
        winner = lottery[0]
        assert tasks[winner].done() and tasks[winner].result().granted
        assert all(not tasks[hk].done() for hk in lottery[1:])
        gate.flush_round(ROUND)
        await _settle()
    asyncio.run(main())


# ── spillover rules ──────────────────────────────────────────────────────────


def test_proven_spill_into_newcomer_units_is_immediate(monkeypatch):
    """No newcomer waiters → proven miners may use all 8 units right away
    (quiet rounds waste no capacity)."""
    async def main():
        ledger = {f"hk-p{i}": 100.0 + i for i in range(8)}
        gate = _gate(ledger=ledger, concurrency=8, monkeypatch=monkeypatch,
                     opened_at=1000.0, open_seconds=300.0)
        tasks = [_spawn(gate, f"s-p{i}", f"hk-p{i}") for i in range(8)]
        await _settle()
        assert all(t.done() and t.result().granted for t in tasks)
        snap = gate.snapshot(ROUND)
        assert snap["proven_charged"] == 6 and snap["newcomer_charged"] == 2
    asyncio.run(main())


def test_newcomer_spill_into_proven_units_waits_for_window(monkeypatch):
    """The open-instant hole (amendment): bots at round open must NOT drain
    the proven pool through the no-proven-waiter spill rule — spill opens only
    after SOLVER_BUILD_NEWCOMER_SPILL_AFTER of the open window."""
    async def main():
        gate = _gate(concurrency=8, monkeypatch=monkeypatch,
                     opened_at=1000.0, open_seconds=300.0)  # threshold: t=1150
        tasks = [_spawn(gate, f"s-n{i}", f"newbie-{i}") for i in range(5)]
        await _settle()
        snap = gate.snapshot(ROUND)
        assert snap["newcomer_charged"] == 2          # own share only
        assert snap["proven_charged"] == 0            # reserve untouched
        assert len(snap["waiting"]) == 3
        # …half the window passes with no proven demand → spill opens.
        gate._clock["t"] = 1151.0
        gate.release(ROUND, "s-n0")                   # any dispatch trigger
        await _settle()
        snap = gate.snapshot(ROUND)
        assert snap["proven_charged"] == 3            # spilled into reserve
        assert all(t.done() and t.result().granted for t in tasks)
    asyncio.run(main())


def test_newcomer_spill_blocked_while_proven_waiter_exists(monkeypatch):
    """Even after the window delay, spill requires NO proven waiter — proven
    units always serve proven demand first."""
    async def main():
        gate = _gate(ledger=PROVEN_LEDGER, concurrency=1, monkeypatch=monkeypatch,
                     opened_at=1000.0, open_seconds=300.0)
        gate._clock["t"] = 1200.0                     # spill window open
        first = await _acquire(gate, "s-x", "sybil-x")
        assert first.granted                          # newcomer unit 1
        t_new = _spawn(gate, "s-n1", "newbie-1")      # newcomer waiter
        t_old = _spawn(gate, "s-old", "hk-old")       # proven waiter
        await _settle()
        gate.release(ROUND, "s-x")
        await _settle()
        assert t_old.done() and t_old.result().granted
        assert not t_new.done()
        gate.flush_round(ROUND)
        await _settle()
    asyncio.run(main())


# ── close-time flush: no-fault denial ────────────────────────────────────────


def test_flush_parks_waiters_no_fault_in_priority_order(monkeypatch):
    async def main():
        gate = _gate(ledger=PROVEN_LEDGER, budget=1, concurrency=1,
                     monkeypatch=monkeypatch)
        first = await _acquire(gate, "s-x", "hk-new")
        assert first.granted                          # the round's only unit
        t_old = _spawn(gate, "s-old", "hk-old")
        t_bot = _spawn(gate, "s-bot", "sybil-bot")
        await _settle()
        parked_calls = []
        parked = gate.flush_round(
            ROUND,
            lambda w, pos, contenders: parked_calls.append(
                (w.submission_id, pos, contenders)
            ),
        )
        await _settle()
        # Proven waiter parked at position 1 (best next-round claim recorded).
        assert parked_calls == [("s-old", 1, 2), ("s-bot", 2, 2)]
        assert parked == ["s-old", "s-bot"]
        for t in (t_old, t_bot):
            grant = t.result()
            assert not grant.granted
            assert grant.parked                        # flush recorded the park
    asyncio.run(main())


def test_flush_park_failure_falls_back_to_caller(monkeypatch):
    async def main():
        gate = _gate(budget=1, share=0.0, concurrency=1, monkeypatch=monkeypatch)
        assert (await _acquire(gate, "s-x", "hk-x")).granted
        t = _spawn(gate, "s-w", "hk-w")
        await _settle()

        def boom(w, pos, contenders):
            raise RuntimeError("store write failed")

        gate.flush_round(ROUND, boom)
        await _settle()
        grant = t.result()
        assert not grant.granted
        assert not grant.parked  # pipeline must park it itself
    asyncio.run(main())


def test_post_flush_straggler_grants_while_budget_remains(monkeypatch):
    """A slow clone/stage-1 that reaches the gate after close keeps today's
    slow-screener semantics — build it if the round's budget wasn't spent —
    but a spent budget denies (bounded at 8 builds/round, hard)."""
    async def main():
        gate = _gate(budget=2, share=0.0, concurrency=8, monkeypatch=monkeypatch)
        assert (await _acquire(gate, "s-1", "hk-1")).granted
        gate.flush_round(ROUND)
        late_ok = await _acquire(gate, "s-late", "hk-late", open_=False)
        assert late_ok.granted and late_ok.charged     # 2nd unit remained
        late_no = await _acquire(gate, "s-later", "hk-later", open_=False)
        assert not late_no.granted and not late_no.parked
    asyncio.run(main())


def test_waiter_self_evicts_when_round_closes_without_flush(monkeypatch):
    """Manual /solver/round/close or an abort never runs the rotation flush —
    the waiter's liveness poll must free the coroutine, denied no-fault."""
    async def main():
        monkeypatch.setattr(bb, "_WAIT_POLL_SECONDS", 0.01)
        gate = _gate(budget=1, share=0.0, concurrency=1, monkeypatch=monkeypatch)
        assert (await _acquire(gate, "s-x", "hk-x")).granted
        open_flag = {"open": True}
        task = asyncio.get_running_loop().create_task(gate.acquire(
            submission_id="s-w", hotkey="hk-w", round_id=ROUND,
            round_is_open=lambda: open_flag["open"],
        ))
        await asyncio.sleep(0.05)
        assert not task.done()
        open_flag["open"] = False
        grant = await asyncio.wait_for(task, timeout=2.0)
        assert not grant.granted and not grant.parked
    asyncio.run(main())


# ── restart rebuild: exactly-once charging ───────────────────────────────────


def test_prior_attempts_rebuild_charges_exactly_once(monkeypatch):
    async def main():
        monkeypatch.setenv("SOLVER_ROUND_INTAKE_MAX", "8")
        monkeypatch.delenv("SOLVER_BUILD_PROVEN_SHARE", raising=False)
        gate = BuildBudgetGate(ledger_loader=lambda: dict(PROVEN_LEDGER))
        # Restart: 3 builds already ran in the previous process life —
        # one id listed twice must count once.
        gate.ensure_round(
            ROUND, opened_at=0.0, open_seconds=0.0,
            prior_attempts=[("s-a", "hk-old"), ("s-b", "sybil-1"),
                            ("s-a", "hk-old"), ("s-c", "sybil-2")],
        )
        snap = gate.snapshot(ROUND)
        assert snap["proven_charged"] == 1
        assert snap["newcomer_charged"] == 2
        assert sorted(snap["charged"]) == ["s-a", "s-b", "s-c"]
        # The resumed pipelines re-acquire: free, no second charge.
        for sid, hk in (("s-a", "hk-old"), ("s-b", "sybil-1")):
            grant = await gate.acquire(
                submission_id=sid, hotkey=hk, round_id=ROUND,
                prior_attempt=True, round_is_open=lambda: True,
            )
            assert grant.granted and not grant.charged
        snap = gate.snapshot(ROUND)
        assert snap["proven_charged"] == 1 and snap["newcomer_charged"] == 2
    asyncio.run(main())


def test_prior_attempt_flag_grants_free_even_if_rebuild_missed_it(monkeypatch):
    async def main():
        gate = _gate(budget=1, concurrency=1, monkeypatch=monkeypatch)
        grant = await _acquire(gate, "s-resumed", "hk-r", prior=True)
        assert grant.granted and not grant.charged
        # …but it IS now counted, so the budget can't be exceeded around it.
        snap = gate.snapshot(ROUND)
        assert snap["charged"] == ["s-resumed"]
    asyncio.run(main())


def test_restart_stranded_mid_build_is_not_double_charged(monkeypatch):
    """A submission stranded in SCREENING_STAGE_2 (build started, no result
    recorded) must not consume a SECOND unit after a restart. The resumed
    pipeline re-walks from scratch and resets the status to SCREENING_STAGE_1
    before it reaches the gate — erasing the build evidence — so
    resume_stranded_screenings bootstraps the gate from the PRISTINE boot-time
    statuses first (_ensure_budget_round), and the later acquire passes free."""
    from minotaur_subnet.api.routes.submissions import screening_pipeline as sp

    async def main():
        monkeypatch.setenv("SOLVER_ROUND_INTAKE_MAX", "8")
        monkeypatch.delenv("SOLVER_BUILD_PROVEN_SHARE", raising=False)
        monkeypatch.delenv("SCREENING_BUILD_CONCURRENCY", raising=False)
        store = SubmissionStore(persist_path=None)
        sub = store.create(
            repo_url="https://example.com/r.git", commit_hash="c" * 40,
            epoch=1, hotkey="hk-1", round_id=ROUND,
            max_per_round=0, max_rounds_per_commit=0,
        )
        store.update_status(sub.submission_id, SubmissionStatus.SCREENING_STAGE_2)
        gate = BuildBudgetGate(ledger_loader=lambda: {})
        bb.set_build_budget_gate(gate)
        try:
            # What resume_stranded_screenings does at boot, statuses pristine.
            sp._ensure_budget_round(store, ROUND)
            assert gate.snapshot(ROUND)["charged"] == [sub.submission_id]
            # The resumed pipeline re-walks: stage 1 downgrades the status…
            store.update_status(sub.submission_id, SubmissionStatus.SCREENING_STAGE_1)
            # …and the gate acquire still passes FREE — exactly one charge.
            grant = await sp._acquire_build_grant(store, store.get(sub.submission_id))
            assert grant.granted and not grant.charged
            snap = gate.snapshot(ROUND)
            assert snap["proven_charged"] + snap["newcomer_charged"] == 1
        finally:
            bb.set_build_budget_gate(None)
    asyncio.run(main())


# ── integration with the store + rotation candidacy ──────────────────────────


def test_flush_waitlist_is_no_fault_and_leaves_rotation_candidacy(monkeypatch):
    """The flush parks via store.waitlist (seniority retained, never REJECTED)
    and the parked submission drops out of rotation candidacy / the #797
    autoscale count through the shared terminal rule."""
    async def main():
        store = SubmissionStore(persist_path=None)
        subs = {}
        for hk in ("hk-1", "hk-2"):
            subs[hk] = store.create(
                repo_url="https://example.com/r.git", commit_hash="c" * 40,
                epoch=1, hotkey=hk, round_id=ROUND,
                max_per_round=0, max_rounds_per_commit=0,
            )
        gate = _gate(budget=1, share=0.0, concurrency=1, monkeypatch=monkeypatch)
        granted = await _acquire(gate, subs["hk-1"].submission_id, "hk-1")
        assert granted.granted
        t = _spawn(gate, subs["hk-2"].submission_id, "hk-2")
        await _settle()

        def park(w, pos, contenders):
            store.waitlist(
                w.submission_id, "build budget spent",
                outcome_code=OUTCOME_BUILD_BUDGET,
                position=pos, contenders=contenders,
            )

        gate.flush_round(ROUND, park)
        await _settle()
        assert t.result().parked
        fresh = store.get(subs["hk-2"].submission_id)
        assert fresh.status == SubmissionStatus.WAITLISTED    # no-fault, not REJECTED
        assert fresh.outcome_code == OUTCOME_BUILD_BUDGET
        assert fresh.waitlist["next_round_priority"] is True
        # Shared terminal rule: parked waiter no longer a slate candidate and
        # no longer inflates the decision-window autoscale.
        assert benchable_candidate_count(store.list_by_round(ROUND)) == 1
    asyncio.run(main())


# ── pool arithmetic edges ────────────────────────────────────────────────────


@pytest.mark.parametrize("budget,share,proven,newcomer", [
    (8, 0.75, 6, 2),
    (8, 0.625, 5, 3),
    (1, 0.75, 1, 0),
    (8, 1.0, 8, 0),
    (8, 0.0, 0, 8),
    (3, 0.5, 2, 1),   # round() ties-to-even avoided by content: 1.5 → 2
])
def test_pool_split_arithmetic(monkeypatch, budget, share, proven, newcomer):
    gate = _gate(budget=budget, share=share, monkeypatch=monkeypatch)
    snap = gate.snapshot(ROUND)
    assert (snap["proven_units"], snap["newcomer_units"]) == (proven, newcomer)


def test_share_one_still_serves_newcomers_via_spill_delay(monkeypatch):
    """share=1.0 leaves newcomers ONLY the delayed spill path — they are not
    permanently locked out when proven demand is absent."""
    async def main():
        gate = _gate(budget=2, share=1.0, concurrency=2, monkeypatch=monkeypatch,
                     opened_at=1000.0, open_seconds=100.0)
        t = _spawn(gate, "s-n", "newbie")
        await _settle()
        assert not t.done()                            # reserve held early
        gate._clock["t"] = 1060.0                      # past 50% of the window
        gate.release(ROUND, "none")                    # dispatch trigger
        await _settle()
        assert t.done() and t.result().granted
    asyncio.run(main())
