"""Actor-keyed queue (harness/actor.py + rotation + build budget + copycat
reject): a coldkey's hotkeys are ONE scheduling identity.

Contract under test (2026-07-22 audit): a fleet rotating N hotkeys must hold
one rotation-seniority entry, one newcomer lottery ticket and (softly) one
build unit / slate seat per round — while behavior with no coldkey map (or the
``SOLVER_ACTOR_KEY=hotkey`` kill-switch) stays byte-identical to the legacy
per-hotkey queue.
"""

import asyncio
from types import SimpleNamespace

import pytest

from minotaur_subnet.harness import actor as actor_mod
from minotaur_subnet.harness.actor import (
    actor_last_selected,
    resolve_actor,
    set_coldkey_provider,
)
from minotaur_subnet.harness.build_budget import BuildBudgetGate
from minotaur_subnet.harness.rotation import (
    actor_rotation_sort_key,
    rotation_sort_key,
    select_rotation_slate,
)

# Fleet: hotkeys A1/A2/A3 share coldkey CK_A; S is a solo miner (own coldkey).
COLDKEYS = {"A1": "CK_A", "A2": "CK_A", "A3": "CK_A", "S": "CK_S", "T": "CK_T"}


@pytest.fixture(autouse=True)
def _clean_actor_state():
    set_coldkey_provider(None)
    yield
    set_coldkey_provider(None)


def _with_map(mapping=COLDKEYS):
    set_coldkey_provider(lambda: mapping)


def _sub(hotkey, sid=None, status="queued"):
    return SimpleNamespace(
        submission_id=sid or f"sub_{hotkey}",
        hotkey=hotkey,
        status=SimpleNamespace(value=status),
    )


# ── resolve_actor ─────────────────────────────────────────────────────────────

def test_resolver_maps_to_coldkey_and_falls_back_to_hotkey():
    _with_map()
    assert resolve_actor("A1") == "CK_A"
    assert resolve_actor("unknown-hk") == "unknown-hk"  # not in map → itself
    assert resolve_actor("") == ""


def test_no_provider_means_per_hotkey_identity():
    assert resolve_actor("A1") == "A1"


def test_kill_switch_restores_per_hotkey(monkeypatch):
    _with_map()
    monkeypatch.setenv("SOLVER_ACTOR_KEY", "hotkey")
    assert resolve_actor("A1") == "A1"


def test_provider_failure_degrades_not_raises():
    def _boom():
        raise RuntimeError("metagraph down")
    set_coldkey_provider(_boom)
    assert resolve_actor("A1") == "A1"


def test_actor_last_selected_takes_max_over_fleet():
    _with_map()
    ledger = {"A1": 100.0, "A2": 300.0, "S": 200.0}
    agg = actor_last_selected(ledger, resolve_actor)
    assert agg == {"CK_A": 300.0, "CK_S": 200.0}


# ── slate selection ───────────────────────────────────────────────────────────

def test_fleet_holds_one_seat_not_three():
    _with_map()
    subs = [_sub("A1"), _sub("A2"), _sub("A3"), _sub("S"), _sub("T")]
    selected, skipped = select_rotation_slate(subs, 3, {}, "r1", actor_of=resolve_actor)
    seated = [s.hotkey for s in selected]
    assert len(selected) == 3
    assert len([hk for hk in seated if hk.startswith("A")]) == 1
    assert "S" in seated and "T" in seated


def test_benching_one_fleet_hotkey_ages_the_whole_fleet():
    _with_map()
    # A2 benched recently; fresh sibling A1 must NOT count as never-benched.
    subs = [_sub("A1"), _sub("S")]
    selected, _ = select_rotation_slate(
        subs, 1, {"A2": 500.0}, "r1", actor_of=resolve_actor,
    )
    assert [s.hotkey for s in selected] == ["S"]  # solo (never benched) wins


def test_leftover_slots_fill_rather_than_waste():
    _with_map()
    subs = [_sub("A1"), _sub("A2"), _sub("A3")]
    selected, skipped = select_rotation_slate(subs, 3, {}, "r1", actor_of=resolve_actor)
    assert len(selected) == 3 and skipped == []  # only one actor → still 3 benched


def test_skipped_stays_in_seniority_order_for_waitlist_positions():
    _with_map()
    subs = [_sub("A1"), _sub("A2"), _sub("A3"), _sub("S")]
    _, skipped = select_rotation_slate(subs, 2, {}, "r1", actor_of=resolve_actor)
    assert len(skipped) == 2
    assert all(s.hotkey.startswith("A") for s in skipped)


def test_identity_resolver_reproduces_legacy_order_exactly():
    subs = [_sub(hk) for hk in ("A", "B", "C", "D", "E")]
    ledger = {"A": 300.0, "B": 100.0}
    legacy = select_rotation_slate(subs, 2, ledger, "r7")
    actored = select_rotation_slate(subs, 2, ledger, "r7", actor_of=lambda h: h)
    assert [s.hotkey for s in legacy[0]] == [s.hotkey for s in actored[0]]
    assert [s.hotkey for s in legacy[1]] == [s.hotkey for s in actored[1]]


def test_actor_sort_key_identity_matches_legacy_prefix():
    ledger = {"X": 42.0}
    legacy = rotation_sort_key("X", "r1", ledger)
    actored = actor_rotation_sort_key("X", "r1", ledger, lambda h: h)
    assert actored[:2] == legacy


# ── build budget ──────────────────────────────────────────────────────────────

def _gate(ledger, now=1000.0):
    return BuildBudgetGate(ledger_loader=lambda: dict(ledger), now=lambda: now)


def _acquire(gate, sid, hotkey, round_id="r1"):
    return asyncio.get_event_loop().create_task(
        gate.acquire(submission_id=sid, hotkey=hotkey, round_id=round_id),
    )


def test_fleet_hotkey_of_benched_coldkey_is_proven_not_newcomer(monkeypatch):
    _with_map()
    gate = _gate({"A2": 500.0})
    gate.ensure_round("r1", opened_at=0.0, open_seconds=1200.0)
    state = gate._rounds["r1"]
    # Fresh sibling A1 inherits the fleet's proven status (no newcomer lottery
    # re-entry) — and an unknown hotkey stays a newcomer.
    assert gate._is_proven(state, "A1") is True
    assert gate._is_proven(state, "fresh") is False


def test_fleet_gets_one_lottery_ticket(monkeypatch):
    _with_map()
    monkeypatch.setenv("SOLVER_ROUND_INTAKE_MAX", "8")
    gate = _gate({})
    gate.ensure_round("r1", opened_at=0.0, open_seconds=1200.0)
    state = gate._rounds["r1"]
    keys = {
        hk: actor_rotation_sort_key(hk, "r1", state.actor_last, state.actor_of)
        for hk in ("A1", "A2", "A3")
    }
    # One actor ⇒ one primary lottery position: the actor-salted element is
    # identical across the fleet's hotkeys (they share one ticket).
    assert len({k[1] for k in keys.values()}) == 1


async def _drain(gate, round_id, grants):
    for sid in grants:
        gate.release(round_id, sid)
        await asyncio.sleep(0)


def test_soft_per_actor_dedup_prefers_fresh_actors(monkeypatch):
    _with_map()
    monkeypatch.setenv("SOLVER_ROUND_INTAKE_MAX", "3")
    monkeypatch.setenv("SCREENING_BUILD_CONCURRENCY", "1")

    async def run():
        gate = _gate({"A1": 100.0, "A2": 200.0, "S": 300.0, "T": 400.0})
        gate.ensure_round("r1", opened_at=0.0, open_seconds=1200.0)
        # Fleet floods first with its two most-LRU hotkeys; solos arrive after.
        t_a1 = asyncio.ensure_future(gate.acquire(submission_id="a1", hotkey="A1", round_id="r1"))
        t_a2 = asyncio.ensure_future(gate.acquire(submission_id="a2", hotkey="A2", round_id="r1"))
        await asyncio.sleep(0)
        t_s = asyncio.ensure_future(gate.acquire(submission_id="s", hotkey="S", round_id="r1"))
        t_t = asyncio.ensure_future(gate.acquire(submission_id="t", hotkey="T", round_id="r1"))
        await asyncio.sleep(0)
        # First grant went to the fleet (A1, most senior at dispatch time).
        assert t_a1.done() and t_a1.result().granted
        gate.release("r1", "a1")
        await asyncio.sleep(0)
        # Next unit must go to a FRESH actor (S — LRU among un-charged actors),
        # not the fleet's second hotkey, despite A2's better LRU timestamp.
        assert t_s.done() and t_s.result().granted
        assert not t_a2.done()
        gate.release("r1", "s")
        await asyncio.sleep(0)
        # Third unit: T (last fresh actor) beats the fleet repeat.
        assert t_t.done() and t_t.result().granted
        assert not t_a2.done()  # budget (3) spent — fleet's 2nd build never granted
        gate.flush_round("r1")
        await asyncio.sleep(0)
        assert t_a2.done() and not t_a2.result().granted
    asyncio.run(run())


def test_repeat_actor_still_served_when_alone(monkeypatch):
    _with_map()
    monkeypatch.setenv("SOLVER_ROUND_INTAKE_MAX", "3")
    monkeypatch.setenv("SCREENING_BUILD_CONCURRENCY", "2")

    async def run():
        # now=1000 with the round open since 100: past the 0.5×1200 spill
        # threshold, so the lone actor may consume idle proven units too.
        gate = _gate({}, now=1000.0)
        gate.ensure_round("r1", opened_at=100.0, open_seconds=1200.0)
        t1 = asyncio.ensure_future(gate.acquire(submission_id="a1", hotkey="A1", round_id="r1"))
        t2 = asyncio.ensure_future(gate.acquire(submission_id="a2", hotkey="A2", round_id="r1"))
        await asyncio.sleep(0)
        # No other actor waiting → the fleet's second submission is NOT starved.
        assert t1.done() and t1.result().granted
        assert t2.done() and t2.result().granted
    asyncio.run(run())
