"""Build budget, structural dimension: the unit buys DISTINCT CODE, not just a
distinct actor (harness/build_budget.py).

THE BUG THIS FIXES (live, 2026-07-27, rounds e29752736/787/959 + e29753023). The
gate dispensed its 8 units on the fresh-ACTOR rule, which cannot see a fleet of
DISTINCT coldkeys running structurally-identical code: 7 of the 8 units went to
one structural fingerprint, the slate's ``STRUCTURAL_DEDUP_MODE=enforce``
collapse then seated exactly ONE of them, and the round benched 2 of its 3 slots
while 23 structurally-distinct submissions sat parked with no image. Ordering the
build queue by fresh-CODE as well as fresh-actor spends the budget on the
diversity the collapsed slate needs to fill.

Contract: the structural bit only augments "already served" (seniority still
decides among fresh waiters), leftover budget still fills with repeats, a
fingerprint-less waiter never collapses, the legacy no-coldkey-map path is
untouched, and the lever follows the slate's ``STRUCTURAL_DEDUP_MODE`` unless
``SOLVER_BUILD_STRUCTURAL_DEDUP`` forces it.
"""
from __future__ import annotations

import asyncio

import pytest

from minotaur_subnet.harness import actor as actor_mod
from minotaur_subnet.harness import build_budget as bb
from minotaur_subnet.harness.actor import set_coldkey_provider
from minotaur_subnet.harness.build_budget import BuildBudgetGate

ROUND = "round-e29753023-n1"

# The live shape: a 7-wide fleet whose hotkeys have DISTINCT coldkeys (invisible
# to the per-actor dedup) all running the same structure, plus two solo miners
# with code of their own.
FLEET = [f"F{i}" for i in range(7)]
COLDKEYS = {hk: f"CK_{hk}" for hk in [*FLEET, "S", "T"]}
FLEET_FP = "9d1fdb278a012caf"  # the live cluster's fingerprint (any constant)

# The fleet is also the most SENIOR set — it submits every round and only one of
# its members ever benches, so its siblings' clocks keep aging. Without the
# structural bit that seniority sweeps the budget.
LEDGER = {**{hk: 100.0 + i for i, hk in enumerate(FLEET)}, "S": 500.0, "T": 600.0}


@pytest.fixture(autouse=True)
def _clean(tmp_path, monkeypatch):
    monkeypatch.setenv("SOLVER_ROTATION_LEDGER_PATH", str(tmp_path / "rotation.json"))
    monkeypatch.setenv("SOLVER_BUILD_HOLDBACK_UNITS", "0")  # dispatch-order tests
    monkeypatch.setenv("SCREENING_BUILD_CONCURRENCY", "1")
    set_coldkey_provider(None)
    actor_mod._reset_caches_for_tests()
    yield
    set_coldkey_provider(None)
    actor_mod._reset_caches_for_tests()


def _gate(*, ledger=None, seen=None, now=1000.0, coldkeys=COLDKEYS):
    if coldkeys is not None:
        set_coldkey_provider(lambda: coldkeys)
    gate = BuildBudgetGate(
        ledger_loader=lambda: dict(ledger if ledger is not None else LEDGER),
        seen_loader=lambda: dict(seen or {}),
        now=lambda: now,
    )
    gate.ensure_round(ROUND)
    return gate


def _spawn(gate, sid, hotkey, fp=""):
    return asyncio.ensure_future(gate.acquire(
        submission_id=sid, hotkey=hotkey, round_id=ROUND,
        structural_fingerprint=fp, round_is_open=lambda: True,
    ))


async def _settle(n: int = 4):
    for _ in range(n):
        await asyncio.sleep(0)


async def _drain(gate, tasks, units):
    """Let the gate dispense ``units`` builds, releasing each as it lands, and
    return the submission ids in grant order."""
    granted: list[str] = []
    for _ in range(units):
        await _settle()
        landed = [t for t in tasks if t.done() and t.result().granted]
        fresh = [t for t in landed if t.get_name() not in granted]
        if not fresh:
            break
        sid = fresh[0].get_name()
        granted.append(sid)
        gate.release(ROUND, sid)
    await _settle()
    return granted


def _named(gate, sid, hotkey, fp=""):
    task = _spawn(gate, sid, hotkey, fp)
    task.set_name(sid)
    return task


# ── the lever ────────────────────────────────────────────────────────────────

def test_lever_follows_the_slate_mode_by_default(monkeypatch):
    monkeypatch.delenv("SOLVER_BUILD_STRUCTURAL_DEDUP", raising=False)
    for mode, expected in (("", False), ("off", False), ("observe", False),
                           ("enforce", True)):
        monkeypatch.setenv("STRUCTURAL_DEDUP_MODE", mode)
        assert bb.structural_build_dedup_enabled() is expected


def test_lever_env_forces_either_way(monkeypatch):
    monkeypatch.setenv("STRUCTURAL_DEDUP_MODE", "off")
    monkeypatch.setenv("SOLVER_BUILD_STRUCTURAL_DEDUP", "1")
    assert bb.structural_build_dedup_enabled() is True
    monkeypatch.setenv("STRUCTURAL_DEDUP_MODE", "enforce")
    monkeypatch.setenv("SOLVER_BUILD_STRUCTURAL_DEDUP", "0")   # kill-switch
    assert bb.structural_build_dedup_enabled() is False


# ── the incident, reproduced and fixed ───────────────────────────────────────

def test_fleet_sweeps_the_budget_when_the_dedup_is_off(monkeypatch):
    # The BUG: distinct coldkeys + identical structure + best seniority ⇒ the
    # fleet takes every unit, and the collapsed slate can seat only one of them.
    monkeypatch.setenv("SOLVER_ROUND_INTAKE_MAX", "3")
    monkeypatch.setenv("SOLVER_BUILD_STRUCTURAL_DEDUP", "0")

    async def run():
        gate = _gate()
        tasks = [_named(gate, f"s-{hk}", hk, FLEET_FP) for hk in FLEET]
        tasks += [_named(gate, "s-S", "S", "solo-fp-S"),
                  _named(gate, "s-T", "T", "solo-fp-T")]
        granted = await _drain(gate, tasks, 3)
        assert granted == ["s-F0", "s-F1", "s-F2"]          # one fingerprint
        snap = gate.snapshot(ROUND)
        assert snap["structural_dedup"] is False
        assert snap["charged_structs"] == [FLEET_FP]        # 3 units, 1 code
        gate.flush_round(ROUND)
        await _settle()
    asyncio.run(run())


def test_structural_dedup_spends_the_budget_on_distinct_code(monkeypatch):
    # The FIX: the fleet's most senior member still wins the first unit (it is
    # genuinely the most senior contender), but its siblings then sort behind
    # every waiter with unbuilt code — so the slate gets 3 distinct candidates.
    monkeypatch.setenv("SOLVER_ROUND_INTAKE_MAX", "3")
    monkeypatch.setenv("SOLVER_BUILD_STRUCTURAL_DEDUP", "1")

    async def run():
        gate = _gate()
        tasks = [_named(gate, f"s-{hk}", hk, FLEET_FP) for hk in FLEET]
        tasks += [_named(gate, "s-S", "S", "solo-fp-S"),
                  _named(gate, "s-T", "T", "solo-fp-T")]
        granted = await _drain(gate, tasks, 3)
        assert granted == ["s-F0", "s-S", "s-T"]
        snap = gate.snapshot(ROUND)
        assert snap["structural_dedup"] is True
        assert snap["charged_structs"] == sorted([FLEET_FP, "solo-fp-S", "solo-fp-T"])
        # Six fleet siblings never bought a build with code already in flight.
        gate.flush_round(ROUND)
        await _settle()
        assert all(t.done() and not t.result().granted
                   for t in tasks if t.get_name() not in granted)
    asyncio.run(run())


def test_seniority_still_decides_among_fresh_code(monkeypatch):
    # The structural bit must not become a tie-breaker of its own: among
    # waiters that are fresh on both dimensions, wait-time still rules.
    monkeypatch.setenv("SOLVER_ROUND_INTAKE_MAX", "3")
    monkeypatch.setenv("SOLVER_BUILD_STRUCTURAL_DEDUP", "1")

    async def run():
        gate = _gate(ledger={"S": 100.0, "T": 200.0, "F0": 300.0})
        junior_first = _named(gate, "s-F0", "F0", "fp-a")   # arrives first
        await _settle()
        mid = _named(gate, "s-T", "T", "fp-b")
        senior = _named(gate, "s-S", "S", "fp-c")
        granted = await _drain(gate, [junior_first, mid, senior], 3)
        # F0 took the idle slot on arrival (inherent pacing); the queued pair
        # then dispatches by seniority, not by fingerprint novelty.
        assert granted == ["s-F0", "s-S", "s-T"]
    asyncio.run(run())


# ── never waste, never over-collapse ─────────────────────────────────────────

def test_repeat_code_still_fills_leftover_budget(monkeypatch):
    # Budget is never wasted: with nobody fresh waiting, the fleet's siblings
    # DO get the remaining units (the collapse is soft, exactly like the
    # per-actor one).
    monkeypatch.setenv("SOLVER_ROUND_INTAKE_MAX", "3")
    monkeypatch.setenv("SOLVER_BUILD_STRUCTURAL_DEDUP", "1")

    async def run():
        gate = _gate()
        tasks = [_named(gate, f"s-{hk}", hk, FLEET_FP) for hk in FLEET[:3]]
        granted = await _drain(gate, tasks, 3)
        assert granted == ["s-F0", "s-F1", "s-F2"]
        assert gate.snapshot(ROUND)["charged_count"] == 3
    asyncio.run(run())


def test_fingerprint_less_waiters_never_collapse(monkeypatch):
    # Same rule as the slate: no fingerprint ⇒ no collapse (a submission whose
    # stage 1 could not compute one must not be punished for it).
    monkeypatch.setenv("SOLVER_ROUND_INTAKE_MAX", "2")
    monkeypatch.setenv("SOLVER_BUILD_STRUCTURAL_DEDUP", "1")

    async def run():
        gate = _gate(ledger={"F0": 100.0, "F1": 110.0, "S": 500.0})
        tasks = [_named(gate, "s-F0", "F0", ""), _named(gate, "s-F1", "F1", ""),
                 _named(gate, "s-S", "S", "solo-fp-S")]
        granted = await _drain(gate, tasks, 2)
        assert granted == ["s-F0", "s-F1"]   # pure seniority, no collapse
    asyncio.run(run())


def test_legacy_no_coldkey_map_is_unchanged(monkeypatch):
    # With no actor map the gate must dispatch EXACTLY as before — pure key
    # order, no dedup of either kind (the slate does not collapse there either).
    monkeypatch.setenv("SOLVER_ROUND_INTAKE_MAX", "3")
    monkeypatch.setenv("SOLVER_BUILD_STRUCTURAL_DEDUP", "1")

    async def run():
        gate = _gate(coldkeys=None)          # snapshot_resolver() is None
        assert gate._rounds[ROUND].actor_of is None
        assert gate.snapshot(ROUND)["structural_dedup"] is False
        tasks = [_named(gate, f"s-{hk}", hk, FLEET_FP) for hk in FLEET]
        tasks += [_named(gate, "s-S", "S", "solo-fp-S")]
        granted = await _drain(gate, tasks, 3)
        assert granted == ["s-F0", "s-F1", "s-F2"]
    asyncio.run(run())


# ── stability + restart safety ───────────────────────────────────────────────

def test_mid_round_env_flip_cannot_reorder_a_live_queue(monkeypatch):
    monkeypatch.setenv("SOLVER_ROUND_INTAKE_MAX", "3")
    monkeypatch.setenv("SOLVER_BUILD_STRUCTURAL_DEDUP", "0")

    async def run():
        gate = _gate()                                   # snapshotted OFF
        monkeypatch.setenv("SOLVER_BUILD_STRUCTURAL_DEDUP", "1")
        tasks = [_named(gate, f"s-{hk}", hk, FLEET_FP) for hk in FLEET]
        tasks += [_named(gate, "s-S", "S", "solo-fp-S")]
        granted = await _drain(gate, tasks, 3)
        assert granted == ["s-F0", "s-F1", "s-F2"]       # the round it started
    asyncio.run(run())


def test_restart_rebuild_restores_the_built_fingerprints(monkeypatch):
    # A restart must not hand the queue a clean slate: the rebuilt charges carry
    # the fingerprints of the builds that already ran, so the fleet cannot
    # re-spend the budget on code the round already built.
    monkeypatch.setenv("SOLVER_ROUND_INTAKE_MAX", "4")
    monkeypatch.setenv("SOLVER_BUILD_STRUCTURAL_DEDUP", "1")

    async def run():
        set_coldkey_provider(lambda: COLDKEYS)
        gate = BuildBudgetGate(
            ledger_loader=lambda: dict(LEDGER), seen_loader=lambda: {},
        )
        gate.ensure_round(ROUND, prior_attempts=[
            ("s-F0", "F0", FLEET_FP),
            ("s-F0", "F0", FLEET_FP),          # counted once
            ("s-legacy", "S"),                 # 2-tuple still accepted
        ])
        snap = gate.snapshot(ROUND)
        assert snap["charged_count"] == 2
        assert snap["charged_structs"] == [FLEET_FP]
        # Hold the one in-flight slot so the next grant is a QUEUE decision,
        # not the idle-slot-on-arrival pacing artifact.
        blocker = _named(gate, "s-block", "F6", "blocker-fp")
        await _settle()
        assert blocker.done() and blocker.result().granted
        # The fleet's sibling now sorts behind the solo despite better seniority:
        # its code was already built before the restart.
        sibling = _named(gate, "s-F1", "F1", FLEET_FP)
        solo = _named(gate, "s-T", "T", "solo-fp-T")
        await _settle()
        gate.release(ROUND, "s-block")
        await _settle()
        assert solo.done() and solo.result().granted
        assert not sibling.done()
        gate.flush_round(ROUND)
        await _settle()
    asyncio.run(run())
