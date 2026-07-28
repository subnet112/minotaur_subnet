"""PR: GET /v1/solver/queue + benched_slate/incumbent_hotkey on round responses.

Covers the layers: (1) the queue endpoint's join of the rotation ledger with
the actor resolver (seniority, per-hotkey last-benched, actor-shared rank);
(2) the honesty flags — `registered` (metagraph membership; deregistered
relics carry rank=null and hold no place in line) and `contending` (live
submission in the open round); (3) the ?hotkey= filter ranking-before-
filtering contract; (4) the additive round-response fields
(RoundState.benched_slate / incumbent_hotkey on live response + summary).
"""

import asyncio
import json
from types import SimpleNamespace

import pytest

from minotaur_subnet.api.routes.submissions import routes as routes_mod
from minotaur_subnet.api.routes.submissions.round_manager import (
    _round_state_to_response,
)
from minotaur_subnet.api.routes.submissions.routes import (
    _round_summary_from_dict,
    get_solver_queue,
)
from minotaur_subnet.harness import actor as actor_mod
from minotaur_subnet.harness.round_store import RoundState


@pytest.fixture
def ledger_path(tmp_path, monkeypatch):
    """Point the rotation ledger at tmp, reset actor-resolver globals, and
    stub the metagraph/round/store lookups to the deterministic defaults
    (no metagraph, no open round) — tests override per-case."""
    path = tmp_path / "solver_rotation.json"
    monkeypatch.setenv("SOLVER_ROTATION_LEDGER_PATH", str(path))
    monkeypatch.delenv("SOLVER_ACTOR_KEY", raising=False)
    # The absence rule is off by default in these tests: the fixtures use tiny
    # epoch-adjacent timestamps that would ALL read as >4d-lapsed against real
    # wall-clock now. The absence-specific tests re-enable it explicitly.
    monkeypatch.setenv("SOLVER_ROTATION_ABSENCE_RESET_SECONDS", "0")
    monkeypatch.setattr(routes_mod, "_hotkey_to_uid_map", lambda: {})
    monkeypatch.setattr(
        routes_mod, "get_round_store",
        lambda: SimpleNamespace(get_current_round=lambda: None),
    )
    monkeypatch.setattr(
        routes_mod, "get_store",
        lambda: SimpleNamespace(list_by_round=lambda rid: []),
    )
    actor_mod._reset_caches_for_tests()
    actor_mod.set_coldkey_provider(None)
    actor_mod.set_owner_links_provider(None)
    yield path
    actor_mod._reset_caches_for_tests()
    actor_mod.set_coldkey_provider(None)
    actor_mod.set_owner_links_provider(None)


def _write_ledger(path, benched, seen):
    path.write_text(json.dumps({"benched": benched, "seen": seen}))


def _queue(hotkey=None):
    return asyncio.run(get_solver_queue(hotkey=hotkey))


# ── the queue endpoint ───────────────────────────────────────────────────────

def test_empty_ledger_yields_empty_queue(ledger_path):
    resp = _queue()
    assert resp.total == 0 and resp.count == 0 and resp.queue == []
    # No coldkey map installed -> legacy per-hotkey path.
    assert resp.actor_keyed is False
    assert resp.generated_at > 0


def test_per_hotkey_seniority_order_and_fields(ledger_path, monkeypatch):
    monkeypatch.setenv("SOLVER_ACTOR_KEY", "hotkey")  # kill-switch: legacy LRU
    _write_ledger(
        ledger_path,
        benched={"hkOld": 100.0},
        seen={"hkOld": 50.0, "hkWaiting": 30.0},
    )
    resp = _queue()
    assert resp.actor_keyed is False and resp.total == 2
    by_hk = {e.hotkey: e for e in resp.queue}
    # Never-benched ranks by first-seen; benched ranks by last-bench.
    assert by_hk["hkWaiting"].waiting_since == 30.0
    assert by_hk["hkWaiting"].last_benched_at is None
    assert by_hk["hkWaiting"].first_seen_at == 30.0
    assert by_hk["hkOld"].waiting_since == 100.0
    assert by_hk["hkOld"].last_benched_at == 100.0
    # Seniority order: lower waiting_since first, ranks dense from 1.
    assert [e.hotkey for e in resp.queue] == ["hkWaiting", "hkOld"]
    assert [e.rank for e in resp.queue] == [1, 2]
    assert all(e.actor is None for e in resp.queue)


def test_actor_aggregation_shares_seniority_and_rank(ledger_path):
    actor_mod.set_coldkey_provider(
        lambda: {"hkA1": "ckA", "hkA2": "ckA", "hkB": "ckB"},
    )
    _write_ledger(
        ledger_path,
        benched={"hkA1": 200.0},
        seen={"hkA1": 10.0, "hkA2": 20.0, "hkB": 50.0},
    )
    resp = _queue()
    assert resp.actor_keyed is True and resp.total == 3
    by_hk = {e.hotkey: e for e in resp.queue}
    # Benching ANY of the actor's hotkeys makes the whole actor junior (MAX):
    # the never-benched sibling shares the actor clock but keeps its own
    # hotkey-level last_benched_at = null ("this hotkey was never benched").
    assert by_hk["hkA1"].waiting_since == 200.0
    assert by_hk["hkA2"].waiting_since == 200.0
    assert by_hk["hkA2"].last_benched_at is None
    assert by_hk["hkA1"].actor == "ckA" and by_hk["hkA2"].actor == "ckA"
    # Never-benched actor ranks by its earliest hotkey's first-seen (MIN).
    assert by_hk["hkB"].waiting_since == 50.0
    # One rank per actor: hkB senior (rank 1), the ckA pair shares rank 2.
    assert by_hk["hkB"].rank == 1
    assert by_hk["hkA1"].rank == 2 and by_hk["hkA2"].rank == 2


def test_hotkey_filter_preserves_global_rank(ledger_path, monkeypatch):
    monkeypatch.setenv("SOLVER_ACTOR_KEY", "hotkey")
    _write_ledger(
        ledger_path,
        benched={"hkOld": 100.0},
        seen={"hkOld": 50.0, "hkWaiting": 30.0},
    )
    resp = _queue(hotkey="hkOld")
    # Filter applies AFTER ranking: a single-miner query reports global standing.
    assert resp.total == 2 and resp.count == 1
    assert resp.queue[0].hotkey == "hkOld" and resp.queue[0].rank == 2


def test_unknown_hotkey_filter_is_empty_not_error(ledger_path):
    _write_ledger(ledger_path, benched={}, seen={"hkA": 1.0})
    resp = _queue(hotkey="hkNeverSeen")
    assert resp.count == 0 and resp.total == 1 and resp.queue == []


# ── honesty flags: registered / contending / relic rank-skip ─────────────────

def test_registered_flag_and_relic_rank_skip(ledger_path, monkeypatch):
    monkeypatch.setenv("SOLVER_ACTOR_KEY", "hotkey")
    monkeypatch.setattr(routes_mod, "_hotkey_to_uid_map", lambda: {"hkLive": 7})
    _write_ledger(ledger_path, benched={}, seen={"hkDead": 10.0, "hkLive": 20.0})
    resp = _queue()
    by = {e.hotkey: e for e in resp.queue}
    # The deregistered relic is listed (never hidden) but holds no place in
    # line — even though it is MORE senior than the live miner.
    assert by["hkDead"].registered is False and by["hkDead"].rank is None
    assert by["hkLive"].registered is True and by["hkLive"].rank == 1
    assert resp.registered_count == 1 and resp.total == 2


def test_no_metagraph_everyone_ranked_legacy(ledger_path, monkeypatch):
    monkeypatch.setenv("SOLVER_ACTOR_KEY", "hotkey")
    _write_ledger(ledger_path, benched={}, seen={"hkA": 1.0, "hkB": 2.0})
    resp = _queue()
    # Fail-open: no metagraph -> indeterminate, never "relic".
    assert all(e.registered is None for e in resp.queue)
    assert [e.rank for e in resp.queue] == [1, 2]
    assert resp.registered_count is None


def test_relic_sibling_shares_registered_actors_rank(ledger_path, monkeypatch):
    actor_mod.set_coldkey_provider(lambda: {"hkA1": "ckA", "hkA2": "ckA"})
    monkeypatch.setattr(routes_mod, "_hotkey_to_uid_map", lambda: {"hkA2": 3})
    _write_ledger(ledger_path, benched={}, seen={"hkA1": 1.0, "hkA2": 2.0})
    resp = _queue()
    by = {e.hotkey: e for e in resp.queue}
    # The actor is rankable through its registered sibling, so the
    # deregistered hotkey still displays the actor's (shared) rank.
    assert by["hkA1"].registered is False and by["hkA1"].rank == 1
    assert by["hkA2"].registered is True and by["hkA2"].rank == 1


def test_contending_flag_from_open_round(ledger_path, monkeypatch):
    monkeypatch.setenv("SOLVER_ACTOR_KEY", "hotkey")
    monkeypatch.setattr(
        routes_mod, "get_round_store",
        lambda: SimpleNamespace(
            get_current_round=lambda: SimpleNamespace(round_id="r-9"),
        ),
    )
    monkeypatch.setattr(
        routes_mod, "get_store",
        lambda: SimpleNamespace(list_by_round=lambda rid: [
            SimpleNamespace(hotkey="hkA", status="benchmarking"),
            SimpleNamespace(hotkey="hkB", status="rejected"),
        ]),
    )
    _write_ledger(ledger_path, benched={}, seen={"hkA": 1.0, "hkB": 2.0})
    resp = _queue()
    by = {e.hotkey: e for e in resp.queue}
    assert resp.round_id == "r-9"
    assert by["hkA"].contending is True
    # Terminal status = out of the running -> not contending.
    assert by["hkB"].contending is False
    assert resp.contending_count == 1


def test_contending_lookup_failure_degrades(ledger_path, monkeypatch):
    monkeypatch.setenv("SOLVER_ACTOR_KEY", "hotkey")

    def boom():
        raise RuntimeError("round store unavailable")

    monkeypatch.setattr(routes_mod, "get_round_store", boom)
    _write_ledger(ledger_path, benched={}, seen={"hkA": 1.0})
    resp = _queue()
    # Best-effort: degrade to "nobody contending", never a 500.
    assert resp.round_id is None and resp.contending_count == 0
    assert resp.queue[0].contending is False and resp.queue[0].rank == 1


# ── absence rule: lapsed seniority demotes to the back of the queue ──────────

def test_lapsed_absentee_demoted_to_newcomer(ledger_path, monkeypatch):
    import time as _time

    monkeypatch.setenv("SOLVER_ACTOR_KEY", "hotkey")
    monkeypatch.setenv("SOLVER_ROTATION_ABSENCE_RESET_SECONDS", str(4 * 86400))
    now = _time.time()
    # hkDormant benched 24d ago with no activity since; hkFresh benched 1h ago.
    _write_ledger(
        ledger_path,
        benched={"hkDormant": now - 24 * 86400, "hkFresh": now - 3600},
        seen={"hkDormant": now - 30 * 86400, "hkFresh": now - 86400},
    )
    resp = _queue()
    by = {e.hotkey: e for e in resp.queue}
    # Absent > window: seniority lapsed — displayed as what selection WOULD
    # do on return (newcomer), so the 24d-idle entry sorts BEHIND the fresh one.
    assert by["hkDormant"].seniority_expired is True
    assert by["hkDormant"].waiting_since >= now - 1
    assert by["hkFresh"].seniority_expired is False
    assert [e.hotkey for e in resp.queue] == ["hkFresh", "hkDormant"]
    assert by["hkFresh"].rank == 1 and by["hkDormant"].rank == 2
    # Bench history stays honest — demotion never rewrites last_benched_at.
    assert by["hkDormant"].last_benched_at == pytest.approx(now - 24 * 86400)


def test_contending_entry_never_demoted(ledger_path, monkeypatch):
    import time as _time

    monkeypatch.setenv("SOLVER_ACTOR_KEY", "hotkey")
    monkeypatch.setenv("SOLVER_ROTATION_ABSENCE_RESET_SECONDS", str(4 * 86400))
    monkeypatch.setattr(
        routes_mod, "get_round_store",
        lambda: SimpleNamespace(
            get_current_round=lambda: SimpleNamespace(round_id="r-1"),
        ),
    )
    monkeypatch.setattr(
        routes_mod, "get_store",
        lambda: SimpleNamespace(list_by_round=lambda rid: [
            SimpleNamespace(hotkey="hkBack", status="benchmarking"),
        ]),
    )
    now = _time.time()
    _write_ledger(ledger_path, benched={"hkBack": now - 24 * 86400}, seen={})
    resp = _queue()
    e = resp.queue[0]
    # Present in the round = not absent: the entry keeps its (stale) clock for
    # display; the selection-side reset lands at close.
    assert e.contending is True and e.seniority_expired is False
    assert e.waiting_since == pytest.approx(now - 24 * 86400)


def test_absence_rule_killswitch_off(ledger_path, monkeypatch):
    import time as _time

    monkeypatch.setenv("SOLVER_ACTOR_KEY", "hotkey")
    monkeypatch.setenv("SOLVER_ROTATION_ABSENCE_RESET_SECONDS", "0")
    now = _time.time()
    _write_ledger(ledger_path, benched={"hkDormant": now - 24 * 86400}, seen={})
    resp = _queue()
    assert resp.queue[0].seniority_expired is False
    assert resp.queue[0].waiting_since == pytest.approx(now - 24 * 86400)


# ── round responses: benched_slate + incumbent_hotkey exposure ───────────────

def test_round_response_exposes_slate_and_incumbent_hotkey():
    # No finalist -> the relative-extra builder returns before touching the
    # submission store, so this stays a pure unit test.
    state = RoundState(
        round_id="r1",
        incumbent_hotkey="hkChamp",
        benched_slate=["sub_a", "sub_b"],
    )
    resp = _round_state_to_response(state)
    assert resp.incumbent_hotkey == "hkChamp"
    assert resp.benched_slate == ["sub_a", "sub_b"]


def test_round_summary_exposes_slate_and_incumbent_hotkey():
    s = _round_summary_from_dict({
        "round_id": "r1", "status": "activated", "created_at": 100.0,
        "incumbent_hotkey": "hkChamp", "benched_slate": ["sub_a", "sub_b"],
    })
    assert s.incumbent_hotkey == "hkChamp"
    assert s.benched_slate == ["sub_a", "sub_b"]


def test_round_summary_tolerates_legacy_rows_without_new_fields():
    s = _round_summary_from_dict({
        "round_id": "r0", "status": "aborted", "created_at": 1.0,
        # pre-field rounds / rotation-disabled rounds carry no slate
        "benched_slate": None,
    })
    assert s.incumbent_hotkey is None and s.benched_slate is None
