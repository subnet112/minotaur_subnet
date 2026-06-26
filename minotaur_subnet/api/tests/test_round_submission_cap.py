"""PR: round-wide submission cap (SOLVER_ROUND_MAX_SUBMISSIONS).

Bounds the TOTAL submissions accepted per round across ALL miners (first-come;
the rest retry next round), which bounds the per-round benchmark batch. The cap
is leader-local admission control (ingest is leader-canonical), enforced both at
the route pre-check and atomically inside store.create() as the TOCTOU backstop.
"""

import pytest

from minotaur_subnet.api.routes.submissions.routes import (
    _max_submissions_per_round_total,
)
from minotaur_subnet.harness.submission_store import SubmissionStatus, SubmissionStore


def _store(tmp_path):
    return SubmissionStore(persist_path=tmp_path / "s.json")


def _mk(store, hotkey, rid="rr", **kw):
    return store.create("r", "h", epoch=1, hotkey=hotkey, round_id=rid, **kw)


# ── count_by_round ───────────────────────────────────────────────────────────

def test_count_by_round_spans_all_miners(tmp_path):
    store = _store(tmp_path)
    _mk(store, "5A")
    _mk(store, "5B")
    _mk(store, "5C", rid="other")
    assert store.count_by_round("rr") == 2
    assert store.count_by_round("other") == 1
    assert store.count_by_round("none") == 0


# ── the round-wide cap (across miners) ───────────────────────────────────────

def test_global_cap_rejects_fourth_distinct_miner(tmp_path):
    store = _store(tmp_path)
    for hk in ("5A", "5B", "5C"):  # 3 distinct miners fill the round
        _mk(store, hk, max_per_round=1, max_total_per_round=3)
    with pytest.raises(ValueError, match="is full"):
        _mk(store, "5D", max_per_round=1, max_total_per_round=3)


def test_global_cap_zero_is_unlimited(tmp_path):
    store = _store(tmp_path)
    for i in range(10):  # default max_total_per_round=0 → no round-wide cap
        _mk(store, f"5_{i}", max_per_round=1, max_total_per_round=0)
    assert store.count_by_round("rr") == 10


def test_global_cap_counts_all_statuses(tmp_path):
    store = _store(tmp_path)
    a = _mk(store, "5A", max_total_per_round=2)
    a.status = SubmissionStatus.REJECTED  # a rejected sub still consumes a slot
    _mk(store, "5B", max_total_per_round=2)
    with pytest.raises(ValueError, match="is full"):
        _mk(store, "5C", max_total_per_round=2)


def test_per_hotkey_and_global_caps_are_independent(tmp_path):
    store = _store(tmp_path)
    # Same miner's 2nd submission is blocked by the PER-HOTKEY cap, not the global.
    _mk(store, "5A", max_per_round=1, max_total_per_round=3)
    with pytest.raises(ValueError, match="already submitted"):
        _mk(store, "5A", max_per_round=1, max_total_per_round=3)
    # Distinct miners still fill up to the global cap.
    _mk(store, "5B", max_per_round=1, max_total_per_round=3)
    _mk(store, "5C", max_per_round=1, max_total_per_round=3)
    with pytest.raises(ValueError, match="is full"):
        _mk(store, "5D", max_per_round=1, max_total_per_round=3)


def test_global_cap_backstop_with_unlimited_per_hotkey(tmp_path):
    # One miner, per-hotkey unlimited (0), but the round-wide cap still bounds it.
    store = _store(tmp_path)
    _mk(store, "5A", max_per_round=0, max_total_per_round=2)
    _mk(store, "5A", max_per_round=0, max_total_per_round=2)
    with pytest.raises(ValueError, match="is full"):
        _mk(store, "5A", max_per_round=0, max_total_per_round=2)


# ── config helper ────────────────────────────────────────────────────────────

def test_total_cap_env_default_zero(monkeypatch):
    monkeypatch.delenv("SOLVER_ROUND_MAX_SUBMISSIONS", raising=False)
    assert _max_submissions_per_round_total() == 0


def test_total_cap_env_parsed(monkeypatch):
    monkeypatch.setenv("SOLVER_ROUND_MAX_SUBMISSIONS", "3")
    assert _max_submissions_per_round_total() == 3


def test_total_cap_env_garbage_is_zero(monkeypatch):
    monkeypatch.setenv("SOLVER_ROUND_MAX_SUBMISSIONS", "lots")
    assert _max_submissions_per_round_total() == 0
