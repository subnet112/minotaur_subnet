"""RoundStore keep-last-K bounding + champion-round pinning.

The store was unbounded (solver_rounds.json grew forever, re-serialized on the
coordinator loop). Bounding evicts old rounds but MUST NEVER drop the standing/
previous champion's activated round (its certificate lives in ``_rounds`` and
``/champion/reattest`` + ``/champion/sync-bundle`` serve it — an evicted champion
round 404s the follower re-adopt path → burn), the current round, or the newest
opened_epoch (so ``_build_round_id`` can't mint a duplicate id).
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from minotaur_subnet.harness import round_store as rs_mod
from minotaur_subnet.harness.round_store import (
    ChampionApproval,
    ChampionCertificate,
    ChampionSnapshot,
    RoundStatus,
    RoundStore,
)


def _open_next(store: RoundStore, epoch: int):
    """Open a fresh round for ``epoch`` (closing the current one if still open)."""
    cur = store._get_current_round_ref()
    if cur is not None and cur.status == RoundStatus.OPEN:
        store.close_current_round(close_epoch=epoch)
    return store.open_next_round(opened_epoch=epoch)


def _certify(store: RoundStore, round_id: str, *, effective_epoch: int) -> None:
    store.set_round_finalist(
        round_id,
        submission_id="sub_" + round_id,
        image_id="sha256:" + "c" * 64,
        shadow_case_log_hash="shadow-" + round_id,
    )
    store.certify_round(
        round_id,
        ChampionCertificate(
            round_id=round_id,
            committee_hash="committee",
            candidate_submission_id="sub_" + round_id,
            candidate_image_id="sha256:" + "c" * 64,
            incumbent_image_id="sha256:" + "a" * 64,
            benchmark_pack_hash="pack",
            shadow_case_log_hash="shadow-" + round_id,
            effective_epoch=effective_epoch,
            quorum_required=1,
            approvals=[
                ChampionApproval(
                    validator_id="0x1",
                    round_id=round_id,
                    candidate_submission_id="sub_" + round_id,
                    candidate_image_id="sha256:" + "c" * 64,
                    effective_epoch=effective_epoch,
                    signature="sig1",
                ),
            ],
        ),
    )


def test_bounding_evicts_oldest_keeps_newest(monkeypatch, tmp_path):
    monkeypatch.setattr(rs_mod, "_ROUND_STORE_MAX_ROUNDS", 5)
    store = RoundStore(persist_path=tmp_path / "solver_rounds.json")

    store.ensure_open_round(opened_epoch=0)
    for e in range(1, 30):
        _open_next(store, e)

    # Bounded: far below the 30 opened. The newest epoch survives; the oldest
    # (non-pinned) rounds are gone.
    assert len(store._rounds) <= 7, len(store._rounds)
    assert store.get_round("round-e29-n1") is not None
    assert store.get_round("round-e0-n1") is None


def test_bounding_pins_champion_and_previous_champion_rounds(monkeypatch, tmp_path):
    """BLOCKER guard: a long-reigning champion's activated round (certified,
    arbitrarily old) survives eviction with its certificate intact."""
    monkeypatch.setattr(rs_mod, "_ROUND_STORE_MAX_ROUNDS", 5)
    store = RoundStore(persist_path=tmp_path / "solver_rounds.json")

    # Round A (epoch 0): certified, becomes the PREVIOUS champion.
    a = store.ensure_open_round(opened_epoch=0)
    _certify(store, a.round_id, effective_epoch=1)
    store.set_previous_champion(
        ChampionSnapshot(submission_id="prev", activated_round_id=a.round_id)
    )
    # Round B (epoch 1): certified, becomes the ACTIVE champion.
    b = _open_next(store, 1)
    _certify(store, b.round_id, effective_epoch=2)
    store.set_active_champion(
        ChampionSnapshot(submission_id="active", activated_round_id=b.round_id)
    )

    # Reign long past the cap: 40 more rounds close over 40 epochs.
    for e in range(2, 42):
        _open_next(store, e)

    # The champion + previous-champion rounds are still present AND certified.
    champ = store.get_round(b.round_id)
    prev = store.get_round(a.round_id)
    assert champ is not None and champ.certificate is not None
    assert prev is not None and prev.certificate is not None
    assert store.get_active_champion().activated_round_id == b.round_id

    # Survives a restart (reload from disk) — the reattest/sync-bundle path.
    store2 = RoundStore(persist_path=tmp_path / "solver_rounds.json")
    reloaded = store2.get_round(b.round_id)
    assert reloaded is not None and reloaded.certificate is not None


def test_bounding_pins_current_round(monkeypatch, tmp_path):
    monkeypatch.setattr(rs_mod, "_ROUND_STORE_MAX_ROUNDS", 3)
    store = RoundStore(persist_path=tmp_path / "solver_rounds.json")
    store.ensure_open_round(opened_epoch=0)
    for e in range(1, 20):
        _open_next(store, e)
    current = store.get_current_round()
    assert current is not None
    assert store.get_round(current.round_id) is not None


def test_bounding_disabled_when_cap_non_positive(monkeypatch, tmp_path):
    monkeypatch.setattr(rs_mod, "_ROUND_STORE_MAX_ROUNDS", 0)
    store = RoundStore(persist_path=tmp_path / "solver_rounds.json")
    store.ensure_open_round(opened_epoch=0)
    for e in range(1, 25):
        _open_next(store, e)
    assert len(store._rounds) == 25


def test_newest_epoch_never_evicted_no_round_id_collision(monkeypatch, tmp_path):
    """Every round in the newest opened_epoch is protected, so the count-based
    _build_round_id can never reset and mint a duplicate id."""
    monkeypatch.setattr(rs_mod, "_ROUND_STORE_MAX_ROUNDS", 4)
    store = RoundStore(persist_path=tmp_path / "solver_rounds.json")
    store.ensure_open_round(opened_epoch=0)
    for e in range(1, 15):
        _open_next(store, e)
    # Open a SECOND round in the current (newest) epoch, then a third — ids must
    # stay distinct (n1, n2, n3), i.e. the earlier same-epoch rounds weren't
    # evicted out from under the counter.
    latest_epoch = max(s.opened_epoch for s in store._rounds.values())
    store.close_current_round(close_epoch=latest_epoch)
    r2 = store.open_next_round(opened_epoch=latest_epoch)
    store.close_current_round(close_epoch=latest_epoch)
    r3 = store.open_next_round(opened_epoch=latest_epoch)
    assert r2.round_id == f"round-e{latest_epoch}-n2"
    assert r3.round_id == f"round-e{latest_epoch}-n3"
    assert store.get_round(f"round-e{latest_epoch}-n1") is not None


def test_evicted_rounds_were_mirrored_to_sink(monkeypatch, tmp_path):
    """Eviction is lossless w.r.t. durable history: every round the store ever
    held was pushed to the record_sink before it could be evicted."""
    monkeypatch.setattr(rs_mod, "_ROUND_STORE_MAX_ROUNDS", 5)
    recorded: set[str] = set()
    store = RoundStore(
        persist_path=tmp_path / "solver_rounds.json",
        record_sink=lambda state: recorded.add(state.round_id),
    )
    store.ensure_open_round(opened_epoch=0)
    for e in range(1, 30):
        _open_next(store, e)
    # Some rounds were evicted from _rounds ...
    assert store.get_round("round-e0-n1") is None
    # ... but every one was mirrored to the sink first.
    assert "round-e0-n1" in recorded
    assert len(recorded) >= 30
