"""Per-round build-budget gate (harness/build_budget.py) — allocation logic.

The gate caps stage-2 docker builds at ``SOLVER_ROUND_INTAKE_MAX`` per round and
dispenses them by WAIT-TIME seniority (rotation.wait_ts), not arrival order:

  * SOLVER_ROUND_INTAKE_MAX default 8 IN CODE, 0 = unlimited pass-through;
  * one wait-time queue, no pools — a fresh identity's clock starts NOW, so it
    sorts junior and cannot out-senior a genuine waiter (this replaced the old
    two-pool / newcomer-lottery / spillover machinery);
  * soft per-actor dedup so a fleet never multiplies its build share by hotkey
    count; leftover budget in a quiet round still fills;
  * close-time flush parks waiters NO-FAULT (waitlist, seniority retained);
  * restart rebuild charges each prior build attempt exactly once.
"""
from __future__ import annotations

import asyncio

import pytest

from minotaur_subnet.harness import build_budget as bb
from minotaur_subnet.harness.build_budget import BuildBudgetGate

ROUND = "round-e77-n1"

# Three PROVEN miners, benched at these times (lower = longer ago = more senior).
PROVEN_LEDGER = {"hk-old": 100.0, "hk-mid": 200.0, "hk-new": 300.0}


def _gate(ledger=None, seen=None, now=1000.0, budget=None,
          concurrency=None, holdback=0, holdback_secs=None,
          monkeypatch=None):
    """Build a gate + round with explicit env, injectable clock/ledger/seen.

    ``holdback`` pins ``SOLVER_BUILD_HOLDBACK_UNITS`` and defaults to 0
    (disabled) so the pre-holdback tests keep testing exactly what they test;
    the holdback section below passes explicit values.
    """
    assert monkeypatch is not None
    if budget is None:
        monkeypatch.delenv("SOLVER_ROUND_INTAKE_MAX", raising=False)
    else:
        monkeypatch.setenv("SOLVER_ROUND_INTAKE_MAX", str(budget))
    if concurrency is None:
        monkeypatch.delenv("SCREENING_BUILD_CONCURRENCY", raising=False)
    else:
        monkeypatch.setenv("SCREENING_BUILD_CONCURRENCY", str(concurrency))
    monkeypatch.setenv("SOLVER_BUILD_HOLDBACK_UNITS", str(holdback))
    if holdback_secs is None:
        monkeypatch.delenv("SOLVER_BUILD_HOLDBACK_SECONDS", raising=False)
    else:
        monkeypatch.setenv("SOLVER_BUILD_HOLDBACK_SECONDS", str(holdback_secs))
    clock = {"t": now}
    gate = BuildBudgetGate(
        ledger_loader=lambda: dict(ledger or {}),
        seen_loader=lambda: dict(seen or {}),
        now=lambda: clock["t"],
    )
    gate._clock = clock
    gate.ensure_round(ROUND)
    return gate


async def _acquire(gate, sid, hotkey, *, prior=False, open_=True):
    return await gate.acquire(
        submission_id=sid, hotkey=hotkey, round_id=ROUND,
        prior_attempt=prior, round_is_open=lambda: open_,
    )


def _spawn(gate, sid, hotkey):
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


# ── 0 = unlimited pass-through ───────────────────────────────────────────────

def test_unlimited_budget_is_transparent(monkeypatch):
    async def main():
        gate = _gate(budget=0, concurrency=1, monkeypatch=monkeypatch)
        for i in range(20):
            grant = await _acquire(gate, f"s{i}", f"hk{i}")
            assert grant.granted and not grant.charged
        assert gate.snapshot(ROUND)["charged_count"] == 0
    asyncio.run(main())


# ── wait-time dispatch: seniority, not arrival ───────────────────────────────

def test_seniority_beats_arrival_among_waiters(monkeypatch):
    async def main():
        gate = _gate(ledger=PROVEN_LEDGER, budget=8, concurrency=1,
                     monkeypatch=monkeypatch)
        first = await _acquire(gate, "s-x", "hk-x")   # takes the one in-flight slot
        assert first.granted
        # Junior arrives first, senior second — senior must win the next unit.
        t_new = _spawn(gate, "s-new", "hk-new")       # benched 300 (junior)
        await _settle()
        t_old = _spawn(gate, "s-old", "hk-old")       # benched 100 (senior)
        await _settle()
        gate.release(ROUND, "s-x")
        await _settle()
        assert t_old.done() and t_old.result().granted
        assert not t_new.done()
        gate.flush_round(ROUND)
        await _settle()
    asyncio.run(main())


def test_fresh_mint_is_junior_not_senior(monkeypatch):
    # The core anti-mint property: a never-benched, never-seen identity gets
    # wait_ts=now (junior), so a proven miner beats it — no pool needed.
    async def main():
        gate = _gate(ledger={"hk-proven": 100.0}, now=1000.0, budget=8,
                     concurrency=1, monkeypatch=monkeypatch)
        blocker = await _acquire(gate, "s-block", "hk-block")
        assert blocker.granted
        t_fresh = _spawn(gate, "s-fresh", "sybil-fresh")   # no benched, no seen
        await _settle()
        t_proven = _spawn(gate, "s-proven", "hk-proven")   # benched 100
        await _settle()
        gate.release(ROUND, "s-block")
        await _settle()
        assert t_proven.done() and t_proven.result().granted  # proven first
        assert not t_fresh.done()                              # fresh mint waits
        gate.flush_round(ROUND)
        await _settle()
    asyncio.run(main())


def test_aged_newcomer_outranks_recently_benched(monkeypatch):
    # A never-benched identity that FIRST APPEARED long ago (seen=10) outranks a
    # miner benched recently (300) — you earn priority by waiting, not minting.
    async def main():
        gate = _gate(ledger={"hk-recent": 300.0}, seen={"hk-aged": 10.0},
                     now=1000.0, budget=8, concurrency=1, monkeypatch=monkeypatch)
        blocker = await _acquire(gate, "s-block", "hk-block")
        assert blocker.granted
        t_recent = _spawn(gate, "s-recent", "hk-recent")   # benched 300
        await _settle()
        t_aged = _spawn(gate, "s-aged", "hk-aged")          # first-seen 10
        await _settle()
        gate.release(ROUND, "s-block")
        await _settle()
        assert t_aged.done() and t_aged.result().granted     # aged waiter wins
        assert not t_recent.done()
        gate.flush_round(ROUND)
        await _settle()
    asyncio.run(main())


def test_proven_beats_fresh_flood_for_the_next_unit(monkeypatch):
    # The guarantee: when a build slot frees and both a fresh flood AND a proven
    # miner are WAITING, seniority (the proven miner) wins — fresh mints are
    # junior, so no reserved pool is needed. (An idle slot at round-open still
    # goes to whoever is there — inherent pacing — so a separate blocker holds
    # the one in-flight slot here.)
    async def main():
        gate = _gate(ledger={"hk-p": 100.0}, now=1000.0, budget=8,
                     concurrency=1, monkeypatch=monkeypatch)
        blocker = await _acquire(gate, "s-block", "hk-block")
        assert blocker.granted
        floods = [_spawn(gate, f"s-bot{i}", f"bot-{i}") for i in range(6)]
        t_p = _spawn(gate, "s-p", "hk-p")     # proven, benched 100
        await _settle()
        gate.release(ROUND, "s-block")        # slot frees → next by seniority
        await _settle()
        assert t_p.done() and t_p.result().granted        # proven wins the flood
        assert all(not t.done() for t in floods)          # no fresh bot jumped it
        gate.flush_round(ROUND)
        await _settle()
    asyncio.run(main())


# ── close-time flush: no-fault denial ────────────────────────────────────────

def test_flush_parks_waiters_no_fault_in_priority_order(monkeypatch):
    async def main():
        gate = _gate(ledger=PROVEN_LEDGER, budget=1, concurrency=1,
                     monkeypatch=monkeypatch)
        first = await _acquire(gate, "s-x", "hk-new")
        assert first.granted
        t_old = _spawn(gate, "s-old", "hk-old")     # senior (benched 100)
        t_bot = _spawn(gate, "s-bot", "sybil-bot")  # junior (fresh)
        await _settle()
        parked_calls = []
        parked = gate.flush_round(
            ROUND, lambda w, pos, c: parked_calls.append((w.submission_id, pos, c)),
        )
        await _settle()
        assert parked_calls == [("s-old", 1, 2), ("s-bot", 2, 2)]  # senior pos 1
        assert parked == ["s-old", "s-bot"]
        for t in (t_old, t_bot):
            assert not t.result().granted and t.result().parked
    asyncio.run(main())


def test_flush_park_failure_falls_back_to_caller(monkeypatch):
    async def main():
        gate = _gate(budget=1, concurrency=1, monkeypatch=monkeypatch)
        assert (await _acquire(gate, "s-x", "hk-x")).granted
        t = _spawn(gate, "s-w", "hk-w")
        await _settle()

        def boom(w, pos, c):
            raise RuntimeError("store write failed")

        gate.flush_round(ROUND, boom)
        await _settle()
        grant = t.result()
        assert not grant.granted and not grant.parked
    asyncio.run(main())


def test_post_flush_straggler_grants_while_budget_remains(monkeypatch):
    async def main():
        gate = _gate(budget=2, concurrency=8, monkeypatch=monkeypatch)
        assert (await _acquire(gate, "s-1", "hk-1")).granted
        gate.flush_round(ROUND)
        late_ok = await _acquire(gate, "s-late", "hk-late", open_=False)
        assert late_ok.granted and late_ok.charged
        late_no = await _acquire(gate, "s-later", "hk-later", open_=False)
        assert not late_no.granted and not late_no.parked
    asyncio.run(main())


def test_waiter_self_evicts_when_round_closes_without_flush(monkeypatch):
    async def main():
        monkeypatch.setattr(bb, "_WAIT_POLL_SECONDS", 0.01)
        gate = _gate(budget=1, concurrency=1, monkeypatch=monkeypatch)
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
        gate = BuildBudgetGate(
            ledger_loader=lambda: dict(PROVEN_LEDGER), seen_loader=lambda: {},
        )
        gate.ensure_round(
            ROUND,
            prior_attempts=[("s-a", "hk-old"), ("s-b", "sybil-1"),
                            ("s-a", "hk-old"), ("s-c", "sybil-2")],
        )
        snap = gate.snapshot(ROUND)
        assert snap["charged_count"] == 3               # s-a counted once
        assert sorted(snap["charged"]) == ["s-a", "s-b", "s-c"]
        for sid, hk in (("s-a", "hk-old"), ("s-b", "sybil-1")):
            grant = await gate.acquire(
                submission_id=sid, hotkey=hk, round_id=ROUND,
                prior_attempt=True, round_is_open=lambda: True,
            )
            assert grant.granted and not grant.charged
        assert gate.snapshot(ROUND)["charged_count"] == 3   # no second charge
    asyncio.run(main())


def test_prior_attempt_flag_grants_free_even_if_rebuild_missed_it(monkeypatch):
    async def main():
        gate = _gate(budget=1, concurrency=1, monkeypatch=monkeypatch)
        grant = await _acquire(gate, "s-resumed", "hk-r", prior=True)
        assert grant.granted and not grant.charged
        assert gate.snapshot(ROUND)["charged"] == ["s-resumed"]
    asyncio.run(main())


# ── holdback: late-senior protection ─────────────────────────────────────────

def test_holdback_env_defaults(monkeypatch):
    monkeypatch.delenv("SOLVER_BUILD_HOLDBACK_UNITS", raising=False)
    monkeypatch.delenv("SOLVER_BUILD_HOLDBACK_SECONDS", raising=False)
    assert bb.holdback_units() == 3
    assert bb.holdback_seconds() == 600.0
    monkeypatch.setenv("SOLVER_BUILD_HOLDBACK_UNITS", "garbage")
    monkeypatch.setenv("SOLVER_BUILD_HOLDBACK_SECONDS", "garbage")
    assert bb.holdback_units() == 3
    assert bb.holdback_seconds() == 600.0
    monkeypatch.setenv("SOLVER_BUILD_HOLDBACK_UNITS", "-2")
    assert bb.holdback_units() == 0                      # clamped, = disabled


def test_holdback_reserves_units_within_window(monkeypatch):
    async def main():
        # Budget 3, hold 2: an open-time flood gets exactly ONE build even
        # though pacing (concurrency 3) would allow all three at once.
        gate = _gate(budget=3, concurrency=3, holdback=2, holdback_secs=600,
                     monkeypatch=monkeypatch)
        tasks = [_spawn(gate, f"s{i}", f"hk{i}") for i in range(3)]
        await _settle()
        assert sum(t.done() for t in tasks) == 1
        assert gate.snapshot(ROUND)["dispensable_budget"] == 1
        # Window lapses: the next dispatch (any grant/release/poll tick)
        # hands out the held units.
        gate._clock["t"] = 1000.0 + 601
        assert gate.snapshot(ROUND)["dispensable_budget"] == 3
        gate.release(ROUND, "s0")
        gate.release(ROUND, "s1")
        gate.release(ROUND, "s2")
        await _settle()
        assert all(t.done() and t.result().granted for t in tasks)
    asyncio.run(main())


def test_late_senior_wins_a_heldback_unit(monkeypatch):
    async def main():
        # The 2026-07-26 starvation shape: a warm-cache flood at open would
        # have spent the whole budget on arrival order before the most senior
        # miner's slower pipeline ever submitted. With one unit held back,
        # the senior arrival at T+5min outranks the queued flood for it.
        gate = _gate(ledger={"hk-old": 100.0}, budget=2, concurrency=2,
                     holdback=1, holdback_secs=600, monkeypatch=monkeypatch)
        t_a = _spawn(gate, "s-a", "hk-a")                # fresh mint, junior
        await _settle()
        t_b = _spawn(gate, "s-b", "hk-b")                # fresh mint, junior
        await _settle()
        assert sum(t.done() for t in (t_a, t_b)) == 1    # 1 immediate unit
        gate._clock["t"] = 1300.0                        # T+5min, in window
        t_old = _spawn(gate, "s-old", "hk-old")          # benched 100 = senior
        await _settle()
        assert not t_old.done()
        gate._clock["t"] = 1700.0                        # window lapsed
        gate.release(ROUND, "s-a")                       # flood build finished
        gate.release(ROUND, "s-b")
        await _settle()
        assert t_old.done() and t_old.result().granted
        # Budget 2/2 spent — the remaining flood waiter stays parked.
        assert sum(t.done() for t in (t_a, t_b)) == 1
        gate.flush_round(ROUND)
        await _settle()
    asyncio.run(main())


def test_holdback_dispenses_via_poll_tick_without_events(monkeypatch):
    async def main():
        # No grant/release traffic after the window lapses: the waiter's own
        # poll tick must run dispatch, else held units strand until close.
        monkeypatch.setattr(bb, "_WAIT_POLL_SECONDS", 0.02)
        gate = _gate(budget=2, concurrency=2, holdback=1, holdback_secs=100,
                     monkeypatch=monkeypatch)
        assert (await _acquire(gate, "s-1", "hk-1")).granted
        task = _spawn(gate, "s-2", "hk-2")
        await _settle()
        assert not task.done()
        gate._clock["t"] = 1000.0 + 101                  # lapse, zero events
        grant = await asyncio.wait_for(task, timeout=2.0)
        assert grant.granted and grant.charged
    asyncio.run(main())


def test_holdback_above_budget_holds_everything_until_lapse(monkeypatch):
    async def main():
        gate = _gate(budget=2, concurrency=2, holdback=5, holdback_secs=600,
                     monkeypatch=monkeypatch)
        tasks = [_spawn(gate, f"s{i}", f"hk{i}") for i in range(2)]
        await _settle()
        assert not any(t.done() for t in tasks)
        assert gate.snapshot(ROUND)["dispensable_budget"] == 0
        gate._clock["t"] = 1000.0 + 601
        gate.release(ROUND, "s-none")                    # any dispatch trigger
        await _settle()
        assert all(t.done() and t.result().granted for t in tasks)
    asyncio.run(main())


def test_post_close_straggler_ignores_holdback(monkeypatch):
    async def main():
        # Flush during the window: the post-close straggler path keeps the
        # FULL budget (pre-holdback semantics for slow screeners).
        gate = _gate(budget=2, concurrency=2, holdback=2, holdback_secs=600,
                     monkeypatch=monkeypatch)
        gate.flush_round(ROUND)
        grant = await _acquire(gate, "s-late", "hk-late", open_=False)
        assert grant.granted and grant.charged
    asyncio.run(main())
