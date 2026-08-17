"""Operator merges (harness/actor.py): identifiers proven to be ONE operator
resolve to one actor — env extras and structural co-occurrence. No identifier is
named in code; every merge comes from live evidence or an operator's env var.

THE SHAPE THIS CLOSES (live, 2026-07-28). A ring of 13 coldkeys with ONE hotkey
each, each under its own throwaway github account, defeats both existing links:
coldkey grouping sees 13 singletons and the github-owner union finds nothing to
join. So ``SUBMISSIONS_MAX_PER_ACTOR_PER_ROUND=1`` was satisfied 13 times in
parallel, the ring held 13 seniority clocks, and it took 5 of 8 build units and
up to 2 of 3 slate seats per round. It also re-rolls its structural fingerprint
EVERY round, so no equality key on the code can catch it — what identifies it is
the pattern: the same actors co-clustering round after round.

A merge adds no new cap and no new rejection path: it supplies the missing edge,
after which the caps that already run do the work.
"""
from __future__ import annotations

import json

import pytest

from minotaur_subnet.harness import actor as actor_mod
from minotaur_subnet.harness.actor import (
    ActorResolver,
    merge_groups,
    record_structural_coclusters,
    set_coldkey_provider,
    snapshot_resolver,
    structural_merge_groups,
)

# A ring: one hotkey per coldkey, one github owner per hotkey (nothing links
# them), plus two genuinely independent solo miners.
RING = {f"HK{i}": f"CK{i}" for i in range(4)}
COLDKEYS = {**RING, "S": "CK_S", "T": "CK_T"}
OWNERS = {hk: [f"gh-{hk.lower()}"] for hk in COLDKEYS}


@pytest.fixture(autouse=True)
def _clean(tmp_path, monkeypatch):
    monkeypatch.setenv("SOLVER_ROTATION_LEDGER_PATH", str(tmp_path / "rotation.json"))
    monkeypatch.delenv("SOLVER_ACTOR_MERGE_EXTRA", raising=False)
    monkeypatch.setenv("STRUCTURAL_ACTOR_MERGE_MODE", "observe")
    monkeypatch.delenv("STRUCTURAL_ACTOR_MERGE_MIN_ROUNDS", raising=False)
    monkeypatch.delenv("STRUCTURAL_ACTOR_MERGE_TTL_DAYS", raising=False)
    set_coldkey_provider(None)
    actor_mod._reset_caches_for_tests()
    yield
    set_coldkey_provider(None)
    actor_mod._reset_caches_for_tests()


def _resolver():
    return ActorResolver.from_maps(COLDKEYS, OWNERS, source="test")


def _actors(resolver, hotkeys=RING):
    return {resolver(hk) for hk in hotkeys}


# ── the gap: nothing links the ring today ────────────────────────────────────

def test_ring_is_n_actors_without_a_merge():
    r = _resolver()
    assert len(_actors(r)) == 4                      # 4 clocks, 4 caps, 4 seats
    assert r("S") == "CK_S" and r("T") == "CK_T"


# ── env extras ───────────────────────────────────────────────────────────────

def test_env_extra_group_collapses_the_ring(monkeypatch):
    monkeypatch.setenv("SOLVER_ACTOR_MERGE_EXTRA", "CK0,CK1,CK2,CK3")
    r = _resolver()
    assert len(_actors(r)) == 1
    assert r("HK0") == "CK0"                         # smallest coldkey labels it
    # Independent miners are untouched.
    assert r("S") == "CK_S" and r("T") == "CK_T"


def test_env_extra_accepts_multiple_groups_and_ignores_singletons(monkeypatch):
    monkeypatch.setenv("SOLVER_ACTOR_MERGE_EXTRA", "CK0,CK1; CK2,CK3 ;CK_S")
    r = _resolver()
    assert r("HK0") == r("HK1") == "CK0"
    assert r("HK2") == r("HK3") == "CK2"
    assert r("HK0") != r("HK2")                      # two distinct operators
    assert r("S") == "CK_S"                          # 1-member group ignored


def test_merge_may_name_a_hotkey_or_an_owner(monkeypatch):
    # A group can name any token form; unused forms are inert.
    monkeypatch.setenv("SOLVER_ACTOR_MERGE_EXTRA", "HK0,gh-hk1,CK2")
    r = _resolver()
    assert r("HK0") == r("HK1") == r("HK2")


def test_no_merges_are_hardcoded(monkeypatch):
    """With no env extras and no recorded evidence, NOTHING is merged: the
    module must not name any identifier of its own (the in-code fp-ring list was
    removed 2026-08-17 — a merge is only ever asserted by live evidence or by an
    explicit operator decision)."""
    monkeypatch.delenv("SOLVER_ACTOR_MERGE_EXTRA", raising=False)
    monkeypatch.setenv("STRUCTURAL_ACTOR_MERGE_MODE", "enforce")
    assert merge_groups() == []
    assert not hasattr(actor_mod, "_STATIC_ACTOR_MERGES")
    assert len(_actors(_resolver())) == 4              # every miner its own actor


# ── structural co-occurrence: record → arm → merge ───────────────────────────

def test_evidence_must_span_distinct_rounds(monkeypatch):
    monkeypatch.setenv("STRUCTURAL_ACTOR_MERGE_MIN_ROUNDS", "3")
    # Same round recorded repeatedly is ONE piece of evidence — a ring cannot
    # manufacture a merge against a victim inside a single round.
    for _ in range(5):
        record_structural_coclusters("r1", [{"CK0", "CK1"}])
    assert structural_merge_groups() == []
    record_structural_coclusters("r2", [{"CK0", "CK1"}])
    assert structural_merge_groups() == []
    record_structural_coclusters("r3", [{"CK0", "CK1"}])
    assert structural_merge_groups() == [frozenset({"CK0", "CK1"})]


def test_observe_records_but_does_not_merge(monkeypatch):
    monkeypatch.setenv("STRUCTURAL_ACTOR_MERGE_MODE", "observe")
    for rid in ("r1", "r2", "r3"):
        record_structural_coclusters(rid, [set(RING.values())])
    assert len(structural_merge_groups()) == 1        # armed and visible…
    assert merge_groups() == []                        # …but not applied
    assert len(_actors(_resolver())) == 4


def test_enforce_applies_the_armed_merge(monkeypatch):
    monkeypatch.setenv("STRUCTURAL_ACTOR_MERGE_MODE", "enforce")
    for rid in ("r1", "r2", "r3"):
        record_structural_coclusters(rid, [set(RING.values())])
    assert len(_actors(_resolver())) == 1              # one operator, one seat
    assert _resolver()("S") == "CK_S"                  # solos untouched


def test_transitive_components_merge(monkeypatch):
    monkeypatch.setenv("STRUCTURAL_ACTOR_MERGE_MODE", "enforce")
    monkeypatch.setenv("STRUCTURAL_ACTOR_MERGE_MIN_ROUNDS", "2")
    # A+B cluster in some rounds, B+C in others — one operator, transitively.
    for rid in ("r1", "r2"):
        record_structural_coclusters(rid, [{"CK0", "CK1"}])
    for rid in ("r3", "r4"):
        record_structural_coclusters(rid, [{"CK1", "CK2"}])
    r = _resolver()
    assert r("HK0") == r("HK1") == r("HK2")
    assert r("HK3") != r("HK0")                        # never co-clustered


def test_mode_off_records_nothing(monkeypatch):
    monkeypatch.setenv("STRUCTURAL_ACTOR_MERGE_MODE", "off")
    for rid in ("r1", "r2", "r3", "r4"):
        record_structural_coclusters(rid, [set(RING.values())])
    assert structural_merge_groups() == []
    assert len(_actors(_resolver())) == 4


def test_evidence_decays_after_the_ttl(monkeypatch):
    monkeypatch.setenv("STRUCTURAL_ACTOR_MERGE_MODE", "enforce")
    monkeypatch.setenv("STRUCTURAL_ACTOR_MERGE_TTL_DAYS", "30")
    t0 = 1_000_000.0
    for i, rid in enumerate(("r1", "r2", "r3")):
        record_structural_coclusters(rid, [{"CK0", "CK1"}], now=t0 + i)
    assert structural_merge_groups() == [frozenset({"CK0", "CK1"})]
    # A later round involving OTHER actors prunes the stale pair (no repeat in
    # 31 days ⇒ the merge is no longer a statement about current behaviour).
    record_structural_coclusters(
        "r99", [{"CK2", "CK3"}], now=t0 + 31 * 86400.0,
    )
    assert structural_merge_groups() == []


def test_single_actor_cluster_is_not_evidence(monkeypatch):
    monkeypatch.setenv("STRUCTURAL_ACTOR_MERGE_MODE", "enforce")
    for rid in ("r1", "r2", "r3", "r4"):
        record_structural_coclusters(rid, [{"CK0"}, set()])
    assert structural_merge_groups() == []


def test_corrupt_ledger_degrades_to_no_merges(monkeypatch, tmp_path):
    monkeypatch.setenv("STRUCTURAL_ACTOR_MERGE_MODE", "enforce")
    path = tmp_path / actor_mod._STRUCT_LINK_SIDECAR
    path.write_text("{not json")
    actor_mod._reset_caches_for_tests()
    assert structural_merge_groups() == []
    assert len(_actors(_resolver())) == 4


def test_ledger_is_bounded_per_pair(monkeypatch):
    monkeypatch.setenv("STRUCTURAL_ACTOR_MERGE_MODE", "enforce")
    for i in range(80):
        record_structural_coclusters(f"r{i}", [{"CK0", "CK1"}])
    with open(actor_mod._sidecar_path(actor_mod._STRUCT_LINK_SIDECAR)) as f:
        raw = json.load(f)
    rounds = raw["pairs"]["CK0|CK1"]["rounds"]
    assert len(rounds) == actor_mod._STRUCT_LINK_MAX_ROUNDS
    assert rounds[-1] == "r79"                         # keeps the most recent


# ── the None-contract still holds ────────────────────────────────────────────

def test_no_coldkey_map_still_means_legacy_path(monkeypatch):
    monkeypatch.setenv("STRUCTURAL_ACTOR_MERGE_MODE", "enforce")
    monkeypatch.setenv("SOLVER_ACTOR_MERGE_EXTRA", "CK0,CK1,CK2,CK3")
    set_coldkey_provider(None)
    actor_mod._reset_caches_for_tests()
    assert snapshot_resolver() is None                 # merges never resurrect one


def test_kill_switch_reverts_everything(monkeypatch):
    monkeypatch.setenv("SOLVER_ACTOR_KEY", "hotkey")
    monkeypatch.setenv("SOLVER_ACTOR_MERGE_EXTRA", "CK0,CK1,CK2,CK3")
    set_coldkey_provider(lambda: COLDKEYS)
    assert snapshot_resolver() is None


# ── the close-time pass feeds the ledger ─────────────────────────────────────

def test_slate_pass_records_co_occurrence_and_arms_the_merge(monkeypatch, tmp_path):
    """apply_rotation_slate must feed the evidence ledger every round — that is
    the only writer, and without it the automatic merge never arms."""
    from types import SimpleNamespace

    from minotaur_subnet.harness.rotation import RotationLedger, apply_rotation_slate

    monkeypatch.setenv("STRUCTURAL_ACTOR_MERGE_MODE", "enforce")
    monkeypatch.setenv("STRUCTURAL_ACTOR_MERGE_MIN_ROUNDS", "2")
    set_coldkey_provider(lambda: COLDKEYS)
    actor_mod._reset_caches_for_tests()

    def _sub(hotkey, fp, status="queued"):
        return SimpleNamespace(
            submission_id=f"sub_{hotkey}_{fp}", hotkey=hotkey,
            status=SimpleNamespace(value=status), created_at=1.0,
            structural_fingerprint=fp,
        )

    class _Store:
        def __init__(self, subs):
            self.subs = subs
            self.waitlisted: list[str] = []

        def list_by_round(self, round_id):
            return self.subs

        def reject(self, sid, reason):
            self.waitlisted.append(sid)

        def waitlist(self, sid, reason, **kw):
            self.waitlisted.append(sid)

    for i, rid in enumerate(("r1", "r2")):
        # The ring ships ONE structure per round — a DIFFERENT one each round
        # (the live evasion). A parked (waitlisted) member still counts.
        subs = [
            _sub("HK0", f"fp-round-{i}"),
            _sub("HK1", f"fp-round-{i}"),
            _sub("HK2", f"fp-round-{i}", status="waitlisted"),
            _sub("S", f"solo-{i}"),
        ]
        apply_rotation_slate(
            _Store(subs), rid, 2, RotationLedger(str(tmp_path / f"led-{rid}.json")),
        )

    # Re-rolling the fingerprint every round did not help: the ACTORS are what
    # got recorded, so the ring is now one operator.
    assert structural_merge_groups() == [frozenset({"CK0", "CK1", "CK2"})]
    r = ActorResolver.from_maps(COLDKEYS, {}, source="test")
    assert r("HK0") == r("HK1") == r("HK2")
    assert r("S") == "CK_S"                            # the solo stays alone


def test_merged_ring_keeps_refreshing_its_own_evidence(monkeypatch, tmp_path):
    """Post-merge the ring resolves to ONE actor. Evidence is keyed on the
    PRE-merge identity, so it keeps refreshing — without this the merge would
    lapse on the TTL and the ring would split again every 30 days."""
    from types import SimpleNamespace

    from minotaur_subnet.harness.rotation import RotationLedger, apply_rotation_slate

    monkeypatch.setenv("STRUCTURAL_ACTOR_MERGE_MODE", "enforce")
    monkeypatch.setenv("STRUCTURAL_ACTOR_MERGE_MIN_ROUNDS", "2")
    set_coldkey_provider(lambda: COLDKEYS)
    actor_mod._reset_caches_for_tests()

    class _Store:
        def __init__(self, subs):
            self.subs = subs

        def list_by_round(self, round_id):
            return self.subs

        def reject(self, sid, reason):
            pass

        def waitlist(self, sid, reason, **kw):
            pass

    def _round(rid, i):
        subs = [
            SimpleNamespace(
                submission_id=f"sub_{hk}_{i}", hotkey=hk,
                status=SimpleNamespace(value="queued"), created_at=1.0,
                structural_fingerprint=f"fp-{i}",
            )
            for hk in ("HK0", "HK1")
        ]
        apply_rotation_slate(
            _Store(subs), rid, 2, RotationLedger(str(tmp_path / f"led-{rid}.json")),
        )

    _round("r1", 1)
    _round("r2", 2)
    armed = structural_merge_groups()
    assert armed == [frozenset({"CK0", "CK1"})]
    # Now merged: both hotkeys are one actor…
    assert ActorResolver.from_maps(COLDKEYS, {}, source="test")("HK0") == \
        ActorResolver.from_maps(COLDKEYS, {}, source="test")("HK1")
    # …and a further round still credits the pair (evidence count grows).
    _round("r3", 3)
    with open(actor_mod._sidecar_path(actor_mod._STRUCT_LINK_SIDECAR)) as f:
        rounds = json.load(f)["pairs"]["CK0|CK1"]["rounds"]
    assert rounds == ["r1", "r2", "r3"]
