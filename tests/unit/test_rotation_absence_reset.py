"""PR: rotation absence reset — seniority is earned by presence.

An identity whose last submission activity is older than the reset window
(SOLVER_ROTATION_ABSENCE_RESET_SECONDS, default 4 days; 0 disables) forfeits
its accrued wait and re-enters the queue as a NEWCOMER (clock = return time).
Keyed on ACTIVITY, never clock age: a continuously-submitting miner merely
starved by capacity keeps full seniority. Covers: the pure fold/evidence
helpers, ledger v3 (active/reset maps, v2-file compat), detection at
apply_rotation_slate (persisted resets, store backfill, fleet siblings), and
the kill-switch.
"""

import json
from types import SimpleNamespace

import pytest

from minotaur_subnet.harness import actor as actor_mod
from minotaur_subnet.harness.rotation import (
    RotationLedger,
    absence_reset_seconds,
    actor_evidence_map,
    apply_rotation_slate,
    fold_reset,
)

DAY = 86400.0


def _sub(hotkey, sid=None, status="queued", created_at=0.0):
    return SimpleNamespace(
        submission_id=sid or f"sub_{hotkey}",
        hotkey=hotkey,
        status=SimpleNamespace(value=status),
        created_at=created_at,
    )


class _FakeStore:
    def __init__(self, subs, latest=None):
        self.subs = list(subs)
        self.rejected: dict[str, str] = {}
        self._latest = latest

    def list_by_round(self, round_id):
        return self.subs

    def reject(self, submission_id, reason):
        self.rejected[submission_id] = reason

    def latest_created_at_by_hotkey(self, *, exclude_round_id=None):
        if self._latest is None:
            return {}
        return dict(self._latest)


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    """Default window 4d; per-hotkey actor path; no resolver globals.

    SOLVER_ROTATION_LEDGER_PATH points at tmp so the actor-map SIDECARS (which
    derive their directory from it) land in tmp too — without this, a test
    that installs a coldkey provider persists actor_coldkeys.json into the
    repo CWD and silently flips OTHER tests onto the actor-keyed path.
    """
    monkeypatch.delenv("SOLVER_ROTATION_ABSENCE_RESET_SECONDS", raising=False)
    monkeypatch.setenv("SOLVER_ROTATION_LEDGER_PATH", str(tmp_path / "rot.json"))
    monkeypatch.setenv("SOLVER_ACTOR_KEY", "hotkey")
    actor_mod._reset_caches_for_tests()
    actor_mod.set_coldkey_provider(None)
    actor_mod.set_owner_links_provider(None)
    yield
    actor_mod._reset_caches_for_tests()
    actor_mod.set_coldkey_provider(None)
    actor_mod.set_owner_links_provider(None)


# ── pure helpers ─────────────────────────────────────────────────────────────

def test_absence_reset_seconds_default_and_override(monkeypatch):
    assert absence_reset_seconds() == 4 * DAY
    monkeypatch.setenv("SOLVER_ROTATION_ABSENCE_RESET_SECONDS", "3600")
    assert absence_reset_seconds() == 3600.0
    monkeypatch.setenv("SOLVER_ROTATION_ABSENCE_RESET_SECONDS", "0")
    assert absence_reset_seconds() == 0.0
    monkeypatch.setenv("SOLVER_ROTATION_ABSENCE_RESET_SECONDS", "garbage")
    assert absence_reset_seconds() == 4 * DAY  # unparseable -> code default


def test_fold_reset_is_max_merge_and_pure():
    benched = {"A": 100.0}
    out = fold_reset(benched, {"A": 50.0, "B": 200.0})
    assert out == {"A": 100.0, "B": 200.0}  # A: bench newer wins; B: phantom bench
    assert benched == {"A": 100.0}  # input untouched
    assert fold_reset(benched, {}) is benched  # no-reset fast path


def test_actor_evidence_prefers_activity_over_trace():
    evidence = actor_evidence_map(
        active={"A": 900.0},
        benched={"A": 100.0, "B": 500.0},
        seen={"B": 50.0, "C": 25.0},
        actor_of=None,
    )
    # A: activity wins over old bench; B: no activity -> bench trace; C: seen.
    assert evidence == {"A": 900.0, "B": 500.0, "C": 25.0}


def test_actor_evidence_fleet_max_over_siblings():
    actor_of = lambda hk: "fleet" if hk in ("A1", "A2") else hk  # noqa: E731
    evidence = actor_evidence_map(
        active={"A2": 900.0}, benched={"A1": 100.0}, seen={}, actor_of=actor_of,
    )
    # One active sibling keeps the whole actor's clock alive.
    assert evidence == {"fleet": 900.0}


# ── ledger v3 ────────────────────────────────────────────────────────────────

def test_v2_ledger_file_loads_with_empty_active_reset(tmp_path):
    p = tmp_path / "rot.json"
    p.write_text(json.dumps({"benched": {"A": 1.0}, "seen": {"A": 1.0}}))
    ledger = RotationLedger(str(p))
    assert ledger.load() == {"A": 1.0}
    assert ledger.load_active() == {} and ledger.load_reset() == {}


def test_mark_active_and_reset_are_max_write(tmp_path):
    ledger = RotationLedger(str(tmp_path / "rot.json"))
    ledger.mark_active({"A": 100.0})
    ledger.mark_active({"A": 50.0})  # stale backfill cannot shrink
    assert ledger.load_active() == {"A": 100.0}
    ledger.mark_reset(["A"], 200.0)
    ledger.mark_reset(["A"], 150.0)
    assert ledger.load_reset() == {"A": 200.0}
    # the other maps survive the v3 writes
    ledger.mark_selected(["A"], 300.0)
    assert ledger.load_active() == {"A": 100.0} and ledger.load_reset() == {"A": 200.0}


# ── detection at apply_rotation_slate ────────────────────────────────────────

def test_dormant_returner_re_enters_as_newcomer(tmp_path):
    now = 100 * DAY
    # Dormant benched 24d ago (would be MOST senior); Fresh benched 2d ago but
    # active since (activity stamped 1h ago).
    ledger = RotationLedger(str(tmp_path / "rot.json"))
    ledger.mark_selected(["Dormant"], now - 24 * DAY)
    ledger.mark_selected(["Fresh"], now - 2 * DAY)
    ledger.mark_active({"Fresh": now - 3600})
    store = _FakeStore([
        _sub("Dormant", created_at=now - 60),
        _sub("Fresh", created_at=now - 60),
    ])
    res = apply_rotation_slate(store, "r1", 1, ledger, now=now)
    # Without the rule Dormant wins (benched longest ago). With it, Dormant's
    # 24d activity gap forfeits the accrued wait -> Fresh takes the seat.
    assert res["selected"] == ["sub_Fresh"]
    assert ledger.load_reset().get("Dormant") == now
    # Bench history stays honest — the reset never rewrites `benched`.
    assert ledger.load()["Dormant"] == now - 24 * DAY


def test_reset_persists_beyond_the_return_round(tmp_path):
    now = 100 * DAY
    ledger = RotationLedger(str(tmp_path / "rot.json"))
    ledger.mark_selected(["Dormant"], now - 24 * DAY)
    ledger.mark_selected(["Fresh"], now - 2 * DAY)
    ledger.mark_active({"Fresh": now - 3600})
    store = _FakeStore([
        _sub("Dormant", created_at=now - 60), _sub("Fresh", created_at=now - 60),
    ])
    apply_rotation_slate(store, "r1", 1, ledger, now=now)
    assert ledger.load_reset().get("Dormant") == now
    # Next round: Dormant is now ACTIVE (stamped at r1) so no NEW reset fires —
    # but the persisted reset must keep its clock at `now`, junior to Mid who
    # was benched a day earlier. Without persistence, Dormant's raw 24d-old
    # bench anchor would out-senior Mid and the demotion would last one round.
    later = now + 1200
    ledger.mark_selected(["Mid"], now - 1 * DAY)
    ledger.mark_active({"Mid": later - 60})
    store2 = _FakeStore([
        _sub("Dormant", sid="sub_D2", created_at=later - 60),
        _sub("Mid", sid="sub_M2", created_at=later - 60),
    ])
    res2 = apply_rotation_slate(store2, "r2", 1, ledger, now=later)
    assert res2["selected"] == ["sub_M2"]
    # No re-stamp happened: the r1 reset is still the stored one.
    assert ledger.load_reset()["Dormant"] == now


def test_starved_but_active_miner_keeps_seniority(tmp_path):
    now = 100 * DAY
    # Starved benched 10d ago but submitting all along (activity 1h ago) —
    # capacity starvation must NOT be mistaken for absence.
    ledger = RotationLedger(str(tmp_path / "rot.json"))
    ledger.mark_selected(["Starved"], now - 10 * DAY)
    ledger.mark_selected(["Fresh"], now - 1 * DAY)
    ledger.mark_active({"Starved": now - 3600, "Fresh": now - 3600})
    store = _FakeStore([
        _sub("Starved", created_at=now - 60), _sub("Fresh", created_at=now - 60),
    ])
    res = apply_rotation_slate(store, "r1", 1, ledger, now=now)
    assert res["selected"] == ["sub_Starved"]
    assert "Starved" not in ledger.load_reset()


def test_store_backfill_protects_active_miner_on_fresh_ledger(tmp_path):
    now = 100 * DAY
    # Ledger predates the activity map (no `active` entries) — the store says
    # Starved submitted 1h ago (prior rounds). First post-upgrade pass must
    # NOT reset it.
    ledger = RotationLedger(str(tmp_path / "rot.json"))
    ledger.mark_selected(["Starved"], now - 10 * DAY)
    ledger.mark_selected(["Fresh"], now - 1 * DAY)
    store = _FakeStore(
        [_sub("Starved", created_at=now - 60), _sub("Fresh", created_at=now - 60)],
        latest={"Starved": now - 3600, "Fresh": now - 3600},
    )
    res = apply_rotation_slate(store, "r1", 1, ledger, now=now)
    assert res["selected"] == ["sub_Starved"]
    assert ledger.load_reset() == {}


def test_activity_stamped_for_the_rounds_submitters(tmp_path):
    now = 100 * DAY
    ledger = RotationLedger(str(tmp_path / "rot.json"))
    store = _FakeStore([_sub("A", created_at=now - 60)])
    apply_rotation_slate(store, "r1", 1, ledger, now=now)
    assert ledger.load_active() == {"A": now - 60}


def test_fleet_sibling_keeps_actor_alive(tmp_path, monkeypatch):
    monkeypatch.delenv("SOLVER_ACTOR_KEY", raising=False)  # actor keying ON
    now = 100 * DAY
    actor_mod.set_coldkey_provider(lambda: {"A1": "ckA", "A2": "ckA", "B": "ckB"})
    ledger = RotationLedger(str(tmp_path / "rot.json"))
    # A1 idle 24d by its own trace, but sibling A2 active 1h ago -> the ACTOR
    # is present; A1 keeps the fleet's shared seniority (benched 24d ago wins
    # over B benched 1d ago).
    ledger.mark_selected(["A1"], now - 24 * DAY)
    ledger.mark_selected(["B"], now - 1 * DAY)
    ledger.mark_active({"A2": now - 3600, "B": now - 3600})
    store = _FakeStore([
        _sub("A1", created_at=now - 60), _sub("B", created_at=now - 60),
    ])
    res = apply_rotation_slate(store, "r1", 1, ledger, now=now)
    assert res["selected"] == ["sub_A1"]
    assert "A1" not in ledger.load_reset()


def test_killswitch_zero_disables_rule(tmp_path, monkeypatch):
    monkeypatch.setenv("SOLVER_ROTATION_ABSENCE_RESET_SECONDS", "0")
    now = 100 * DAY
    ledger = RotationLedger(str(tmp_path / "rot.json"))
    ledger.mark_selected(["Dormant"], now - 24 * DAY)
    ledger.mark_selected(["Fresh"], now - 2 * DAY)
    ledger.mark_active({"Fresh": now - 3600})
    store = _FakeStore([
        _sub("Dormant", created_at=now - 60), _sub("Fresh", created_at=now - 60),
    ])
    res = apply_rotation_slate(store, "r1", 1, ledger, now=now)
    # Legacy behavior: benched longest ago wins, no resets stamped.
    assert res["selected"] == ["sub_Dormant"]
    assert ledger.load_reset() == {}
