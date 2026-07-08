"""Blind-spot REPEAT guard — ARMING wiring (persistence + parity surfaces).

Phase-0 (#573) shipped the rule + the leader's in-memory bar. This locks in the
arming prerequisites:
- RoundStore.champion_adoption_bar: persisted record, atomic-write roundtrip,
  tolerant of pre-upgrade stores.
- bar_kwargs_from_record: the ONE round-store-sourced kwarg builder (follower
  vote / worker shadow vote / ranking) — inert on mismatch/absence.
- relative_counts / relative_reason: repeats fold into matched WITH an explicit
  count + phrase, so the miner-facing report agrees with an armed verdict.
The switch itself (BLIND_SPOT_BAR_TTL_S) stays None — armed behavior is
exercised via monkeypatch, as in test_relative_scoring.py.
"""

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from minotaur_subnet.epoch.relative_scoring import (
    bar_kwargs_from_record,
    relative_counts,
    relative_reason,
)
from minotaur_subnet.harness.round_store import RoundStore


def _rows(pairs):
    return [{"intent_id": iid, "raw_output": out} for iid, out in pairs]


# ── RoundStore persistence ────────────────────────────────────────────────────


def test_round_store_bar_roundtrip(tmp_path):
    path = tmp_path / "rounds.json"
    store = RoundStore(persist_path=path)
    store.set_champion_adoption_bar(
        submission_id="sub_a", outputs={"o1": "5000"}, activated_at=123.0,
    )
    assert store.get_champion_adoption_bar() == {
        "submission_id": "sub_a", "outputs": {"o1": "5000"}, "activated_at": 123.0,
    }
    # Restart: a fresh store on the same path restores the record.
    reloaded = RoundStore(persist_path=path)
    assert reloaded.get_champion_adoption_bar()["outputs"] == {"o1": "5000"}


def test_round_store_bar_overwrite_clears(tmp_path):
    # A rows-less adoption must CLEAR the displaced champion's bar.
    store = RoundStore(persist_path=tmp_path / "rounds.json")
    store.set_champion_adoption_bar(
        submission_id="sub_a", outputs={"o1": "5000"}, activated_at=1.0,
    )
    store.set_champion_adoption_bar(submission_id="sub_b", outputs=None, activated_at=2.0)
    rec = store.get_champion_adoption_bar()
    assert rec["submission_id"] == "sub_b"
    assert rec["outputs"] == {}


def test_round_store_without_bar_key_loads_empty(tmp_path):
    # Pre-upgrade store file (no champion_adoption_bar key) → {} not a crash.
    path = tmp_path / "rounds.json"
    RoundStore(persist_path=path)._persist_path  # noqa: B018 — just construct
    path.write_text('{"current_round_id": null, "rounds": {}}')
    assert RoundStore(persist_path=path).get_champion_adoption_bar() == {}


# ── bar_kwargs_from_record ────────────────────────────────────────────────────


_REC = {"submission_id": "sub_a", "outputs": {"o1": "5000"}, "activated_at": 100.0}


def test_bar_kwargs_match():
    kw = bar_kwargs_from_record(_REC, "sub_a", now=160.0)
    assert kw == {"champion_bar": {"o1": "5000"}, "bar_age_s": 60.0}


def test_bar_kwargs_inert_on_mismatch_or_absence():
    # A displaced champion's record must never gate covers against the wrong bar.
    assert bar_kwargs_from_record(_REC, "sub_b", now=160.0) == {}
    assert bar_kwargs_from_record({}, "sub_a", now=160.0) == {}
    assert bar_kwargs_from_record(None, "sub_a", now=160.0) == {}
    assert bar_kwargs_from_record(_REC, None, now=160.0) == {}
    empty = dict(_REC, outputs={})
    assert bar_kwargs_from_record(empty, "sub_a", now=160.0) == {}


def test_bar_kwargs_clock_skew_clamps_to_zero():
    kw = bar_kwargs_from_record(_REC, "sub_a", now=50.0)
    assert kw["bar_age_s"] == 0.0


# ── report surfaces agree with an armed verdict ───────────────────────────────


def test_relative_counts_repeat_folds_into_matched(monkeypatch):
    from minotaur_subnet.epoch import relative_scoring as rs

    monkeypatch.setattr(rs, "BLIND_SPOT_BAR_TTL_S", 24 * 3600.0)
    champ = _rows([("o1", "100"), ("o2", "0")])
    chal = _rows([("o1", "100"), ("o2", "5000")])
    counts = relative_counts(
        champ, chal, champion_bar={"o2": "5000"}, bar_age_s=60.0,
    )
    assert counts["verdict"] != "dethrone"
    assert counts["better"] == 0 and counts["new"] == 0
    assert counts["repeats"] == 1
    assert counts["matched"] == 2  # o1 matched + o2 repeat (compared-but-neutral)
    reason = relative_reason(counts)
    assert "1 blind-spot repeat(s) not credited" in reason


def test_relative_counts_disarmed_unchanged(monkeypatch):
    # Disarmed (revert to None): a within-bar cover still dethrones, no repeat.
    from minotaur_subnet.epoch import relative_scoring as rs

    monkeypatch.setattr(rs, "BLIND_SPOT_BAR_TTL_S", None)
    champ = _rows([("o1", "100"), ("o2", "0")])
    chal = _rows([("o1", "100"), ("o2", "5000")])
    counts = relative_counts(champ, chal, champion_bar={"o2": "5000"}, bar_age_s=60.0)
    assert counts["verdict"] == "dethrone"
    assert counts["repeats"] == 0
    assert "repeat" not in (relative_reason(counts) or "")


def test_report_md_renders_repeat_row(monkeypatch):
    from minotaur_subnet.api.routes.submissions.report import render_report_md
    from minotaur_subnet.epoch import relative_scoring as rs

    monkeypatch.setattr(rs, "BLIND_SPOT_BAR_TTL_S", 24 * 3600.0)
    champ = _rows([("o1", "100"), ("o2", "0")])
    chal = _rows([("o1", "100"), ("o2", "5000")])
    rel = relative_counts(champ, chal, champion_bar={"o2": "5000"}, bar_age_s=60.0)
    md = render_report_md({"outcome": "matched", "relative": rel})
    assert "1 repeat (not credited)" in md
    assert "beat the recorded value" in md
    assert "`o2`" in md
