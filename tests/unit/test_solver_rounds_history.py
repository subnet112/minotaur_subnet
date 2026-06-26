"""PR: solver-round history in the order-book DB + GET /v1/solver/rounds.

Covers the three layers: (1) AppIntentStore.save_round/list_rounds/count_rounds
(the durable mirror, same SQLite DB as the order book); (2) the RoundStore
record_sink that mirrors each mutation (best-effort — never breaks round logic);
(3) the route summary builder's outcome derivation (adopted vs aborted).
"""

import pytest

from minotaur_subnet.store import AppIntentStore
from minotaur_subnet.harness.round_store import RoundStatus, RoundStore
from minotaur_subnet.api.routes.submissions.routes import _round_summary_from_dict


# ── AppIntentStore: solver_rounds table (the order-book DB mirror) ────────────

def _store(tmp_path):
    return AppIntentStore(store_path=tmp_path / "s.db")


def test_save_and_list_rounds_newest_first(tmp_path):
    store = _store(tmp_path)
    store.save_round({"round_id": "r1", "status": "open", "opened_epoch": 1, "created_at": 100.0})
    store.save_round({"round_id": "r2", "status": "activated", "opened_epoch": 2, "created_at": 200.0})
    rows = store.list_rounds()
    assert [r["round_id"] for r in rows] == ["r2", "r1"]  # created_at DESC
    assert store.count_rounds() == 2
    # the full RoundState dict round-trips through the data blob
    assert rows[0]["status"] == "activated" and rows[0]["opened_epoch"] == 2


def test_save_round_upserts_by_round_id(tmp_path):
    store = _store(tmp_path)
    store.save_round({"round_id": "r1", "status": "open", "opened_epoch": 1, "created_at": 100.0})
    store.save_round({"round_id": "r1", "status": "activated", "opened_epoch": 1, "created_at": 100.0})
    rows = store.list_rounds()
    assert len(rows) == 1 and rows[0]["status"] == "activated"
    assert store.count_rounds() == 1


def test_list_rounds_pagination_and_status_filter(tmp_path):
    store = _store(tmp_path)
    for i in range(5):
        store.save_round({
            "round_id": f"r{i}",
            "status": "activated" if i % 2 == 0 else "aborted",
            "opened_epoch": i, "created_at": float(i),
        })
    assert len(store.list_rounds(limit=2)) == 2
    assert len(store.list_rounds(limit=2, offset=4)) == 1            # only 1 left
    assert store.count_rounds(status="activated") == 3
    assert all(r["status"] == "activated" for r in store.list_rounds(status="activated"))


# ── RoundStore.record_sink: mirror each mutation, best-effort ─────────────────

def test_record_sink_fires_on_each_round_mutation(tmp_path):
    seen: list[str] = []
    store = RoundStore(
        persist_path=tmp_path / "r.json",
        record_sink=lambda rs: seen.append(rs.status.value),
    )
    store.ensure_open_round(opened_epoch=1)
    rid = store.get_current_round().round_id
    store.close_current_round(close_epoch=2)
    store.abort_round(rid, "certification_deadline_elapsed")
    assert "open" in seen and "closed" in seen and "aborted" in seen


def test_record_sink_is_best_effort_never_breaks_rounds(tmp_path):
    def boom(_rs):
        raise RuntimeError("db down")
    store = RoundStore(persist_path=tmp_path / "r.json", record_sink=boom)
    # The sink raising must NOT propagate — round consensus can't depend on it.
    rs = store.ensure_open_round(opened_epoch=1)
    assert rs.status is RoundStatus.OPEN
    # and the round is still usable
    assert store.get_current_round().round_id == rs.round_id


def test_no_record_sink_is_inert(tmp_path):
    store = RoundStore(persist_path=tmp_path / "r.json")  # legacy: no sink
    store.ensure_open_round(opened_epoch=1)  # must not error
    assert store.get_current_round() is not None


def test_record_sink_receives_the_mutated_round(tmp_path):
    captured: list[tuple[str, str]] = []
    store = RoundStore(
        persist_path=tmp_path / "r.json",
        record_sink=lambda rs: captured.append((rs.round_id, rs.status.value)),
    )
    st = store.ensure_open_round(opened_epoch=7)
    assert captured[-1] == (st.round_id, "open")


# ── route summary: outcome derivation ────────────────────────────────────────

def test_summary_activated_round_is_adopted_with_winner():
    s = _round_summary_from_dict({
        "round_id": "r1", "status": "activated", "opened_epoch": 1, "close_epoch": 2,
        "finalist_submission_id": "sub_x", "finalist_score": 0.91,
        "certificate": {"candidate_submission_id": "sub_x"},
        "effective_epoch": 8, "created_at": 100.0, "updated_at": 150.0,
    })
    assert s.adopted is True
    assert s.adopted_submission_id == "sub_x"
    assert s.finalist_score == 0.91 and s.status == "activated"


def test_summary_aborted_round_is_not_adopted():
    s = _round_summary_from_dict({
        "round_id": "r2", "status": "aborted",
        "abort_reason": "certification_deadline_elapsed", "created_at": 100.0,
    })
    assert s.adopted is False
    assert s.adopted_submission_id is None
    assert s.abort_reason == "certification_deadline_elapsed"


def test_summary_open_round_is_in_progress():
    s = _round_summary_from_dict({"round_id": "r3", "status": "open", "opened_epoch": 5, "created_at": 100.0})
    assert s.adopted is False and s.status == "open" and s.close_epoch is None


def test_summary_activated_falls_back_to_finalist_when_no_certificate():
    s = _round_summary_from_dict({
        "round_id": "r4", "status": "activated",
        "finalist_submission_id": "sub_y", "created_at": 100.0,
    })
    assert s.adopted is True and s.adopted_submission_id == "sub_y"
