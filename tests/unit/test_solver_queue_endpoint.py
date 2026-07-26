"""PR: GET /v1/solver/queue + benched_slate/incumbent_hotkey on round responses.

Covers the three layers: (1) the queue endpoint's join of the rotation ledger
with the actor resolver (seniority, per-hotkey last-benched, actor-shared
rank); (2) the ?hotkey= filter ranking-before-filtering contract; (3) the
additive round-response fields (RoundState.benched_slate / incumbent_hotkey
threaded through both the live response and the history summary).
"""

import asyncio
import json

import pytest

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
    """Point the rotation ledger at tmp and reset actor-resolver globals."""
    path = tmp_path / "solver_rotation.json"
    monkeypatch.setenv("SOLVER_ROTATION_LEDGER_PATH", str(path))
    monkeypatch.delenv("SOLVER_ACTOR_KEY", raising=False)
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
