"""AppIntentStore.get_stats: avg_success_score (avg quality of FILLED executions).

avg_score blends in failures (recorded as ~0 BPS), so it understates fill
quality. avg_success_score averages the on-chain score over successful
executions only. Both are in on-chain BPS (0..10000)."""

import json

from minotaur_subnet.store.app_intent_store import AppIntentStore


def _store(tmp_path):
    return AppIntentStore(store_path=tmp_path / "store.json")


def test_avg_success_score_excludes_failures(tmp_path):
    s = _store(tmp_path)
    # Two fills at 7000/8000 BPS; two hard failures at 0.
    s.record_execution("app1", 7000.0, success=True)
    s.record_execution("app1", 8000.0, success=True)
    s.record_execution("app1", 0.0, success=False)
    s.record_execution("app1", 0.0, success=False)

    st = s.get_stats("app1")
    assert st["total_executions"] == 4
    assert st["successful_executions"] == 2
    # avg_score blends in the two zero-scored failures.
    assert st["avg_score"] == 15000.0 / 4  # 3750
    # avg_success_score is the two fills only.
    assert st["avg_success_score"] == 15000.0 / 2  # 7500


def test_avg_success_score_zero_when_no_fills(tmp_path):
    s = _store(tmp_path)
    s.record_execution("app1", 0.0, success=False)
    st = s.get_stats("app1")
    assert st["successful_executions"] == 0
    assert st["avg_success_score"] == 0.0


def test_rejected_but_scored_not_counted_as_fill(tmp_path):
    """A rejected order can still carry a non-zero on-chain score (below
    threshold). It must NOT inflate avg_success_score."""
    s = _store(tmp_path)
    s.record_execution("app1", 9000.0, success=True)   # a good fill
    s.record_execution("app1", 4000.0, success=False)  # scored but rejected
    st = s.get_stats("app1")
    assert st["successful_executions"] == 1
    assert st["avg_success_score"] == 9000.0  # only the fill, not the 4000


def test_legacy_row_without_successful_score_migrates(tmp_path):
    """Pre-existing app_stats rows have no successful_score key. get_stats falls
    back to total_score; the next record_execution seeds the accumulator so the
    lifetime average stays consistent with the lifetime successful count."""
    s = _store(tmp_path)
    legacy = {
        "total_executions": 10,
        "successful_executions": 4,
        "total_score": 20000.0,  # failures recorded ~0, so ≈ successful sum
        "best_score": 7000.0,
        "last_triggered": 123.0,
        "recent_scores": [7000.0, 6000.0, 0.0, 7000.0],
    }
    with s._connect() as conn:
        conn.execute(
            "INSERT INTO app_stats(app_id, data) VALUES(?, ?)",
            ("legacy", json.dumps(legacy)),
        )

    # Fallback before any new execution: total_score / successful.
    st = s.get_stats("legacy")
    assert st["avg_success_score"] == 20000.0 / 4  # 5000

    # A new fill seeds successful_score (from total_score) then adds the score.
    s.record_execution("legacy", 8000.0, success=True)
    st = s.get_stats("legacy")
    assert st["successful_executions"] == 5
    assert st["avg_success_score"] == (20000.0 + 8000.0) / 5  # 5600
